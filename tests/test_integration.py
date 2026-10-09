"""Suite de pruebas de integracion y simulacion de fallos (Fail-Closed, Timeouts, Heartbeats)."""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
import zoneinfo

from src.gate.client import GateClient
from src.gate.config import load_config, ServerConfig, GateConfig
from src.gate.risk_engine import RiskEngine, AuthRequest, PositionState
from src.gate.server import GateServer


class TestIntegrationAndFailures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base_cfg = load_config("config/lucid_rules.yaml")
        self.tmp_dir = tempfile.TemporaryDirectory()
        audit_file = str(Path(self.tmp_dir.name) / "integration_audit.log")

        self.port = 9888
        server_cfg = ServerConfig(
            host="127.0.0.1",
            port=self.port,
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

        # Fijar un miercoles 10:30 hora de Chicago (determinista, independiente del reloj real)
        cme_tz = zoneinfo.ZoneInfo("America/Chicago")
        self.risk_engine.evaluate_authorization_orig = self.risk_engine.evaluate_authorization
        self.risk_engine.evaluate_authorization = lambda req: self.risk_engine.evaluate_authorization_orig(
            req, current_time=datetime(2026, 10, 7, 10, 30, tzinfo=cme_tz)
        )

        self.server = GateServer(self.config, risk_engine=self.risk_engine)

    async def asyncTearDown(self):
        if self.server._running:
            await self.server.stop()
        self.tmp_dir.cleanup()

    async def test_failure_python_server_down_fail_closed(self):
        # Servidor NO iniciado. Cliente intenta conectar.
        client = GateClient(host="127.0.0.1", port=self.port, timeout_ms=100)
        connected = await client.connect()
        self.assertFalse(connected)

        # Solicitar entrada debe ser rechazada inmediatamente (Fail-closed)
        req = AuthRequest(
            request_id="req-srv-down",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=2,
            stop_distance=20.0,
        )
        resp = await client.request_authorization(req)
        self.assertFalse(resp.allow)
        self.assertEqual(resp.max_qty, 0)
        self.assertIn("FAIL_CLOSED", resp.reason)

    async def test_failure_latency_timeout_denies_entry(self):
        # Iniciar servidor
        await self.server.start()

        # Inyectar un delay artificial en el servidor mayor al timeout del cliente
        orig_proc = self.server._process_message

        async def slow_process(msg, writer):
            if msg.get("type") == "AUTH_REQUEST":
                await asyncio.sleep(0.15)  # 150 ms > timeout de 50 ms
            await orig_proc(msg, writer)

        self.server._process_message = slow_process

        # Cliente con timeout estricto de 50 ms
        client = GateClient(host="127.0.0.1", port=self.port, timeout_ms=50)
        connected = await client.connect()
        self.assertTrue(connected)

        req = AuthRequest(
            request_id="req-slow",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=20.0,
        )
        resp = await client.request_authorization(req)
        self.assertFalse(resp.allow)
        self.assertIn("TIMEOUT_EXCEEDED", resp.reason)

        await client.disconnect()

    async def test_failure_missing_heartbeat_fail_closed(self):
        await self.server.start()

        # Cliente con umbral de heartbeat muy corto (0.3 seg)
        client = GateClient(
            host="127.0.0.1",
            port=self.port,
            timeout_ms=100,
            heartbeat_timeout_seconds=0.2,
        )
        connected = await client.connect()
        self.assertTrue(connected)

        # Pausar temporalmente el loop de heartbeat del servidor
        if self.server._heartbeat_task:
            self.server._heartbeat_task.cancel()

        # Esperar a que venza el umbral del cliente
        await asyncio.sleep(0.3)
        self.assertFalse(client.is_heartbeat_healthy())

        req = AuthRequest(
            request_id="req-hb-dead",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=20.0,
        )
        resp = await client.request_authorization(req)
        self.assertFalse(resp.allow)
        self.assertIn("FAIL_CLOSED", resp.reason)

        await client.disconnect()

    async def test_reconciliation_flow_after_restart(self):
        await self.server.start()

        client = GateClient(host="127.0.0.1", port=self.port, timeout_ms=100)
        await client.connect()

        # Simular que NT8 reinicio y encontro 3 contratos MNQ abiertos en broker
        open_pos = [
            PositionState(account="Sim101", instrument="MNQ", qty=3, entry_price=21020.0, side="LONG")
        ]
        ok = await client.reconcile("Sim101", open_pos)
        self.assertTrue(ok)
        await asyncio.sleep(0.05)

        # El servidor ahora sabe que hay 3 contratos
        acc = self.risk_engine.get_account_state("Sim101")
        self.assertEqual(acc.current_positions["MNQ"].qty, 3)

        # Pedir 18 contratos mas (3 + 18 = 21, excede el maximo de 20)
        req = AuthRequest(
            request_id="req-after-rec",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=18,
            stop_distance=20.0,
        )
        resp = await client.request_authorization(req)
        self.assertFalse(resp.allow)
        self.assertEqual(resp.max_qty, 17)  # 20 max - 3 = 17 disponibles

        await client.disconnect()

    async def test_local_rejection_for_zero_stop(self):
        # Ni siquiera se necesita servidor levantado; el cliente rechaza localmente
        client = GateClient(host="127.0.0.1", port=self.port, timeout_ms=100)
        client.is_connected = True
        client.last_heartbeat_time = asyncio.get_event_loop().time()

        req = AuthRequest(
            request_id="req-no-stop-local",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=0.0,
        )
        resp = await client.request_authorization(req)
        self.assertFalse(resp.allow)
        self.assertIn("LOCAL_REJECT_STOP_REQUIRED", resp.reason)


if __name__ == "__main__":
    unittest.main()
