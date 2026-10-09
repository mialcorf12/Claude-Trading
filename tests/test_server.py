"""Tests asincronos para el servidor TCP del Gate."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
import zoneinfo

from src.gate.config import load_config, ServerConfig, GateConfig
from src.gate.protocol import decode_message, encode_message
from src.gate.risk_engine import RiskEngine
from src.gate.server import GateServer


class TestGateServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base_cfg = load_config("config/lucid_rules.yaml")
        # Usar puerto efimero / alto y log temporal para no colisionar
        self.tmp_dir = tempfile.TemporaryDirectory()
        audit_file = str(Path(self.tmp_dir.name) / "test_audit.log")

        server_cfg = ServerConfig(
            host="127.0.0.1",
            port=9876,
            timeout_ms=100,
            heartbeat_interval_seconds=1,
            audit_log_path=audit_file,
        )
        self.config = GateConfig(
            server=server_cfg,
            instruments=base_cfg.instruments,
            presets=base_cfg.presets,
            accounts=base_cfg.accounts,
        )
        self.risk_engine = RiskEngine(self.config)
        self.risk_engine.register_account("Sim101", balance=25000.0)

        self.server = GateServer(self.config, risk_engine=self.risk_engine, flatten_check_seconds=None)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        self.tmp_dir.cleanup()

    async def test_reconcile_and_auth_request_flow(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", 9876)

        # 1. Enviar RECONCILE
        rec_msg = {
            "type": "RECONCILE",
            "account": "Sim101",
            "positions": [
                {"instrument": "MNQ", "qty": 2, "entry_price": 21050.0, "side": "LONG"}
            ],
        }
        writer.write(encode_message(rec_msg))
        await writer.drain()

        rec_ack_line = await reader.readline()
        rec_ack = decode_message(rec_ack_line.decode())
        self.assertEqual(rec_ack["type"], "RECONCILE_ACK")
        self.assertEqual(rec_ack["status"], "OK")

        # Verificar que RiskEngine tiene la posicion reconciliada
        acc = self.risk_engine.get_account_state("Sim101")
        self.assertEqual(acc.current_positions["MNQ"].qty, 2)

        # 2. Enviar AUTH_REQUEST
        # Fijar un miercoles 10:30 hora de Chicago (determinista, independiente del reloj real)
        cme_tz = zoneinfo.ZoneInfo("America/Chicago")
        self.risk_engine.evaluate_authorization_orig = self.risk_engine.evaluate_authorization
        self.risk_engine.evaluate_authorization = lambda req: self.risk_engine.evaluate_authorization_orig(
            req, current_time=datetime(2026, 10, 7, 10, 30, tzinfo=cme_tz)
        )

        auth_req = {
            "type": "AUTH_REQUEST",
            "request_id": "test-req-001",
            "strategy_id": "strat-vwap-1",
            "account": "Sim101",
            "instrument": "MNQ",
            "side": "BUY",
            "qty": 4,
            "stop_distance": 20.0,
        }
        writer.write(encode_message(auth_req))
        await writer.drain()

        auth_resp_line = await reader.readline()
        auth_resp = decode_message(auth_resp_line.decode())
        self.assertEqual(auth_resp["type"], "AUTH_RESPONSE")
        self.assertEqual(auth_resp["request_id"], "test-req-001")
        self.assertTrue(auth_resp["allow"])
        self.assertEqual(auth_resp["max_qty"], 4)

        writer.close()
        await writer.wait_closed()

    async def _read_non_heartbeat(self, reader):
        while True:
            line = await reader.readline()
            msg = decode_message(line.decode())
            if msg.get("type") != "HEARTBEAT":
                return msg

    async def test_telemetry_with_balance_only_derives_daily_pnl_over_the_wire(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", 9876)
        # Formato que envia NT8: balance y unrealized, sin realized_pnl_today
        writer.write(encode_message({
            "type": "TELEMETRY", "account": "Sim101", "strategy_id": "s",
            "current_balance": 25080.5, "unrealized_pnl": 0,
        }))
        writer.write(encode_message({"type": "HEARTBEAT"}))  # el ACK confirma que el TELEMETRY ya se proceso
        await writer.drain()
        while decode_message((await reader.readline()).decode())["type"] != "HEARTBEAT_ACK":
            pass

        acc = self.risk_engine.get_account_state("Sim101")
        self.assertEqual(acc.current_balance, 25080.5)
        self.assertEqual(acc.realized_pnl_today, 80.5)
        writer.close()
        await writer.wait_closed()

    async def test_audit_log_timestamps_use_configured_timezone_not_utc(self):
        audit_path = Path(self.config.server.audit_log_path)
        first_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])

        self.assertNotIn("timestamp_utc", first_record)
        stamp = datetime.fromisoformat(first_record["timestamp"])
        chicago = zoneinfo.ZoneInfo(self.config.server.log_timezone)
        self.assertEqual(stamp.utcoffset(), stamp.astimezone(chicago).utcoffset())
        self.assertNotEqual(stamp.utcoffset().total_seconds(), 0)  # Chicago nunca es UTC+0

    async def test_dynamic_commands_broadcast(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", 9876)
        # Dar un ciclo de event loop para que el server registre el writer en connected_clients
        await asyncio.sleep(0.05)

        # Broadcast PAUSE desde el servidor
        await self.server.pause_strategy("strat-breakout-1")
        cmd = await self._read_non_heartbeat(reader)
        self.assertEqual(cmd["type"], "COMMAND")
        self.assertEqual(cmd["action"], "PAUSE")
        self.assertEqual(cmd["strategy_id"], "strat-breakout-1")

        # Broadcast FLATTEN
        await self.server.flatten_account("Sim101")
        flat_cmd = await self._read_non_heartbeat(reader)
        self.assertEqual(flat_cmd["type"], "COMMAND")
        self.assertEqual(flat_cmd["action"], "FLATTEN")
        self.assertEqual(flat_cmd["account"], "Sim101")

        writer.close()
        await writer.wait_closed()


class TestTradingDayTick(unittest.IsolatedAsyncioTestCase):
    async def test_server_ticks_trading_day_on_start_and_periodically(self):
        base_cfg = load_config("config/lucid_rules.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            server_cfg = ServerConfig(
                host="127.0.0.1", port=9877, heartbeat_interval_seconds=1,
                audit_log_path=str(Path(tmp) / "audit.log"),
            )
            config = GateConfig(
                server=server_cfg, instruments=base_cfg.instruments,
                presets=base_cfg.presets, accounts=base_cfg.accounts,
            )
            engine = RiskEngine(config)
            calls = []
            engine.tick = lambda now=None: calls.append(now) or []

            server = GateServer(config, risk_engine=engine, trading_day_tick_seconds=0.02)
            await server.start()
            await asyncio.sleep(0.15)
            await server.stop()

        self.assertGreaterEqual(len(calls), 3)  # un tick inmediato al arrancar (cierra dias perdidos) + periodicos


if __name__ == "__main__":
    unittest.main()
