from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_quality.py"
SPEC = importlib.util.spec_from_file_location("research_quality_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

STORE_SPEC = importlib.util.spec_from_file_location(
    "briefing_store_contract_test_module",
    SCRIPT.parent / "briefing_store.py",
)
assert STORE_SPEC and STORE_SPEC.loader
STORE = importlib.util.module_from_spec(STORE_SPEC)
sys.modules[STORE_SPEC.name] = STORE
STORE_SPEC.loader.exec_module(STORE)


def v2_prediction(prediction_id: str = "2026-07-12-P01", probability: float = 0.8) -> dict:
    return {
        "schema_version": 2,
        "prediction_id": prediction_id,
        "date": "2026-07-12",
        "deadline": "2026-07-13",
        "horizon": "1d",
        "scenario": "A precisely resolvable event occurs",
        "probability": probability,
        "status": "open",
        "trigger": "An official release",
        "verification_signals": ["official release"],
        "falsification_signals": ["official denial"],
        "resolution": {
            "question": "Did the event occur by the deadline?",
            "success_criteria": "The official release confirms it.",
            "failure_criteria": "No confirming release exists by the deadline.",
        },
        "evidence": [{"source": "Official", "url": "https://official.example/source"}],
        "tickers": [{"symbol": "TEST"}],
        "market_mapping": [
            {
                "symbol": "TEST",
                "direction": "outperform",
                "benchmark": "SPY",
                "verification_rule": "TEST return exceeds SPY return by the deadline",
                "evaluation_deadline": "2026-07-13",
            }
        ],
    }


def resolved_review(prediction_id: str = "2026-07-12-P01", outcome: int = 1) -> dict:
    return {
        "prediction_id": prediction_id,
        "date": "2026-07-14",
        "status": "validated" if outcome else "wrong",
        "review": {
            "review_date": "2026-07-14",
            "observed_outcome": outcome,
            "evidence": [{"source": "Official", "url": "https://official.example/result"}],
        },
    }


def market_review(
    prediction_id: str = "2026-07-12-P01",
    deadline: str = "2026-07-13",
    review_date: str = "2026-07-14",
) -> dict:
    return {
        "prediction_id": prediction_id,
        "date": review_date,
        "status": "active",
        "review": {
            "resolution_scope": "market",
            "review_date": review_date,
            "market_resolution": [{
                "symbol": "TEST",
                "benchmark": "SPY",
                "evaluation_deadline": deadline,
                "status": "resolved",
                "observed_outcome": 1,
                "symbol_return_pct": 2.0,
                "benchmark_return_pct": 1.0,
                "excess_return_pct": 1.0,
                "start_price_date": "2026-07-10",
                "end_price_date": deadline,
                "evidence": [{"source": "Official market data", "url": "https://market.example/result"}],
            }],
        },
    }


class ResearchQualityTests(unittest.TestCase):
    def test_v2_contract_accepts_pre_registered_numeric_forecast(self) -> None:
        self.assertEqual(MODULE.validate_v2_prediction(v2_prediction()), [])

    def test_v2_contract_rejects_categorical_probability_and_missing_resolution(self) -> None:
        row = v2_prediction()
        row["probability"] = "high"
        row.pop("resolution")

        errors = MODULE.validate_v2_prediction(row)

        self.assertTrue(any("probability must be numeric" in item for item in errors))
        self.assertTrue(any("resolution object is required" in item for item in errors))

    def test_v2_contract_requires_complete_session_aligned_market_mapping(self) -> None:
        row = v2_prediction()
        row["tickers"].append({"symbol": "MISSING"})
        row["market_mapping"][0]["direction"] = "strong_buy"
        row["market_mapping"][0].pop("evaluation_deadline")

        errors = MODULE.validate_v2_prediction(row)

        self.assertTrue(any("direction is invalid" in item for item in errors))
        self.assertTrue(any("evaluation_deadline" in item for item in errors))
        self.assertTrue(any("ticker MISSING has no market_mapping" in item for item in errors))

    def test_machine_market_evaluation_is_required_only_after_its_enforcement_date(self) -> None:
        row = v2_prediction()
        errors = MODULE.validate_v2_prediction(
            row,
            machine_evaluation_enforce_from_date="2026-07-12",
        )
        row["market_mapping"][0]["evaluation"] = {
            "metric": "total_return",
            "window_start": "2026-07-13",
            "price_field": "adjusted_close",
            "comparison": "symbol_gt_benchmark",
        }

        self.assertTrue(any("evaluation is required" in item for item in errors))
        self.assertEqual(
            MODULE.validate_v2_prediction(row, machine_evaluation_enforce_from_date="2026-07-12"),
            [],
        )
        row["market_mapping"][0]["evaluation"]["comparison"] = "symbol_lt_benchmark"
        mismatch = MODULE.validate_v2_prediction(row, machine_evaluation_enforce_from_date="2026-07-12")
        self.assertTrue(any("conflicts with direction" in item for item in mismatch))

    def test_proper_scoring_excludes_legacy_labels_and_scores_resolved_numeric_predictions(self) -> None:
        rows = [
            v2_prediction(probability=0.9),
            resolved_review(),
            {
                "prediction_id": "2026-07-12-P02",
                "date": "2026-07-12",
                "horizon": "1d",
                "probability": "high",
                "status": "open",
            },
            {
                "prediction_id": "2026-07-12-P02",
                "date": "2026-07-13",
                "status": "validated",
                "review": {
                    "review_date": "2026-07-14",
                    "observed_outcome": 1,
                    "evidence": [{"url": "https://example.test/result"}],
                },
            },
        ]

        metrics = MODULE.proper_scoring_metrics(rows, cutoff="2026-07-14", minimum_sample=1)

        self.assertEqual(metrics["eligible_sample_count"], 1)
        self.assertEqual(metrics["brier_score"], 0.01)
        self.assertTrue(metrics["is_research_ready"])
        self.assertTrue(metrics["gates"]["resolved_coverage_at_least_80pct"])
        self.assertEqual(metrics["legacy_matured_prediction_count_excluded"], 1)

    def test_asset_only_prediction_is_excluded_from_event_calibration(self) -> None:
        original = v2_prediction()
        original["scenario"] = "TEST total return exceeds SPY total return by the deadline"
        original["resolution"] = {
            "question": "Did TEST total return exceed SPY total return by the deadline?",
            "success_criteria": "TEST return is greater than SPY return.",
            "failure_criteria": "TEST return is not greater than SPY return.",
        }

        metrics = MODULE.proper_scoring_metrics(
            [original, resolved_review()],
            cutoff="2026-07-14",
            minimum_sample=1,
        )

        self.assertEqual(metrics["matured_prediction_count"], 0)
        self.assertEqual(metrics["asset_only_matured_prediction_count_excluded"], 1)
        self.assertEqual(metrics["eligible_sample_count"], 0)
        self.assertEqual(metrics["exclusion_counts"]["asset_only_prediction"], 1)

    def test_asset_only_event_contract_is_rejected_after_separation_enforcement(self) -> None:
        row = v2_prediction("2026-07-15-P01")
        row["date"] = "2026-07-15"
        row["deadline"] = "2026-07-16"
        row["market_mapping"][0]["evaluation_deadline"] = "2026-07-16"
        row["scenario"] = "TEST return will outperform SPY return"
        row["resolution"] = {
            "question": "Did TEST return outperform SPY return?",
            "success_criteria": "TEST return exceeds SPY return.",
            "failure_criteria": "TEST return does not exceed SPY return.",
        }

        errors = MODULE.validate_v2_prediction(
            row,
            event_asset_separation_enforce_from_date="2026-07-15",
        )

        self.assertTrue(any("event resolution duplicates a market-mapping outcome" in error for error in errors))

    def test_early_review_without_terminal_evidence_is_ineligible(self) -> None:
        original = v2_prediction()
        original["deadline"] = "2026-07-19"
        original["horizon"] = "1w"
        review = resolved_review()

        metrics = MODULE.proper_scoring_metrics([original, review], cutoff="2026-07-20", minimum_sample=1)

        self.assertEqual(metrics["eligible_sample_count"], 0)
        self.assertEqual(metrics["exclusion_counts"]["early_closure_without_terminal_evidence"], 1)

    def test_report_gate_checks_sources_sections_and_thesis_layers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            layers = "\n".join(
                "- **结论：** x\n- **硬证据：** x\n- **机制：** x\n- **反证：** x\n- **证伪：** x"
                for _ in range(3)
            )
            path.write_text(
                "# 核心摘要\n## 昨日预测复盘\n## 今日预测与市场映射\n## 风险信号\n## 来源与质量\n"
                + layers
                + "\n[A](https://a.example/x) [B](https://b.example/y)",
                encoding="utf-8",
            )
            result = MODULE.audit_report(
                path,
                {
                    "minimum_report_characters": 10,
                    "maximum_report_characters": 10000,
                    "minimum_distinct_links": 2,
                    "minimum_distinct_domains": 2,
                },
                enforce=True,
            )

        self.assertTrue(result["passed"])
        self.assertEqual(result["distinct_domain_count"], 2)
        self.assertEqual(result["thesis_layer_counts"]["falsification_signal"], 3)

    def test_storage_rejects_noncompliant_post_enforcement_prediction_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text("", encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}),
                encoding="utf-8",
            )
            invalid = v2_prediction()
            invalid["probability"] = "high"
            input_path = root / "input.json"
            input_path.write_text(json.dumps(invalid), encoding="utf-8")
            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                with self.assertRaisesRegex(ValueError, "probability must be numeric"):
                    STORE.append_prediction_records(input_path, "2026-07-12", predictions)

            self.assertEqual(predictions.read_text(encoding="utf-8"), "")

    def test_storage_allows_legacy_review_after_v2_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text(
                json.dumps(
                    {
                        "prediction_id": "2026-07-11-P01",
                        "date": "2026-07-11",
                        "horizon": "1d",
                        "probability": "high",
                        "status": "open",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}),
                encoding="utf-8",
            )
            review = {
                "prediction_id": "2026-07-11-P01",
                "date": "2026-07-12",
                "status": "partial",
                "review": {"review_date": "2026-07-12", "why": "legacy review"},
            }
            input_path = root / "review.json"
            input_path.write_text(json.dumps(review), encoding="utf-8")
            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                count = STORE.append_prediction_records(input_path, "2026-07-12", predictions)

            self.assertEqual(count, 1)
            self.assertEqual(len(predictions.read_text(encoding="utf-8").splitlines()), 2)

    def test_due_review_queue_scans_full_ledger_and_does_not_close_same_day_early(self) -> None:
        overdue = v2_prediction("2026-07-11-P01")
        overdue["date"] = "2026-07-11"
        overdue["deadline"] = "2026-07-12"
        overdue["market_mapping"][0]["evaluation_deadline"] = "2026-07-12"
        matures_today = v2_prediction("2026-07-12-P01")
        future = v2_prediction("2026-07-12-P02")
        future["deadline"] = "2026-07-20"
        future["market_mapping"][0]["evaluation_deadline"] = "2026-07-20"

        queue = STORE.due_review_queue("2026-07-13", [overdue, matures_today, future])

        self.assertEqual(queue["counts"]["due_reviews"], 1)
        self.assertEqual(queue["counts"]["matures_today"], 1)
        self.assertEqual(queue["counts"]["open_not_due"], 1)
        self.assertEqual(queue["due_reviews"][0]["prediction_id"], "2026-07-11-P01")

    def test_due_review_queue_keeps_invalid_early_review_open_but_skips_mature_review(self) -> None:
        original = v2_prediction()
        original["deadline"] = "2026-07-19"
        original["market_mapping"][0]["evaluation_deadline"] = "2026-07-19"
        early = resolved_review()
        mature = resolved_review()
        mature["date"] = "2026-07-20"
        mature["review"]["review_date"] = "2026-07-20"

        early_queue = STORE.due_review_queue("2026-07-20", [original, early])
        mature_queue = STORE.due_review_queue("2026-07-20", [original, mature])
        fully_resolved_queue = STORE.due_review_queue(
            "2026-07-20",
            [original, mature, market_review(deadline="2026-07-19", review_date="2026-07-20")],
        )

        self.assertEqual(early_queue["counts"]["due_reviews"], 1)
        self.assertEqual(mature_queue["counts"]["due_reviews"], 1)
        self.assertEqual(mature_queue["due_reviews"][0]["event_resolution_status"], "resolved")
        self.assertEqual(fully_resolved_queue["counts"]["due_reviews"], 0)
        self.assertEqual(fully_resolved_queue["counts"]["closed"], 1)

    def test_market_only_review_does_not_close_or_score_event(self) -> None:
        original = v2_prediction()
        market = market_review()

        event_metrics = MODULE.proper_scoring_metrics([original, market], cutoff="2026-07-14", minimum_sample=1)
        mapping_metrics = MODULE.market_mapping_metrics([original, market], cutoff="2026-07-14")

        self.assertEqual(event_metrics["eligible_sample_count"], 0)
        self.assertEqual(event_metrics["exclusion_counts"]["missing_review"], 1)
        self.assertEqual(mapping_metrics["resolved_mapping_count"], 1)
        self.assertEqual(mapping_metrics["hit_rate_pct"], 100.0)
        self.assertEqual(mapping_metrics["mean_signed_performance_pct"], 1.0)

    def test_reciprocal_market_mappings_count_as_one_independent_thesis(self) -> None:
        original = v2_prediction()
        original["tickers"] = [{"symbol": "TEST"}, {"symbol": "SPY"}]
        original["market_mapping"].append({
            "symbol": "SPY",
            "direction": "underperform",
            "benchmark": "TEST",
            "verification_rule": "SPY return is below TEST return by the deadline",
            "evaluation_deadline": "2026-07-13",
        })
        reciprocal_review = market_review()
        reciprocal_review["review"]["market_resolution"].append({
            "symbol": "SPY",
            "benchmark": "TEST",
            "evaluation_deadline": "2026-07-13",
            "status": "resolved",
            "observed_outcome": 1,
            "symbol_return_pct": 1.0,
            "benchmark_return_pct": 2.0,
            "excess_return_pct": -1.0,
            "start_price_date": "2026-07-10",
            "end_price_date": "2026-07-13",
            "evidence": [{"source": "Official market data", "url": "https://market.example/result"}],
        })

        metrics = MODULE.market_mapping_metrics([original, reciprocal_review], cutoff="2026-07-14")

        self.assertEqual(metrics["matured_mapping_count"], 1)
        self.assertEqual(metrics["resolved_mapping_count"], 1)
        self.assertEqual(metrics["reciprocal_duplicate_mapping_count_excluded"], 1)

    def test_combined_review_validates_event_and_mapping_dimensions_independently(self) -> None:
        original = v2_prediction()
        original["market_mapping"][0]["evaluation"] = {
            "metric": "total_return",
            "window_start": "2026-07-13",
            "price_field": "adjusted_close",
            "comparison": "symbol_gt_benchmark",
        }
        combined = resolved_review()
        combined["review"]["resolution_scope"] = "combined"
        combined["review"]["market_resolution"] = market_review()["review"]["market_resolution"]

        self.assertEqual(MODULE.validate_v2_review(combined, original), [])
        combined["review"]["market_resolution"][0]["observed_outcome"] = 0
        errors = MODULE.validate_v2_review(combined, original)
        self.assertTrue(any("outcome conflicts" in item for item in errors))

    def test_same_day_review_requires_terminal_evidence_under_end_of_day_deadlines(self) -> None:
        original = v2_prediction()
        same_day = resolved_review()
        same_day["date"] = "2026-07-13"
        same_day["review"]["review_date"] = "2026-07-13"

        errors = MODULE.validate_v2_review(same_day, original)
        same_day["review"]["terminal_evidence"] = True

        self.assertTrue(any("premature review" in item for item in errors))
        self.assertEqual(MODULE.validate_v2_review(same_day, original), [])

    def test_storage_skips_duplicate_review_identity_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = v2_prediction()
            review = resolved_review()
            predictions = root / "predictions.jsonl"
            predictions.write_text(
                json.dumps(original, ensure_ascii=False) + "\n" + json.dumps(review, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            input_path = root / "review.json"
            input_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}), encoding="utf-8")

            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                count = STORE.append_prediction_records(input_path, "2026-07-14", predictions)

            self.assertEqual(count, 0)
            self.assertEqual(len(predictions.read_text(encoding="utf-8").splitlines()), 2)

    def test_unresolved_matured_v2_blocks_only_after_deadline_day(self) -> None:
        original = v2_prediction()
        on_deadline = MODULE.audit_prediction_records(
            [original],
            cutoff="2026-07-13",
            enforce_from_date="2026-07-12",
            review_policy={"block_deployment_on_unresolved_due_v2": True},
        )
        after_deadline = MODULE.audit_prediction_records(
            [original],
            cutoff="2026-07-14",
            enforce_from_date="2026-07-12",
            review_policy={"block_deployment_on_unresolved_due_v2": True},
        )
        resolved = MODULE.audit_prediction_records(
            [original, resolved_review()],
            cutoff="2026-07-14",
            enforce_from_date="2026-07-12",
            review_policy={"block_deployment_on_unresolved_due_v2": True},
        )

        self.assertTrue(on_deadline["operational_passed"])
        self.assertFalse(after_deadline["operational_passed"])
        self.assertEqual(after_deadline["unresolved_matured_v2_prediction_ids"], ["2026-07-12-P01"])
        self.assertTrue(resolved["operational_passed"])


if __name__ == "__main__":
    unittest.main()
