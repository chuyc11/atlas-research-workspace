from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prediction_ledger_repair.py"
SPEC = importlib.util.spec_from_file_location("prediction_ledger_repair_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def encoded(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"


class PredictionLedgerRepairTests(unittest.TestCase):
    RUN_DATE = "2026-07-17"
    AUTHORIZATION = "Authorized historical-ledger normalization based on dated report evidence."

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "predictions.jsonl"
        self.audit_dir = self.root / "audit"
        self.report = self.root / "每日全球晨间简报-2026-06-14.md"
        self.report.write_text("line one\nline two\nline three\n", encoding="utf-8")

        self.original_old = {
            "prediction_id": "P01",
            "date": "2026-06-13",
            "status": "open",
            "scenario": "old",
        }
        self.original_new = {**self.original_old, "scenario": "report-matched"}
        self.review_old = {
            "prediction_id": "P01",
            "date": "2026-06-14",
            "status": "partial",
            "review": {"review_date": "2026-06-14", "why": "old"},
        }
        self.review_new = {
            **self.review_old,
            "review": {"review_date": "2026-06-14", "why": "report-matched"},
        }
        self.unique = {"prediction_id": "P02", "date": "2026-06-14", "status": "open"}
        self.lines = [
            encoded(self.original_old),
            encoded(self.original_old),
            encoded(self.original_new),
            encoded(self.original_new),
            encoded(self.review_old),
            encoded(self.review_old),
            encoded(self.review_new),
            encoded(self.unique),
        ]
        self.original_raw = b"".join(self.lines)
        self.ledger.write_bytes(self.original_raw)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def decision_file(self) -> Path:
        report_sha = hashlib.sha256(self.report.read_bytes()).hexdigest()
        payload = {
            "schema_version": 1,
            "run_date": self.RUN_DATE,
            "decisions": [
                {
                    "identity": {"kind": "original", "prediction_id": "P01"},
                    "keep": {
                        "source_line": 3,
                        "record_sha256": MODULE.record_sha256(self.original_new),
                    },
                    "decision_reason": "The dated report matches this version.",
                    "report_evidence": [
                        {
                            "report_path": str(self.report),
                            "report_sha256": report_sha,
                            "line_numbers": [2, 3],
                            "reason": "Scenario wording is present on the cited report lines.",
                        }
                    ],
                },
                {
                    "identity": {
                        "kind": "review",
                        "prediction_id": "P01",
                        "status": "partial",
                        "review_date": "2026-06-14",
                    },
                    "keep": {
                        "source_line": 7,
                        "record_sha256": MODULE.record_sha256(self.review_new),
                    },
                    "decision_reason": "The dated report matches this review text.",
                    "report_evidence": [
                        {
                            "report_path": str(self.report),
                            "report_sha256": report_sha,
                            "line_numbers": [1],
                            "reason": "Review wording is bound to the dated report.",
                        }
                    ],
                },
            ],
        }
        path = self.root / "decisions.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_conflicting_variants_fail_closed_without_decisions_and_do_not_write(self) -> None:
        with self.assertRaises(MODULE.DecisionRequiredError) as caught:
            MODULE.run_repair(
                mode="dry-run",
                ledger=self.ledger,
                audit_dir=self.audit_dir,
                run_date=self.RUN_DATE,
                authorization_reason=self.AUTHORIZATION,
            )

        self.assertEqual(len(caught.exception.requirements), 2)
        self.assertEqual(self.ledger.read_bytes(), self.original_raw)
        self.assertFalse(self.audit_dir.exists())

    def test_dry_run_is_read_only_and_records_detailed_selection(self) -> None:
        result = MODULE.run_repair(
            mode="dry-run",
            ledger=self.ledger,
            audit_dir=self.audit_dir,
            run_date=self.RUN_DATE,
            authorization_reason=self.AUTHORIZATION,
            decision_path=self.decision_file(),
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["writes_performed"])
        self.assertEqual(result["kept_source_lines"], [3, 7, 8])
        self.assertEqual(result["quarantined_source_lines"], [1, 2, 4, 5, 6])
        self.assertEqual(result["original"]["sha256"], hashlib.sha256(self.original_raw).hexdigest())
        expected_canonical = self.lines[2] + self.lines[6] + self.lines[7]
        self.assertEqual(result["canonical"]["sha256"], hashlib.sha256(expected_canonical).hexdigest())
        self.assertEqual(self.ledger.read_bytes(), self.original_raw)
        self.assertFalse(self.audit_dir.exists())
        by_line = {item["source_line"]: item for item in result["quarantined_records"]}
        self.assertEqual(by_line[1]["classification"], "conflicting_variant")
        self.assertEqual(by_line[2]["classification"], "conflicting_variant_exact_duplicate")
        self.assertEqual(by_line[4]["classification"], "exact_duplicate")
        self.assertEqual(by_line[1]["field_differences"][0]["path"], "/scenario")
        self.assertEqual(by_line[5]["field_differences"][0]["path"], "/review/why")
        self.assertTrue(result["decision_manifest"]["decisions"][0]["report_evidence"][0]["verified"])

    def test_apply_is_atomic_and_writes_immutable_content_addressed_audit(self) -> None:
        decisions = self.decision_file()
        original_sha = hashlib.sha256(self.original_raw).hexdigest()
        preexisting_backup = self.audit_dir / "backups" / f"{original_sha}.jsonl"
        preexisting_backup.parent.mkdir(parents=True)
        preexisting_backup.write_bytes(self.original_raw)
        before_mtime = preexisting_backup.stat().st_mtime_ns

        result = MODULE.run_repair(
            mode="apply",
            ledger=self.ledger,
            audit_dir=self.audit_dir,
            run_date=self.RUN_DATE,
            authorization_reason=self.AUTHORIZATION,
            decision_path=decisions,
            expected_original_sha256=original_sha,
        )

        expected_canonical = self.lines[2] + self.lines[6] + self.lines[7]
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.ledger.read_bytes(), expected_canonical)
        self.assertEqual(preexisting_backup.read_bytes(), self.original_raw)
        self.assertEqual(preexisting_backup.stat().st_mtime_ns, before_mtime)
        self.assertTrue(Path(result["audit_manifest_path"]).is_file())
        self.assertEqual(
            hashlib.sha256(Path(result["audit_manifest_path"]).read_bytes()).hexdigest(),
            result["audit_manifest_sha256"],
        )
        self.assertTrue(all(Path(item["artifact_path"]).is_file() for item in result["quarantined_records"]))
        self.assertTrue(Path(result["decision_manifest"]["backup_path"]).is_file())
        self.assertTrue(result["verification"]["after_apply"]["zero_duplicate_identities"])
        self.assertEqual(result["verification"]["after_apply"]["scan"]["duplicate_identity_groups"], 0)

        second = MODULE.run_repair(
            mode="apply",
            ledger=self.ledger,
            audit_dir=self.audit_dir,
            run_date=self.RUN_DATE,
            authorization_reason=self.AUTHORIZATION,
        )
        self.assertEqual(second["status"], "unchanged")
        self.assertFalse(second["writes_performed"])

    def test_verification_failure_automatically_restores_original(self) -> None:
        def fail_verification(_ledger: Path, _expected: str) -> dict:
            raise MODULE.RepairError("injected post-replacement verification failure")

        with self.assertRaisesRegex(MODULE.RepairError, "original ledger was restored"):
            MODULE.run_repair(
                mode="apply",
                ledger=self.ledger,
                audit_dir=self.audit_dir,
                run_date=self.RUN_DATE,
                authorization_reason=self.AUTHORIZATION,
                decision_path=self.decision_file(),
                verifier=fail_verification,
            )

        self.assertEqual(self.ledger.read_bytes(), self.original_raw)
        manifests = [json.loads(path.read_text(encoding="utf-8")) for path in (self.audit_dir / "manifests").glob("*.json")]
        rolled_back = [item for item in manifests if item["status"] == "rolled_back"]
        self.assertEqual(len(rolled_back), 1)
        self.assertTrue(rolled_back[0]["rollback"]["performed"])
        self.assertTrue(rolled_back[0]["rollback"]["verified_original_sha256"])

    def test_hard_interrupt_after_replace_is_terminalized_on_retry(self) -> None:
        def hard_interrupt(_ledger: Path, _expected: str) -> dict:
            raise KeyboardInterrupt("injected crash after atomic replacement")

        with self.assertRaises(KeyboardInterrupt):
            MODULE.run_repair(
                mode="apply",
                ledger=self.ledger,
                audit_dir=self.audit_dir,
                run_date=self.RUN_DATE,
                authorization_reason=self.AUTHORIZATION,
                decision_path=self.decision_file(),
                verifier=hard_interrupt,
            )

        expected_canonical = self.lines[2] + self.lines[6] + self.lines[7]
        self.assertEqual(self.ledger.read_bytes(), expected_canonical)
        prepared = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.audit_dir / "manifests").glob("*.json")
        ]
        self.assertEqual([item["status"] for item in prepared], ["prepared"])

        recovered = MODULE.run_repair(
            mode="apply",
            ledger=self.ledger,
            audit_dir=self.audit_dir,
            run_date=self.RUN_DATE,
            authorization_reason=self.AUTHORIZATION,
        )

        self.assertEqual(recovered["status"], "recovered_applied")
        self.assertEqual(recovered["recovery"]["resumed_from"], "canonical")
        self.assertFalse(recovered["recovery"]["ledger_replacement_performed"])
        self.assertTrue(Path(recovered["audit_manifest_path"]).is_file())
        self.assertEqual(self.ledger.read_bytes(), expected_canonical)
        terminal = json.loads(Path(recovered["audit_manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(terminal["status"], "recovered_applied")
        self.assertEqual(terminal["prepared_manifest"]["sha256"], Path(terminal["prepared_manifest"]["path"]).stem)

    def test_prepared_original_is_retried_and_unknown_ledger_sha_fails_closed(self) -> None:
        def interrupt_before_replace(_ledger: Path, _payload: bytes) -> None:
            raise KeyboardInterrupt("injected crash before atomic replacement")

        with patch.object(MODULE, "_atomic_replace", side_effect=interrupt_before_replace):
            with self.assertRaises(KeyboardInterrupt):
                MODULE.run_repair(
                    mode="apply",
                    ledger=self.ledger,
                    audit_dir=self.audit_dir,
                    run_date=self.RUN_DATE,
                    authorization_reason=self.AUTHORIZATION,
                    decision_path=self.decision_file(),
                )
        self.assertEqual(self.ledger.read_bytes(), self.original_raw)

        recovered = MODULE.run_repair(
            mode="apply",
            ledger=self.ledger,
            audit_dir=self.audit_dir,
            run_date=self.RUN_DATE,
            authorization_reason=self.AUTHORIZATION,
        )
        self.assertEqual(recovered["status"], "recovered_applied")
        self.assertEqual(recovered["recovery"]["resumed_from"], "original")
        self.assertTrue(recovered["recovery"]["ledger_replacement_performed"])

        other_root = self.root / "unknown-case"
        other_root.mkdir()
        other_ledger = other_root / "predictions.jsonl"
        other_audit = other_root / "audit"
        other_ledger.write_bytes(self.original_raw)
        decisions = self.decision_file()
        with self.assertRaises(KeyboardInterrupt):
            MODULE.run_repair(
                mode="apply",
                ledger=other_ledger,
                audit_dir=other_audit,
                run_date=self.RUN_DATE,
                authorization_reason=self.AUTHORIZATION,
                decision_path=decisions,
                verifier=lambda _path, _sha: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        other_ledger.write_bytes(b'{"prediction_id":"UNKNOWN"}\n')
        with self.assertRaisesRegex(MODULE.RepairError, "neither the prepared original nor canonical"):
            MODULE.run_repair(
                mode="apply",
                ledger=other_ledger,
                audit_dir=other_audit,
                run_date=self.RUN_DATE,
                authorization_reason=self.AUTHORIZATION,
            )

    def test_decision_is_bound_to_source_line_digest_and_evidence_hash(self) -> None:
        decisions = self.decision_file()
        payload = json.loads(decisions.read_text(encoding="utf-8"))
        payload["decisions"][0]["keep"]["record_sha256"] = "0" * 64
        decisions.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.RepairError, "does not bind one matching"):
            MODULE.build_repair_plan(self.ledger, self.RUN_DATE, self.AUTHORIZATION, decisions)

        decisions = self.decision_file()
        payload = json.loads(decisions.read_text(encoding="utf-8"))
        payload["decisions"][0]["report_evidence"][0]["report_sha256"] = "0" * 64
        decisions.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.RepairError, "report SHA mismatch"):
            MODULE.build_repair_plan(self.ledger, self.RUN_DATE, self.AUTHORIZATION, decisions)


if __name__ == "__main__":
    unittest.main()
