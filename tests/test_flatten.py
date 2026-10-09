"""Tests del flatten automatico: ventana previa al break del CME y tope de consistencia de Eval.

Horas en hora de Chicago, fechas fijas (2026-10-07 miercoles, 10-10 sabado).
"""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
import zoneinfo

from src.gate.config import GateConfig, ServerConfig, SessionConfig, load_config
from src.gate.protocol import decode_message, encode_message
from src.gate.risk_engine import AuthRequest, RiskEngine, TelemetryUpdate
from src.gate.server import GateServer

CT = zoneinfo.ZoneInfo("America/Chicago")


def ct(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=CT)


class TestConsistencyAgainstAccumulatedProfit(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.engine = RiskEngine(self.config)
        # Sim101 = 25K Eval. Dia que empieza con $900 de profit acumulado -> tope de hoy $900
        self.engine.register_account("Sim101", balance=25900.0)

    def telemetry(self, balance):
        self.engine.update_telemetry(TelemetryUpdate("Sim101", "s", current_balance=balance), now=ct(7, 10))

    def request(self):
        return self.engine.evaluate_authorization(
            AuthRequest("r", "strat", "Sim101", "MNQ", "BUY", 1, 20.0), current_time=ct(7, 10, 30)
        )

    def test_below_the_accumulated_cap_keeps_trading(self):
        self.telemetry(25900.0 + 700.0)  # hoy +700 (> 625 del target, < 900 acumulado)
        self.assertFalse(self.engine.get_account_state("Sim101").is_flattened)
        self.assertTrue(self.request().allow)

    def test_reaching_the_accumulated_cap_blocks_and_queues_a_flatten(self):
        self.telemetry(25900.0 + 900.0)
        acc = self.engine.get_account_state("Sim101")
        self.assertTrue(acc.is_flattened)
        acc.is_flattened = False  # aislar la regla de autorizacion
        res = self.request()
        self.assertFalse(res.allow)
        self.assertIn("CONSISTENCY_CAP_REACHED", res.reason)
        self.assertIn("900.00", res.reason)

    def test_first_day_uses_the_target_floor(self):
        engine = RiskEngine(self.config)
        engine.register_account("Sim101", balance=25000.0)
        engine.update_telemetry(TelemetryUpdate("Sim101", "s", current_balance=25630.0), now=ct(7, 10))
        self.assertTrue(engine.get_account_state("Sim101").is_flattened)  # +630 >= 625


class TestFlattenRequests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.engine = RiskEngine(self.config)
        self.engine.register_account("Sim101", balance=25000.0)
        self.engine.register_account("Lucid_50K_01", balance=50000.0)

    def accounts(self, requests):
        return sorted(account for account, _ in requests)

    def test_no_flatten_outside_the_pre_break_window(self):
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 15, 54, 59)), [])
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 10, 0)), [])

    def test_flatten_requested_for_every_account_when_the_window_opens(self):
        requests = self.engine.collect_flatten_requests(ct(7, 15, 55))
        self.assertEqual(self.accounts(requests), ["Lucid_50K_01", "Sim101"])
        self.assertEqual({reason for _, reason in requests}, {"PRE_BREAK"})

    def test_retries_every_flatten_retry_seconds_not_on_every_check(self):
        self.engine.collect_flatten_requests(ct(7, 15, 55, 0))
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 15, 55, 10)), [])
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 15, 55, 29)), [])
        self.assertEqual(len(self.engine.collect_flatten_requests(ct(7, 15, 55, 31))), 2)

    def test_window_closes_when_the_break_starts(self):
        self.engine.collect_flatten_requests(ct(7, 15, 59, 50))
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 16, 0, 30)), [])

    def test_friday_close_is_also_flattened(self):
        self.assertEqual(len(self.engine.collect_flatten_requests(ct(9, 15, 57))), 2)

    def test_no_flatten_on_weekends(self):
        self.assertEqual(self.engine.collect_flatten_requests(ct(10, 15, 57)), [])

    def test_rth_only_mode_has_no_pre_break_flatten(self):
        self.config.session = SessionConfig(mode="rth_only")
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 15, 57)), [])

    def test_window_length_is_configurable(self):
        self.config.session = SessionConfig(flatten_before_break_minutes=15)
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 15, 44)), [])
        self.assertEqual(len(self.engine.collect_flatten_requests(ct(7, 15, 45))), 2)

    def test_consistency_cap_queues_one_flatten_only(self):
        self.engine.update_telemetry(TelemetryUpdate("Sim101", "s", current_balance=25650.0), now=ct(7, 10))
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 10, 1)), [("Sim101", "CONSISTENCY_CAP")])
        self.engine.update_telemetry(TelemetryUpdate("Sim101", "s", current_balance=25700.0), now=ct(7, 10, 2))
        self.assertEqual(self.engine.collect_flatten_requests(ct(7, 10, 3)), [])  # ya estaba marcada


class TestFlattenOverTheWire(unittest.IsolatedAsyncioTestCase):
    PORT = 9879

    async def asyncSetUp(self):
        base = load_config("config/lucid_rules.yaml")
        self.tmp = tempfile.TemporaryDirectory()
        config = GateConfig(
            server=ServerConfig(port=self.PORT, heartbeat_interval_seconds=60, audit_log_path=str(Path(self.tmp.name) / "a.log")),
            instruments=base.instruments, presets=base.presets, accounts=base.accounts,
        )
        self.engine = RiskEngine(config)
        self.engine.register_account("Sim101", balance=25000.0)
        self.server = GateServer(config, risk_engine=self.engine, trading_day_tick_seconds=3600, flatten_check_seconds=0.02)
        await self.server.start()
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.PORT)
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        self.writer.close()
        await self.server.stop()
        self.tmp.cleanup()

    async def next_flatten(self, reason):
        while True:
            message = decode_message((await asyncio.wait_for(self.reader.readline(), 3)).decode())
            if message["type"] == "COMMAND" and message["action"] == "FLATTEN" and message.get("reason") == reason:
                return message

    async def test_pre_break_window_sends_flatten_command_to_nt8(self):
        self.engine.collect_flatten_requests = lambda now=None: [("Sim101", "PRE_BREAK")]
        command = await self.next_flatten("PRE_BREAK")
        self.assertEqual(command["account"], "Sim101")

    async def test_consistency_cap_from_telemetry_sends_flatten_immediately(self):
        self.engine.collect_flatten_requests = RiskEngine.collect_flatten_requests.__get__(self.engine)
        # Con el reloj real fuera de la ventana pre-break, solo debe llegar el flatten por consistencia
        self.writer.write(encode_message({
            "type": "TELEMETRY", "account": "Sim101", "strategy_id": "s", "current_balance": 25700.0, "unrealized_pnl": 0,
        }))
        await self.writer.drain()
        command = await self.next_flatten("CONSISTENCY_CAP")
        self.assertEqual(command["account"], "Sim101")


if __name__ == "__main__":
    unittest.main()
