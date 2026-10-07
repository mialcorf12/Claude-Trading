"""Tests para el motor de riesgo (RiskEngine) de Lucid Trading."""
import unittest
from datetime import datetime, time
import zoneinfo
from src.gate.config import load_config
from src.gate.risk_engine import RiskEngine, AuthRequest, PositionState, TelemetryUpdate


class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.engine = RiskEngine(self.config)
        self.tz = zoneinfo.ZoneInfo("America/New_York")
        self.rth_time = datetime.now(self.tz).replace(hour=10, minute=30, second=0)
        # Inicializar cuenta Sim101 (25k_flex_eval: DD $1000, DLL $600, Max 2 NQ / 20 MNQ)
        self.engine.register_account("Sim101", balance=25000.0)

    def test_auth_approved_standard_mnq(self):
        req = AuthRequest(
            request_id="req-1",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=2,
            stop_distance=25.0, # 25 puntos
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertTrue(res.allow)
        self.assertEqual(res.max_qty, 2)
        self.assertEqual(res.reason, "APPROVED")

    def test_auth_rejected_missing_stop_loss(self):
        req = AuthRequest(
            request_id="req-no-stop",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=2,
            stop_distance=0.0, # SIN STOP
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 0)
        self.assertIn("STOP_REQUIRED", res.reason)

    def test_auth_rejected_max_contracts_exceeded(self):
        # Cuenta 25k permite max 2 NQ
        req = AuthRequest(
            request_id="req-too-many",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="NQ",
            side="BUY",
            qty=3, # Pide 3, max es 2
            stop_distance=15.0,
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 2)
        self.assertIn("MAX_CONTRACTS_EXCEEDED", res.reason)

    def test_auth_rejected_daily_loss_limit_hit(self):
        # DLL en 25k es $600
        # Simular perdidas acumuladas de $650 en el dia
        self.engine.update_telemetry(
            TelemetryUpdate(
                account="Sim101",
                strategy_id="strat-orb-1",
                realized_pnl_today=-650.0,
                unrealized_pnl=0.0,
            )
        )
        req = AuthRequest(
            request_id="req-dll",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=15.0,
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 0)
        self.assertIn("DAILY_LOSS_LIMIT_REACHED", res.reason)

    def test_auth_rejected_trailing_drawdown_floor_breached(self):
        # 25k piso inicial es 24.000 (DD max $1000)
        # Si el balance cae a 23.950, la cuenta esta liquidada
        self.engine.update_telemetry(
            TelemetryUpdate(
                account="Sim101",
                strategy_id="strat-orb-1",
                current_balance=23950.0,
                realized_pnl_today=-1050.0,
                unrealized_pnl=0.0,
            )
        )
        req = AuthRequest(
            request_id="req-dd-blown",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=15.0,
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertIn("DRAWDOWN_LIMIT_BREACHED", res.reason)

    def test_auth_rejected_outside_trading_hours(self):
        req = AuthRequest(
            request_id="req-late",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=15.0,
        )
        # 15:58 ET (posterior al flattening de 15:55)
        late_et = datetime.now(self.tz).replace(hour=15, minute=58, second=0)

        res = self.engine.evaluate_authorization(req, current_time=late_et)
        self.assertFalse(res.allow)
        self.assertIn("OUTSIDE_TRADING_HOURS", res.reason)

    def test_auth_rejected_when_strategy_paused(self):
        self.engine.pause_strategy("strat-orb-1", paused=True)
        req = AuthRequest(
            request_id="req-paused",
            strategy_id="strat-orb-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=15.0,
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertIn("STRATEGY_PAUSED", res.reason)

    def test_reconciliation_updates_positions(self):
        positions = [
            PositionState(account="Sim101", instrument="MNQ", qty=5, entry_price=21000.0, side="LONG")
        ]
        self.engine.reconcile_account("Sim101", positions=positions)
        acc = self.engine.get_account_state("Sim101")
        self.assertEqual(acc.current_positions["MNQ"].qty, 5)

        # Ahora pedir 18 contratos mas (total seria 23, excede max 20)
        req = AuthRequest(
            request_id="req-rec-check",
            strategy_id="strat-2",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=18,
            stop_distance=10.0,
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 15)  # 20 max - 5 actuales = 15 permitidos


if __name__ == "__main__":
    unittest.main()
