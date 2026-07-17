from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_production_site.py"
SPEC = importlib.util.spec_from_file_location("production_site_verification_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"ok", headers: list[tuple[str, str]] | None = None):
        self.status = status
        self._body = body
        self._headers = headers or [("content-type", "text/html")]

    def read(self, _limit: int) -> bytes:
        return self._body

    def getheaders(self) -> list[tuple[str, str]]:
        return self._headers


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

    def identity(self) -> dict[str, object]:
        return {
            "content_hash": "a" * 64,
            "report_date": "2026-07-16",
            "payload_sha256": "b" * 64,
            "snapshot_revision": 3,
            "snapshot_sha256": "c" * 64,
            "candidate_fingerprint": "d" * 64,
            "build_id": "atlas-build-" + "e" * 64,
            "deployment_id": "atlas-deployment-" + "f" * 64,
        }

    def live_body(self, identity: dict[str, object] | None = None) -> str:
        value = identity or self.identity()
        return (
            f'<main data-atlas-report-date="{value["report_date"]}" '
            f'data-atlas-content-hash="{value["content_hash"]}" '
            f'data-atlas-payload-sha256="{value["payload_sha256"]}" '
            f'data-atlas-snapshot-revision="{value["snapshot_revision"]}" '
            f'data-atlas-snapshot-sha256="{value["snapshot_sha256"]}" '
            f'data-atlas-candidate-fingerprint="{value["candidate_fingerprint"]}" '
            f'data-atlas-build-id="{value["build_id"]}" '
            f'data-atlas-deployment-id="{value["deployment_id"]}">'
        )

    def test_live_response_requires_exact_frozen_identity_and_staged_security_headers(self) -> None:
        identity = self.identity()
        result = MODULE.evaluate_live_response(
            status=200,
            final_url="https://example.com/",
            headers=self.live_headers(),
            body=self.live_body(identity),
            expected=identity,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["observed_payload_sha256"], identity["payload_sha256"])
        self.assertEqual(result["observed_snapshot_revision"], identity["snapshot_revision"])
        self.assertEqual(result["observed_build_id"], identity["build_id"])

        unsafe = self.live_headers()
        unsafe["strict-transport-security"] = "max-age=31536000; includeSubDomains; preload"
        wrong_body = self.live_body({**identity, "payload_sha256": "0" * 64})
        rejected = MODULE.evaluate_live_response(
            status=200,
            final_url="https://example.com/",
            headers=unsafe,
            body=wrong_body,
            expected=identity,
        )
        self.assertFalse(rejected["passed"])
        self.assertTrue(any("includeSubDomains" in error for error in rejected["errors"]))
        self.assertTrue(any("observed_payload_sha256" in error for error in rejected["errors"]))

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
            (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        ).hexdigest()
        self.assertEqual(MODULE.payload_sha256(payload), expected)

    def test_publication_identity_rejects_unfrozen_or_locally_mismatched_manifest(self) -> None:
        payload = {"reportDate": "2026-07-16", "contentHash": "a" * 64}
        identity = self.identity()
        manifest = {
            "schemaVersion": 1,
            "frozen": True,
            "reportDate": payload["reportDate"],
            "contentHash": payload["contentHash"],
            "payloadSha256": MODULE.payload_sha256(payload),
            "snapshotRevision": identity["snapshot_revision"],
            "snapshotSha256": identity["snapshot_sha256"],
            "candidateFingerprint": identity["candidate_fingerprint"],
            "buildId": identity["build_id"],
            "deploymentId": identity["deployment_id"],
            "repositoryCommits": {"root": "1" * 40, "site": "2" * 40, "trading-core": "3" * 40},
        }
        _expected, errors = MODULE.publication_identity(payload, manifest)
        self.assertEqual(errors, [])
        manifest["frozen"] = False
        manifest["payloadSha256"] = "9" * 64
        _expected, errors = MODULE.publication_identity(payload, manifest)
        self.assertTrue(any("frozen" in error for error in errors))
        self.assertTrue(any("payload hash" in error for error in errors))

    def test_verification_records_live_identity_instead_of_masquerading_local_values(self) -> None:
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
            payload = {"reportDate": "2026-07-16", "contentHash": "a" * 64}
            identity = self.identity()
            identity["payload_sha256"] = MODULE.payload_sha256(payload)
            manifest = {
                "schemaVersion": 1,
                "frozen": True,
                "reportDate": identity["report_date"],
                "contentHash": identity["content_hash"],
                "payloadSha256": identity["payload_sha256"],
                "snapshotRevision": identity["snapshot_revision"],
                "snapshotSha256": identity["snapshot_sha256"],
                "candidateFingerprint": identity["candidate_fingerprint"],
                "buildId": identity["build_id"],
                "deploymentId": identity["deployment_id"],
                "repositoryCommits": {"root": "1" * 40, "site": "2" * 40, "trading-core": "3" * 40},
            }
            (root / "app" / "briefing.generated.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (root / "app" / "publication.generated.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            network = {
                "connected_ip": "93.184.216.34",
                "validated_addresses": ["93.184.216.34"],
                "redirects": [],
                "dns_pinned": True,
                "tls_server_name": "example.com",
                "host_header": "example.com",
            }
            with patch.object(
                MODULE,
                "fetch_production",
                return_value=(200, "https://example.com/", self.live_headers(), self.live_body(identity), network),
            ):
                verified = MODULE.build_verification("https://example.com/", site_root=root)

            wrong_live = self.live_body({**identity, "payload_sha256": "0" * 64})
            with patch.object(
                MODULE,
                "fetch_production",
                return_value=(200, "https://example.com/", self.live_headers(), wrong_live, network),
            ):
                rejected = MODULE.build_verification("https://example.com/", site_root=root)

        self.assertTrue(verified["passed"])
        self.assertNotIn("site_payload_sha256", verified)
        self.assertEqual(verified["expected"]["payload_sha256"], identity["payload_sha256"])
        self.assertEqual(verified["live"]["observed_payload_sha256"], identity["payload_sha256"])
        self.assertFalse(rejected["passed"])
        self.assertEqual(rejected["live"]["observed_payload_sha256"], "0" * 64)

    def test_fetch_rejects_non_https_targets_before_network_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            MODULE.fetch_production("file:///etc/passwd", 1.0)

    def test_url_validation_rejects_nonstandard_ports_private_dns_and_cross_origin(self) -> None:
        with patch.object(
            MODULE.socket,
            "getaddrinfo",
            return_value=[(MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaisesRegex(ValueError, "non-global"):
                MODULE.validate_production_url("https://example.com/")

        with patch.object(MODULE, "resolve_public_addresses", return_value=["93.184.216.34"]):
            origin, _addresses = MODULE.validate_production_url("https://example.com/")
            with self.assertRaisesRegex(ValueError, "port 443"):
                MODULE.validate_production_url("https://example.com:8443/")
            with self.assertRaisesRegex(ValueError, "original HTTPS origin"):
                MODULE.validate_production_url("https://other.example/path", expected_origin=origin)

    def test_pinned_connection_uses_validated_ip_with_hostname_sni(self) -> None:
        calls: dict[str, object] = {}

        class RawSocket:
            def close(self) -> None:
                calls["raw_closed"] = True

        class TLSSocket:
            def getpeername(self):
                return ("93.184.216.34", 443)

            def close(self) -> None:
                calls["tls_closed"] = True

        class Context:
            def wrap_socket(self, raw_socket, *, server_hostname):
                calls["wrapped"] = raw_socket
                calls["server_hostname"] = server_hostname
                return TLSSocket()

        with patch.object(
            MODULE.socket,
            "create_connection",
            return_value=RawSocket(),
        ) as create_connection:
            connection = MODULE.PinnedHTTPSConnection(
                "example.com",
                "93.184.216.34",
                port=443,
                timeout=1.0,
                context=Context(),
            )
            connection.connect()
            connection.close()

        self.assertEqual(create_connection.call_args.args[0], ("93.184.216.34", 443))
        self.assertEqual(calls["server_hostname"], "example.com")
        self.assertEqual(connection.connected_ip, "93.184.216.34")

    def test_fetch_pins_every_same_origin_redirect_and_preserves_host_header(self) -> None:
        calls: list[dict[str, object]] = []

        class Connection:
            def __init__(self, hostname, address, **kwargs):
                self.connected_ip = address
                self.hostname = hostname
                self.address = address
                self.kwargs = kwargs
                self.index = len(calls)
                calls.append({"hostname": hostname, "address": address, **kwargs})

            def request(self, method, target, headers):
                calls[self.index].update({"method": method, "target": target, "headers": headers})

            def getresponse(self):
                if self.index == 0:
                    return FakeResponse(302, headers=[("Location", "/briefing")])
                return FakeResponse(200)

            def close(self):
                return None

        with (
            patch.object(MODULE.ssl, "create_default_context", return_value=object()),
            patch.object(MODULE, "resolve_public_addresses", return_value=["93.184.216.34"]) as resolver,
        ):
            status, final_url, _headers, body, network = MODULE.fetch_production(
                "https://example.com/", 1.0, connection_factory=Connection
            )

        self.assertEqual(status, 200)
        self.assertEqual(final_url, "https://example.com/briefing")
        self.assertEqual(body, "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["address"], "93.184.216.34")
        self.assertEqual(calls[0]["hostname"], "example.com")
        self.assertEqual(calls[0]["headers"]["Host"], "example.com")
        self.assertEqual(network["connected_ip"], "93.184.216.34")
        self.assertTrue(network["dns_pinned"])
        self.assertEqual(resolver.call_count, 2)

    def test_fetch_rejects_cross_origin_redirect_before_second_connection(self) -> None:
        calls: list[str] = []

        class Connection:
            def __init__(self, hostname, address, **_kwargs):
                calls.append(hostname)
                self.connected_ip = address

            def request(self, _method, _target, headers):
                self.headers = headers
                return None

            def getresponse(self):
                return FakeResponse(302, headers=[("Location", "https://other.example/briefing")])

            def close(self):
                return None

        with (
            patch.object(MODULE.ssl, "create_default_context", return_value=object()),
            patch.object(MODULE, "resolve_public_addresses", return_value=["93.184.216.34"]),
        ):
            with self.assertRaisesRegex(ValueError, "original HTTPS origin"):
                MODULE.fetch_production("https://example.com/", 1.0, connection_factory=Connection)

        self.assertEqual(calls, ["example.com"])


if __name__ == "__main__":
    unittest.main()
