from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


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

            newer = DR.create_snapshot(date="2026-07-12", root=root, config_path=config, latest_path=latest)
            self.assertNotEqual(newer["archive"], result["archive"])
            self.assertFalse(Path(result["archive"]).exists())
            self.assertTrue(Path(newer["archive"]).exists())

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
            self.assertTrue((runtime / "alerts" / "latest.json").is_file())

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
