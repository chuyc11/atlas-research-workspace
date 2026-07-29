from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "drift_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("drift_diagnostics_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DriftDiagnosticsTests(unittest.TestCase):
    def test_calibration_windows_count_rolling_family_once(self) -> None:
        metrics = MODULE.sample_metrics([
            {"prediction_id": "P1", "event_family_id": "FAMILY", "probability": 0.8, "observed_outcome": 1, "brier": 0.04},
            {"prediction_id": "P2", "event_family_id": "FAMILY", "probability": 0.6, "observed_outcome": 1, "brier": 0.16},
        ])

        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["raw_observation_count"], 2)
        self.assertAlmostEqual(metrics["brier_score"], 0.1)

    def test_source_family_counts_deduplicate_links_within_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = root / "outputs"
            data = root / "data"
            outputs.mkdir()
            data.mkdir()
            report = outputs / "每日全球晨间简报-2026-07-13.md"
            report.write_text(
                "[AP](https://apnews.com/article/one) [AP again](https://apnews.com/article/one?x=1) "
                "[BBC](https://www.bbc.co.uk/news/articles/two)",
                encoding="utf-8",
            )
            old_output, old_data = MODULE.OUTPUT_DIR, MODULE.DATA_DIR
            MODULE.OUTPUT_DIR, MODULE.DATA_DIR = outputs, data
            try:
                result, _paths = MODULE.source_diagnostics(
                    date(2026, 7, 13),
                    {
                        "lookback_days": 7,
                        "minimum_history_days": 1,
                        "source_family_aliases": {"apnews.com": "AP", "bbc.co.uk": "BBC"},
                        "primary_domain_suffixes": [],
                    },
                    {"sources": []},
                )
            finally:
                MODULE.OUTPUT_DIR, MODULE.DATA_DIR = old_output, old_data

        self.assertEqual(result["citation_instances_after_daily_dedup"], 2)
        self.assertEqual(result["distinct_source_families"], 2)
        self.assertAlmostEqual(result["source_family_hhi"], 0.5)

    def test_future_reviews_are_excluded_and_small_calibration_sample_is_not_judged(self) -> None:
        records = [
            {"prediction_id": "P1", "date": "2026-07-01", "deadline": "2026-07-02", "schema_version": 2, "status": "open", "probability": 0.7},
            {"prediction_id": "P1", "date": "2026-07-20", "status": "validated", "review": {"review_date": "2026-07-20", "resolution_scope": "event", "observed_outcome": 1}},
        ]

        point = MODULE.point_in_time_records(records, date(2026, 7, 13))
        result = MODULE.forecast_diagnostics(date(2026, 7, 13), {"lookback_days": 7, "minimum_event_samples": 2}, records)

        self.assertEqual(len(point), 1)
        self.assertEqual(result["status"], "insufficient_sample")
        self.assertEqual(result["recent_window"]["sample_count"], 0)

    def test_theme_and_symbol_counts_are_deduplicated_per_prediction(self) -> None:
        records = [{
            "prediction_id": "P1",
            "date": "2026-07-13",
            "scenario": "AI AI chip strength",
            "drivers": ["semiconductor capex"],
            "market_mapping": [
                {"symbol": "SMH", "direction": "outperform"},
                {"symbol": "SMH", "direction": "outperform"},
            ],
        }]
        result = MODULE.theme_diagnostics(
            date(2026, 7, 13),
            {
                "lookback_days": 7,
                "minimum_theme_predictions": 1,
                "theme_taxonomy": {"ai_semiconductors": ["ai", "chip", "semiconductor"]},
            },
            records,
        )

        self.assertEqual(result["theme_counts"], {"ai_semiconductors": 1})
        self.assertEqual(result["symbol_counts"], {"SMH": 1})
        self.assertEqual(result["directional_exposure_counts"], {"SMH|outperform": 1})
        self.assertFalse(MODULE.keyword_matches("daily market update", "ai"))
        self.assertTrue(MODULE.keyword_matches("AI market update", "ai"))

    def test_paper_accounts_remain_separate_and_theme_gap_is_visible(self) -> None:
        attribution = {
            "accounts": [
                {
                    "account": "US",
                    "latest_valuation": {"equity": 100, "cash": 50, "base_currency": "USD"},
                    "positions": [{"symbol": "AAA", "market_value": 50, "last_price_date": "2026-07-13", "prediction_id": "P1", "scenario": "AI"}],
                },
                {
                    "account": "CHINA",
                    "latest_valuation": {"equity": 200, "cash": 100, "base_currency": "CNY"},
                    "positions": [{"symbol": "510300.SH", "market_value": 100, "last_price_date": "2026-07-13", "prediction_id": "P2", "scenario": "中国宽基"}],
                },
            ]
        }
        result = MODULE.paper_account_diagnostics(
            attribution,
            {"max_position_pct": 0.6, "min_cash_pct": 0.02},
            date(2026, 7, 13),
            {"theme_taxonomy": {"ai": ["ai"], "china": ["中国"]}},
        )

        self.assertTrue(result["accounts_are_never_aggregated"])
        self.assertEqual({row["base_currency"] for row in result["accounts"]}, {"USD", "CNY"})
        self.assertNotIn("combined_equity", result)
        self.assertTrue(all(row["explicit_theme_attribution_coverage_pct"] == 0 for row in result["accounts"]))

    def test_unlinked_prediction_exposure_escalates_the_affected_account_only(self) -> None:
        attribution = {
            "accounts": [
                {
                    "account": "US",
                    "latest_valuation": {"equity": 100, "cash": 60, "base_currency": "USD"},
                    "positions": [
                        {
                            "symbol": "LEGACY",
                            "market_value": 40,
                            "last_price_date": "2026-07-13",
                            "prediction_id": "unlinked",
                            "prediction_lineage_status": "legacy_unlinked",
                            "prediction_ids": ["P-LATER"],
                            "unlinked_buy_count": 1,
                            "scenario": "legacy position",
                        }
                    ],
                },
                {
                    "account": "CHINA",
                    "latest_valuation": {"equity": 100, "cash": 60, "base_currency": "CNY"},
                    "positions": [
                        {
                            "symbol": "510300.SH",
                            "market_value": 40,
                            "last_price_date": "2026-07-13",
                            "prediction_id": "P-CHINA",
                            "prediction_lineage_status": "linked",
                            "prediction_ids": ["P-CHINA"],
                            "scenario": "china broad market",
                        }
                    ],
                },
            ]
        }
        result = MODULE.paper_account_diagnostics(
            attribution,
            {"max_position_pct": 0.6, "min_cash_pct": 0.02},
            date(2026, 7, 13),
            {
                "theme_taxonomy": {},
                "paper_unlinked_prediction_value_watch_pct": 5.0,
                "paper_unlinked_prediction_value_alert_pct": 15.0,
            },
        )
        us = next(row for row in result["accounts"] if row["account"] == "US")
        china = next(row for row in result["accounts"] if row["account"] == "CHINA")

        self.assertEqual(us["unlinked_prediction_value_pct"], 100.0)
        self.assertEqual(us["status"], "alert")
        self.assertTrue(any("prediction lineage" in signal for signal in us["signals"]))
        self.assertEqual(china["unlinked_prediction_value_pct"], 0.0)
        self.assertNotIn("prediction lineage", " ".join(china["signals"]))


if __name__ == "__main__":
    unittest.main()
