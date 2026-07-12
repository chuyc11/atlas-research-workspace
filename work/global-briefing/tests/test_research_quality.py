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
        "date": "2026-07-13",
        "status": "validated" if outcome else "wrong",
        "review": {
            "review_date": "2026-07-13",
            "observed_outcome": outcome,
            "evidence": [{"source": "Official", "url": "https://official.example/result"}],
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
                    "review_date": "2026-07-13",
                    "observed_outcome": 1,
                    "evidence": [{"url": "https://example.test/result"}],
                },
            },
        ]

        metrics = MODULE.proper_scoring_metrics(rows, cutoff="2026-07-13", minimum_sample=1)

        self.assertEqual(metrics["eligible_sample_count"], 1)
        self.assertEqual(metrics["brier_score"], 0.01)
        self.assertTrue(metrics["is_research_ready"])
        self.assertTrue(metrics["gates"]["resolved_coverage_at_least_80pct"])
        self.assertEqual(metrics["legacy_matured_prediction_count_excluded"], 1)

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


if __name__ == "__main__":
    unittest.main()
