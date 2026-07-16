from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_test_module", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DR = load("disaster_recovery")
ALERTS = load("alert_dispatch")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class ResilienceControlTests(unittest.TestCase):
    def test_backup_configuration_and_file_selection_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid_json = root / "array.json"
            invalid_json.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected a JSON object"):
                DR.read_json(invalid_json)

            disabled = root / "disabled.json"
            write_json(disabled, {"disaster_recovery": {"enabled": False}})
            with self.assertRaisesRegex(ValueError, "must be enabled"):
                DR.load_config(disabled)

            source = root / "work" / "global-briefing" / "source.txt"
            source.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            scratch = root / "work" / "global-briefing" / "tmp" / "scratch.txt"
            scratch.parent.mkdir(parents=True)
            scratch.write_text("scratch", encoding="utf-8")
            backup_output = root / "work" / "shared" / "atlas" / "backups" / "latest.json"
            backup_output.parent.mkdir(parents=True)
            backup_output.write_text("{}", encoding="utf-8")
            selected = DR.iter_files(root, ["missing", "work"])
            self.assertEqual(selected, [source.resolve()])

    def test_git_bundle_creation_validates_tool_repository_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            with patch.object(DR.shutil, "which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "git executable"):
                    DR.create_git_bundles(root, [], stage)

            with patch.object(DR.shutil, "which", return_value="git"):
                with self.assertRaisesRegex(ValueError, "repository is missing"):
                    DR.create_git_bundles(root, ["missing"], stage)

            (root / ".git").mkdir()
            failed = SimpleNamespace(returncode=1, stdout="", stderr="bundle failed")
            with (
                patch.object(DR.shutil, "which", return_value="git"),
                patch.object(DR.subprocess, "run", return_value=failed),
            ):
                with self.assertRaisesRegex(RuntimeError, "bundle failed"):
                    DR.create_git_bundles(root, ["."], stage)

            def create_bundle(command: list[str], **_kwargs):
                Path(command[5]).write_bytes(b"valid bundle")
                return SimpleNamespace(returncode=0, stdout="created", stderr="")

            with (
                patch.object(DR.shutil, "which", return_value="git"),
                patch.object(DR.subprocess, "run", side_effect=create_bundle),
            ):
                entries = DR.create_git_bundles(root, ["."], stage)
            self.assertEqual(entries[0][0], "_atlas_git_bundles/workspace-root.bundle")
            self.assertEqual(entries[0][2], ".")
            self.assertTrue(entries[0][1].is_file())

    def test_restore_drill_rejects_unsafe_missing_and_modified_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            missing_manifest = root / "missing-manifest.zip"
            with zipfile.ZipFile(missing_manifest, "w") as archive:
                archive.writestr("data.txt", "data")
            passed, errors = DR.verify_archive(missing_manifest, {"files": []})
            self.assertFalse(passed)
            self.assertTrue(any("embedded manifest" in error for error in errors))

            unsafe_manifest = {"files": []}
            unsafe = root / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape.txt", "escape")
                archive.writestr("_atlas_backup_manifest.json", json.dumps(unsafe_manifest))
            passed, errors = DR.verify_archive(unsafe, unsafe_manifest)
            self.assertFalse(passed)
            self.assertTrue(any("unsafe member" in error for error in errors))

            modified_manifest = {
                "files": [{"path": "data.txt", "sha256": "0" * 64, "kind": "workspace_file"}],
            }
            modified = root / "modified.zip"
            with zipfile.ZipFile(modified, "w") as archive:
                archive.writestr("data.txt", "data")
                archive.writestr("_atlas_backup_manifest.json", json.dumps(modified_manifest))
            passed, errors = DR.verify_archive(modified, modified_manifest)
            self.assertFalse(passed)
            self.assertTrue(any("hash mismatch" in error for error in errors))

            missing_file_manifest = {
                "files": [{"path": "absent.txt", "sha256": "0" * 64, "kind": "workspace_file"}],
            }
            missing_file = root / "missing-file.zip"
            with zipfile.ZipFile(missing_file, "w") as archive:
                archive.writestr("_atlas_backup_manifest.json", json.dumps(missing_file_manifest))
            passed, errors = DR.verify_archive(missing_file, missing_file_manifest)
            self.assertFalse(passed)
            self.assertTrue(any("missing: absent.txt" in error for error in errors))

    def test_restore_drill_requires_a_working_git_for_bundle_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "repo.bundle"
            bundle.write_bytes(b"bundle evidence")
            manifest = {
                "files": [{
                    "path": "repo.bundle",
                    "sha256": DR.sha256(bundle),
                    "kind": "git_bundle",
                }],
            }
            archive_path = root / "bundle.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(bundle, "repo.bundle")
                archive.writestr("_atlas_backup_manifest.json", json.dumps(manifest))

            with patch.object(DR.shutil, "which", return_value=None):
                passed, errors = DR.verify_archive(archive_path, manifest)
            self.assertFalse(passed)
            self.assertTrue(any("without git" in error for error in errors))

            completed = SimpleNamespace(returncode=1, stdout="", stderr="invalid")
            with (
                patch.object(DR.shutil, "which", return_value="git"),
                patch.object(DR.subprocess, "run", return_value=completed),
            ):
                passed, errors = DR.verify_archive(archive_path, manifest)
            self.assertFalse(passed)
            self.assertTrue(any("invalid git bundle" in error for error in errors))

    def test_repository_backup_config_covers_uncommitted_first_party_sources(self) -> None:
        config = DR.load_config(DR.CONFIG_PATH)
        includes = set(config["include_paths"])

        self.assertTrue({
            ".github",
            "atlas.py",
            "pyproject.toml",
            "requirements-dev.txt",
            "tests",
            "work/global-briefing",
            "src",
            "work/trading-core",
        }.issubset(includes))
        self.assertEqual(config["git_repositories"], [".", "src", "work/trading-core"])

    def test_backup_is_external_and_restore_verified(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as external:
            root = Path(workspace)
            config = root / "config.json"
            source = root / "work" / "global-briefing" / "data" / "predictions.jsonl"
            source.parent.mkdir(parents=True)
            source.write_text('{"prediction_id":"P1"}\n', encoding="utf-8")
            write_json(config, {"disaster_recovery": {"enabled": True, "target_directory": external, "include_paths": ["work/global-briefing/data"], "retention_days": 14}})
            latest = root / "runtime" / "latest.json"

            result = DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)

            self.assertTrue(result["restore_verified"])
            self.assertTrue(Path(result["archive"]).is_file())
            self.assertFalse(Path(result["archive"]).is_relative_to(root))
            self.assertEqual(result["file_count"], 1)
            self.assertIsNone(result["previous_manifest_sha256"])

            newer = DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)
            self.assertNotEqual(newer["archive"], result["archive"])
            self.assertEqual(newer["previous_manifest_sha256"], result["manifest_sha256"])
            self.assertTrue(Path(result["archive"]).exists())
            self.assertTrue(Path(newer["archive"]).exists())

            Path(newer["manifest_sidecar"]).write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity check failed"):
                DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)

    def test_backup_migrates_verified_v1_snapshot_to_auditable_v2_genesis(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as external:
            root = Path(workspace)
            target = Path(external)
            config = root / "config.json"
            source = root / "source.txt"
            source.write_text("recover me", encoding="utf-8")
            write_json(config, {"disaster_recovery": {
                "enabled": True,
                "target_directory": external,
                "include_paths": ["source.txt"],
            }})
            legacy_archive = target / "atlas-backup-v1.zip"
            legacy_archive.write_bytes(b"legacy snapshot")
            latest = root / "runtime" / "latest.json"
            write_json(latest, {
                "schema_version": 1,
                "date": "2026-07-11",
                "archive": str(legacy_archive),
                "archive_sha256": DR.sha256(legacy_archive),
            })

            result = DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)
            manifest = json.loads(Path(result["manifest_sidecar"]).read_text(encoding="utf-8"))

            self.assertIsNone(result["previous_manifest_sha256"])
            self.assertEqual(result["legacy_previous_archive_sha256"], DR.sha256(legacy_archive))
            self.assertEqual(manifest["legacy_predecessor"]["schema_version"], 1)

            legacy_archive.write_bytes(b"tampered")
            write_json(latest, {
                "schema_version": 1,
                "archive": str(legacy_archive),
                "archive_sha256": "0" * 64,
            })
            with self.assertRaisesRegex(ValueError, "archive integrity check failed"):
                DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)

    def test_backup_rejects_workspace_target(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            config = root / "config.json"
            write_json(config, {"disaster_recovery": {"enabled": True, "target_directory": str(root / "backups")}})
            with self.assertRaisesRegex(ValueError, "outside the workspace"):
                DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=root / "latest.json")

    def test_alert_payload_routes_open_actions_to_task_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            config = root / "config.json"
            runtime = root / "runtime"
            write_json(config, {"external_alerting": {"enabled": True, "destinations": ["codex_task_inbox"]}})
            write_json(runtime / "improvements" / "latest.json", {"actions": [{"action_id": "A1", "status": "regressed", "spec": {"severity": "high", "title": "source health"}, "last_evaluation": {"summary": "42/100"}}]})

            payload = ALERTS.build_alert("2026-07-12", root=root, config_path=config, runtime_root=runtime)

            self.assertEqual(payload["status"], "attention_required")
            self.assertEqual(payload["findings"][0]["id"], "A1")
            self.assertTrue(payload["requires_acknowledgement"])
            self.assertEqual(payload["delivery_state"], "pending")
            self.assertTrue((runtime / "alerts" / "latest.json").is_file())

            retried = ALERTS.retry_alert("2026-07-12", runtime_root=runtime)
            acknowledged = ALERTS.acknowledge_alert("2026-07-12", "operator", runtime_root=runtime)
            self.assertEqual(len(retried["delivery_attempts"]), 2)
            self.assertEqual(acknowledged["delivery_state"], "acknowledged")

    def test_alert_payload_includes_current_self_healing_issue_schema(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            config = root / "config.json"
            runtime = root / "runtime"
            write_json(config, {"external_alerting": {"enabled": True, "destinations": ["codex_task_inbox"]}})
            write_json(runtime / "self_healing" / "latest.json", {"unresolved_issues": [{
                "issue_id": "HEAL-1",
                "status": "requires_approval",
                "severity": "critical",
                "title": "deployment integrity",
                "summary": "payload mismatch",
            }]})

            payload = ALERTS.build_alert("2026-07-12", root=root, config_path=config, runtime_root=runtime)

            self.assertEqual(payload["status"], "attention_required")
            self.assertEqual(payload["findings"][0]["kind"], "self_healing")
            self.assertEqual(payload["findings"][0]["id"], "HEAL-1")


if __name__ == "__main__":
    unittest.main()
