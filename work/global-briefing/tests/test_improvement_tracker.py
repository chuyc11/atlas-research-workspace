from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "improvement_tracker.py"
SPEC = importlib.util.spec_from_file_location("improvement_tracker_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class ImprovementTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "work" / "global-briefing" / "config" / "improvement_tracking.json"
        write_json(
            self.config_path,
            {
                "schema_version": 1,
                "default_due_days": 7,
                "blocking_severities": ["critical"],
                "auto_fix_risks": ["low"],
                "external_alerting": {"enabled": False, "destinations": []},
                "disaster_recovery": {"enabled": False},
                "acceptance_rules": {
                    "failure-asset_mapping_error": "v2_contract",
                    "report-decision-density": "report_quality",
                    "review-explicit-scoring": "new_review_integrity",
                },
            },
        )
        self.runtime = self.root / "work" / "shared" / "atlas" / "improvements"
        self.tracker = MODULE.ImprovementTracker(root=self.root, config_path=self.config_path, runtime_root=self.runtime)
        write_json(
            self.root / "work" / "global-briefing" / "data" / "evolution_state.json",
            {
                "active_rules": [
                    {"rule_id": "failure-asset_mapping_error", "instruction": "补资产映射证据", "trigger_count": 2},
                    {"rule_id": "report-decision-density", "instruction": "提高决策密度", "trigger_count": 1},
                    {"rule_id": "review-explicit-scoring", "instruction": "完整记录复盘评分", "trigger_count": 1},
                ]
            },
        )
        self.write_quality("2026-07-12", passed=True)
        self.write_site_health(85)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_quality(self, date: str, *, passed: bool) -> None:
        write_json(
            self.root / "work" / "global-briefing" / "data" / f"research-quality-{date}.json",
            {
                "date": date,
                "report_audit": {
                    "passed": passed,
                    "errors": [] if passed else ["thin"],
                    "all_core_stories_passed": passed,
                    "story_evidence_enforced": True,
                    "source_role_coverage_pct": 100.0 if passed else 0.0,
                },
                "prediction_audit": {"v2_contract": {"errors": [], "review_errors": []}},
            },
        )

    def write_site_health(self, score: int) -> None:
        write_json(
            self.root / "src" / "app" / "briefing.generated.json",
            {
                "metrics": {
                    "sourceHealth": {
                        "score": score,
                        "label": "良好" if score >= 80 else "受限",
                        "staleMarketItemCount": 0,
                        "rssStaleOrUnknownPct": 0,
                        "chinaMissingPriceDateItemCount": 0,
                        "limitations": [],
                    }
                }
            },
        )

    def test_retrospective_action_waits_for_next_run_then_verifies(self) -> None:
        _rc, first = self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        action = next(item for item in first["actions"] if item["spec"]["source_key"] == "failure-asset_mapping_error")
        self.assertEqual(action["status"], "monitoring")
        self.assertEqual(action["eligible_from"], "2026-07-13")

        self.write_quality("2026-07-13", passed=True)
        _rc, second = self.tracker.run("2026-07-13", apply_safe=False, strict=False)
        action = next(item for item in second["actions"] if item["spec"]["source_key"] == "failure-asset_mapping_error")

        self.assertEqual(action["status"], "verified")
        self.assertEqual(action["last_evaluation"]["outcome"], "pass")

    def test_verified_capability_reopens_on_regression(self) -> None:
        _rc, first = self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        source = next(item for item in first["actions"] if item["spec"]["source_key"] == "capability-source-health")
        self.assertEqual(source["status"], "verified")

        self.write_quality("2026-07-13", passed=True)
        self.write_site_health(42)
        _rc, second = self.tracker.run("2026-07-13", apply_safe=False, strict=False)
        source = next(item for item in second["actions"] if item["spec"]["source_key"] == "capability-source-health")

        self.assertEqual(source["status"], "regressed")
        self.assertEqual(source["last_evaluation"]["evidence"]["score"], 42)
        occurrence_count = source["occurrences"]
        history_count = len(source["verification_history"])

        _rc, third = self.tracker.run("2026-07-13", apply_safe=False, strict=False)
        source = next(item for item in third["actions"] if item["spec"]["source_key"] == "capability-source-health")
        self.assertEqual(source["status"], "regressed")
        self.assertEqual(source["occurrences"], occurrence_count)
        self.assertEqual(len(source["verification_history"]), history_count)

    def test_high_source_score_cannot_verify_failed_core_story_evidence(self) -> None:
        self.write_quality("2026-07-12", passed=False)
        self.write_site_health(90)

        _rc, result = self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        source = next(item for item in result["actions"] if item["spec"]["source_key"] == "capability-source-health")

        self.assertNotEqual(source["status"], "verified")
        self.assertFalse(source["last_evaluation"]["evidence"]["all_core_stories_passed"])

    def test_external_alerting_requires_actual_delivery_receipts(self) -> None:
        date = "2026-07-12"
        self.tracker.config["external_alerting"] = {
            "enabled": True,
            "destinations": ["codex_task_inbox"],
            "channel_health_max_age_hours": 168,
        }
        spec = next(
            item
            for item in self.tracker.capability_specs(date)
            if item.source_key == "capability-external-alerting"
        )
        action = {"eligible_from": date}
        alert = self.root / "work" / "shared" / "atlas" / "alerts" / "latest.json"
        write_json(
            alert,
            {
                "date": date,
                "destinations": ["codex_task_inbox"],
                "status": "attention_required",
                "findings": [{"severity": "medium"}],
                "delivery_state": "pending",
                "delivery_receipts": {},
            },
        )

        pending = self.tracker.evaluate(spec, action, date)
        self.assertEqual(pending.outcome, "fail")
        self.assertEqual(
            pending.evidence["missing_or_stale_destinations"],
            ["codex_task_inbox"],
        )

        write_json(
            alert,
            {
                "date": date,
                "destinations": ["codex_task_inbox"],
                "status": "attention_required",
                "findings": [{"severity": "medium"}],
                "delivery_state": "delivered",
                "delivery_receipts": {
                    "codex_task_inbox": {
                        "receipt_id": "receipt-123",
                        "recorded_at": "2026-07-12T01:00:00Z",
                    }
                },
            },
        )
        delivered = self.tracker.evaluate(spec, action, date)

        self.assertEqual(delivered.outcome, "fail")

        channel_health = self.root / "work" / "shared" / "atlas" / "alerts" / "channel_health.json"
        write_json(
            channel_health,
            {
                "schema_version": 1,
                "updated_at": "2026-07-12T01:00:00Z",
                "destinations": {
                    "codex_task_inbox": {
                        "status": "healthy",
                        "destination": "codex_task_inbox",
                        "provider_message_id": "receipt-123",
                        "receipt_id": "receipt-123",
                        "verified_at": "2026-07-12T01:00:00Z",
                        "source_alert_id": "ATLAS-ALERT-1",
                        "source_alert_date": date,
                    }
                },
            },
        )
        delivered = self.tracker.evaluate(spec, action, date)

        self.assertEqual(delivered.outcome, "pass")
        self.assertEqual(delivered.evidence["healthy_destinations"], ["codex_task_inbox"])

        alert_payload = json.loads(alert.read_text(encoding="utf-8"))
        alert_payload.update(
            {
                "status": "attention_required",
                "findings": [{"severity": "high"}],
                "requires_acknowledgement": True,
                "delivery_state": "delivered",
            }
        )
        write_json(alert, alert_payload)
        unacknowledged = self.tracker.evaluate(spec, action, date)
        self.assertEqual(unacknowledged.outcome, "fail")
        self.assertFalse(unacknowledged.evidence["acknowledgement_satisfied"])

        alert_payload.update(
            {
                "delivery_state": "acknowledged",
                "acknowledged_at": "2026-07-12T02:00:00Z",
                "acknowledged_by": "operator",
            }
        )
        write_json(alert, alert_payload)
        acknowledged = self.tracker.evaluate(spec, action, date)
        self.assertEqual(acknowledged.outcome, "pass")

        write_json(
            alert,
            {
                "date": date,
                "status": "healthy",
                "delivery_state": "not_required",
                "findings": [],
            },
        )
        healthy_day = self.tracker.evaluate(spec, action, date)
        self.assertEqual(healthy_day.outcome, "pass")

    def test_disaster_recovery_requires_authenticated_encryption_when_configured(self) -> None:
        date = "2026-07-12"
        self.tracker.config["disaster_recovery"] = {
            "enabled": True,
            "maximum_backup_age_hours": 24,
            "encryption": {"required": True, "algorithm": "AES-256-GCM"},
        }
        spec = next(
            item
            for item in self.tracker.capability_specs(date)
            if item.source_key == "capability-disaster-recovery"
        )
        action = {"eligible_from": date}
        latest = self.root / "work" / "shared" / "atlas" / "backups" / "latest.json"
        with tempfile.TemporaryDirectory() as external:
            archive = Path(external) / "backup.atlasdr"
            archive.write_bytes(b"encrypted backup")
            payload = {
                "archive": str(archive),
                "archive_sha256": MODULE.hashlib.sha256(archive.read_bytes()).hexdigest(),
                "created_at": MODULE.utc_now(),
                "archive_integrity_verified": True,
                "restore_verified": True,
                "restore_scope": "configured_workspace_files_and_git_bundles",
                "encrypted": False,
                "encryption_algorithm": None,
                "encrypted_container_authenticated": False,
                "full_runtime_restore_verified": False,
            }
            write_json(latest, payload)

            unencrypted = self.tracker.evaluate(spec, action, date)
            self.assertEqual(unencrypted.outcome, "fail")

            payload.update(
                {
                    "encrypted": True,
                    "encryption_algorithm": "AES-256-GCM",
                    "encrypted_container_authenticated": True,
                }
            )
            write_json(latest, payload)
            unauthenticated = self.tracker.evaluate(spec, action, date)
            self.assertEqual(unauthenticated.outcome, "fail")

            authenticated_result = {
                "verified": True,
                "archive_sha256": MODULE.hashlib.sha256(archive.read_bytes()).hexdigest(),
                "archive_integrity_verified": True,
                "restore_verified": True,
                "restore_scope": "configured_workspace_files_and_git_bundles",
                "snapshot_profile": "full",
                "full_baseline_authenticated": False,
                "verification_mode": "authenticated_archive",
            }
            with patch.object(
                self.tracker,
                "verify_disaster_recovery_snapshot",
                return_value=authenticated_result,
            ):
                encrypted = self.tracker.evaluate(spec, action, date)

        self.assertEqual(encrypted.outcome, "pass")
        self.assertFalse(encrypted.evidence["full_runtime_restore_verified"])

    def test_manual_action_becomes_overdue_after_due_date(self) -> None:
        spec = MODULE.ActionSpec(
            source_key="manual-critical",
            domain="governance",
            title="manual evidence",
            recommendation="provide durable proof",
            acceptance_key="manual_evidence",
            acceptance_criteria="proof exists",
            severity="critical",
            risk="high",
        )
        with (
            patch.object(self.tracker, "retrospective_specs", return_value=[spec]),
            patch.object(self.tracker, "capability_specs", return_value=[]),
        ):
            self.tracker.run("2026-07-12", apply_safe=False, strict=False)
            rc, report = self.tracker.run("2026-07-20", apply_safe=False, strict=True)

        action = report["actions"][0]
        self.assertEqual(rc, 1)
        self.assertEqual(action["status"], "overdue")
        self.assertEqual(report["counts"]["blocking"], 1)

    def test_reverse_date_cannot_downgrade_a_verified_action(self) -> None:
        self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        self.write_quality("2026-07-13", passed=True)
        _rc, verified = self.tracker.run("2026-07-13", apply_safe=False, strict=False)
        action = next(
            item
            for item in verified["actions"]
            if item["spec"]["source_key"] == "failure-asset_mapping_error"
        )
        self.assertEqual(action["status"], "verified")

        with self.assertRaisesRegex(ValueError, "earlier than its last check"):
            self.tracker.run("2026-07-12", apply_safe=False, strict=False)

    def test_same_day_dynamic_age_is_semantically_idempotent_before_fixer(self) -> None:
        spec = MODULE.ActionSpec(
            source_key="dynamic-age",
            domain="resilience",
            title="dynamic evidence",
            recommendation="keep the gate stable",
            acceptance_key="dynamic",
            acceptance_criteria="passes",
            severity="critical",
            risk="low",
            origin="capability_audit",
            auto_fixer="safe-test-fixer",
        )
        observed_ages = iter([1.0, 1.1])

        def evaluate(_spec, _action, _date):
            return MODULE.Evaluation(
                "fail",
                "same semantic failure",
                {"age_hours": next(observed_ages), "stable_reason": "missing"},
            )

        with (
            patch.object(self.tracker, "retrospective_specs", return_value=[]),
            patch.object(self.tracker, "capability_specs", return_value=[spec]),
            patch.object(self.tracker, "evaluate", side_effect=evaluate),
            patch.object(self.tracker, "apply_fixer", return_value=(False, "still missing")) as fixer,
        ):
            _rc, first = self.tracker.run("2026-07-12", apply_safe=True, strict=False)
            _rc, second = self.tracker.run("2026-07-12", apply_safe=True, strict=False)

        first_action = first["actions"][0]
        second_action = second["actions"][0]
        self.assertEqual(fixer.call_count, 1)
        self.assertEqual(second_action["occurrences"], first_action["occurrences"])
        self.assertEqual(
            len(second_action["verification_history"]),
            len(first_action["verification_history"]),
        )
        self.assertEqual(second_action["last_evaluation"]["evidence"]["age_hours"], 1.1)

    def test_verified_manual_result_reopens_as_regressed(self) -> None:
        spec = MODULE.ActionSpec(
            source_key="manual-regression",
            domain="governance",
            title="manual regression",
            recommendation="retain evidence",
            acceptance_key="manual",
            acceptance_criteria="evidence remains valid",
            severity="high",
            risk="high",
            origin="capability_audit",
        )
        evaluations = iter(
            [
                MODULE.Evaluation("pass", "evidence present", {"proof": "one"}),
                MODULE.Evaluation("manual", "evidence must be renewed", {}),
            ]
        )
        with (
            patch.object(self.tracker, "retrospective_specs", return_value=[]),
            patch.object(self.tracker, "capability_specs", return_value=[spec]),
            patch.object(self.tracker, "evaluate", side_effect=lambda *_args: next(evaluations)),
        ):
            self.tracker.run("2026-07-12", apply_safe=False, strict=False)
            _rc, report = self.tracker.run("2026-07-13", apply_safe=False, strict=False)

        self.assertEqual(report["actions"][0]["status"], "regressed")

    def test_paper_attribution_uses_only_allowlisted_safe_fixer(self) -> None:
        target = self.root / "work" / "global-briefing" / "data" / "paper-attribution-day-2026-07-12.json"

        def safe_fixer(spec, date):
            if spec.auto_fixer == "write_paper_attribution":
                write_json(target, {"end": date})
                return True, "written"
            if spec.auto_fixer == "write_drift_diagnostics":
                write_json(
                    self.root / "work" / "global-briefing" / "data" / f"drift-diagnostics-{date}.json",
                    {
                        "date": date,
                        "input_fingerprint": "test",
                        "dimension_statuses": {
                            "source_concentration": "healthy",
                            "forecast_calibration": "insufficient_sample",
                            "theme_crowding": "healthy",
                            "paper_account_attribution": "watch",
                        },
                        "no_opaque_composite_score": True,
                        "deployment_blocking": False,
                        "paper_account_attribution": {"accounts": []},
                    },
                )
                return True, "written"
            self.fail(f"unexpected fixer {spec.auto_fixer}")

        with patch.object(self.tracker, "apply_fixer", side_effect=safe_fixer):
            _rc, report = self.tracker.run("2026-07-12", apply_safe=True, strict=False)

        action = next(item for item in report["actions"] if item["spec"]["source_key"] == "capability-paper-attribution")
        self.assertEqual(action["status"], "verified")
        self.assertTrue(target.exists())
        self.assertEqual(report["repairs"][0]["fixer"], "write_paper_attribution")

    def test_paper_theme_provenance_remains_a_durable_gap_until_both_accounts_reach_80pct(self) -> None:
        date = "2026-07-12"
        write_json(
            self.root / "work" / "global-briefing" / "data" / f"drift-diagnostics-{date}.json",
            {
                "date": date,
                "input_fingerprint": "test",
                "dimension_statuses": {
                    "source_concentration": "healthy",
                    "forecast_calibration": "insufficient_sample",
                    "theme_crowding": "healthy",
                    "paper_account_attribution": "watch",
                },
                "no_opaque_composite_score": True,
                "deployment_blocking": False,
                "paper_account_attribution": {
                    "accounts": [
                        {"account": "US", "explicit_theme_attribution_coverage_pct": 75},
                        {"account": "CHINA", "explicit_theme_attribution_coverage_pct": 90},
                    ]
                },
            },
        )

        _rc, report = self.tracker.run(date, apply_safe=False, strict=False)
        action = next(item for item in report["actions"] if item["spec"]["source_key"] == "capability-paper-theme-provenance")

        self.assertEqual(action["status"], "open")
        self.assertEqual(action["last_evaluation"]["evidence"]["coverage_pct_by_account"]["US"], 75)

    def test_action_id_is_stable_across_runs(self) -> None:
        _rc, first = self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        first_ids = {item["spec"]["source_key"]: item["action_id"] for item in first["actions"]}
        self.write_quality("2026-07-13", passed=True)
        _rc, second = self.tracker.run("2026-07-13", apply_safe=False, strict=False)
        second_ids = {item["spec"]["source_key"]: item["action_id"] for item in second["actions"]}
        self.assertEqual(first_ids, second_ids)

    def test_empty_git_directory_does_not_satisfy_delivery_governance(self) -> None:
        (self.root / ".git").mkdir()

        _rc, report = self.tracker.run("2026-07-12", apply_safe=False, strict=False)

        action = next(item for item in report["actions"] if item["spec"]["source_key"] == "capability-core-version-control")
        self.assertEqual(action["status"], "open")
        self.assertEqual(action["last_evaluation"]["outcome"], "fail")

    def test_next_run_rejects_early_or_unscored_review(self) -> None:
        predictions = self.root / "work" / "global-briefing" / "data" / "predictions.jsonl"
        predictions.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "schema_version": 2,
                "prediction_id": "2026-07-12-P01",
                "date": "2026-07-12",
                "deadline": "2026-07-20",
            },
            {
                "prediction_id": "2026-07-12-P01",
                "status": "partial",
                "review": {"review_date": "2026-07-13", "observed_outcome": 1},
                "evidence": [{"url": "https://example.com"}],
            },
        ]
        predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        self.write_quality("2026-07-13", passed=True)

        _rc, report = self.tracker.run("2026-07-13", apply_safe=False, strict=False)
        action = next(item for item in report["actions"] if item["spec"]["source_key"] == "review-explicit-scoring")
        errors = action["last_evaluation"]["evidence"]["errors"]

        self.assertEqual(action["last_evaluation"]["outcome"], "fail")
        self.assertTrue(any("closed before deadline" in error for error in errors))
        self.assertTrue(any("component scores" in error for error in errors))
        self.assertTrue(any("failure_reasons" in error for error in errors))

    def test_new_review_integrity_accepts_evidence_inside_review_object(self) -> None:
        predictions = self.root / "work" / "global-briefing" / "data" / "predictions.jsonl"
        predictions.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "schema_version": 2,
                "prediction_id": "2026-07-12-P01",
                "date": "2026-07-12",
                "deadline": "2026-07-12",
            },
            {
                "prediction_id": "2026-07-12-P01",
                "date": "2026-07-13",
                "status": "validated",
                "review": {
                    "review_date": "2026-07-13",
                    "observed_outcome": 1,
                    "evidence": [{"source": "Official", "url": "https://example.com/result"}],
                },
            },
        ]
        predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        self.write_quality("2026-07-13", passed=True)

        _rc, report = self.tracker.run("2026-07-13", apply_safe=False, strict=False)
        action = next(item for item in report["actions"] if item["spec"]["source_key"] == "review-explicit-scoring")

        self.assertEqual(action["last_evaluation"]["outcome"], "pass")
        self.assertEqual(action["last_evaluation"]["evidence"]["errors"], [])

    def test_new_review_integrity_scores_market_only_reviews_at_mapping_level(self) -> None:
        predictions = self.root / "work" / "global-briefing" / "data" / "predictions.jsonl"
        predictions.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "schema_version": 2,
                "prediction_id": "2026-07-12-P01",
                "date": "2026-07-12",
                "deadline": "2026-07-13",
            },
            {
                "prediction_id": "2026-07-12-P01",
                "date": "2026-07-14",
                "status": "active",
                "review": {
                    "resolution_scope": "market",
                    "review_date": "2026-07-14",
                    "market_resolution": [
                        {
                            "evaluation_deadline": "2026-07-13",
                            "observed_outcome": 1,
                            "evidence": [{"source": "Market", "url": "https://example.com/market"}],
                        }
                    ],
                },
            },
        ]
        predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.tracker.run("2026-07-12", apply_safe=False, strict=False)
        self.write_quality("2026-07-14", passed=True)

        _rc, report = self.tracker.run("2026-07-14", apply_safe=False, strict=False)
        action = next(item for item in report["actions"] if item["spec"]["source_key"] == "review-explicit-scoring")

        self.assertEqual(action["last_evaluation"]["outcome"], "pass")
        self.assertEqual(action["last_evaluation"]["evidence"]["errors"], [])


if __name__ == "__main__":
    unittest.main()
