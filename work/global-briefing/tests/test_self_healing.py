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
