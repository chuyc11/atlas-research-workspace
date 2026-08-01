from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "site_deployment_receipt_test_module",
    ROOT / "work" / "global-briefing" / "scripts" / "sync_briefing_site.py",
)
assert SPEC and SPEC.loader
SITE_SYNC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SITE_SYNC
SPEC.loader.exec_module(SITE_SYNC)


def valid_receipt(manifest_sha256: str, content_hash: str, deployment_url: str) -> dict:
    return {
        "schema_version": SITE_SYNC.DEPLOYMENT_RECEIPT_SCHEMA_VERSION,
        "status": "succeeded",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_id": SITE_SYNC.SITES_PROJECT_ID,
        "deployment_url": deployment_url,
        "source_base_commit": "1" * 40,
        "published_commit": "2" * 40,
        "artifact_sha256": "3" * 64,
        "artifact_size_bytes": 1234,
        "sites_version_id": f"{SITE_SYNC.SITES_PROJECT_ID}~appgver_00885d0976e881919aecd23fd48fefd8",
        "sites_deployment_id": "appgdep_6a6e1454ff3881919f909d3f5d783a2e",
        "publication_manifest_sha256": manifest_sha256,
        "content_hash": content_hash,
    }


def test_deployment_receipt_binds_build_commit_artifact_and_platform_identity(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    receipt = valid_receipt("a" * 64, "b" * 64, SITE_SYNC.SITES_PRODUCTION_URL)
    path.write_text(json.dumps(receipt), encoding="utf-8")

    errors, loaded, raw = SITE_SYNC.deployment_receipt_errors(
        path,
        expected_manifest_sha256="a" * 64,
        expected_content_hash="b" * 64,
        deployment_url=SITE_SYNC.SITES_PRODUCTION_URL,
    )

    assert errors == []
    assert loaded == receipt
    assert json.loads(raw) == receipt


def test_deployment_receipt_rejects_self_declared_or_mismatched_identity(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    receipt = valid_receipt("a" * 64, "b" * 64, SITE_SYNC.SITES_PRODUCTION_URL)
    receipt["published_commit"] = receipt["source_base_commit"]
    receipt["artifact_sha256"] = "not-a-digest"
    receipt["project_id"] = "other-project"
    receipt["content_hash"] = "c" * 64
    path.write_text(json.dumps(receipt), encoding="utf-8")

    errors, _, _ = SITE_SYNC.deployment_receipt_errors(
        path,
        expected_manifest_sha256="a" * 64,
        expected_content_hash="b" * 64,
        deployment_url=SITE_SYNC.SITES_PRODUCTION_URL,
    )

    assert any("project" in error for error in errors)
    assert any("published commit" in error for error in errors)
    assert any("artifact" in error for error in errors)
    assert any("content hash" in error for error in errors)


def test_deployment_receipt_rejects_unknown_fields_and_nonproduction_url(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    receipt = valid_receipt("a" * 64, "b" * 64, "https://attacker.example")
    receipt["access_token"] = "must-not-be-archived"
    path.write_text(json.dumps(receipt), encoding="utf-8")

    errors, _, _ = SITE_SYNC.deployment_receipt_errors(
        path,
        expected_manifest_sha256="a" * 64,
        expected_content_hash="b" * 64,
        deployment_url="https://attacker.example",
    )

    assert any("exact credential-free schema" in error for error in errors)
    assert any("production URL" in error for error in errors)
