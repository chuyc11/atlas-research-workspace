from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import atlas


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def ready_gate_check() -> dict:
    return {
        "ready": True,
        "blockers": [],
        "errors": [],
        "artifact": {
            "path": "runtime/latest.json",
            "present": True,
            "sha256": "a" * 64,
            "date": "2026-07-18",
        },
    }


def ready_artifact_gates() -> dict:
    return {
        "ready": True,
        "blockers": [],
        "errors": [],
        "checks": {
            "improvements": ready_gate_check(),
            "self_healing": ready_gate_check(),
            "alerts": ready_gate_check(),
            "backup": ready_gate_check(),
        },
    }


def ready_core_gate() -> dict:
    return {
        "ready": True,
        "blockers": [],
        "errors": [],
        "evidence": {"audit": {"present": True}, "history": {"integrity_passed": True}},
    }


def ready_candidate() -> dict:
    return {
        "ready": True,
        "returncode": 0,
        "reason": None,
        "evidence": {"present": True, "date": "2026-07-18"},
    }


class AtlasPublicationOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.trust_root = Path(self.tmp.name) / "external-trust"
        self.environment_patch = patch.dict(
            os.environ,
            {
                atlas.TRUST_ANCHOR_ROOT_ENV: str(self.trust_root),
                atlas.TRUST_ANCHOR_HMAC_KEY_ENV: "publication-test-only-anchor-key",
                atlas.TRUST_ANCHOR_NAMESPACE_ENV: "atlas-publication-test",
            },
            clear=False,
        )
        self.environment_patch.start()
        self.runtime = Path(self.tmp.name) / "runtime"
        self.runtime_patch = patch.object(atlas, "ATLAS_RUNTIME_ROOT", self.runtime)
        self.runtime_patch.start()
        self.backup_bootstrap_patch = patch.object(
            atlas,
            "publication_backup_bootstrap_readiness",
            return_value={"ready": True, "reasons": [], "evidence": {}},
        )
        self.backup_bootstrap_patch.start()

    def tearDown(self) -> None:
        self.backup_bootstrap_patch.stop()
        self.runtime_patch.stop()
        self.environment_patch.stop()
        self.tmp.cleanup()

    def test_daily_backup_requires_authenticated_full_baseline_shape(self) -> None:
        daily = {"snapshot_profile": "daily"}
        self.assertIn(
            "daily backup is not bound to a full recovery baseline",
            atlas._daily_backup_baseline_reasons(daily),
        )
        daily["full_snapshot"] = {
            "archive_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "date": "2026-07-18",
            "created_at": "2026-07-18T00:00:00Z",
            "restore_verified": True,
        }
        self.assertEqual(atlas._daily_backup_baseline_reasons(daily), [])
        self.assertIn(
            "daily backup full baseline is outside the configured freshness window",
            atlas._daily_backup_baseline_reasons(
                daily,
                maximum_age_hours=24,
                now=atlas.datetime(2026, 7, 20, tzinfo=atlas.UTC),
            ),
        )

    def test_manual_daily_control_config_writes_one_final_backup_after_controls(self) -> None:
        settings_path = Path(__file__).resolve().parents[1] / "work" / "global-briefing" / "config" / "settings.json"
        commands = json.loads(settings_path.read_text(encoding="utf-8"))["self_healing_commands"]

        self.assertEqual(
            commands,
            [
                "python atlas.py improvements --date YYYY-MM-DD --apply-safe --strict",
                "python atlas.py heal --date YYYY-MM-DD --apply-safe --deep --strict",
                "python atlas.py alerts --date YYYY-MM-DD",
                "python atlas.py improvements --date YYYY-MM-DD --apply-safe --strict",
                "python atlas.py alerts --date YYYY-MM-DD",
                "python atlas.py backup --date YYYY-MM-DD",
            ],
        )
        self.assertLess(
            commands.index("python atlas.py improvements --date YYYY-MM-DD --apply-safe --strict"),
            commands.index("python atlas.py heal --date YYYY-MM-DD --apply-safe --deep --strict"),
        )
        self.assertLess(
            commands.index("python atlas.py alerts --date YYYY-MM-DD"),
            commands.index("python atlas.py backup --date YYYY-MM-DD"),
        )

    def test_post_gate_orchestration_runs_controls_in_order_then_retries_only_staged_candidate(self) -> None:
        calls: list[str] = []

        def record(name: str):
            def command(_args: argparse.Namespace) -> int:
                calls.append(name)
                return 0

            return command

        with (
            patch.object(atlas, "publication_core_cycle_readiness", return_value=ready_core_gate()),
            patch.object(atlas, "publication_staged_candidate_readiness", return_value=ready_candidate()),
            patch.object(atlas, "publication_gate_artifact_readiness", return_value=ready_artifact_gates()),
            patch.object(atlas, "command_improvements", side_effect=record("improvements")),
            patch.object(atlas, "command_heal", side_effect=record("self_healing")),
            patch.object(atlas, "command_alerts", side_effect=record("alerts")),
            patch.object(atlas, "command_backup", side_effect=record("backup")),
            patch.object(
                atlas,
                "command_retry_staged_publication",
                side_effect=lambda _date: calls.append("retry_staged_candidate") or 0,
            ),
            patch.object(atlas, "command_sync") as sync,
            patch.object(atlas, "command_quality") as quality,
        ):
            result = atlas.run_post_gate_publication("2026-07-18", initiated_by="test")

        self.assertEqual(
            calls,
            [
                "improvements",
                "self_healing",
                "alerts",
                "improvements",
                "alerts",
                "backup",
                "retry_staged_candidate",
            ],
        )
        sync.assert_not_called()
        quality.assert_not_called()
        self.assertEqual(result["status"], "frozen_or_pending_deployment")
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["phase_a_reexecuted"])
        self.assertEqual(
            [stage["status"] for stage in result["stages"]],
            ["passed", "passed", "passed", "passed", "passed", "passed", "passed"],
        )
        saved = json.loads(Path(result["audit_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "frozen_or_pending_deployment")
        self.assertFalse(saved["phase_a_reexecuted"])

    def test_stale_backup_is_bootstrapped_before_final_control_sequence(self) -> None:
        calls: list[str] = []
        backup_results = iter((0, 0))

        def record(name: str):
            def command(_args: argparse.Namespace) -> int:
                calls.append(name)
                return 0

            return command

        def backup(_args: argparse.Namespace) -> int:
            calls.append("backup")
            return next(backup_results)

        with (
            patch.object(
                atlas,
                "publication_backup_bootstrap_readiness",
                return_value={
                    "ready": False,
                    "reasons": ["existing backup is outside the configured freshness window"],
                    "evidence": {"age_hours": 48.0},
                },
            ),
            patch.object(atlas, "publication_core_cycle_readiness", return_value=ready_core_gate()),
            patch.object(atlas, "publication_staged_candidate_readiness", return_value=ready_candidate()),
            patch.object(atlas, "publication_gate_artifact_readiness", return_value=ready_artifact_gates()),
            patch.object(atlas, "command_improvements", side_effect=record("improvements")),
            patch.object(atlas, "command_heal", side_effect=record("self_healing")),
            patch.object(atlas, "command_alerts", side_effect=record("alerts")),
            patch.object(atlas, "command_backup", side_effect=backup),
            patch.object(
                atlas,
                "command_retry_staged_publication",
                side_effect=lambda _date: calls.append("retry_staged_candidate") or 0,
            ),
        ):
            result = atlas.run_post_gate_publication("2026-07-18", initiated_by="test")

        self.assertEqual(
            calls,
            [
                "backup",
                "improvements",
                "self_healing",
                "alerts",
                "improvements",
                "alerts",
                "backup",
                "retry_staged_candidate",
            ],
        )
        self.assertTrue(result["backup_bootstrap"]["required"])
        self.assertTrue(result["backup_bootstrap"]["attempted"])
        self.assertEqual(result["backup_bootstrap"]["status"], "passed")
        self.assertFalse(result["stages"][0]["final_publication_evidence"])
        self.assertEqual(result["status"], "frozen_or_pending_deployment")

    def test_post_gate_block_does_not_call_site_retry_but_keeps_collecting_independent_evidence(self) -> None:
        calls: list[str] = []
        gates = ready_artifact_gates()
        gates["ready"] = False
        gates["blockers"] = ["date-aligned deep self-healing is blocking"]
        gates["checks"]["self_healing"] = {
            **ready_gate_check(),
            "ready": False,
            "blockers": ["date-aligned deep self-healing is blocking"],
        }

        def record(name: str):
            return lambda _args: calls.append(name) or 0

        with (
            patch.object(atlas, "publication_core_cycle_readiness", return_value=ready_core_gate()),
            patch.object(atlas, "publication_staged_candidate_readiness", return_value=ready_candidate()),
            patch.object(atlas, "publication_gate_artifact_readiness", return_value=gates),
            patch.object(atlas, "command_improvements", side_effect=record("improvements")),
            patch.object(atlas, "command_heal", side_effect=record("self_healing")),
            patch.object(atlas, "command_alerts", side_effect=record("alerts")),
            patch.object(atlas, "command_backup", side_effect=record("backup")),
            patch.object(atlas, "command_retry_staged_publication") as retry,
        ):
            result = atlas.run_post_gate_publication("2026-07-18", initiated_by="test")

        self.assertEqual(
            calls,
            ["improvements", "self_healing", "alerts", "improvements", "alerts", "backup"],
        )
        retry.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["returncode"], atlas.PUBLICATION_BLOCKED_RETURN_CODE)
        self.assertIn("date-aligned deep self-healing is blocking", result["blocking_reasons"])

    def test_missing_candidate_stops_before_any_control_side_effect(self) -> None:
        candidate = {
            "ready": False,
            "returncode": atlas.PUBLICATION_NO_CANDIDATE_RETURN_CODE,
            "reason": "no staged publication candidate exists for the requested date",
            "evidence": {"present": False},
        }
        with (
            patch.object(atlas, "publication_core_cycle_readiness", return_value=ready_core_gate()),
            patch.object(atlas, "publication_staged_candidate_readiness", return_value=candidate),
            patch.object(atlas, "command_improvements") as improvements,
            patch.object(atlas, "command_heal") as healing,
            patch.object(atlas, "command_alerts") as alerts,
            patch.object(atlas, "command_backup") as backup,
            patch.object(atlas, "command_retry_staged_publication") as retry,
        ):
            result = atlas.run_post_gate_publication("2026-07-18", initiated_by="test")

        self.assertEqual(result["status"], "no_candidate")
        self.assertEqual(result["returncode"], atlas.PUBLICATION_NO_CANDIDATE_RETURN_CODE)
        improvements.assert_not_called()
        healing.assert_not_called()
        alerts.assert_not_called()
        backup.assert_not_called()
        retry.assert_not_called()

    def test_current_strict_gate_failure_is_blocked_but_missing_current_evidence_is_an_error(self) -> None:
        gates = ready_artifact_gates()
        gates["ready"] = False
        gates["blockers"] = ["date-aligned deep self-healing is blocking"]
        gates["checks"]["self_healing"] = {
            **ready_gate_check(),
            "ready": False,
            "blockers": ["date-aligned deep self-healing is blocking"],
        }

        with (
            patch.object(atlas, "publication_core_cycle_readiness", return_value=ready_core_gate()),
            patch.object(atlas, "publication_staged_candidate_readiness", return_value=ready_candidate()),
            patch.object(atlas, "publication_gate_artifact_readiness", return_value=gates),
            patch.object(atlas, "command_improvements", return_value=0),
            patch.object(atlas, "command_heal", return_value=1),
            patch.object(atlas, "command_alerts", return_value=0),
            patch.object(atlas, "command_backup", return_value=0),
            patch.object(atlas, "command_retry_staged_publication") as retry,
        ):
            result = atlas.run_post_gate_publication("2026-07-18", initiated_by="test")

        retry.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["returncode"], atlas.PUBLICATION_BLOCKED_RETURN_CODE)

        stale_gates = ready_artifact_gates()
        stale_gates["ready"] = False
        stale_gates["blockers"] = ["date-aligned encrypted backup is missing"]
        stale_gates["checks"]["backup"] = {
            **ready_gate_check(),
            "ready": False,
            "blockers": ["date-aligned encrypted backup is missing"],
            "artifact": {**ready_gate_check()["artifact"], "date": "2026-07-17"},
        }
        with (
            patch.object(atlas, "publication_core_cycle_readiness", return_value=ready_core_gate()),
            patch.object(atlas, "publication_staged_candidate_readiness", return_value=ready_candidate()),
            patch.object(atlas, "publication_gate_artifact_readiness", return_value=stale_gates),
            patch.object(atlas, "command_improvements", return_value=0),
            patch.object(atlas, "command_heal", return_value=0),
            patch.object(atlas, "command_alerts", return_value=0),
            patch.object(atlas, "command_backup", return_value=1),
            patch.object(atlas, "command_retry_staged_publication") as retry,
        ):
            stale_result = atlas.run_post_gate_publication("2026-07-18", initiated_by="test")

        retry.assert_not_called()
        self.assertEqual(stale_result["status"], "error")
        self.assertEqual(stale_result["returncode"], atlas.PUBLICATION_ERROR_RETURN_CODE)
        self.assertIn(
            "backup control command failed without a date-aligned artifact",
            stale_result["errors"],
        )

    def test_artifact_preflight_requires_deep_strict_healing_and_authenticated_backup(self) -> None:
        paths = atlas.publication_gate_artifact_paths()
        write_json(paths["improvements"], {"date": "2026-07-18", "counts": {"blocking": 0}})
        write_json(
            paths["self_healing"],
            {
                "date": "2026-07-18",
                "deep": True,
                "strict": True,
                "overall_status": "healthy",
                "counts": {"blocking": 0, "failed": 0, "unresolved": 0},
                "checks": [
                    {"check_id": "briefing_tests", "executed": True, "passed": True},
                    {"check_id": "site_quality", "executed": True, "passed": True},
                ],
            },
        )
        write_json(paths["alerts"], {"date": "2026-07-18", "status": "healthy"})
        write_json(
            paths["backup"],
            {
                "date": "2026-07-18",
                "schema_version": 4,
                "verified": True,
                "encrypted": True,
                "encryption_algorithm": "AES-256-GCM",
                "encrypted_container_authenticated": True,
                "archive_integrity_verified": True,
                "restore_verified": True,
                "restore_scope": "configured_workspace_files_and_git_bundles",
                "target_outside_workspace": True,
            },
        )

        passed = atlas.publication_gate_artifact_readiness("2026-07-18")
        self.assertTrue(passed["ready"])

        write_json(
            paths["alerts"],
            {
                "date": "2026-07-18",
                "status": "attention_required",
                "finding_count": 1,
                "findings": [
                    {
                        "id": "ATLAS-IMP-NONBLOCKING",
                        "severity": "medium",
                        "status": "regressed",
                        "summary": "External delivery receipt remains unavailable",
                    }
                ],
            },
        )
        attention = atlas.publication_gate_artifact_readiness("2026-07-18")
        self.assertTrue(attention["ready"])

        alert_payload = json.loads(paths["alerts"].read_text(encoding="utf-8"))
        alert_payload["findings"][0]["severity"] = "high"
        write_json(paths["alerts"], alert_payload)
        high = atlas.publication_gate_artifact_readiness("2026-07-18")
        self.assertFalse(high["ready"])
        self.assertIn(
            "date-aligned alerts are missing or still require attention",
            high["blockers"],
        )

        alert_payload["findings"][0]["severity"] = "medium"
        alert_payload["finding_count"] = 2
        write_json(paths["alerts"], alert_payload)
        malformed = atlas.publication_gate_artifact_readiness("2026-07-18")
        self.assertFalse(malformed["ready"])

        write_json(paths["self_healing"], {"date": "2026-07-18", "deep": False})
        blocked = atlas.publication_gate_artifact_readiness("2026-07-18")
        self.assertFalse(blocked["ready"])
        self.assertIn("date-aligned self-healing was not run with deep and strict gates", blocked["blockers"])
        self.assertTrue(blocked["errors"])

    def test_cli_defaults_to_post_gate_publication_with_an_explicit_escape_hatch(self) -> None:
        parser = atlas.build_parser()
        default_cycle = parser.parse_args(["cycle", "--date", "2026-07-18"])
        skipped_cycle = parser.parse_args(
            ["cycle", "--date", "2026-07-18", "--skip-publication"]
        )
        self.assertFalse(default_cycle.skip_publication)
        self.assertTrue(skipped_cycle.skip_publication)

    def test_retry_adapter_uses_the_narrow_staged_candidate_command(self) -> None:
        with patch.object(atlas, "run_command", return_value=3) as run:
            self.assertEqual(
                atlas.command_retry_staged_publication("2026-07-18"),
                atlas.PUBLICATION_BLOCKED_RETURN_CODE,
            )
        command = run.call_args.args[0]
        self.assertEqual(command[-3:], ["--retry-staged-candidate", "--date", "2026-07-18"])
        self.assertIn("sync_briefing_site.py", command[1])

    def test_default_cli_cycle_returns_blocked_phase_b_without_rewriting_core_success(self) -> None:
        root = Path(self.tmp.name) / "cycle-root"
        runtime = root / "work" / "shared" / "atlas"
        ledger_root = runtime / "virtual_execution"
        audit = {
            "overall_passed": True,
            "blocking_reasons": [],
            "boundary": {"single_canonical_virtual_ledger": True},
        }

        def ledger_result(*, write_files: bool) -> dict:
            return {
                "ledger_path": str(ledger_root / "ledger.jsonl"),
                "state_path": str(ledger_root / "state.json"),
                "audit_path": str(ledger_root / "audit.json"),
                "event_count": 0,
                "account_count": 0,
                "content_hash": "c" * 64,
                "anchor_path": None,
                "anchor_sha256": None,
                "write_requested": write_files,
                "write_performed": write_files,
                "audit": audit,
            }

        replay = {
            "overall_passed": True,
            "safety_gate_passed": True,
            "strategy_evidence_passed": False,
            "replay": {"passed": True, "strategy_evidence_passed": False},
            "shadow_promotion_gate": {"passed": True, "evidence_passed": False},
        }
        history = {
            "passed": True,
            "errors": [],
            "next_sequence": 1,
            "last_audit_sha256": None,
            "legacy_record_count": 0,
            "chained_record_count": 0,
            "anchor_present": False,
            "anchor_path": str(root / "external-anchor.json"),
        }
        publication = {
            "status": "blocked",
            "returncode": atlas.PUBLICATION_BLOCKED_RETURN_CODE,
            "audit_path": str(runtime / "publication_orchestration" / "result.json"),
            "phase_a_reexecuted": False,
        }
        parser = atlas.build_parser()
        args = parser.parse_args(
            ["cycle", "--date", "2026-07-18", "--skip-site", "--skip-trading-core"]
        )
        with (
            patch.multiple(
                atlas,
                ROOT=root,
                BRIEFING_ROOT=root / "work" / "global-briefing",
                TRADING_ROOT=root / "work" / "trading-core",
                SITE_ROOT=root / "src",
                OUTPUTS_ROOT=root / "outputs",
                ATLAS_RUNTIME_ROOT=runtime,
                VIRTUAL_LEDGER_ROOT=ledger_root,
                VIRTUAL_LEDGER_PATH=ledger_root / "ledger.jsonl",
                VIRTUAL_LEDGER_STATE_PATH=ledger_root / "state.json",
                VIRTUAL_LEDGER_AUDIT_PATH=ledger_root / "audit.json",
                RUN_AUDIT_ROOT=runtime / "run_audits",
                CYCLE_STATE_PATH=runtime / "cycle_state.json",
            ),
            patch.object(atlas, "cycle_lock", side_effect=lambda _label: contextlib.nullcontext()),
            patch.object(atlas, "doctor_checks", return_value=[atlas.Check("Python", "ok", "3.12")]),
            patch.object(atlas, "command_sync", return_value=0),
            patch.object(atlas, "build_virtual_execution_ledger", side_effect=ledger_result),
            patch.object(atlas, "run_replay_shadow_validation", return_value=replay),
            patch.object(atlas, "command_test", return_value=0),
            patch.object(atlas, "audit_cycle_history", return_value=history),
            patch.object(atlas, "write_cycle_history_anchor", return_value={}),
            patch.object(
                atlas,
                "audit_global_cycle_history",
                return_value={
                    "passed": True,
                    "errors": [],
                    "anchor_present": True,
                    "migration_required": False,
                    "append_pending": False,
                },
            ),
            patch.object(atlas, "write_global_cycle_history_anchor", return_value={}),
            patch.object(
                atlas,
                "build_cycle_fingerprint",
                return_value={"fingerprint": "f" * 64, "files": []},
            ),
            patch.object(
                atlas,
                "build_workspace_lock",
                return_value={"content_sha256": "w" * 64, "release_reproducible": False, "repositories": []},
            ),
            patch.object(atlas, "run_post_gate_publication", return_value=publication) as post_gate,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                atlas.command_cycle(args),
                atlas.PUBLICATION_BLOCKED_RETURN_CODE,
            )

        post_gate.assert_called_once_with("2026-07-18", initiated_by="atlas cycle")
        cycle_audit = json.loads(
            (runtime / "run_audits" / "atlas-cycle-2026-07-18.json").read_text(encoding="utf-8")
        )
        self.assertTrue(cycle_audit["overall_passed"])
        self.assertEqual(cycle_audit["publication"]["status"], "awaiting_post_audit_orchestration")
        cycle_state = json.loads((runtime / "cycle_state.json").read_text(encoding="utf-8"))
        self.assertEqual(cycle_state["publication"]["status"], "blocked")
        self.assertFalse(cycle_state["publication"]["phase_a_reexecuted"])


if __name__ == "__main__":
    unittest.main()
