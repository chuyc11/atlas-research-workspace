from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evolution.py"
SPEC = importlib.util.spec_from_file_location("evolution_policy_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EvolutionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.originals = {
            "CONFIG_PATH": MODULE.CONFIG_PATH,
            "SETTINGS_PATH": MODULE.SETTINGS_PATH,
            "PREDICTIONS_PATH": MODULE.PREDICTIONS_PATH,
            "DATA_DIR": MODULE.DATA_DIR,
            "EVOLUTION_STATE_PATH": MODULE.EVOLUTION_STATE_PATH,
        }
        MODULE.DATA_DIR = root / "data"
        MODULE.DATA_DIR.mkdir(parents=True)
        MODULE.CONFIG_PATH = root / "evolution.json"
        MODULE.SETTINGS_PATH = root / "settings.json"
        MODULE.PREDICTIONS_PATH = MODULE.DATA_DIR / "predictions.jsonl"
        MODULE.EVOLUTION_STATE_PATH = MODULE.DATA_DIR / "evolution_state.json"
        MODULE.CONFIG_PATH.write_text(
            json.dumps(
                {
                    "prediction_review_fields": {"failure_reasons": list(MODULE.FAILURE_GUARDRAILS)},
                    "default_score_by_status": {
                        "partial": {"direction": 0.7, "timing": 0.6, "transmission": 0.45, "calibration": 0.5, "total": 56},
                        "validated": {"direction": 1, "timing": 1, "transmission": 1, "calibration": 0.9, "total": 95},
                    },
                    "scoring_weights": {"direction": 0.35, "timing": 0.2, "transmission": 0.3, "calibration": 0.15},
                }
            ),
            encoding="utf-8",
        )
        MODULE.SETTINGS_PATH.write_text(
            json.dumps(
                {
                    "prediction_contract": {"enforce_from_date": "2026-07-12"},
                    "research_evaluation": {
                        "minimum_sample": 30,
                        "maximum_brier": 0.25,
                        "maximum_expected_calibration_error": 0.15,
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(MODULE, name, value)
        self.temporary.cleanup()

    def write_records(self, records: list[dict]) -> None:
        MODULE.PREDICTIONS_PATH.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_policy_stays_shadow_when_reviews_are_early_or_incomplete(self) -> None:
        self.write_records(
            [
                {"date": "2026-07-01", "prediction_id": "P1", "horizon": "1d", "probability": "high", "scenario": "one", "status": "open", "falsification_signals": ["x"]},
                {"date": "2026-07-02", "prediction_id": "P1", "status": "partial", "review": {"review_date": "2026-07-02", "why": "asset transmission failed"}},
                {"date": "2026-07-01", "prediction_id": "P2", "horizon": "1w", "probability": "medium", "scenario": "two", "status": "open", "falsification_signals": ["y"]},
                {"date": "2026-07-01", "prediction_id": "P3", "horizon": "1m", "probability": "medium", "scenario": "three", "status": "open", "falsification_signals": ["z"]},
                {"date": "2026-07-01", "prediction_id": "P4", "horizon": "1m", "probability": "medium", "scenario": "four", "status": "open", "falsification_signals": ["z"]},
                {"date": "2026-07-02", "prediction_id": "P4", "status": "validated", "review": {"review_date": "2026-07-02", "why": "headline moved"}},
            ]
        )

        state, changed = MODULE.update_evolution_state("month", "2026-07-10", write=True)
        _, changed_again = MODULE.update_evolution_state("month", "2026-07-10", write=True)

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(state["state"], "shadow")
        self.assertFalse(state["promotion_allowed"])
        self.assertIn("P2", state["integrity"]["overdue_unreviewed_prediction_ids"])
        self.assertIn("P4", state["integrity"]["early_closed_without_terminal_evidence"])
        self.assertTrue(any(rule["rule_id"] == "failure-asset_mapping_error" for rule in state["active_rules"]))
        self.assertTrue(state["report_contract"]["ban_generic_fillers"])

    def test_validation_requires_explicit_score_and_failure_reason(self) -> None:
        self.write_records(
            [
                {"date": "2026-07-01", "prediction_id": "P1", "horizon": "1d", "status": "open", "verification_signals": ["x"]},
                {"date": "2026-07-02", "prediction_id": "P1", "status": "partial", "review": {"review_date": "2026-07-02", "why": "not enough time"}},
            ]
        )

        result = MODULE.validate_records("month", "2026-07-10")

        self.assertEqual(result["nonvalidated_reviews_missing_explicit_score"], ["P1"])
        self.assertEqual(result["nonvalidated_reviews_missing_explicit_failure_reasons"], ["P1"])

    def test_legacy_integrity_warnings_do_not_permanently_block_valid_v2_cohort(self) -> None:
        records = [
            {
                "date": "2026-07-01",
                "prediction_id": "LEGACY-P1",
                "horizon": "1m",
                "probability": "high",
                "scenario": "legacy",
                "status": "open",
            },
            {
                "date": "2026-07-02",
                "prediction_id": "LEGACY-P1",
                "status": "validated",
                "review": {"review_date": "2026-07-02"},
            },
        ]
        for index in range(1, 31):
            prediction_id = f"2026-07-12-P{index:02d}"
            records.extend(
                [
                    {
                        "schema_version": 2,
                        "date": "2026-07-12",
                        "deadline": "2026-07-13",
                        "prediction_id": prediction_id,
                        "horizon": "1d",
                        "probability": 0.9,
                        "scenario": f"event {index}",
                        "status": "open",
                        "trigger": "official update",
                        "verification_signals": ["official confirmation"],
                        "falsification_signals": ["official denial"],
                        "resolution": {
                            "question": "Did it occur?",
                            "success_criteria": "Official confirmation",
                            "failure_criteria": "No confirmation by deadline",
                        },
                        "evidence": [{"source": "Official", "url": "https://official.example/input"}],
                    },
                    {
                        "date": "2026-07-13",
                        "prediction_id": prediction_id,
                        "status": "validated",
                        "review": {
                            "review_date": "2026-07-13",
                            "observed_outcome": 1,
                            "evidence": [{"source": "Official", "url": "https://official.example/result"}],
                        },
                    },
                ]
            )
        self.write_records(records)

        state, _ = MODULE.update_evolution_state("month", "2026-07-31", write=False)

        self.assertTrue(state["promotion_allowed"])
        self.assertEqual(state["state"], "validated")
        self.assertEqual(state["proper_scoring"]["eligible_sample_count"], 30)
        self.assertEqual(state["gate_reasons"], [])
        self.assertTrue(any("early_closure_bias" in warning for warning in state["legacy_diagnostic_warnings"]))


if __name__ == "__main__":
    unittest.main()
