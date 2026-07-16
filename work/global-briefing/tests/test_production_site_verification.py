from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_production_site.py"
SPEC = importlib.util.spec_from_file_location("production_site_verification_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ProductionSiteVerificationTests(unittest.TestCase):
    def live_headers(self) -> dict[str, str]:
        return {
            "content-security-policy": "default-src 'self'; script-src 'self' 'nonce-abc123' 'strict-dynamic'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
            "strict-transport-security": "max-age=86400",
            "permissions-policy": "camera=(), microphone=(), geolocation=(), browsing-topics=()",
            "referrer-policy": "strict-origin-when-cross-origin",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
        }

    def test_live_response_requires_exact_version_and_staged_security_headers(self) -> None:
        content_hash = "a" * 64
        body = f'<main data-atlas-report-date="2026-07-16" data-atlas-content-hash="{content_hash}">'
        result = MODULE.evaluate_live_response(
            status=200,
            final_url="https://example.com/",
            headers=self.live_headers(),
            body=body,
            expected_content_hash=content_hash,
            expected_report_date="2026-07-16",
        )
        self.assertTrue(result["passed"])

        unsafe = self.live_headers()
        unsafe["strict-transport-security"] = "max-age=31536000; includeSubDomains; preload"
        rejected = MODULE.evaluate_live_response(
            status=200,
            final_url="https://example.com/",
            headers=unsafe,
            body=body,
            expected_content_hash=content_hash,
            expected_report_date="2026-07-16",
        )
        self.assertFalse(rejected["passed"])
        self.assertTrue(any("includeSubDomains" in error for error in rejected["errors"]))

    def test_runtime_isolation_rejects_database_bindings_and_remote_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".openai").mkdir()
            (root / "app").mkdir()
            (root / "worker").mkdir()
            (root / ".openai" / "hosting.json").write_text(
                json.dumps({"project_id": "project", "d1": None, "r2": None}), encoding="utf-8"
            )
            (root / "app" / "page.tsx").write_text("export default function Page() { return null; }", encoding="utf-8")
            (root / "worker" / "index.ts").write_text(
                "const worker = { async fetch(request: Request, env: Env, ctx: ExecutionContext) { return handler.fetch(request, env, ctx); } };",
                encoding="utf-8",
            )
            self.assertTrue(MODULE.audit_runtime_isolation(root)["passed"])

            (root / "app" / "page.tsx").write_text("fetch('https://unapproved.example/data')", encoding="utf-8")
            remote = MODULE.audit_runtime_isolation(root)
            self.assertFalse(remote["passed"])
            self.assertTrue(any("unapproved runtime network call" in error for error in remote["errors"]))

            (root / "app" / "page.tsx").write_text("export default function Page() { return null; }", encoding="utf-8")
            (root / ".openai" / "hosting.json").write_text(
                json.dumps({"project_id": "project", "d1": {"binding": "DB"}, "r2": None}), encoding="utf-8"
            )
            bound = MODULE.audit_runtime_isolation(root)
            self.assertFalse(bound["passed"])
            self.assertTrue(any("binding d1" in error for error in bound["errors"]))

    def test_payload_hash_uses_canonical_sync_serialization(self) -> None:
        payload = {"reportDate": "2026-07-16", "contentHash": "a" * 64}
        expected = MODULE.hashlib.sha256(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ).hexdigest()
        self.assertEqual(MODULE.payload_sha256(payload), expected)

    def test_fetch_rejects_non_https_targets_before_network_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            MODULE.fetch_production("file:///etc/passwd", 1.0)


if __name__ == "__main__":
    unittest.main()
