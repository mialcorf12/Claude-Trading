"""Tests del cierre automatico del dia de trading (record_closed_day, HWM EOD, persistencia).

Dia de trading = 16:00 CT -> 16:00 CT (17:00 ET). Todas las horas en hora de Chicago, fechas fijas.
Calendario: 2026-10-07 miercoles, 10-08 jueves, 10-09 viernes, 10-10 sabado, 10-11 domingo, 10-12 lunes.
"""
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
import zoneinfo

from src.gate.config import load_config
from src.gate.risk_engine import AuthRequest, RiskEngine, TelemetryUpdate
from src.gate.state_store import StateStore

CT = zoneinfo.ZoneInfo("America/Chicago")
FUNDED = "Funded_25K_01"
EVAL = "Sim101"


def ct(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, 0, tzinfo=CT)


def tele(account, balance=None, pnl=None, unrealized=None):
    return TelemetryUpdate(
        account=account, strategy_id="s",
        current_balance=balance, realized_pnl_today=pnl, unrealized_pnl=unrealized,
    )


def req(account, rid="r"):
    return AuthRequest(rid, "strat", account, "MNQ", "BUY", 1, 20.0)


class TradingDayTestBase(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.config.accounts[FUNDED] = "25k_flex_funded"
        self.engine = RiskEngine(self.config)
        self.engine.register_account(FUNDED, balance=25000.0)
        self.engine.register_account(EVAL, balance=25000.0)

    @property
    def funded(self):
        return self.engine.get_account_state(FUNDED)


class TestTradingDayLabel(TradingDayTestBase):
    def test_day_rolls_at_16_00_chicago(self):
        label = self.engine.trading_day_label
        self.assertEqual(label(ct(7, 15, 59)), date(2026, 10, 7))
        self.assertEqual(label(ct(7, 16, 0)), date(2026, 10, 8))
        self.assertEqual(label(ct(7, 23, 30)), date(2026, 10, 8))
        self.assertEqual(label(ct(8, 2, 0)), date(2026, 10, 8))

    def test_friday_close_and_sunday_reopen_roll_to_next_trading_day(self):
        label = self.engine.trading_day_label
        self.assertEqual(label(ct(9, 16, 0)), date(2026, 10, 10))   # viernes 16:00 -> "sabado" (sin trading)
        self.assertEqual(label(ct(11, 17, 0)), date(2026, 10, 12))  # domingo 17:00 -> lunes


class TestRecordClosedDay(TradingDayTestBase):
    def test_rollover_closes_day_and_records_qualifying_profit(self):
        self.engine.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(7, 10))
        self.assertEqual(self.funded.realized_pnl_today, 150.0)  # derivado: balance - day_start_balance

        closed = self.engine.tick(now=ct(7, 16, 1))

        self.assertEqual(closed, [(FUNDED, date(2026, 10, 7))])
        self.assertEqual(self.funded.qualifying_days_history, [150.0])
        self.assertEqual(self.funded.trading_day, date(2026, 10, 8))
        self.assertEqual(self.funded.day_start_balance, 25150.0)
        self.assertEqual(self.funded.realized_pnl_today, 0.0)

    def test_day_below_minimum_is_logged_but_not_qualifying(self):
        self.engine.update_telemetry(tele(FUNDED, balance=25050.0), now=ct(7, 10))
        self.engine.tick(now=ct(7, 16, 1))
        self.assertEqual(self.funded.qualifying_days_history, [])
        self.assertEqual(self.funded.closed_days[-1]["date"], "2026-10-07")
        self.assertEqual(self.funded.closed_days[-1]["pnl"], 50.0)
        self.assertFalse(self.funded.closed_days[-1]["qualified"])

    def test_five_rollovers_with_profit_complete_the_required_days(self):
        balance = 25000.0
        for day in (5, 6, 7, 8, 9):  # lunes a viernes
            balance += 120.0
            self.engine.update_telemetry(tele(FUNDED, balance=balance), now=ct(day, 10))
            self.engine.tick(now=ct(day, 16, 1))
        self.assertEqual(len(self.funded.qualifying_days_history), 5)

    def test_eod_high_water_mark_updates_only_at_close_not_intraday(self):
        self.engine.update_telemetry(tele(FUNDED, balance=26000.0), now=ct(7, 10))
        self.assertEqual(self.funded.eod_hwm, 25000.0)
        self.engine.tick(now=ct(7, 16, 1))
        self.assertEqual(self.funded.eod_hwm, 26000.0)

    def test_hwm_never_decreases_after_a_losing_day(self):
        self.engine.update_telemetry(tele(FUNDED, balance=26000.0), now=ct(7, 10))
        self.engine.tick(now=ct(7, 16, 1))
        self.engine.update_telemetry(tele(FUNDED, balance=25500.0), now=ct(8, 10))
        self.engine.tick(now=ct(8, 16, 1))
        self.assertEqual(self.funded.eod_hwm, 26000.0)

    def test_rollover_resets_daily_flatten_lock_of_eval_account(self):
        self.engine.update_telemetry(tele(EVAL, balance=25650.0), now=ct(7, 10))  # +650 >= cap 625
        self.assertTrue(self.engine.get_account_state(EVAL).is_flattened)
        self.engine.tick(now=ct(7, 16, 1))
        eval_acc = self.engine.get_account_state(EVAL)
        self.assertFalse(eval_acc.is_flattened)
        self.assertEqual(eval_acc.realized_pnl_today, 0.0)

    def test_authorization_also_triggers_rollover(self):
        self.engine.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(7, 10))
        self.engine.evaluate_authorization(req(FUNDED), current_time=ct(8, 10))
        self.assertEqual(self.funded.qualifying_days_history, [150.0])

    def test_no_double_close_within_the_same_trading_day(self):
        self.engine.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(7, 10))
        self.engine.tick(now=ct(7, 16, 1))
        self.assertEqual(self.engine.tick(now=ct(7, 20, 0)), [])
        self.assertEqual(self.funded.qualifying_days_history, [150.0])

    def test_weekend_label_is_not_logged_as_a_trading_day(self):
        self.engine.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(9, 10))
        self.engine.tick(now=ct(9, 16, 1))   # cierra el viernes
        self.engine.tick(now=ct(11, 17, 1))  # cierra la etiqueta "sabado" (sin trading)
        self.assertEqual([d["date"] for d in self.funded.closed_days], ["2026-10-09"])


class TestBalanceBaseline(unittest.TestCase):
    def test_first_balance_sets_baseline_so_pnl_today_starts_at_zero(self):
        config = load_config("config/lucid_rules.yaml")
        engine = RiskEngine(config)
        engine.register_account(EVAL)  # sin balance explicito: el baseline lo da la primera telemetria
        engine.update_telemetry(tele(EVAL, balance=25430.0), now=ct(7, 10))
        acc = engine.get_account_state(EVAL)
        self.assertEqual((acc.day_start_balance, acc.eod_hwm, acc.realized_pnl_today), (25430.0, 25430.0, 0.0))

        engine.update_telemetry(tele(EVAL, balance=25480.0), now=ct(7, 11))
        self.assertEqual(acc.realized_pnl_today, 50.0)

    def test_explicit_pnl_in_telemetry_still_takes_precedence(self):
        config = load_config("config/lucid_rules.yaml")
        engine = RiskEngine(config)
        engine.register_account(EVAL, balance=25000.0)
        engine.update_telemetry(tele(EVAL, balance=25100.0, pnl=-300.0), now=ct(7, 10))
        self.assertEqual(engine.get_account_state(EVAL).realized_pnl_today, -300.0)


class TestStatePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "gate_state.json"
        self.config = load_config("config/lucid_rules.yaml")
        self.config.accounts[FUNDED] = "25k_flex_funded"

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self):
        return RiskEngine(self.config, state_store=StateStore(self.path))

    def test_state_survives_gate_restart(self):
        first = self._engine()
        first.register_account(FUNDED, balance=25000.0)
        first.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(7, 10))
        first.tick(now=ct(7, 16, 1))
        first.update_telemetry(tele(FUNDED, balance=25230.0), now=ct(8, 10))

        restarted = self._engine()
        acc = restarted.register_account(FUNDED)
        self.assertEqual(acc.qualifying_days_history, [150.0])
        self.assertEqual(acc.eod_hwm, 25150.0)
        self.assertEqual(acc.day_start_balance, 25150.0)
        self.assertEqual(acc.current_balance, 25230.0)
        self.assertEqual(acc.trading_day, date(2026, 10, 8))
        self.assertEqual(acc.realized_pnl_today, 80.0)

    def test_restart_after_rollover_closes_the_missed_day_on_next_tick(self):
        first = self._engine()
        first.register_account(FUNDED, balance=25000.0)
        first.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(7, 10))

        restarted = self._engine()
        restarted.register_account(FUNDED)
        restarted.tick(now=ct(8, 9))  # el gate estuvo caido durante el cierre del 7
        self.assertEqual(restarted.get_account_state(FUNDED).qualifying_days_history, [150.0])

    def test_corrupt_state_file_is_ignored_without_crashing(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ esto no es json", encoding="utf-8")
        acc = self._engine().register_account(FUNDED, balance=25000.0)
        self.assertEqual(acc.qualifying_days_history, [])

    def test_state_for_a_different_preset_is_discarded(self):
        first = self._engine()
        first.register_account(FUNDED, balance=25000.0)
        first.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(7, 10))
        first.tick(now=ct(7, 16, 1))

        self.config.accounts[FUNDED] = "50k_flex_funded"
        acc = self._engine().register_account(FUNDED, balance=50000.0)
        self.assertEqual(acc.qualifying_days_history, [])
        self.assertEqual(acc.eod_hwm, 50000.0)

    def test_state_file_is_valid_json_with_preset_id(self):
        engine = self._engine()
        engine.register_account(FUNDED, balance=25000.0)
        engine.update_telemetry(tele(FUNDED, balance=25150.0), now=ct(7, 10))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["accounts"][FUNDED]["preset_id"], "25k_flex_funded")


if __name__ == "__main__":
    unittest.main()
