"""Tests de las formulas puras de reglas (src/gate/rules.py)."""
import unittest

from src.gate.config import load_config
from src.gate.rules import eval_daily_profit_cap


class TestEvalDailyProfitCap(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config/lucid_rules.yaml")
        self.eval25 = self.config.presets["25k_flex_eval"]   # target 1.250, consistencia 50%
        self.eval50 = self.config.presets["50k_flex_eval"]   # target 3.000

    def test_without_accumulated_profit_cap_is_the_safe_target_floor(self):
        # Con 0 de profit acumulado, "50% del acumulado" bloquearia todo: se usa el piso c x target
        self.assertEqual(eval_daily_profit_cap(self.eval25, 0.0), 625.0)
        self.assertEqual(eval_daily_profit_cap(self.eval50, 0.0), 1500.0)

    def test_cap_grows_with_accumulated_profit(self):
        # c/(1-c) = 1 con c = 50%: hoy puede igualar lo acumulado y seguir en 50% del total
        self.assertEqual(eval_daily_profit_cap(self.eval25, 900.0), 900.0)
        self.assertEqual(eval_daily_profit_cap(self.eval25, 1000.0), 1000.0)

    def test_floor_applies_while_accumulated_is_below_it(self):
        self.assertEqual(eval_daily_profit_cap(self.eval25, 600.0), 625.0)

    def test_negative_accumulated_profit_is_treated_as_zero(self):
        self.assertEqual(eval_daily_profit_cap(self.eval25, -400.0), 625.0)

    def test_cap_math_matches_the_consistency_definition(self):
        # Si hoy gano exactamente el cap y es el mejor dia, su peso en el total es <= c
        prev = 1000.0
        cap = eval_daily_profit_cap(self.eval25, prev)
        self.assertLessEqual(cap / (prev + cap), self.eval25.consistency_cap_pct + 1e-9)

    def test_non_eval_presets_have_no_cap(self):
        for name in ("25k_flex_funded", "25k_flex_payout"):
            self.assertIsNone(eval_daily_profit_cap(self.config.presets[name], 5000.0))


if __name__ == "__main__":
    unittest.main()
