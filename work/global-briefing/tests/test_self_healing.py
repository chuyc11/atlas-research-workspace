from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import atlas as ATLAS


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

    def seed_briefing_test_inputs(self) -> dict:
        (self.root / "atlas.py").write_text("# control plane\n", encoding="utf-8")
        briefing = self.root / "work" / "global-briefing"
        (briefing / "scripts").mkdir(parents=True, exist_ok=True)
        (briefing / "tests").mkdir(parents=True, exist_ok=True)
        (briefing / "scripts" / "collector.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (briefing / "tests" / "test_contract.py").write_text(
            "# contract\n", encoding="utf-8"
        )
        (briefing / "requirements.txt").write_text("requests==1\n", encoding="utf-8")
        site_payload = self.root / "src" / "app" / "briefing.generated.json"
        site_payload.parent.mkdir(parents=True, exist_ok=True)
        site_payload.write_text("{}\n", encoding="utf-8")
        report = self.root / "outputs" / "每日全球晨间简报-2026-07-12.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# report\n", encoding="utf-8")
        return MODULE.build_briefing_test_input_fingerprint(self.root)

    def write_cycle_test_evidence(
        self,
        *,
        date: str = "2026-07-12",
        completed_at: str = "2026-07-12T02:15:00Z",
        returncode: int = 0,
        briefing_discovery: bool = True,
        input_fingerprint: dict | None = None,
    ) -> dict:
        fingerprint = input_fingerprint or MODULE.build_briefing_test_input_fingerprint(
            self.root
        )
        run_id = f"ATLAS-CYCLE-{date.replace('-', '')}-RUN-TEST"
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "date": date,
            "overall_passed": returncode == 0,
            "operational_gate_passed": returncode == 0,
            "test_returncode": returncode,
            "execution_profile": {"skip_tests": False},
            "stages": [
                {
                    "name": "targeted_integration_tests",
                    "status": "passed" if returncode == 0 else "failed",
                    "detail": {
                        "returncode": returncode,
                        "scope": {
                            "kind": "targeted_integration_suite",
                            "briefing_unittest_discovery": briefing_discovery,
                        },
                        "briefing_test_evidence": {
                            "schema_version": 1,
                            "suite": "python_unittest_discovery",
                            "discovery_root": "work/global-briefing/tests",
                            "pattern": "test_*.py",
                            "completed_at": completed_at,
                            "returncode": returncode,
                            "passed": returncode == 0,
                            "input": fingerprint,
                        },
                    },
                }
            ],
            "audit_chain": {
                "schema_version": 1,
                "sequence": 1,
                "previous_audit_sha256": None,
            },
        }
        payload["audit_chain"]["entry_sha256"] = MODULE.cycle_audit_record_hash(
            payload
        )
        latest = (
            self.root
            / "work"
            / "shared"
            / "atlas"
            / "run_audits"
            / f"atlas-cycle-{date}.json"
        )
        history = latest.parent / "history" / date / f"{run_id}.json"
        write_json(latest, payload)
        write_json(history, payload)
        return payload

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

    def test_verified_repair_exposes_effective_status_without_rewriting_observed_evidence(self) -> None:
        state_path = self.root / "work" / "global-briefing" / "data" / "site-sync-state.json"
        write_json(
            state_path,
            {
                "pending_sha": "same",
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
        with (
            patch.object(self.engine, "detect", return_value=[finding]),
            patch.object(self.engine, "verify_finding", return_value=True),
        ):
            returncode, report = self.engine.run(
                "2026-07-12", apply_safe=True, deep=False, strict=False
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(report["evidence_observed_at"], report["finished_at"])
        self.assertFalse(report["checks"][0]["passed"])
        self.assertTrue(report["checks"][0]["effective_passed"])
        self.assertEqual(report["checks"][0]["effective_status"], "repaired")
        self.assertEqual(report["counts"]["passed"], 1)

        status = MODULE.status_view(report, read_at="2026-07-12T12:00:00Z")
        self.assertEqual(status["status_view"]["read_at"], "2026-07-12T12:00:00Z")
        self.assertEqual(
            status["status_view"]["evidence_observed_at"],
            report["finished_at"],
        )
        self.assertTrue(status["status_view"]["historical_snapshot"])

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

    def test_deep_probe_reuses_fresh_matching_cycle_briefing_tests_but_runs_site(self) -> None:
        fingerprint = self.seed_briefing_test_inputs()
        self.write_cycle_test_evidence(input_fingerprint=fingerprint)
        calls = []

        def runner(command, *, cwd, timeout):
            calls.append((list(command), cwd, timeout))
            return subprocess.CompletedProcess(command, 0, stdout="site ok", stderr="")

        self.engine.runner = runner
        results = self.engine.probe_deep(
            "2026-07-12", now=datetime(2026, 7, 12, 2, 30, tzinfo=UTC)
        )

        briefing, site = results
        self.assertEqual([item.check_id for item in results], ["briefing_tests", "site_quality"])
        self.assertTrue(briefing.passed)
        self.assertTrue(briefing.reused)
        self.assertEqual(briefing.evidence["execution_mode"], "reused_cycle_evidence")
        self.assertFalse(briefing.evidence["suite_executed_by_heal"])
        self.assertTrue(site.passed)
        self.assertFalse(site.reused)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], self.engine.site_root)

    def test_briefing_input_fingerprint_matches_cycle_evidence_producer(self) -> None:
        self.seed_briefing_test_inputs()

        producer = ATLAS.build_briefing_test_input_fingerprint(
            self.root, "2026-07-12"
        )
        verifier = MODULE.build_briefing_test_input_fingerprint(
            self.root, "2026-07-12"
        )

        self.assertEqual(producer, verifier)

    def test_briefing_test_reuse_fails_closed_for_invalid_cycle_evidence(self) -> None:
        baseline = self.seed_briefing_test_inputs()
        now = datetime(2026, 7, 12, 2, 30, tzinfo=UTC)
        cases = (
            {
                "name": "stale",
                "write": {"completed_at": "2026-07-12T00:00:00Z"},
                "expected": "stale",
            },
            {
                "name": "failed_returncode",
                "write": {"returncode": 1},
                "expected": "returncode",
            },
            {
                "name": "insufficient_scope",
                "write": {"briefing_discovery": False},
                "expected": "scope",
            },
            {
                "name": "fingerprint_mismatch",
                "write": {
                    "input_fingerprint": baseline
                    | {"fingerprint_sha256": "f" * 64}
                },
                "expected": "fingerprint",
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                self.write_cycle_test_evidence(**case["write"])
                decision = self.engine.briefing_test_reuse_decision(
                    "2026-07-12", now=now
                )
                self.assertFalse(decision["reused"])
                self.assertTrue(
                    any(case["expected"] in reason for reason in decision["reasons"]),
                    decision["reasons"],
                )

    def test_deep_probe_executes_briefing_tests_when_input_changed_after_cycle(self) -> None:
        fingerprint = self.seed_briefing_test_inputs()
        self.write_cycle_test_evidence(input_fingerprint=fingerprint)
        script = self.root / "work" / "global-briefing" / "scripts" / "collector.py"
        script.write_text("VALUE = 2\n", encoding="utf-8")
        calls = []

        def runner(command, *, cwd, timeout):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        self.engine.runner = runner
        results = self.engine.probe_deep(
            "2026-07-12", now=datetime(2026, 7, 12, 2, 30, tzinfo=UTC)
        )

        briefing = results[0]
        self.assertTrue(briefing.passed)
        self.assertFalse(briefing.reused)
        self.assertEqual(briefing.evidence["execution_mode"], "executed")
        self.assertTrue(briefing.evidence["suite_executed_by_heal"])
        self.assertTrue(any(command[0] == sys.executable for command in calls))

    def test_briefing_test_reuse_requires_hashed_matching_history_record(self) -> None:
        fingerprint = self.seed_briefing_test_inputs()
        payload = self.write_cycle_test_evidence(input_fingerprint=fingerprint)
        latest = (
            self.root
            / "work"
            / "shared"
            / "atlas"
            / "run_audits"
            / "atlas-cycle-2026-07-12.json"
        )
        history = latest.parent / "history" / "2026-07-12" / f"{payload['run_id']}.json"
        history.unlink()

        missing_history = self.engine.briefing_test_reuse_decision(
            "2026-07-12", now=datetime(2026, 7, 12, 2, 30, tzinfo=UTC)
        )
        self.assertFalse(missing_history["reused"])
        self.assertTrue(
            any("history" in reason for reason in missing_history["reasons"])
        )

        write_json(history, payload)
        payload["operational_gate_passed"] = False
        write_json(latest, payload)
        invalid_hash = self.engine.briefing_test_reuse_decision(
            "2026-07-12", now=datetime(2026, 7, 12, 2, 30, tzinfo=UTC)
        )
        self.assertFalse(invalid_hash["reused"])
        self.assertTrue(
            any("content hash" in reason for reason in invalid_hash["reasons"])
        )

    def test_independent_deep_checks_start_concurrently_and_keep_stable_order(self) -> None:
        site_started = threading.Event()
        briefing_observed_site = []

        def runner(command, *, cwd, timeout):
            if command[0] == sys.executable:
                briefing_observed_site.append(site_started.wait(timeout=0.5))
            else:
                site_started.set()
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        self.engine.runner = runner
        results = self.engine.probe_deep("2026-07-12")

        self.assertEqual(briefing_observed_site, [True])
        self.assertEqual([item.check_id for item in results], ["briefing_tests", "site_quality"])
        self.assertTrue(all(item.passed for item in results))


if __name__ == "__main__":
    unittest.main()
