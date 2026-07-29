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
            "theme_registry_file": "paper_theme_registry.json",
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
        (self.root / "paper_theme_registry.json").write_text(json.dumps({
            "schema_version": 1,
            "allowed_themes": ["test_theme", "china_technology"],
            "entries": [
                {"account": "US", "symbol": "OLD", "exchange": "NASDAQ", "primary_theme": "test_theme", "secondary_themes": [], "status": "verified", "effective_from": "2026-06-01", "evidence": ["test"]},
                {"account": "CHINA", "symbol": "3033.HK", "exchange": "HK", "primary_theme": "china_technology", "secondary_themes": [], "status": "verified", "effective_from": "2026-06-01", "evidence": ["test"]},
            ],
        }), encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "data" / "us-trades.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in [
                {"date": "2026-06-12", "price_date": "2026-06-11", "action": "BUY", "symbol": "OLD", "exchange": "NASDAQ", "currency": "USD", "quantity": 10, "price": 100, "gross_value": 1000, "fee": 0, "prediction_id": "P-OLD"},
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
        self.assertEqual(us["positions"][0]["last_price_date"], "2026-06-11")
        self.assertEqual(us["positions"][0]["last_price_date_provenance"], "explicit_ledger_field")
        self.assertEqual(us["positions"][0]["theme"], "test_theme")
        self.assertEqual(us["positions"][0]["theme_source"], "verified_registry")
        self.assertAlmostEqual(china["positions"][0]["market_value"], 4600.0)
        self.assertEqual(china["positions"][0]["base_currency"], "CNY")
        self.assertEqual({item["account"] for item in result["by_prediction"]}, {"US", "CHINA"})

    def test_daily_attribution_uses_previous_valuation_and_reports_period_not_lifetime_pnl(self) -> None:
        valuations = [
            {
                "date": "2026-06-13",
                "price_snapshot": [{"key": "NASDAQ:OLD", "price": 100, "price_date": "2026-06-13", "currency": "USD", "fx_to_base": 1}],
            },
            {
                "date": "2026-06-14",
                "price_snapshot": [{"key": "NASDAQ:OLD", "price": 110, "price_date": "2026-06-14", "currency": "USD", "fx_to_base": 1}],
            },
        ]
        (self.root / "data" / "us-valuations.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in valuations), encoding="utf-8"
        )

        result = MODULE.paper_attribution("day", "2026-06-14")
        us = next(item for item in result["accounts"] if item["account"] == "US")
        prediction = next(item for item in result["by_prediction"] if item["account"] == "US")

        self.assertEqual(us["attribution_window"]["start_exclusive"], "2026-06-13")
        self.assertEqual(us["attribution_window"]["basis"], "previous_recorded_valuation")
        self.assertAlmostEqual(us["period_performance"]["period_pnl"], 100.0)
        self.assertAlmostEqual(us["period_performance"]["reconciliation_difference"], 0.0)
        self.assertAlmostEqual(prediction["period_pnl"], 100.0)
        self.assertAlmostEqual(prediction["unrealized_pnl"], 100.0)

    def test_legacy_account_equity_difference_is_exposed_not_hidden_in_position_attribution(self) -> None:
        valuations = [
            {"date": "2026-06-13", "equity": 99950.0},
            {
                "date": "2026-06-14",
                "equity": 100100.0,
                "price_snapshot": [{"key": "NASDAQ:OLD", "price": 110, "price_date": "2026-06-14", "currency": "USD", "fx_to_base": 1}],
            },
        ]
        (self.root / "data" / "us-valuations.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in valuations), encoding="utf-8"
        )

        result = MODULE.paper_attribution("day", "2026-06-14")
        us = next(item for item in result["accounts"] if item["account"] == "US")

        self.assertAlmostEqual(us["period_performance"]["period_pnl"], 150.0)
        self.assertAlmostEqual(us["period_performance"]["attributed_period_pnl"], 100.0)
        self.assertAlmostEqual(us["period_performance"]["reconciliation_difference"], 50.0)
        self.assertEqual(us["period_performance"]["reconciliation_status"], "legacy_position_mark_gap")

    def test_open_position_retains_all_buy_prediction_lineage(self) -> None:
        trades_path = self.root / "data" / "us-trades.jsonl"
        with trades_path.open("a", encoding="utf-8") as handle:
            for row in [
                {"date": "2026-06-12", "action": "BUY", "symbol": "LEGACY", "exchange": "NASDAQ", "currency": "USD", "quantity": 10, "price": 20, "gross_value": 200, "fee": 0},
                {"date": "2026-06-13", "action": "BUY", "symbol": "LEGACY", "exchange": "NASDAQ", "currency": "USD", "quantity": 5, "price": 22, "gross_value": 110, "fee": 0, "prediction_id": "P-LATER"},
                {"date": "2026-06-12", "action": "BUY", "symbol": "MIXED", "exchange": "NASDAQ", "currency": "USD", "quantity": 5, "price": 10, "gross_value": 50, "fee": 0, "prediction_id": "P-EARLY"},
                {"date": "2026-06-13", "action": "BUY", "symbol": "MIXED", "exchange": "NASDAQ", "currency": "USD", "quantity": 5, "price": 12, "gross_value": 60, "fee": 0, "prediction_id": "P-LATER"},
            ]:
                handle.write(json.dumps(row) + "\n")

        result = MODULE.paper_attribution("day", "2026-06-14")
        us = next(item for item in result["accounts"] if item["account"] == "US")
        legacy = next(item for item in us["positions"] if item["symbol"] == "LEGACY")
        mixed = next(item for item in us["positions"] if item["symbol"] == "MIXED")

        self.assertEqual(legacy["prediction_id"], "unlinked")
        self.assertEqual(legacy["prediction_ids"], ["P-LATER"])
        self.assertEqual(legacy["unlinked_buy_count"], 1)
        self.assertEqual(legacy["prediction_lineage_status"], "legacy_unlinked")
        self.assertEqual(mixed["prediction_ids"], ["P-EARLY", "P-LATER"])
        self.assertEqual(mixed["prediction_lineage_status"], "requires_review")


if __name__ == "__main__":
    unittest.main()
