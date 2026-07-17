from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
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


def valid_manifest(root: Path, files: list[dict], *, key: bytes | None = None) -> dict:
    payload = {
        "schema_version": DR.MANIFEST_SCHEMA_VERSION,
        "date": "2026-07-12",
        "created_at": "2026-07-12T00:00:00Z",
        "workspace": str(root.resolve()),
        "encrypted": key is not None,
        "encryption_algorithm": "AES-256-GCM" if key is not None else None,
        "restore_scope": DR.RESTORE_SCOPE,
        "full_runtime_restore_expected": False,
        "excluded_sensitive_file_patterns": ["*.key"],
        "previous_manifest_sha256": None,
        "files": files,
    }
    return DR.signed_metadata(payload, key, "manifest")


class ResilienceControlTests(unittest.TestCase):
    def test_backup_configuration_and_file_selection_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid_json = root / "array.json"
            invalid_json.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected a JSON object"):
                DR.read_json(invalid_json)
            duplicate_json = root / "duplicate.json"
            duplicate_json.write_text('{"value":1,"value":2}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                DR.read_json(duplicate_json)

            disabled = root / "disabled.json"
            write_json(disabled, {"disaster_recovery": {"enabled": False}})
            with self.assertRaisesRegex(ValueError, "must be enabled"):
                DR.load_config(disabled)
            malformed_encryption = root / "malformed-encryption.json"
            write_json(malformed_encryption, {"disaster_recovery": {
                "enabled": True,
                "target_directory": str(root / "external"),
                "encryption": {"required": "true"},
            }})
            with self.assertRaisesRegex(ValueError, "encryption.required must be boolean"):
                DR.load_config(malformed_encryption)

            source = root / "work" / "global-briefing" / "source.txt"
            source.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            (source.parent / ".env").write_text("SAFE_FIXTURE=value", encoding="utf-8")
            (source.parent / "service-account-prod.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (source.parent / ".npmrc").write_text("//registry/:_authToken=fixture", encoding="utf-8")
            (source.parent / ".git-credentials").write_text("fixture", encoding="utf-8")
            (source.parent / "client_secret-prod.json").write_text("{}", encoding="utf-8")
            secrets_dir = source.parent / "secrets"
            secrets_dir.mkdir()
            (secrets_dir / "runtime.txt").write_text("fixture", encoding="utf-8")
            kube_dir = source.parent / ".kube"
            kube_dir.mkdir()
            (kube_dir / "config").write_text("fixture", encoding="utf-8")
            scratch = root / "work" / "global-briefing" / "tmp" / "scratch.txt"
            scratch.parent.mkdir(parents=True)
            scratch.write_text("scratch", encoding="utf-8")
            backup_output = root / "work" / "shared" / "atlas" / "backups" / "latest.json"
            backup_output.parent.mkdir(parents=True)
            backup_output.write_text("{}", encoding="utf-8")
            selected = DR.iter_files(root, ["missing", "work"])
            self.assertEqual(selected, [source.resolve()])

    def test_encrypted_backup_requires_external_key_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as external:
            root = Path(workspace)
            target = Path(external)
            source = root / "source.txt"
            source.write_text("recoverable state", encoding="utf-8")
            config = root / "config.json"
            key_variable = "ATLAS_TEST_BACKUP_KEY"
            write_json(
                config,
                {
                    "disaster_recovery": {
                        "enabled": True,
                        "target_directory": external,
                        "include_paths": ["source.txt"],
                        "git_repositories": [],
                        "encryption": {
                            "required": True,
                            "algorithm": "AES-256-GCM",
                            "key_environment_variable": key_variable,
                            "key_encoding": "base64",
                        },
                    }
                },
            )
            latest = root / "runtime" / "latest.json"

            with patch.dict(DR.os.environ, {key_variable: ""}, clear=False):
                with self.assertRaisesRegex(ValueError, "environment variable is not set"):
                    DR.create_snapshot(
                        date="2026-07-12",
                        root=root,
                        config_path=config,
                        latest_path=latest,
                    )

            key = b"atlas-test-backup-key-material!!"[:32]
            encoded_key = DR.base64.b64encode(key).decode("ascii")
            with patch.dict(DR.os.environ, {"CUSTOM_DR_MATERIAL": encoded_key}, clear=False):
                self.assertNotIn(
                    "CUSTOM_DR_MATERIAL",
                    DR.sanitized_subprocess_environment(encryption_key=key),
                )
            with patch.dict(DR.os.environ, {key_variable: encoded_key}, clear=False):
                result = DR.create_snapshot(
                    date="2026-07-12",
                    root=root,
                    config_path=config,
                    latest_path=latest,
                )

            archive = Path(result["archive"])
            manifest = DR.read_json(Path(result["manifest_sidecar"]))
            latest_payload = DR.read_json(latest)
            self.assertEqual(archive.suffix, ".atlasdr")
            self.assertTrue(result["encrypted"])
            self.assertTrue(result["encrypted_container_authenticated"])
            self.assertFalse(result["full_runtime_restore_verified"])
            self.assertEqual(result["schema_version"], DR.MANIFEST_SCHEMA_VERSION)
            self.assertEqual(
                latest_payload["metadata_authentication"]["algorithm"],
                DR.METADATA_AUTHENTICATION_ALGORITHM,
            )
            self.assertNotEqual(
                latest_payload["metadata_authentication"]["value"],
                manifest["metadata_authentication"]["value"],
            )
            with self.assertRaises(zipfile.BadZipFile):
                zipfile.ZipFile(archive).close()
            without_key, errors = DR.verify_archive(archive, manifest)
            self.assertFalse(without_key)
            self.assertTrue(any("requires the encryption key" in error for error in errors))
            verified, errors = DR.verify_archive(archive, manifest, encryption_key=key)
            self.assertTrue(verified, errors)

            forged_manifest = json.loads(json.dumps(manifest))
            forged_manifest["metadata_authentication"]["value"] = "0" * 64
            verified, errors = DR.verify_archive(archive, forged_manifest, encryption_key=key)
            self.assertFalse(verified)
            self.assertTrue(any("manifest metadata authentication failed" in error for error in errors))

            tampered = target / "tampered.atlasdr"
            DR.shutil.copyfile(archive, tampered)
            payload = bytearray(tampered.read_bytes())
            payload[-1] ^= 1
            tampered.write_bytes(payload)
            verified, errors = DR.verify_archive(tampered, manifest, encryption_key=key)
            self.assertFalse(verified)
            self.assertTrue(any("authentication failed" in error for error in errors))

            with patch.dict(DR.os.environ, {key_variable: encoded_key}, clear=False):
                chained = DR.create_snapshot(
                    date="2026-07-12",
                    root=root,
                    config_path=config,
                    latest_path=latest,
                )
            self.assertEqual(chained["previous_manifest_sha256"], result["manifest_sha256"])
            self.assertFalse(any(path.name.startswith(".atlas-") for path in target.iterdir()))

            forged_latest = DR.read_json(latest)
            forged_latest["archive_sha256"] = "0" * 64
            write_json(latest, forged_latest)
            with patch.dict(DR.os.environ, {key_variable: encoded_key}, clear=False):
                with self.assertRaisesRegex(ValueError, "latest metadata authentication failed"):
                    DR.create_snapshot(
                        date="2026-07-12",
                        root=root,
                        config_path=config,
                        latest_path=latest,
                    )

    def test_encrypted_backup_plaintext_staging_stays_in_private_target_and_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as external:
            root = Path(workspace)
            target = Path(external).resolve()
            (root / "state.txt").write_text("recoverable", encoding="utf-8")
            config = root / "config.json"
            key_variable = "ATLAS_TEST_PRIVATE_STAGING_KEY"
            write_json(config, {"disaster_recovery": {
                "enabled": True,
                "target_directory": str(target),
                "include_paths": ["state.txt"],
                "git_repositories": [],
                "encryption": {
                    "required": True,
                    "algorithm": "AES-256-GCM",
                    "key_environment_variable": key_variable,
                },
            }})
            key = DR.base64.b64encode(b"P" * 32).decode("ascii")
            original_mkdtemp = DR.tempfile.mkdtemp
            staging_parents: list[Path] = []

            def tracked_mkdtemp(*args, **kwargs):
                staging_parents.append(Path(kwargs["dir"]).resolve())
                return original_mkdtemp(*args, **kwargs)

            with (
                patch.dict(DR.os.environ, {key_variable: key}, clear=False),
                patch.object(DR.tempfile, "mkdtemp", side_effect=tracked_mkdtemp),
            ):
                result = DR.create_snapshot(
                    date="2026-07-12",
                    root=root,
                    config_path=config,
                    latest_path=root / "latest.json",
                )
            self.assertTrue(result["restore_verified"])
            self.assertGreaterEqual(len(staging_parents), 2)
            self.assertEqual(set(staging_parents), {target})
            self.assertFalse(any(path.name.startswith(".atlas-") for path in target.iterdir()))

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

            seen_environments: list[dict[str, str]] = []

            def create_bundle(command: list[str], **kwargs):
                Path(command[5]).write_bytes(b"valid bundle")
                seen_environments.append(kwargs["env"])
                return SimpleNamespace(returncode=0, stdout="created", stderr="")

            with (
                patch.dict(DR.os.environ, {"ATLAS_TEST_BACKUP_KEY": "must-not-leak"}, clear=False),
                patch.object(DR.shutil, "which", return_value="git"),
                patch.object(DR.subprocess, "run", side_effect=create_bundle),
            ):
                entries = DR.create_git_bundles(
                    root,
                    ["."],
                    stage,
                    secret_environment_variables=["ATLAS_TEST_BACKUP_KEY"],
                )
            self.assertEqual(entries[0][0], "_atlas_git_bundles/workspace-root.bundle")
            self.assertEqual(entries[0][2], ".")
            self.assertTrue(entries[0][1].is_file())
            self.assertNotIn("ATLAS_TEST_BACKUP_KEY", seen_environments[0])

    def test_restore_drill_rejects_unsafe_missing_and_modified_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data.txt"
            data.write_text("data", encoding="utf-8")
            data_item = {
                "path": "data.txt",
                "size": data.stat().st_size,
                "sha256": DR.sha256(data),
                "kind": "workspace_file",
            }

            missing_manifest = root / "missing-manifest.zip"
            with zipfile.ZipFile(missing_manifest, "w") as archive:
                archive.writestr("data.txt", "data")
            missing_manifest_payload = valid_manifest(root, [data_item])
            passed, errors = DR.verify_archive(missing_manifest, missing_manifest_payload)
            self.assertFalse(passed)
            self.assertTrue(any("missing archive members" in error for error in errors))

            unsafe_manifest = valid_manifest(root, [data_item])
            unsafe = root / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("data.txt", "data")
                archive.writestr("../escape.txt", "escape")
                archive.writestr(DR.MANIFEST_MEMBER, json.dumps(unsafe_manifest))
            passed, errors = DR.verify_archive(unsafe, unsafe_manifest)
            self.assertFalse(passed)
            self.assertTrue(any("unsafe member" in error for error in errors))

            modified_manifest = valid_manifest(root, [{**data_item, "sha256": "0" * 64}])
            modified = root / "modified.zip"
            with zipfile.ZipFile(modified, "w") as archive:
                archive.writestr("data.txt", "data")
                archive.writestr(DR.MANIFEST_MEMBER, json.dumps(modified_manifest))
            passed, errors = DR.verify_archive(modified, modified_manifest)
            self.assertFalse(passed)
            self.assertTrue(any("hash mismatch" in error for error in errors))

            missing_file_manifest = valid_manifest(root, [{
                "path": "absent.txt",
                "size": 0,
                "sha256": "0" * 64,
                "kind": "workspace_file",
            }])
            missing_file = root / "missing-file.zip"
            with zipfile.ZipFile(missing_file, "w") as archive:
                archive.writestr(DR.MANIFEST_MEMBER, json.dumps(missing_file_manifest))
            passed, errors = DR.verify_archive(missing_file, missing_file_manifest)
            self.assertFalse(passed)
            self.assertTrue(any("missing archive members" in error for error in errors))

            extra = root / "extra.zip"
            with zipfile.ZipFile(extra, "w") as archive:
                archive.writestr("data.txt", "data")
                archive.writestr("untracked.txt", "surprise")
                archive.writestr(DR.MANIFEST_MEMBER, json.dumps(missing_manifest_payload))
            passed, errors = DR.verify_archive(extra, missing_manifest_payload)
            self.assertFalse(passed)
            self.assertTrue(any("unexpected archive members" in error for error in errors))

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("data.txt", "data")
                archive.writestr("DATA.TXT", "data")
                archive.writestr(DR.MANIFEST_MEMBER, json.dumps(missing_manifest_payload))
            passed, errors = DR.verify_archive(duplicate, missing_manifest_payload)
            self.assertFalse(passed)
            self.assertTrue(any("duplicate archive member" in error for error in errors))

            with self.assertRaisesRegex(ValueError, "non-empty list"):
                DR.validate_manifest(valid_manifest(root, []))
            with self.assertRaisesRegex(ValueError, "duplicate path"):
                DR.validate_manifest(valid_manifest(root, [data_item, dict(data_item)]))
            with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                DR.validate_manifest(valid_manifest(root, [{**data_item, "path": "../data.txt"}]))

    def test_restore_drill_requires_a_working_git_for_bundle_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "repo.bundle"
            bundle.write_bytes(b"bundle evidence")
            manifest = valid_manifest(root, [{
                    "path": "repo.bundle",
                    "size": bundle.stat().st_size,
                    "sha256": DR.sha256(bundle),
                    "kind": "git_bundle",
                    "repository": ".",
                }])
            archive_path = root / "bundle.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(bundle, "repo.bundle")
                archive.writestr(DR.MANIFEST_MEMBER, json.dumps(manifest))

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

    def test_restore_drill_clones_and_fscks_real_git_bundle_without_backup_key(self) -> None:
        git = DR.shutil.which("git")
        if not git:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            for command in (
                [git, "init", str(repository)],
                [git, "-C", str(repository), "config", "user.email", "atlas-tests@example.invalid"],
                [git, "-C", str(repository), "config", "user.name", "ATLAS Tests"],
            ):
                completed = DR.subprocess.run(command, text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
            (repository / "state.txt").write_text("recoverable", encoding="utf-8")
            for command in (
                [git, "-C", str(repository), "add", "state.txt"],
                [git, "-C", str(repository), "commit", "-m", "fixture"],
            ):
                completed = DR.subprocess.run(command, text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
            stage = root / "stage"
            stage.mkdir()
            entries = DR.create_git_bundles(root, ["repository"], stage)
            archive_name, bundle_path, repository_name = entries[0]
            manifest = valid_manifest(root, [{
                "path": archive_name,
                "size": bundle_path.stat().st_size,
                "sha256": DR.sha256(bundle_path),
                "kind": "git_bundle",
                "repository": repository_name,
            }])
            archive_path = root / "bundle.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(bundle_path, archive_name)
                archive.writestr(DR.MANIFEST_MEMBER, json.dumps(manifest))

            original_run = DR.subprocess.run
            observed_environments: list[dict[str, str]] = []

            def recording_run(command, **kwargs):
                observed_environments.append(kwargs["env"])
                return original_run(command, **kwargs)

            with (
                patch.dict(DR.os.environ, {"ATLAS_BACKUP_ENCRYPTION_KEY": "must-not-leak"}, clear=False),
                patch.object(DR.subprocess, "run", side_effect=recording_run),
            ):
                passed, errors = DR.verify_archive(archive_path, manifest)
            self.assertTrue(passed, errors)
            self.assertGreaterEqual(len(observed_environments), 4)
            self.assertTrue(
                all("ATLAS_BACKUP_ENCRYPTION_KEY" not in environment for environment in observed_environments)
            )

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
        self.assertEqual(config["encryption"]["algorithm"], "AES-256-GCM")
        self.assertTrue(config["encryption"]["required"])
        self.assertEqual(
            config["encryption"]["key_environment_variable"],
            "ATLAS_BACKUP_ENCRYPTION_KEY",
        )

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

            with self.assertRaisesRegex(ValueError, "AES-256-GCM encrypted and authenticated"):
                DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)

    def test_backup_rejects_legacy_snapshot_that_cannot_be_authenticated_and_restored(self) -> None:
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

            with self.assertRaisesRegex(ValueError, "predates authenticated schema 4"):
                DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)

            legacy_archive.write_bytes(b"tampered")
            write_json(latest, {
                "schema_version": 1,
                "archive": str(legacy_archive),
                "archive_sha256": "0" * 64,
            })
            with self.assertRaisesRegex(ValueError, "predates authenticated schema 4"):
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
            with self.assertRaisesRegex(ValueError, "before every destination"):
                ALERTS.acknowledge_alert("2026-07-12", "operator", runtime_root=runtime)
            delivered = ALERTS.record_delivery_receipt(
                "2026-07-12",
                "codex_task_inbox",
                "receipt-123",
                runtime_root=runtime,
            )
            with self.assertRaisesRegex(ValueError, "receipt is immutable"):
                ALERTS.record_delivery_receipt(
                    "2026-07-12",
                    "codex_task_inbox",
                    "receipt-replaced",
                    runtime_root=runtime,
                )
            acknowledged = ALERTS.acknowledge_alert("2026-07-12", "operator", runtime_root=runtime)
            self.assertEqual(len(retried["delivery_attempts"]), 2)
            self.assertEqual(delivered["delivery_state"], "delivered")
            self.assertEqual(acknowledged["delivery_state"], "acknowledged")
            channel_health = json.loads(
                (runtime / "alerts" / "channel_health.json").read_text(encoding="utf-8")
            )
            inbox_health = channel_health["destinations"]["codex_task_inbox"]
            self.assertEqual(inbox_health["provider_message_id"], "receipt-123")
            self.assertEqual(inbox_health["source_alert_id"], payload["alert_id"])

            write_json(runtime / "improvements" / "latest.json", {"actions": []})
            healthy = ALERTS.build_alert(
                "2026-07-13",
                root=root,
                config_path=config,
                runtime_root=runtime,
            )
            self.assertEqual(healthy["delivery_state"], "not_required")
            persisted_health = json.loads(
                (runtime / "alerts" / "channel_health.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted_health, channel_health)

    def test_alert_requires_a_receipt_from_every_configured_destination(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            config = root / "config.json"
            runtime = root / "runtime"
            write_json(
                config,
                {
                    "external_alerting": {
                        "enabled": True,
                        "destinations": ["codex_task_inbox", "on_call_webhook"],
                    }
                },
            )
            write_json(
                runtime / "improvements" / "latest.json",
                {
                    "actions": [
                        {
                            "action_id": "A1",
                            "status": "open",
                            "spec": {"severity": "high", "title": "source health"},
                        }
                    ]
                },
            )

            ALERTS.build_alert("2026-07-12", root=root, config_path=config, runtime_root=runtime)
            partial = ALERTS.record_delivery_receipt(
                "2026-07-12",
                "codex_task_inbox",
                "receipt-inbox",
                runtime_root=runtime,
            )
            delivered = ALERTS.record_delivery_receipt(
                "2026-07-12",
                "on_call_webhook",
                "receipt-webhook",
                runtime_root=runtime,
            )

            self.assertEqual(partial["delivery_state"], "pending")
            self.assertEqual(delivered["delivery_state"], "delivered")
            health = json.loads(
                (runtime / "alerts" / "channel_health.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(health["destinations"]),
                {"codex_task_inbox", "on_call_webhook"},
            )

    def test_concurrent_alert_receipts_do_not_lose_an_update(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            config = root / "config.json"
            runtime = root / "runtime"
            write_json(
                config,
                {
                    "external_alerting": {
                        "enabled": True,
                        "destinations": ["destination-a", "destination-b"],
                    }
                },
            )
            write_json(
                runtime / "improvements" / "latest.json",
                {
                    "actions": [
                        {
                            "action_id": "A1",
                            "status": "open",
                            "spec": {"severity": "medium", "title": "channel health"},
                        }
                    ]
                },
            )
            ALERTS.build_alert("2026-07-12", root=root, config_path=config, runtime_root=runtime)

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        ALERTS.record_delivery_receipt,
                        "2026-07-12",
                        destination,
                        f"receipt-{destination}",
                        runtime_root=runtime,
                    )
                    for destination in ("destination-a", "destination-b")
                ]
                for future in futures:
                    future.result()

            payload = json.loads(
                (runtime / "alerts" / "alert-2026-07-12.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["delivery_state"], "delivered")
            self.assertEqual(set(payload["delivery_receipts"]), {"destination-a", "destination-b"})

    def test_alert_payload_includes_cycle_blocking_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            config = root / "config.json"
            runtime = root / "runtime"
            write_json(
                config,
                {"external_alerting": {"enabled": True, "destinations": ["codex_task_inbox"]}},
            )
            write_json(
                runtime / "cycle_state.json",
                {
                    "cycle_id": "cycle-1",
                    "operational_gate_passed": False,
                    "blocking_reasons": ["site synchronization failed"],
                },
            )

            payload = ALERTS.build_alert(
                "2026-07-12",
                root=root,
                config_path=config,
                runtime_root=runtime,
            )

            cycle_finding = next(item for item in payload["findings"] if item["kind"] == "cycle")
            self.assertEqual(cycle_finding["summary"], "site synchronization failed")

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
