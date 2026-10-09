"""Tests para el motor de riesgo (RiskEngine) de Lucid Trading.

Todas las horas se expresan en hora de Chicago (CT, la hora oficial del CME) y usan
fechas fijas: nunca dependen de datetime.now() ni del dia de la semana en que corre el test.
Referencia de calendario: 2026-10-07 es miercoles, 10-09 viernes, 10-10 sabado, 10-11 domingo.
"""
import dataclasses
import unittest
from datetime import datetime
import zoneinfo

from src.gate.config import SessionConfig, load_config
from src.gate.risk_engine import RiskEngine, AuthRequest, PositionState, TelemetryUpdate

CT = zoneinfo.ZoneInfo("America/Chicago")


def ct(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, 0, tzinfo=CT)


def make_req(account="Sim101", strategy="strat-1", instrument="MNQ", qty=1, stop=20.0, rid="req"):
    return AuthRequest(
        request_id=rid,
        strategy_id=strategy,
        account=account,
        instrument=instrument,
        side="BUY",
        qty=qty,
        stop_distance=stop,
    )


class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.engine = RiskEngine(self.config)
        self.rth_time = ct(7, 10, 30)  # miercoles 10:30 CT
        # Inicializar cuenta Sim101 (25k_flex_eval: DD $1000, DLL $600, Max 2 NQ / 20 MNQ)
        self.engine.register_account("Sim101", balance=25000.0)

    def test_auth_approved_standard_mnq(self):
        res = self.engine.evaluate_authorization(make_req(qty=2, stop=25.0), current_time=self.rth_time)
        self.assertTrue(res.allow)
        self.assertEqual(res.max_qty, 2)
        self.assertEqual(res.reason, "APPROVED")

    def test_auth_rejected_missing_stop_loss(self):
        res = self.engine.evaluate_authorization(make_req(qty=2, stop=0.0), current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 0)
        self.assertIn("STOP_REQUIRED", res.reason)

    def test_auth_rejected_max_contracts_exceeded(self):
        # Cuenta 25k permite max 2 NQ
        res = self.engine.evaluate_authorization(
            make_req(instrument="NQ", qty=3, stop=15.0), current_time=self.rth_time
        )
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 2)
        self.assertIn("MAX_CONTRACTS_EXCEEDED", res.reason)

    def test_auth_rejected_daily_loss_limit_hit(self):
        # DLL en 25k es $600; simular perdidas acumuladas de $650 en el dia
        self.engine.update_telemetry(
            TelemetryUpdate(account="Sim101", strategy_id="s", realized_pnl_today=-650.0, unrealized_pnl=0.0)
        )
        res = self.engine.evaluate_authorization(make_req(stop=15.0), current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 0)
        self.assertIn("DAILY_LOSS_LIMIT_REACHED", res.reason)

    def test_auth_rejected_trailing_drawdown_floor_breached(self):
        # 25k piso inicial es 24.000 (DD max $1000); con balance 23.950 la cuenta esta liquidada
        self.engine.update_telemetry(
            TelemetryUpdate(
                account="Sim101", strategy_id="s", current_balance=23950.0,
                realized_pnl_today=-1050.0, unrealized_pnl=0.0,
            )
        )
        res = self.engine.evaluate_authorization(make_req(stop=15.0), current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertIn("DRAWDOWN_LIMIT_BREACHED", res.reason)

    # ------------------------------------------------------------------
    # Sesion CME (24h con break diario 16:00-17:00 CT = 17:00-18:00 ET)
    # ------------------------------------------------------------------
    def test_break_blocks_entries_from_pre_break_buffer(self):
        # Buffer de 5 min: bloquea desde 15:55 CT
        res = self.engine.evaluate_authorization(make_req(), current_time=ct(7, 15, 58))
        self.assertFalse(res.allow)
        self.assertIn("CME_DAILY_BREAK", res.reason)

    def test_break_blocks_entries_during_maintenance_hour(self):
        res = self.engine.evaluate_authorization(make_req(), current_time=ct(7, 16, 30))
        self.assertFalse(res.allow)
        self.assertIn("CME_DAILY_BREAK", res.reason)

    def test_entry_allowed_just_before_pre_break_buffer(self):
        res = self.engine.evaluate_authorization(make_req(), current_time=ct(7, 15, 54))
        self.assertTrue(res.allow)

    def test_entry_allowed_after_globex_reopen(self):
        res = self.engine.evaluate_authorization(make_req(qty=2), current_time=ct(7, 17, 15))
        self.assertTrue(res.allow)
        self.assertEqual(res.reason, "APPROVED")

    def test_auth_approved_overnight_24h_session(self):
        res = self.engine.evaluate_authorization(make_req(qty=2), current_time=ct(7, 2, 30))
        self.assertTrue(res.allow)
        self.assertEqual(res.reason, "APPROVED")

    def test_weekend_closed_from_friday_pre_break_until_sunday_reopen(self):
        friday_late = self.engine.evaluate_authorization(make_req(), current_time=ct(9, 15, 56))
        saturday = self.engine.evaluate_authorization(make_req(), current_time=ct(10, 12, 0))
        sunday_early = self.engine.evaluate_authorization(make_req(), current_time=ct(11, 16, 59))
        for res in (friday_late, saturday, sunday_early):
            self.assertFalse(res.allow)
            self.assertIn("WEEKEND_CLOSED", res.reason)

        sunday_open = self.engine.evaluate_authorization(make_req(), current_time=ct(11, 17, 1))
        self.assertTrue(sunday_open.allow)

    def test_session_boundaries_follow_chicago_dst_not_fixed_offset(self):
        # Despues del cambio de hora (2026-11-01) el break sigue siendo 16:00 CT.
        before = datetime(2026, 11, 3, 15, 54, tzinfo=CT)
        inside = datetime(2026, 11, 3, 16, 5, tzinfo=CT)
        self.assertTrue(self.engine.evaluate_authorization(make_req(), current_time=before).allow)
        self.assertFalse(self.engine.evaluate_authorization(make_req(), current_time=inside).allow)

    def test_rth_only_mode_uses_session_start_and_flatten_in_chicago_time(self):
        self.config.session = SessionConfig(mode="rth_only")
        preset = self.config.get_preset_for_account("Sim101")
        self.assertEqual((preset.session_start_time, preset.session_flatten_time), ("08:30", "14:55"))

        early = self.engine.evaluate_authorization(make_req(), current_time=ct(7, 8, 0))
        inside = self.engine.evaluate_authorization(make_req(), current_time=ct(7, 9, 0))
        late = self.engine.evaluate_authorization(make_req(), current_time=ct(7, 14, 56))
        self.assertIn("OUTSIDE_TRADING_HOURS", early.reason)
        self.assertTrue(inside.allow)
        self.assertIn("OUTSIDE_TRADING_HOURS", late.reason)

    def test_auth_rejected_when_strategy_paused(self):
        self.engine.pause_strategy("strat-orb-1", paused=True)
        res = self.engine.evaluate_authorization(
            make_req(strategy="strat-orb-1", stop=15.0), current_time=self.rth_time
        )
        self.assertFalse(res.allow)
        self.assertIn("STRATEGY_PAUSED", res.reason)

    def test_reconciliation_updates_positions(self):
        positions = [PositionState(account="Sim101", instrument="MNQ", qty=5, entry_price=21000.0, side="LONG")]
        self.engine.reconcile_account("Sim101", positions=positions)
        acc = self.engine.get_account_state("Sim101")
        self.assertEqual(acc.current_positions["MNQ"].qty, 5)

        # Pedir 18 contratos mas (total seria 23, excede max 20)
        res = self.engine.evaluate_authorization(make_req(qty=18, stop=10.0), current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertEqual(res.max_qty, 15)  # 20 max - 5 actuales = 15 permitidos

    def test_eval_consistency_cap_rejects_and_flattens(self):
        # En Sim101 (25k eval, target $1.250, cap 50% = $625): hoy $650 -> aplanar y rechazar
        self.engine.update_telemetry(
            TelemetryUpdate(account="Sim101", strategy_id="s", realized_pnl_today=650.0, unrealized_pnl=0.0)
        )
        acc = self.engine.get_account_state("Sim101")
        self.assertTrue(acc.is_flattened)

        # Desmarcar is_flattened para probar directamente el bloqueo de consistency_cap en auth
        acc.is_flattened = False
        res = self.engine.evaluate_authorization(make_req(), current_time=self.rth_time)
        self.assertFalse(res.allow)
        self.assertIn("CONSISTENCY_CAP_REACHED", res.reason)


class TestFundedPayoutRules(unittest.TestCase):
    """Cuenta 25K Funded (Rules/Lucid.xlsx): min_profit_day_amount $100 x 5 dias; buffer $1.100 + payout $1.000.

    balance_for_payout = 25.000 + 1.100 + 1.000 = 27.100.
    """

    ACCOUNT = "Funded_25K_01"
    BALANCE_FOR_PAYOUT = 27100.0

    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.when = ct(7, 10, 30)

    def _engine(self, buffer=1100.0, payout=1000.0) -> RiskEngine:
        preset = dataclasses.replace(self.config.presets["25k_flex_funded"], buffer=buffer, payout=payout)
        self.config.presets["25k_flex_funded"] = preset
        self.config.accounts[self.ACCOUNT] = "25k_flex_funded"
        engine = RiskEngine(self.config)
        engine.register_account(self.ACCOUNT, balance=25000.0)
        return engine

    def _telemetry(self, engine, balance, pnl_today):
        engine.update_telemetry(
            TelemetryUpdate(
                account=self.ACCOUNT, strategy_id="s",
                current_balance=balance, realized_pnl_today=pnl_today, unrealized_pnl=0.0,
            )
        )

    def _auth(self, engine):
        return engine.evaluate_authorization(make_req(account=self.ACCOUNT), current_time=self.when)

    def test_balance_for_payout_is_initial_plus_buffer_plus_payout(self):
        engine = self._engine()
        self.assertEqual(engine.accounts[self.ACCOUNT].preset.balance_for_payout, self.BALANCE_FOR_PAYOUT)

    def test_funded_account_is_not_liquidated_at_initial_balance(self):
        # Regresion: min_account_balance (25.100) es el tope del trailing, no un piso fijo
        engine = self._engine()
        res = self._auth(engine)
        self.assertTrue(res.allow, res.reason)

    def test_funded_floor_stops_trailing_at_min_account_balance(self):
        engine = self._engine()
        self._telemetry(engine, balance=27000.0, pnl_today=0.0)  # HWM 27.000: trail 26.000 > 25.100
        self._telemetry(engine, balance=25100.0, pnl_today=0.0)
        breached = self._auth(engine)
        self.assertFalse(breached.allow)
        self.assertIn("DRAWDOWN_LIMIT_BREACHED", breached.reason)

        self._telemetry(engine, balance=25200.0, pnl_today=0.0)
        self.assertTrue(self._auth(engine).allow)

    def test_no_day_lock_before_balance_for_payout_is_reached(self):
        # Objetivo: llegar a balance_for_payout lo antes posible -> seguir operando aunque hoy ya hay $150
        engine = self._engine()
        self._telemetry(engine, balance=25300.0, pnl_today=150.0)
        res = self._auth(engine)
        self.assertTrue(res.allow, res.reason)

    def test_day_locks_once_balance_for_payout_reached_and_day_qualifies(self):
        engine = self._engine()
        self._telemetry(engine, balance=27150.0, pnl_today=110.0)
        res = self._auth(engine)
        self.assertFalse(res.allow)
        self.assertIn("FUNDED_QUALIFYING_DAY_LOCKED", res.reason)

    def test_keeps_trading_until_day_reaches_min_profit_day_amount(self):
        engine = self._engine()
        self._telemetry(engine, balance=27150.0, pnl_today=50.0)  # balance ok, dia aun no califica
        self.assertTrue(self._auth(engine).allow)

    def test_no_lock_when_enough_qualifying_days_already_recorded(self):
        engine = self._engine()
        for day_profit in [105.0, 150.0, 120.0, 100.0, 130.0]:
            engine.record_closed_day(self.ACCOUNT, day_profit)
        self._telemetry(engine, balance=27150.0, pnl_today=110.0)
        self.assertTrue(self._auth(engine).allow)

    def test_record_closed_day_ignores_days_below_minimum(self):
        engine = self._engine()
        self.assertFalse(engine.record_closed_day(self.ACCOUNT, 99.99))
        self.assertTrue(engine.record_closed_day(self.ACCOUNT, 100.0))
        self.assertEqual(len(engine.accounts[self.ACCOUNT].qualifying_days_history), 1)

    def test_payout_status_requires_balance_for_payout_not_min_account_balance(self):
        engine = self._engine()
        for day_profit in [105.0, 150.0, 120.0, 100.0]:
            engine.record_closed_day(self.ACCOUNT, day_profit)

        # 4 historicos + hoy ($110) = 5 dias, pero balance 25.300 < 25.600 (aunque > min_account_balance 25.100)
        self._telemetry(engine, balance=25300.0, pnl_today=110.0)
        status = engine.get_payout_status(self.ACCOUNT)
        self.assertEqual(status["qualified_days_count"], 5)
        self.assertTrue(status["has_min_days"])
        self.assertFalse(status["has_balance_for_payout"])
        self.assertFalse(status["is_payout_eligible"])
        self.assertEqual(status["balance_for_payout"], self.BALANCE_FOR_PAYOUT)
        self.assertEqual(status["balance_remaining"], self.BALANCE_FOR_PAYOUT - 25300.0)

        self._telemetry(engine, balance=27100.0, pnl_today=110.0)
        status = engine.get_payout_status(self.ACCOUNT)
        self.assertTrue(status["has_balance_for_payout"])
        self.assertTrue(status["is_payout_eligible"])

    def test_payout_status_mode_reports_current_objective(self):
        engine = self._engine()
        self._telemetry(engine, balance=25300.0, pnl_today=0.0)
        self.assertEqual(engine.get_payout_status(self.ACCOUNT)["mode"], "accumulate_balance")

        self._telemetry(engine, balance=27150.0, pnl_today=0.0)
        self.assertEqual(engine.get_payout_status(self.ACCOUNT)["mode"], "qualify_days")

    def test_payout_not_configured_does_not_block_trading(self):
        engine = self._engine(buffer=None, payout=None)
        self.assertIsNone(engine.accounts[self.ACCOUNT].preset.balance_for_payout)
        self._telemetry(engine, balance=26500.0, pnl_today=300.0)
        self.assertTrue(self._auth(engine).allow)

        status = engine.get_payout_status(self.ACCOUNT)
        self.assertFalse(status["payout_configured"])
        self.assertFalse(status["is_payout_eligible"])
        self.assertEqual(status["mode"], "payout_not_configured")


class TestPayoutPhaseRules(unittest.TestCase):
    """Fase FLEX PAYOUT 25K (posterior al primer retiro): buffer $1.100 + payout $2.000 -> balance_for_payout $28.100.

    Misma mecanica que Funded: piso del trailing tope en min_account_balance y bloqueo del dia calificado.
    """

    ACCOUNT = "Payout_25K_01"

    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.config.accounts[self.ACCOUNT] = "25k_flex_payout"
        self.engine = RiskEngine(self.config)
        self.engine.register_account(self.ACCOUNT, balance=25000.0)
        self.when = ct(7, 10, 30)

    def _telemetry(self, balance, pnl_today):
        self.engine.update_telemetry(
            TelemetryUpdate(
                account=self.ACCOUNT, strategy_id="s",
                current_balance=balance, realized_pnl_today=pnl_today, unrealized_pnl=0.0,
            )
        )

    def _auth(self):
        return self.engine.evaluate_authorization(make_req(account=self.ACCOUNT), current_time=self.when)

    def test_account_is_not_liquidated_at_initial_balance(self):
        self.assertTrue(self._auth().allow)

    def test_trailing_floor_stops_at_min_account_balance(self):
        self._telemetry(29000.0, 0.0)  # HWM 29.000: trail 28.000 > 25.100
        self._telemetry(25100.0, 0.0)
        breached = self._auth()
        self.assertFalse(breached.allow)
        self.assertIn("DRAWDOWN_LIMIT_BREACHED", breached.reason)

    def test_no_day_lock_before_balance_for_payout(self):
        self._telemetry(27000.0, 150.0)
        self.assertTrue(self._auth().allow)

    def test_day_locks_after_balance_for_payout_and_qualifying_profit(self):
        self._telemetry(28150.0, 110.0)
        res = self._auth()
        self.assertFalse(res.allow)
        self.assertIn("FUNDED_QUALIFYING_DAY_LOCKED", res.reason)

    def test_payout_status_is_applicable_and_eligible_with_days_and_balance(self):
        for day_profit in [105.0, 150.0, 120.0, 100.0]:
            self.engine.record_closed_day(self.ACCOUNT, day_profit)
        self._telemetry(28100.0, 110.0)
        status = self.engine.get_payout_status(self.ACCOUNT)
        self.assertTrue(status["payout_applicable"])
        self.assertEqual(status["balance_for_payout"], 28100.0)
        self.assertTrue(status["is_payout_eligible"])
        self.assertEqual(status["mode"], "eligible")


if __name__ == "__main__":
    unittest.main()
