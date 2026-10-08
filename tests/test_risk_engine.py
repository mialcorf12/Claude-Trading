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
        # 15:58 ET (14:58 CST - dentro del buffer previo al cierre de 15:00 CST)
        # Fijar un día de semana (Miércoles)
        late_et = datetime(2026, 10, 7, 15, 58, 0, tzinfo=self.tz)

        res = self.engine.evaluate_authorization(req, current_time=late_et)
        self.assertFalse(res.allow)
        self.assertIn("CME_DAILY_BREAK", res.reason)

    def test_auth_approved_overnight_24h_session(self):
        req = AuthRequest(
            request_id="req-overnight",
            strategy_id="strat-london-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=2,
            stop_distance=20.0,
        )
        # 02:30 CST (03:30 ET - sesión de Londres en plena noche)
        cme_tz = zoneinfo.ZoneInfo("America/Chicago")
        night_time = datetime(2026, 10, 7, 2, 30, 0, tzinfo=cme_tz)
        res = self.engine.evaluate_authorization(req, current_time=night_time)
        self.assertTrue(res.allow)
        self.assertEqual(res.reason, "APPROVED")

    def test_auth_approved_post_break_globex_reopen(self):
        req = AuthRequest(
            request_id="req-post-break",
            strategy_id="strat-reopen-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=2,
            stop_distance=20.0,
        )
        # 16:15 CST (17:15 ET - después de la reapertura de las 16:00 CST)
        cme_tz = zoneinfo.ZoneInfo("America/Chicago")
        post_break_time = datetime(2026, 10, 7, 16, 15, 0, tzinfo=cme_tz)
        res = self.engine.evaluate_authorization(req, current_time=post_break_time)
        self.assertTrue(res.allow)
        self.assertEqual(res.reason, "APPROVED")

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

    def test_eval_consistency_cap_rejects_and_flattens(self):
        # En Sim101 (25k eval, target $1.250, cap 50% = $625)
        # Si hoy se alcanza $650, debe marcar aplanado y rechazar entradas nuevas
        self.engine.update_telemetry(
            TelemetryUpdate(
                account="Sim101",
                strategy_id="strat-1",
                realized_pnl_today=650.0,
                unrealized_pnl=0.0,
            )
        )
        acc = self.engine.get_account_state("Sim101")
        self.assertTrue(acc.is_flattened)

        req = AuthRequest(
            request_id="req-cap-check",
            strategy_id="strat-1",
            account="Sim101",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=20.0,
        )
        # Desmarcar temporalmente is_flattened para probar directamente el bloqueo de consistency_cap en auth
        acc.is_flattened = False
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertIn("CONSISTENCY_CAP_REACHED", res.reason)

    def test_funded_min_days_of_profit_locks_day_and_tracks_payout(self):
        # Mapear cuenta funded de 25k (min 5 dias de $100, buffer min $25.100)
        self.config.accounts["Funded_25K_01"] = "25k_flex_funded"
        self.engine.register_account("Funded_25K_01", balance=25000.0)

        status_init = self.engine.get_payout_status("Funded_25K_01")
        self.assertFalse(status_init["is_payout_eligible"])
        self.assertEqual(status_init["qualified_days_count"], 0)

        # Si hoy el PnL alcanza $110 (supera los $100 minimos requeridos para el dia)
        self.engine.update_telemetry(
            TelemetryUpdate(
                account="Funded_25K_01",
                strategy_id="strat-funded-1",
                realized_pnl_today=110.0,
                unrealized_pnl=0.0,
                current_balance=25110.0,
            )
        )

        # 1. El gate debe rechazar nuevas entradas para proteger y asegurar el dia ganador
        req = AuthRequest(
            request_id="req-funded-day-lock",
            strategy_id="strat-funded-1",
            account="Funded_25K_01",
            instrument="MNQ",
            side="BUY",
            qty=1,
            stop_distance=20.0,
        )
        res = self.engine.evaluate_authorization(req, current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertIn("FUNDED_QUALIFYING_DAY_LOCKED", res.reason)

        # 2. Simular historial de 4 dias previos ganadores de $100+
        for p in [105.0, 150.0, 120.0, 100.0]:
            self.engine.record_closed_day("Funded_25K_01", p)

        # Con 4 dias historicos + hoy ($110) = 5 dias calificados, y balance $25.110 >= $25.100
        status_final = self.engine.get_payout_status("Funded_25K_01")
        self.assertEqual(status_final["qualified_days_count"], 5)
        self.assertTrue(status_final["has_min_days"])
        self.assertTrue(status_final["has_min_balance"])
        self.assertTrue(status_final["is_payout_eligible"])


if __name__ == "__main__":
    unittest.main()
