"""Cliente de referencia para el Gate de autorizacion (simulador / driver de integracion).
Implementa la misma logica fail-closed y timeout que la clase C# AuthorizedStrategyBase.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Callable, Dict, List, Optional

from src.gate.protocol import decode_message, encode_message
from src.gate.risk_engine import AuthRequest, AuthResponse, PositionState

logger = logging.getLogger("gate.client")


class GateClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        timeout_ms: int = 200,
        heartbeat_timeout_seconds: float = 10.0,
        on_command: Optional[Callable[[dict], None]] = None,
    ):
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.on_command = on_command

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self.last_heartbeat_time: float = 0.0
        self.is_connected: bool = False
        self.is_paused: bool = False
        self.is_flattened: bool = False

    async def connect(self) -> bool:
        try:
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            self.last_heartbeat_time = asyncio.get_event_loop().time()
            self._reader_task = asyncio.create_task(self._read_loop())
            logger.info("Conectado exitosamente al Gate en %s:%d", self.host, self.port)
            return True
        except Exception as e:
            logger.warning("Fallo al conectar con el Gate en %s:%d: %s", self.host, self.port, e)
            self.is_connected = False
            return False

    async def disconnect(self) -> None:
        self.is_connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None

    def is_heartbeat_healthy(self) -> bool:
        if not self.is_connected:
            return False
        elapsed = asyncio.get_event_loop().time() - self.last_heartbeat_time
        return elapsed <= self.heartbeat_timeout_seconds

    async def reconcile(self, account: str, positions: List[PositionState]) -> bool:
        if not self.is_connected or not self.writer:
            return False
        msg = {
            "type": "RECONCILE",
            "account": account,
            "positions": [
                {
                    "instrument": p.instrument,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                    "side": p.side,
                }
                for p in positions
            ],
        }
        self.writer.write(encode_message(msg))
        await self.writer.drain()
        return True

    async def send_telemetry(
        self,
        account: str,
        strategy_id: str,
        current_balance: float,
        realized_pnl_today: float,
        unrealized_pnl: float,
    ) -> None:
        if not self.is_connected or not self.writer:
            return
        msg = {
            "type": "TELEMETRY",
            "account": account,
            "strategy_id": strategy_id,
            "current_balance": current_balance,
            "realized_pnl_today": realized_pnl_today,
            "unrealized_pnl": unrealized_pnl,
        }
        self.writer.write(encode_message(msg))
        await self.writer.drain()

    async def request_authorization(self, req: AuthRequest) -> AuthResponse:
        """Solicita autorizacion a Python con logica Fail-Closed y timeout estricto T ms."""
        # Fail-closed local si no hay conexion o el heartbeat fallo (Criterio 5)
        if not self.is_connected or not self.is_heartbeat_healthy():
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason="FAIL_CLOSED: Socket desconectado o heartbeat vencido",
            )

        # Fail-closed local si se recibio comando FLATTEN o PAUSE (Criterio 7)
        if self.is_flattened or self.is_paused:
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason="FAIL_CLOSED: Estrategia en PAUSE o cuenta en FLATTEN",
            )

        # Fail-closed local si la orden no incluye Stop Loss > 0 (Criterio 8)
        if req.stop_distance <= 0:
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason="LOCAL_REJECT_STOP_REQUIRED: Stop loss distance debe ser > 0",
            )

        fut = asyncio.get_event_loop().create_future()
        self._pending_requests[req.request_id] = fut

        msg = {
            "type": "AUTH_REQUEST",
            "request_id": req.request_id,
            "strategy_id": req.strategy_id,
            "account": req.account,
            "instrument": req.instrument,
            "side": req.side,
            "qty": req.qty,
            "stop_distance": req.stop_distance,
        }

        try:
            self.writer.write(encode_message(msg))
            await self.writer.drain()

            # Esperar respuesta hasta el timeout parametrizado T ms (Criterio 4)
            timeout_sec = self.timeout_ms / 1000.0
            resp_dict = await asyncio.wait_for(fut, timeout=timeout_sec)
            return AuthResponse(
                request_id=resp_dict["request_id"],
                allow=bool(resp_dict["allow"]),
                max_qty=int(resp_dict["max_qty"]),
                reason=str(resp_dict["reason"]),
            )
        except asyncio.TimeoutError:
            logger.warning("Timeout esperando autorizacion para %s en %d ms", req.request_id, self.timeout_ms)
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=f"TIMEOUT_EXCEEDED: Servidor no respondio en {self.timeout_ms} ms",
            )
        except Exception as e:
            logger.error("Error en solicitud de autorizacion: %s", e)
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=f"SOCKET_ERROR: {str(e)}",
            )
        finally:
            self._pending_requests.pop(req.request_id, None)

    async def _read_loop(self) -> None:
        try:
            while self.is_connected and self.reader:
                line_bytes = await self.reader.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace")
                msg = decode_message(line)
                mtype = msg.get("type")

                if mtype == "HEARTBEAT":
                    self.last_heartbeat_time = asyncio.get_event_loop().time()
                    # Responder ACK al heartbeat
                    if self.writer:
                        self.writer.write(encode_message({"type": "HEARTBEAT_ACK"}))
                        await self.writer.drain()

                elif mtype == "AUTH_RESPONSE":
                    req_id = msg.get("request_id")
                    if req_id in self._pending_requests:
                        fut = self._pending_requests[req_id]
                        if not fut.done():
                            fut.set_result(msg)

                elif mtype == "COMMAND":
                    action = msg.get("action")
                    if action == "PAUSE":
                        self.is_paused = True
                    elif action == "FLATTEN":
                        self.is_flattened = True

                    if self.on_command:
                        self.on_command(msg)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error en reader loop del cliente: %s", e)
        finally:
            self.is_connected = False
