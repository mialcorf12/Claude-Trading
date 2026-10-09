"""Servidor TCP asincrono para el gate de autorizacion Python <-> NinjaTrader 8."""
import asyncio
from datetime import datetime, tzinfo
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Set
import zoneinfo

from src.gate.config import GateConfig
from src.gate.protocol import decode_message, encode_message, ProtocolError
from src.gate.risk_engine import (
    RiskEngine,
    AuthRequest,
    PositionState,
    TelemetryUpdate,
)
from src.gate.state_store import StateStore

logger = logging.getLogger("gate.server")


class AuditLogger:
    """Registra cada solicitud, respuesta y evento con timestamp ISO-8601 en la zona horaria configurada.

    El offset va incluido en el timestamp (ej. 2026-10-09T12:34:56-05:00), por lo que sigue siendo inequivoco.
    """

    def __init__(self, log_path: str, tz: Optional[tzinfo] = None):
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tz = tz or zoneinfo.ZoneInfo("America/Chicago")

    def log_event(self, event_type: str, payload: dict) -> None:
        record = {
            "timestamp": datetime.now(self.tz).isoformat(),
            "event": event_type,
            "data": payload,
        }
        line = json.dumps(record) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
        logger.debug("AUDIT [%s]: %s", event_type, payload)


class GateServer:
    def __init__(
        self,
        config: GateConfig,
        risk_engine: Optional[RiskEngine] = None,
        trading_day_tick_seconds: float = 30.0,
    ):
        self.config = config
        self.risk_engine = risk_engine or RiskEngine(config, StateStore(config.server.state_path))
        self.trading_day_tick_seconds = trading_day_tick_seconds
        self._trading_day_task: Optional[asyncio.Task] = None
        self.log_tz = zoneinfo.ZoneInfo(config.server.log_timezone)
        self.audit = AuditLogger(config.server.audit_log_path, self.log_tz)
        self.server: Optional[asyncio.Server] = None
        self.connected_clients: Set[asyncio.StreamWriter] = set()
        self.client_tasks: Set[asyncio.Task] = set()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self.server = await asyncio.start_server(
            self._handle_client,
            self.config.server.host,
            self.config.server.port,
        )
        addr = self.server.sockets[0].getsockname()
        logger.info("GateServer escuchando en %s:%s", addr[0], addr[1])
        self.audit.log_event("SERVER_START", {"host": addr[0], "port": addr[1]})

        # Iniciar latido de corazon periodico
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._trading_day_task = asyncio.create_task(self._trading_day_loop())

    async def stop(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._trading_day_task:
            self._trading_day_task.cancel()
            try:
                await self._trading_day_task
            except asyncio.CancelledError:
                pass

        for task in list(self.client_tasks):
            task.cancel()

        for writer in list(self.connected_clients):
            try:
                writer.close()
            except Exception:
                pass
        self.connected_clients.clear()

        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("GateServer detenido")
            self.audit.log_event("SERVER_STOP", {})

    async def _heartbeat_loop(self) -> None:
        interval = self.config.server.heartbeat_interval_seconds
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self.connected_clients:
                    continue

                msg = {
                    "type": "HEARTBEAT",
                    "timestamp": datetime.now(self.log_tz).isoformat(),
                }
                raw = encode_message(msg)
                for writer in list(self.connected_clients):
                    try:
                        writer.write(raw)
                        await writer.drain()
                    except Exception as e:
                        logger.warning("Fallo al enviar heartbeat a cliente: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en heartbeat loop: %s", e)

    async def _trading_day_loop(self) -> None:
        """Cierra los dias de trading vencidos (16:00 CT). Corre al arrancar (dias perdidos) y luego periodicamente."""
        while self._running:
            try:
                for account, closed_day in self.risk_engine.tick():
                    self.audit.log_event("TRADING_DAY_CLOSED", {"account": account, "day": closed_day.isoformat()})
                await asyncio.sleep(self.trading_day_tick_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # un fallo del cierre no debe tumbar el gate
                logger.error("Error cerrando el dia de trading: %s", exc)
                await asyncio.sleep(self.trading_day_tick_seconds)

    async def broadcast_command(self, action: str, **kwargs) -> None:
        """Envia comandos dinamicos hacia NT8 (e.g. PAUSE, FLATTEN)."""
        cmd = {"type": "COMMAND", "action": action, **kwargs}
        self.audit.log_event("COMMAND_BROADCAST", cmd)
        raw = encode_message(cmd)
        for writer in list(self.connected_clients):
            try:
                writer.write(raw)
                await writer.drain()
            except Exception as e:
                logger.error("Error enviando comando a cliente: %s", e)

    async def pause_strategy(self, strategy_id: str) -> None:
        self.risk_engine.pause_strategy(strategy_id, paused=True)
        await self.broadcast_command("PAUSE", strategy_id=strategy_id)

    async def flatten_account(self, account: str) -> None:
        self.risk_engine.flatten_account(account)
        await self.broadcast_command("FLATTEN", account=account)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        curr_task = asyncio.current_task()
        if curr_task:
            self.client_tasks.add(curr_task)

        peer = writer.get_extra_info("peername")
        logger.info("Cliente conectado desde %s", peer)
        self.connected_clients.add(writer)
        self.audit.log_event("CLIENT_CONNECTED", {"peer": str(peer)})

        try:
            while self._running:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break  # Conexión cerrada

                line = line_bytes.decode("utf-8-sig", errors="replace")
                try:
                    msg = decode_message(line)
                    await self._process_message(msg, writer)
                except ProtocolError as pe:
                    logger.warning("Mensaje de protocolo invalido: %s", pe)
                    err_resp = {"type": "ERROR", "reason": str(pe)}
                    writer.write(encode_message(err_resp))
                    await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Excepcion en conexion de cliente %s: %s", peer, e)
        finally:
            if curr_task:
                self.client_tasks.discard(curr_task)
            self.connected_clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
            logger.info("Cliente desconectado: %s", peer)
            self.audit.log_event("CLIENT_DISCONNECTED", {"peer": str(peer)})

    async def _process_message(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        msg_type = msg.get("type")

        if msg_type == "AUTH_REQUEST":
            req = AuthRequest(
                request_id=str(msg.get("request_id", "")),
                strategy_id=str(msg.get("strategy_id", "")),
                account=str(msg.get("account", "")),
                instrument=str(msg.get("instrument", "")),
                side=str(msg.get("side", "")),
                qty=int(msg.get("qty", 1)),
                stop_distance=float(msg.get("stop_distance", 0.0)),
            )
            self.audit.log_event("AUTH_REQUEST_RECEIVED", msg)

            # Evaluar en motor de riesgo
            res = self.risk_engine.evaluate_authorization(req)
            resp_dict = {
                "type": "AUTH_RESPONSE",
                "request_id": res.request_id,
                "allow": res.allow,
                "max_qty": res.max_qty,
                "reason": res.reason,
            }
            self.audit.log_event("AUTH_RESPONSE_SENT", resp_dict)
            writer.write(encode_message(resp_dict))
            await writer.drain()

        elif msg_type == "TELEMETRY":
            self.audit.log_event("TELEMETRY_RECEIVED", msg)
            raw_pos = msg.get("positions", [])
            positions = [
                PositionState(
                    account=msg.get("account", ""),
                    instrument=p.get("instrument", ""),
                    qty=int(p.get("qty", 0)),
                    entry_price=float(p.get("entry_price", 0.0)),
                    side=p.get("side", "FLAT"),
                )
                for p in raw_pos
            ]
            t = TelemetryUpdate(
                account=str(msg.get("account", "")),
                strategy_id=str(msg.get("strategy_id", "")),
                current_balance=float(msg["current_balance"]) if "current_balance" in msg else None,
                realized_pnl_today=float(msg["realized_pnl_today"]) if "realized_pnl_today" in msg else None,
                unrealized_pnl=float(msg["unrealized_pnl"]) if "unrealized_pnl" in msg else None,
                positions=positions if raw_pos else None,
            )
            self.risk_engine.update_telemetry(t)

        elif msg_type == "RECONCILE":
            self.audit.log_event("RECONCILE_RECEIVED", msg)
            acc_name = str(msg.get("account", ""))
            raw_pos = msg.get("positions", [])
            positions = [
                PositionState(
                    account=acc_name,
                    instrument=p.get("instrument", ""),
                    qty=int(p.get("qty", 0)),
                    entry_price=float(p.get("entry_price", 0.0)),
                    side=p.get("side", "FLAT"),
                )
                for p in raw_pos
            ]
            self.risk_engine.reconcile_account(acc_name, positions)
            ack = {"type": "RECONCILE_ACK", "account": acc_name, "status": "OK"}
            self.audit.log_event("RECONCILE_ACK_SENT", ack)
            writer.write(encode_message(ack))
            await writer.drain()

        elif msg_type == "HEARTBEAT":
            ack = {
                "type": "HEARTBEAT_ACK",
                "timestamp": datetime.now(self.log_tz).isoformat(),
            }
            writer.write(encode_message(ack))
            await writer.drain()

        elif msg_type == "HEARTBEAT_ACK":
            pass  # Recibido del cliente, conexión viva

        else:
            logger.warning("Tipo de mensaje no reconocido: %s", msg_type)
