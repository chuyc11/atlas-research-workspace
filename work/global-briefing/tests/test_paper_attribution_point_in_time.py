from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evolution.py"
SPEC = importlib.util.spec_from_file_location("evolution_attribution_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PaperAttributionPointInTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.originals = {"ROOT": MODULE.ROOT, "PAPER_CONFIG_PATH": MODULE.PAPER_CONFIG_PATH}
        MODULE.ROOT = self.root
        MODULE.PAPER_CONFIG_PATH = self.root / "paper_trading.json"
        config = {
            "initial_cash": 100000,
            "base_unit": "points",
            "accounts": {
                "US": {
                    "account_id": "us-test",
                    "initial_cash": 100000,
                    "base_currency": "USD",
                    "fx_rates_to_base": {"USD": 1},
                    "portfolio_file": "data/us-portfolio.json",
                    "trades_file": "data/us-trades.jsonl",
                    "valuations_file": "data/us-valuations.jsonl",
                },
                "CHINA": {
                    "account_id": "china-test",
                    "initial_cash": 100000,
                    "base_currency": "CNY",
                    "fx_rates_to_base": {"CNY": 1, "HKD": 0.92},
                    "portfolio_file": "data/china-portfolio.json",
                    "trades_file": "data/china-trades.jsonl",
                    "valuations_file": "data/china-valuations.jsonl",
                },
            },
        }
        MODULE.PAPER_CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "data" / "us-trades.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in [
                {"date": "2026-06-12", "action": "BUY", "symbol": "OLD", "exchange": "NASDAQ", "currency": "USD", "quantity": 10, "price": 100, "gross_value": 1000, "fee": 0, "prediction_id": "P-OLD"},
                {"date": "2026-06-16", "action": "BUY", "symbol": "FUTURE", "exchange": "NASDAQ", "currency": "USD", "quantity": 10, "price": 50, "gross_value": 500, "fee": 0, "prediction_id": "P-FUTURE"},
            ]),
            encoding="utf-8",
        )
        (self.root / "data" / "china-trades.jsonl").write_text(
            json.dumps({"date": "2026-06-12", "action": "BUY", "symbol": "3033.HK", "exchange": "HK", "currency": "HKD", "quantity": 1000, "price": 5, "gross_value": 5000, "fee": 0, "prediction_id": "P-HK"}) + "\n",
            encoding="utf-8",
        )
        for name in ("us-valuations.jsonl", "china-valuations.jsonl"):
            (self.root / "data" / name).write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(MODULE, name, value)
        self.temporary.cleanup()

    def test_historical_attribution_excludes_future_positions_and_keeps_account_currencies_separate(self) -> None:
        result = MODULE.paper_attribution("day", "2026-06-14")
        us = next(item for item in result["accounts"] if item["account"] == "US")
        china = next(item for item in result["accounts"] if item["account"] == "CHINA")

        self.assertEqual([item["symbol"] for item in us["positions"]], ["OLD"])
        self.assertEqual(us["latest_valuation"]["valuation_source"], "reconstructed_point_in_time_from_trades")
        self.assertAlmostEqual(china["positions"][0]["market_value"], 4600.0)
        self.assertEqual(china["positions"][0]["base_currency"], "CNY")
        self.assertEqual({item["account"] for item in result["by_prediction"]}, {"US", "CHINA"})


if __name__ == "__main__":
    unittest.main()
