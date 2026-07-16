from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "self_healing.py"
SPEC = importlib.util.spec_from_file_location("self_healing_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class SelfHealingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = self.root / "work" / "global-briefing" / "config" / "self_healing.json"
        write_json(
            self.policy,
            {
                "schema_version": 1,
                "max_repairs_per_run": 4,
                "max_attempts_per_issue": 2,
                "cooldown_minutes": 60,
                "lock_stale_minutes": 120,
                "rollback_on_verification_failure": True,
                "auto_fix_risks": ["low"],
                "blocking_severities": ["critical"],
                "boundaries": {"may_change_source_code": False},
                "checks": {
                    "site_state_consistency": {"severity": "medium", "risk": "low"},
                    "site_payload_freshness": {"severity": "high", "risk": "low"},
                    "research_operational_gate": {"severity": "critical", "risk": "high"},
                    "review_queue_freshness": {"severity": "critical", "risk": "low"},
                    "resolution_evidence_freshness": {"severity": "high", "risk": "low"},
                    "drift_diagnostics_freshness": {"severity": "medium", "risk": "low"},
                    "paper_theme_registry_validity": {"severity": "high", "risk": "high"},
                },
            },
        )
        self.runtime = self.root / "work" / "shared" / "atlas" / "self_healing"
        self.engine = MODULE.SelfHealingEngine(
            root=self.root,
            policy_path=self.policy,
            runtime_root=self.runtime,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fingerprint_is_stable_and_issue_resolves_when_probe_passes(self) -> None:
        failed = self.engine.result(
            "site_state_consistency",
            False,
            "work/global-briefing/data/site-sync-state.json",
            "stale",
            fixer="normalize_site_state",
        )
        state = self.engine.reconcile_issues([failed])
        issue = state["issues"][failed.fingerprint]
        self.assertEqual(issue["status"], "open")
        self.assertEqual(issue["occurrences"], 1)

        self.engine.state_path.parent.mkdir(parents=True, exist_ok=True)
        MODULE.atomic_write_json(self.engine.state_path, state)
        passed = self.engine.result(
            "site_state_consistency",
            True,
            "work/global-briefing/data/site-sync-state.json",
            "healthy",
        )
        resolved = self.engine.reconcile_issues([passed])

        self.assertEqual(passed.fingerprint, failed.fingerprint)
        self.assertEqual(resolved["issues"][failed.fingerprint]["status"], "resolved")

    def test_low_risk_site_state_is_repaired_and_verified(self) -> None:
        state_path = self.root / "work" / "global-briefing" / "data" / "site-sync-state.json"
        write_json(
            state_path,
            {
                "pending_sha": "same",
                "pending_report": "report.md",
                "pending_date": "2026-07-12",
                "last_deployed_sha": "same",
            },
        )
        finding = self.engine.result(
            "site_state_consistency",
            False,
            state_path,
            "stale pending marker",
            fixer="normalize_site_state",
        )
        issue = {"repair_attempts": 0}

        eligible, _reason = self.engine.can_auto_fix(finding, issue)
        repaired = self.engine.execute_fixer(finding, "2026-07-12")

        self.assertTrue(eligible)
        self.assertEqual(repaired.status, "applied")
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("pending_sha", payload)
        self.assertEqual(payload["last_deployed_sha"], "same")
        self.assertTrue(self.engine.probe_site("2026-07-12")[1].passed)

    def test_valid_candidate_does_not_deadlock_on_stale_deployed_payload(self) -> None:
        site_data = self.root / "src" / "app" / "briefing.generated.json"
        write_json(site_data, {"reportDate": "2026-07-12", "contentHash": "old"})
        completed = type("Completed", (), {"returncode": 0, "stderr": ""})()
        candidate = {
            "status": "candidate_valid",
            "sha256": "new",
            "payload_sha256": "payload-new",
            "date": "2026-07-12",
        }

        with patch.object(self.engine, "command_json", return_value=(completed, candidate)):
            freshness = self.engine.probe_site("2026-07-12")[0]

        self.assertTrue(freshness.passed)
        self.assertTrue(freshness.evidence["candidate_valid"])
        self.assertTrue(freshness.evidence["publication_refresh_required"])
        self.assertFalse(freshness.evidence["deployed_matches"])

    def test_high_risk_issue_never_auto_fixes(self) -> None:
        finding = self.engine.result(
            "research_operational_gate",
            False,
            "outputs/report.md",
            "research gate failed",
            fixer="invent_research_conclusion",
        )

        eligible, reason = self.engine.can_auto_fix(finding, {"repair_attempts": 0})

        self.assertFalse(eligible)
        self.assertIn("approval", reason)

    def test_review_queue_probe_requires_consistent_partition_counts(self) -> None:
        queue = self.root / "work" / "global-briefing" / "data" / "review-queue-2026-07-13.json"
        payload = {
            "run_date": "2026-07-13",
            "counts": {"due_reviews": 3, "review_now": 2, "review_backlog": 1},
            "review_now": [{"prediction_id": "P1"}, {"prediction_id": "P2"}],
            "review_backlog": [{"prediction_id": "P3"}],
        }
        predictions = self.root / "work" / "global-briefing" / "data" / "predictions.jsonl"
        predictions.parent.mkdir(parents=True, exist_ok=True)
        predictions.write_text("", encoding="utf-8")
        settings = self.root / "work" / "global-briefing" / "config" / "settings.json"
        write_json(settings, {})
        payload["input_fingerprint"] = MODULE.stable_hash({
            "predictions_sha256": MODULE.file_hash(predictions),
            "deadline_semantics": None,
            "mature_for_daily_review_when": None,
            "review_queue": {},
        })
        write_json(queue, payload)

        passed = self.engine.probe_review_queue("2026-07-13")
        stored = json.loads(queue.read_text(encoding="utf-8"))
        stored["counts"]["review_backlog"] = 0
        write_json(queue, stored)
        failed = self.engine.probe_review_queue("2026-07-13")

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)

    def test_resolution_evidence_probe_tracks_queue_and_policy_fingerprint(self) -> None:
        date = "2026-07-13"
        queue = self.root / "work" / "global-briefing" / "data" / f"review-queue-{date}.json"
        write_json(queue, {"input_fingerprint": "queue-sha"})
        settings = self.root / "work" / "global-briefing" / "config" / "settings.json"
        policy = {"automatic_ledger_append": False}
        write_json(settings, {"resolution_evidence": policy})
        evidence = self.root / "work" / "global-briefing" / "data" / f"resolution-evidence-{date}.json"
        fingerprint = MODULE.stable_hash({
            "schema_version": 1,
            "run_date": date,
            "review_queue_input_fingerprint": "queue-sha",
            "resolution_evidence_policy": policy,
        })
        write_json(evidence, {
            "date": date,
            "input_fingerprint": fingerprint,
            "counts": {"prediction_work_items": 1},
            "items": [{"prediction_id": "P1"}],
        })

        passed = self.engine.probe_resolution_evidence(date)
        stored = json.loads(evidence.read_text(encoding="utf-8"))
        stored["input_fingerprint"] = "stale"
        write_json(evidence, stored)
        failed = self.engine.probe_resolution_evidence(date)

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)

    def test_drift_probe_requires_matching_fingerprint_and_separate_dimensions(self) -> None:
        date = "2026-07-13"
        path = self.root / "work" / "global-briefing" / "data" / f"drift-diagnostics-{date}.json"
        statuses = {
            "source_concentration": "healthy",
            "forecast_calibration": "insufficient_sample",
            "theme_crowding": "watch",
            "paper_account_attribution": "healthy",
        }
        write_json(path, {
            "date": date,
            "input_fingerprint": "current",
            "dimension_statuses": statuses,
            "no_opaque_composite_score": True,
            "deployment_blocking": False,
        })
        completed = type("Completed", (), {"returncode": 0, "stderr": ""})()
        with patch.object(self.engine, "command_json", return_value=(completed, {"input_fingerprint": "current"})):
            passed = self.engine.probe_drift_diagnostics(date)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["input_fingerprint"] = "stale"
        write_json(path, stored)
        with patch.object(self.engine, "command_json", return_value=(completed, {"input_fingerprint": "current"})):
            failed = self.engine.probe_drift_diagnostics(date)

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)

    def test_theme_registry_probe_requires_full_account_coverage(self) -> None:
        date = "2026-07-13"
        registry = self.root / "work" / "global-briefing" / "config" / "paper_theme_registry.json"
        write_json(registry, {"schema_version": 1})
        completed = type("Completed", (), {"returncode": 0, "stderr": ""})()
        healthy = {
            "date": date,
            "valid": True,
            "audit_passed": True,
            "history_current": True,
            "chain_valid": True,
            "snapshots_valid": True,
            "current_revision_id": "THEME-REG-TEST",
            "verified_entry_count": 2,
            "coverage": {
                "all_accounts_fully_covered": True,
                "accounts": [
                    {"account": "US", "position_value_coverage_pct": 100.0},
                    {"account": "CHINA", "position_value_coverage_pct": 100.0},
                ],
            },
        }
        with patch.object(self.engine, "command_json", return_value=(completed, healthy)):
            passed = self.engine.probe_paper_theme_registry(date)
        incomplete = json.loads(json.dumps(healthy))
        incomplete["coverage"]["all_accounts_fully_covered"] = False
        incomplete["coverage"]["accounts"][1]["position_value_coverage_pct"] = 75.0
        with patch.object(self.engine, "command_json", return_value=(completed, incomplete)):
            failed = self.engine.probe_paper_theme_registry(date)

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)

    def test_failed_verification_rolls_back_original_file(self) -> None:
        state_path = self.root / "work" / "global-briefing" / "data" / "site-sync-state.json"
        original = {
            "pending_sha": "same",
            "pending_report": "report.md",
            "pending_date": "2026-07-12",
            "last_deployed_sha": "same",
        }
        write_json(state_path, original)
        finding = self.engine.result(
            "site_state_consistency",
            False,
            state_path,
            "stale pending marker",
            fixer="normalize_site_state",
        )
        with (
            patch.object(self.engine, "detect", return_value=[finding]),
            patch.object(self.engine, "verify_finding", return_value=False),
        ):
            returncode, report = self.engine.run(
                "2026-07-12", apply_safe=True, deep=False, strict=False
            )

        self.assertEqual(returncode, 0)
        self.assertTrue(report["repairs"][0]["rolled_back"])
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original)
        issue = next(iter(json.loads(self.engine.state_path.read_text(encoding="utf-8"))["issues"].values()))
        self.assertEqual(issue["last_repair_status"], "failed")

    def test_circuit_breaker_stops_repeated_repairs(self) -> None:
        finding = self.engine.result(
            "site_state_consistency",
            False,
            "state.json",
            "stale",
            fixer="normalize_site_state",
        )

        eligible, reason = self.engine.can_auto_fix(finding, {"repair_attempts": 2})

        self.assertFalse(eligible)
        self.assertIn("maximum repair attempts", reason)


if __name__ == "__main__":
    unittest.main()
