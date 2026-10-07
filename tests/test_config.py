"""Tests para el modulo de configuracion de reglas."""
import unittest
from pathlib import Path
from src.gate.config import load_config, GateConfig


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
