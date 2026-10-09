"""Tests para el modulo de configuracion de reglas."""
import tempfile
import unittest
from pathlib import Path
from src.gate.config import load_config, GateConfig

FUNDED_YAML = """
presets:
  "25k_flex_funded":
    tier: "25K"
    phase: "funded"
    initial_balance: 25000.0
    daily_loss_limit: 600.0
    max_loss_limit: 1000.0
    min_account_balance: 25100.0
    max_contracts_mini: 2
    max_contracts_micro: 20
    min_profit_day_amount: 100.0
    buffer: 100.0
    payout: 500.0
accounts:
  "Funded_01": "25k_flex_funded"
"""


class TestGateConfig(unittest.TestCase):
    def setUp(self):
        self.config_path = Path("config/lucid_rules.yaml")

    def test_load_default_config(self):
        config = load_config(self.config_path)
        self.assertIsInstance(config, GateConfig)
        self.assertEqual(config.server.host, "127.0.0.1")
        self.assertEqual(config.server.port, 8765)
        self.assertEqual(config.server.timeout_ms, 200)
        self.assertEqual(config.server.heartbeat_interval_seconds, 5)
        self.assertEqual(config.session.mode, "24h_with_break")
        # Break diario oficial del CME: 16:00-17:00 hora de Chicago (17:00-18:00 ET)
        self.assertEqual(config.session.timezone, "America/Chicago")
        self.assertEqual(config.session.daily_break_start, "16:00")
        self.assertEqual(config.session.daily_break_end, "17:00")

    def test_log_timezone_defaults_to_chicago_and_replaces_utc(self):
        config = load_config(self.config_path)
        self.assertEqual(config.server.log_timezone, "America/Chicago")

    def test_session_time_fields_are_renamed_and_expressed_in_chicago_time(self):
        config = load_config(self.config_path)
        for preset in config.presets.values():
            self.assertFalse(hasattr(preset, "session_start_time_et"))
            self.assertFalse(hasattr(preset, "session_flatten_time_et"))
            # 09:30 ET / 15:55 ET -> 08:30 CT / 14:55 CT
            self.assertEqual(preset.session_start_time, "08:30")
            self.assertEqual(preset.session_flatten_time, "14:55")

    def test_funded_presets_match_rules_workbook_payout_values(self):
        # Rules/Lucid.xlsx: Buffer, Payout y Balance for Payout de las columnas FLEX FUNDED
        expected = {
            "25k_flex_funded": (1100.0, 1000.0, 27100.0),
            "50k_flex_funded": (2100.0, 2000.0, 54100.0),
            "100k_flex_funded": (3100.0, 3000.0, 106100.0),
        }
        config = load_config(self.config_path)
        for name, (buffer, payout, balance_for_payout) in expected.items():
            preset = config.presets[name]
            self.assertEqual((preset.buffer, preset.payout), (buffer, payout))
            self.assertEqual(preset.balance_for_payout, balance_for_payout)

    def test_balance_for_payout_is_derived_from_initial_buffer_and_payout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "funded.yaml"
            path.write_text(FUNDED_YAML, encoding="utf-8")
            preset = load_config(path).presets["25k_flex_funded"]
        self.assertEqual(preset.buffer, 100.0)
        self.assertEqual(preset.payout, 500.0)
        self.assertEqual(preset.balance_for_payout, 25000.0 + 100.0 + 500.0)

    def test_balance_for_payout_is_none_when_buffer_or_payout_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "funded.yaml"
            path.write_text(FUNDED_YAML.replace("    payout: 500.0\n", ""), encoding="utf-8")
            preset = load_config(path).presets["25k_flex_funded"]
        self.assertIsNone(preset.payout)
        self.assertIsNone(preset.balance_for_payout)

    def test_instruments_nq_mnq(self):
        config = load_config(self.config_path)
        self.assertIn("NQ", config.instruments)
        self.assertIn("MNQ", config.instruments)
        self.assertEqual(config.instruments["NQ"].point_value, 20.0)
        self.assertEqual(config.instruments["MNQ"].point_value, 2.0)

    def test_presets_specs(self):
        config = load_config(self.config_path)
        self.assertIn("25k_flex_eval", config.presets)
        self.assertIn("50k_flex_eval", config.presets)
        
        p25 = config.presets["25k_flex_eval"]
        self.assertEqual(p25.initial_balance, 25000.0)
        self.assertEqual(p25.max_loss_limit, 1000.0)
        self.assertEqual(p25.daily_loss_limit, 600.0)
        self.assertEqual(p25.max_contracts_mini, 2)
        self.assertEqual(p25.max_contracts_micro, 20)
        self.assertEqual(p25.drawdown_type, "EOD")
        self.assertEqual(p25.consistency_cap_pct, 0.50)

        p50 = config.presets["50k_flex_eval"]
        self.assertEqual(p50.initial_balance, 50000.0)
        self.assertEqual(p50.max_loss_limit, 2000.0)
        self.assertEqual(p50.daily_loss_limit, 1200.0)
        self.assertEqual(p50.max_contracts_mini, 4)
        self.assertEqual(p50.max_contracts_micro, 40)

    def test_account_mapping(self):
        config = load_config(self.config_path)
        preset = config.get_preset_for_account("Sim101")
        self.assertIsNotNone(preset)
        self.assertEqual(preset.tier, "25K")

        unknown = config.get_preset_for_account("UnknownAccount")
        self.assertIsNone(unknown)


if __name__ == "__main__":
    unittest.main()
