from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resolution_evidence.py"
SPEC = importlib.util.spec_from_file_location("resolution_evidence_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def mapping() -> dict:
    return {
        "symbol": "TEST",
        "benchmark": "SPY",
        "direction": "outperform",
        "evaluation_deadline": "2026-07-13",
        "verification_rule": "TEST outperforms SPY",
        "evaluation": {
            "metric": "total_return",
            "window_start": "2026-07-13",
            "price_field": "adjusted_close",
            "comparison": "symbol_gt_benchmark",
        },
    }


class ResolutionEvidenceTests(unittest.TestCase):
    def test_market_candidate_uses_comparable_pre_registered_sessions(self) -> None:
        series = {
            "TEST": {
                "source": "Market",
                "source_url": "https://market.example/test",
                "points": [
                    {"date": "2026-07-10", "adjusted_close": 100.0},
                    {"date": "2026-07-13", "adjusted_close": 103.0},
                ],
            },
            "SPY": {
                "source": "Market",
                "source_url": "https://market.example/spy",
                "points": [
                    {"date": "2026-07-10", "adjusted_close": 200.0},
                    {"date": "2026-07-13", "adjusted_close": 202.0},
                ],
            },
        }

        result = MODULE.evaluate_market_mapping("2026-07-12-P01", mapping(), series)

        self.assertEqual(result["status"], "resolved_candidate")
        self.assertEqual(result["observed_outcome"], 1)
        self.assertAlmostEqual(result["symbol_return_pct"], 3.0)
        self.assertAlmostEqual(result["benchmark_return_pct"], 1.0)
        self.assertFalse(result["ledger_mutation_allowed"])
        self.assertTrue(result["requires_source_verification"])
        fragment = result["ledger_fragment_after_verification"]
        self.assertEqual(fragment["price_field"], "adjusted_close")
        self.assertEqual(fragment["symbol_start_price"], 100.0)
        self.assertEqual(fragment["symbol_end_price"], 103.0)
        self.assertEqual(fragment["benchmark_start_price"], 200.0)
        self.assertEqual(fragment["benchmark_end_price"], 202.0)

    def test_missing_machine_contract_blocks_post_hoc_rule_inference(self) -> None:
        item = mapping()
        item.pop("evaluation")

        result = MODULE.evaluate_market_mapping("2026-07-12-P01", item, {})

        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_machine_evaluation_contract", result["blockers"])

    def test_stale_deadline_close_cannot_resolve_mapping(self) -> None:
        series = {
            symbol: {
                "source": "Market",
                "source_url": f"https://market.example/{symbol}",
                "points": [
                    {"date": "2026-07-10", "adjusted_close": 100.0},
                    {"date": "2026-07-12", "adjusted_close": 101.0},
                ],
            }
            for symbol in ("TEST", "SPY")
        }

        result = MODULE.evaluate_market_mapping("2026-07-12-P01", mapping(), series)

        self.assertEqual(result["status"], "blocked")
        self.assertIn("deadline_close_missing_or_stale", result["blockers"])


if __name__ == "__main__":
    unittest.main()
