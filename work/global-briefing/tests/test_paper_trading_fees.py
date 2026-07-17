from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_trading.py"
SPEC = importlib.util.spec_from_file_location("paper_trading_fee_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PaperTradingFeeTests(unittest.TestCase):
    def test_a_share_fee_matches_trading_core_broker_rules(self) -> None:
        rules = {
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_tax_sell_rate": 0.0005,
        }
        buy_fee, buy_breakdown = MODULE.calculate_fee("BUY", 10_000.0, rules)
        sell_fee, sell_breakdown = MODULE.calculate_fee("SELL", 10_000.0, rules)

        self.assertEqual(buy_fee, 5.0)
        self.assertEqual(buy_breakdown, {"commission": 5.0, "stamp_tax": 0.0})
        self.assertEqual(sell_fee, 10.0)
        self.assertEqual(sell_breakdown, {"commission": 5.0, "stamp_tax": 5.0})

    def test_us_fee_keeps_zero_cost_default(self) -> None:
        fee, breakdown = MODULE.calculate_fee("BUY", 10_000.0, {}, 0.0)
        self.assertEqual(fee, 0.0)
        self.assertEqual(breakdown, {"commission": 0.0, "stamp_tax": 0.0})

    def test_fee_model_rejects_non_finite_or_negative_fee_and_tax_parameters(self) -> None:
        with self.assertRaisesRegex(ValueError, "gross notional must be finite"):
            MODULE.calculate_fee("BUY", float("nan"), {})
        with self.assertRaisesRegex(ValueError, "commission_rate must be non-negative"):
            MODULE.calculate_fee("BUY", 10_000.0, {"commission_rate": -0.01})
        with self.assertRaisesRegex(ValueError, "min_commission must be non-negative"):
            MODULE.calculate_fee(
                "BUY",
                10_000.0,
                {"commission_rate": 0.001, "min_commission": -1},
            )
        with self.assertRaisesRegex(ValueError, "stamp_tax_sell_rate must be non-negative"):
            MODULE.calculate_fee(
                "SELL",
                10_000.0,
                {"stamp_tax_sell_rate": -0.001},
            )
