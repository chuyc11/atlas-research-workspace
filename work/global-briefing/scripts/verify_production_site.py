"""Verify the deployed ATLAS site and emit fail-closed release evidence."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "work" / "global-briefing" / "data"
SITE_ROOT = ROOT / "src"
SITE_DATA = SITE_ROOT / "app" / "briefing.generated.json"
SITE_PUBLICATION_MANIFEST = SITE_ROOT / "app" / "publication.generated.json"
SITE_STATE = DATA_DIR / "site-sync-state.json"
CODE_SUFFIXES = {".js", ".jsx", ".mjs", ".ts", ".tsx"}
NETWORK_CALL_RE = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a prevalidated IP while preserving hostname TLS verification."""

    def __init__(
        self,
        hostname: str,
        pinned_address: str,
        *,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self.pinned_address = pinned_address
        self.connected_ip = ""

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self.pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise
        peer_address = str(self.sock.getpeername()[0]).split("%", 1)[0]
        if ipaddress.ip_address(peer_address) != ipaddress.ip_address(self.pinned_address):
            self.close()
            raise OSError("TLS peer address does not match the pinned DNS address")
        self.connected_ip = peer_address


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def payload_sha256(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def publication_identity(
    payload: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    identity = {
        "content_hash": manifest.get("contentHash"),
        "report_date": manifest.get("reportDate"),
        "payload_sha256": manifest.get("payloadSha256"),
        "snapshot_revision": manifest.get("snapshotRevision"),
        "snapshot_sha256": manifest.get("snapshotSha256"),
        "candidate_fingerprint": manifest.get("candidateFingerprint"),
        "build_id": manifest.get("buildId"),
        "deployment_id": manifest.get("deploymentId"),
    }
    errors: list[str] = []
    if manifest.get("schemaVersion") != 1 or manifest.get("frozen") is not True:
        errors.append("local publication manifest must be schema 1 and frozen")
    if identity["content_hash"] != payload.get("contentHash"):
        errors.append("local publication manifest content hash does not match the site payload")
    if identity["report_date"] != payload.get("reportDate"):
        errors.append("local publication manifest report date does not match the site payload")
    if identity["payload_sha256"] != payload_sha256(payload):
        errors.append("local publication manifest payload hash does not match the site payload")
    for field in ("content_hash", "payload_sha256", "snapshot_sha256", "candidate_fingerprint"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(identity[field] or "")):
            errors.append(f"local publication identity {field} is invalid")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(identity["report_date"] or "")):
        errors.append("local publication identity report_date is invalid")
    if not isinstance(identity["snapshot_revision"], int) or identity["snapshot_revision"] <= 0:
        errors.append("local publication identity snapshot_revision is invalid")
    for field in ("build_id", "deployment_id"):
        if not re.fullmatch(r"[a-z0-9-]{16,160}", str(identity[field] or "")):
            errors.append(f"local publication identity {field} is invalid")
    commits = manifest.get("repositoryCommits")
    if not isinstance(commits, dict) or set(commits) != {"root", "site", "trading-core"}:
        errors.append("local publication manifest must contain exactly three repository commits")
    elif any(not re.fullmatch(r"[0-9a-f]{40}", str(value or "")) for value in commits.values()):
        errors.append("local publication manifest contains an invalid repository commit")
    return identity, errors


def csp_directives(value: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw in value.split(";"):
        parts = raw.strip().split()
        if parts:
            result[parts[0].lower()] = parts[1:]
    return result


def evaluate_live_response(
    *,
    status: int,
    final_url: str,
    headers: dict[str, str],
    body: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    if status != 200:
        errors.append(f"production HTTP status is {status}, expected 200")
    if urlparse(final_url).scheme.lower() != "https":
        errors.append("production final URL is not HTTPS")

    exact_headers = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "strict-origin-when-cross-origin",
    }
    for name, expected_header_value in exact_headers.items():
        if normalized.get(name, "").lower() != expected_header_value.lower():
            errors.append(f"production header {name} is missing or invalid")
    permissions = normalized.get("permissions-policy", "")
    for directive in ("camera=()", "microphone=()", "geolocation=()"):
        if directive not in permissions:
            errors.append(f"production permissions-policy is missing {directive}")

    hsts = normalized.get("strict-transport-security", "")
    max_age_match = re.search(r"(?:^|;)\s*max-age=(\d+)(?:;|$)", hsts, re.IGNORECASE)
    if not max_age_match or int(max_age_match.group(1)) < 86400:
        errors.append("production HSTS must set max-age of at least 86400 seconds")
    if re.search(r"\bincludesubdomains\b|\bpreload\b", hsts, re.IGNORECASE):
        errors.append("staged HSTS must not enable includeSubDomains or preload")

    csp = csp_directives(normalized.get("content-security-policy", ""))
    required_csp = {
        "default-src": {"'self'"},
        "connect-src": {"'self'"},
        "object-src": {"'none'"},
        "frame-ancestors": {"'none'"},
        "base-uri": {"'self'"},
        "form-action": {"'self'"},
    }
    for directive, required in required_csp.items():
        actual = set(csp.get(directive, []))
        if not required.issubset(actual):
            errors.append(f"production CSP {directive} is missing {', '.join(sorted(required))}")
    if set(csp.get("connect-src", [])) != {"'self'"}:
        errors.append("production CSP connect-src permits an external origin")
    script_tokens = csp.get("script-src", [])
    if "'self'" not in script_tokens or "'strict-dynamic'" not in script_tokens:
        errors.append("production CSP script-src lacks self/strict-dynamic")
    if "'unsafe-inline'" in script_tokens:
        errors.append("production CSP script-src permits unsafe-inline")
    if not any(re.fullmatch(r"'nonce-[A-Za-z0-9+/_-]+'", token) for token in script_tokens):
        errors.append("production CSP script-src lacks a nonce")

    marker_patterns = {
        "observed_content_hash": r'data-atlas-content-hash=["\']([0-9a-f]{64})["\']',
        "observed_report_date": r'data-atlas-report-date=["\'](\d{4}-\d{2}-\d{2})["\']',
        "observed_payload_sha256": r'data-atlas-payload-sha256=["\']([0-9a-f]{64})["\']',
        "observed_snapshot_revision": r'data-atlas-snapshot-revision=["\']([1-9]\d*)["\']',
        "observed_snapshot_sha256": r'data-atlas-snapshot-sha256=["\']([0-9a-f]{64})["\']',
        "observed_candidate_fingerprint": r'data-atlas-candidate-fingerprint=["\']([0-9a-f]{64})["\']',
        "observed_build_id": r'data-atlas-build-id=["\']([a-z0-9-]{16,160})["\']',
        "observed_deployment_id": r'data-atlas-deployment-id=["\']([a-z0-9-]{16,160})["\']',
    }
    observed: dict[str, Any] = {}
    for field, pattern in marker_patterns.items():
        match = re.search(pattern, body)
        value: Any = match.group(1) if match else ""
        if field == "observed_snapshot_revision":
            value = int(value) if value else 0
        observed[field] = value
    expected_fields = {
        "observed_content_hash": "content_hash",
        "observed_report_date": "report_date",
        "observed_payload_sha256": "payload_sha256",
        "observed_snapshot_revision": "snapshot_revision",
        "observed_snapshot_sha256": "snapshot_sha256",
        "observed_candidate_fingerprint": "candidate_fingerprint",
        "observed_build_id": "build_id",
        "observed_deployment_id": "deployment_id",
    }
    for observed_field, expected_field in expected_fields.items():
        if observed.get(observed_field) != expected.get(expected_field):
            errors.append(f"production HTML {observed_field} does not match the frozen publication identity")
    return {
        "passed": not errors,
        "errors": errors,
        "status": status,
        "final_url": final_url,
        "headers": {
            key: normalized.get(key, "")
            for key in (
                "content-security-policy",
                "strict-transport-security",
                "permissions-policy",
                "referrer-policy",
                "x-content-type-options",
                "x-frame-options",
                "server",
            )
        },
        **observed,
        "body_bytes": len(body.encode("utf-8")),
    }


def audit_runtime_isolation(site_root: Path = SITE_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    hosting_path = site_root / ".openai" / "hosting.json"
    try:
        hosting = load_json(hosting_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"passed": False, "errors": [f"cannot read hosting configuration: {exc}"], "files_scanned": 0}
    allowed_hosting_keys = {"project_id", "d1", "r2"}
    unknown_keys = sorted(set(hosting) - allowed_hosting_keys)
    if unknown_keys:
        errors.append("hosting configuration has unreviewed bindings: " + ", ".join(unknown_keys))
    for binding in ("d1", "r2"):
        if hosting.get(binding) is not None:
            errors.append(f"hosting binding {binding} must remain null for the static briefing site")

    runtime_files = sorted(
        path
        for directory in (site_root / "app", site_root / "worker")
        if directory.exists()
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in CODE_SUFFIXES
    )
    digest = hashlib.sha256()
    digest.update(hosting_path.relative_to(site_root).as_posix().encode("utf-8"))
    digest.update(hosting_path.read_bytes())
    for path in runtime_files:
        relative = path.relative_to(site_root).as_posix()
        content = path.read_text(encoding="utf-8")
        digest.update(relative.encode("utf-8"))
        digest.update(content.encode("utf-8"))
        for line_number, line in enumerate(content.splitlines(), start=1):
            if "navigator.sendBeacon(" in line:
                errors.append(f"{relative}:{line_number} uses navigator.sendBeacon")
            if not NETWORK_CALL_RE.search(line):
                continue
            allowed_worker_call = relative == "worker/index.ts" and any(
                token in line
                for token in (
                    "async fetch(request:",
                    "env.ASSETS.fetch(",
                    "handler.fetch(",
                )
            )
            if not allowed_worker_call:
                errors.append(f"{relative}:{line_number} contains an unapproved runtime network call")
    return {
        "passed": not errors,
        "errors": errors,
        "hosting": {key: hosting.get(key) for key in sorted(allowed_hosting_keys)},
        "files_scanned": len(runtime_files),
        "source_digest": digest.hexdigest(),
        "boundary": {
            "runtime_remote_fetch_allowed": False,
            "database_bindings_allowed": False,
            "asset_binding_allowed": True,
        },
    }


def resolve_public_addresses(hostname: str, port: int = 443) -> list[str]:
    addresses = sorted({
        item[4][0].split("%", 1)[0]
        for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    })
    if not addresses:
        raise ValueError(f"production hostname did not resolve: {hostname}")
    unsafe = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            unsafe.append(address)
            continue
        if not parsed.is_global:
            unsafe.append(address)
    if unsafe:
        raise ValueError(f"production hostname resolves to non-global address(es): {', '.join(unsafe)}")
    return addresses


def network_evidence_errors(network: dict[str, Any], deployment_url: str) -> list[str]:
    errors: list[str] = []
    parsed = urlparse(deployment_url)
    expected_hostname = (
        parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        if parsed.hostname
        else ""
    )
    connected_ip = str(network.get("connected_ip") or "").split("%", 1)[0]
    addresses = network.get("validated_addresses")
    if network.get("dns_pinned") is not True:
        errors.append("production connection was not pinned to prevalidated DNS")
    if network.get("tls_server_name") != expected_hostname:
        errors.append("production TLS server name does not match the deployment hostname")
    if network.get("host_header") != expected_hostname:
        errors.append("production Host header does not match the deployment hostname")
    try:
        if not ipaddress.ip_address(connected_ip).is_global:
            raise ValueError
    except ValueError:
        errors.append("production connected IP is missing or non-global")
    if not isinstance(addresses, list) or connected_ip not in addresses:
        errors.append("production connected IP is not in the prevalidated DNS address set")
    else:
        try:
            if any(not ipaddress.ip_address(str(address)).is_global for address in addresses):
                errors.append("production prevalidated DNS address set contains a non-global address")
        except ValueError:
            errors.append("production prevalidated DNS address set contains an invalid address")
    redirects = network.get("redirects", [])
    if not isinstance(redirects, list):
        errors.append("production redirect network evidence is invalid")
    else:
        for redirect in redirects:
            if not isinstance(redirect, dict):
                errors.append("production redirect network evidence is invalid")
                continue
            redirect_ip = str(redirect.get("connected_ip") or "")
            redirect_addresses = redirect.get("validated_addresses", [])
            if not isinstance(redirect_addresses, list) or redirect_ip not in redirect_addresses:
                errors.append("production redirect peer was not in its prevalidated DNS address set")
                continue
            try:
                if not ipaddress.ip_address(redirect_ip).is_global or any(
                    not ipaddress.ip_address(str(address)).is_global
                    for address in redirect_addresses
                ):
                    errors.append("production redirect DNS evidence contains a non-global address")
            except ValueError:
                errors.append("production redirect DNS evidence contains an invalid address")
    return errors


def validate_production_url(
    url: str,
    *,
    expected_origin: tuple[str, int] | None = None,
    resolve_dns: bool = True,
) -> tuple[tuple[str, int], list[str]]:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("production verification URL must use HTTPS and include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("production verification URL must not contain credentials")
    if parsed.fragment:
        raise ValueError("production verification URL must not contain a fragment")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("production verification URL has an invalid port") from exc
    if port != 443:
        raise ValueError("production verification URL must use port 443")
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("production verification URL hostname is invalid") from exc
    origin = (hostname, port)
    if expected_origin is not None and origin != expected_origin:
        raise ValueError("production redirect must remain on the original HTTPS origin")
    return origin, resolve_public_addresses(origin[0], port) if resolve_dns else []


def _request_over_pinned_address(
    url: str,
    address: str,
    *,
    timeout: float,
    context: ssl.SSLContext,
    connection_factory: Any | None = None,
) -> tuple[int, dict[str, str], bytes, str]:
    parsed = urlparse(url)
    hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold() if parsed.hostname else ""
    port = parsed.port or 443
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("production verification URL path and query must be percent-encoded") from exc
    factory = connection_factory or PinnedHTTPSConnection
    connection = factory(
        hostname,
        address,
        port=port,
        timeout=timeout,
        context=context,
    )
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept": "text/html",
                "Host": hostname,
                "User-Agent": "ATLAS-Production-Verifier/2.0",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("production response exceeds the verifier body limit")
        headers: dict[str, str] = {}
        for raw_name, raw_value in response.getheaders():
            name = str(raw_name).lower()
            value = str(raw_value)
            headers[name] = f"{headers[name]}, {value}" if name in headers else value
        connected_ip = str(getattr(connection, "connected_ip", "") or "").split("%", 1)[0]
        if not connected_ip and getattr(connection, "sock", None) is not None:
            connected_ip = str(connection.sock.getpeername()[0]).split("%", 1)[0]
        if ipaddress.ip_address(connected_ip) != ipaddress.ip_address(address):
            raise OSError("production connection peer does not match the prevalidated DNS address")
        return int(response.status), headers, body, connected_ip
    finally:
        connection.close()


def fetch_production(
    url: str,
    timeout: float,
    *,
    connection_factory: Any | None = None,
) -> tuple[int, str, dict[str, str], str, dict[str, Any]]:
    origin, _addresses = validate_production_url(url, resolve_dns=False)
    context = ssl.create_default_context()
    current_url = url
    redirects: list[dict[str, Any]] = []
    for redirect_count in range(MAX_REDIRECTS + 1):
        _current_origin, addresses = validate_production_url(current_url, expected_origin=origin)
        failures: list[str] = []
        response: tuple[int, dict[str, str], bytes, str] | None = None
        for address in addresses:
            try:
                response = _request_over_pinned_address(
                    current_url,
                    address,
                    timeout=timeout,
                    context=context,
                    connection_factory=connection_factory,
                )
                break
            except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
                failures.append(f"{address}: {type(exc).__name__}: {exc}")
        if response is None:
            raise OSError("all prevalidated production addresses failed: " + "; ".join(failures))
        status, headers, body_bytes, connected_ip = response
        if status in REDIRECT_STATUSES:
            location = next((value for key, value in headers.items() if key.lower() == "location"), "")
            if not location:
                raise ValueError("production redirect is missing Location")
            if redirect_count >= MAX_REDIRECTS:
                raise ValueError("production redirect limit exceeded")
            next_url = urljoin(current_url, location)
            validate_production_url(next_url, expected_origin=origin, resolve_dns=False)
            redirects.append({
                "from": current_url,
                "to": next_url,
                "status": status,
                "connected_ip": connected_ip,
                "validated_addresses": addresses,
            })
            current_url = next_url
            continue
        return (
            status,
            current_url,
            headers,
            body_bytes.decode("utf-8", errors="replace"),
            {
                "connected_ip": connected_ip,
                "validated_addresses": addresses,
                "redirects": redirects,
                "dns_pinned": True,
                "tls_server_name": origin[0],
                "host_header": origin[0],
            },
        )
    raise ValueError("production redirect limit exceeded")


def build_verification(url: str, *, timeout: float = 20.0, site_root: Path = SITE_ROOT) -> dict[str, Any]:
    payload_path = site_root / "app" / "briefing.generated.json"
    manifest_path = site_root / "app" / "publication.generated.json"
    payload = load_json(payload_path)
    manifest = load_json(manifest_path)
    expected, local_errors = publication_identity(payload, manifest)
    isolation = audit_runtime_isolation(site_root)
    try:
        status, final_url, headers, body, network = fetch_production(url, timeout)
        live = evaluate_live_response(
            status=status,
            final_url=final_url,
            headers=headers,
            body=body,
            expected=expected,
        )
        network_error = ""
    except (OSError, ssl.SSLError, socket.gaierror, http.client.HTTPException, ValueError) as exc:
        live = {"passed": False, "errors": [f"production request failed: {type(exc).__name__}: {exc}"]}
        network = {
            "connected_ip": "",
            "validated_addresses": [],
            "redirects": [],
            "dns_pinned": False,
        }
        network_error = str(exc)
    network_errors = network_evidence_errors(network, url) if not network_error else []
    errors = [
        *local_errors,
        *isolation.get("errors", []),
        *live.get("errors", []),
        *network_errors,
    ]
    return {
        "schema_version": 2,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "deployment_url": url,
        "expected": expected,
        "local_publication": {
            "passed": not local_errors,
            "errors": local_errors,
            "manifest_sha256": payload_sha256(manifest),
        },
        "source_isolation": isolation,
        "live": live,
        "network": network,
        "network_errors": network_errors,
        "network_error": network_error,
        "errors": errors,
        "passed": not errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the live ATLAS site and local runtime isolation.")
    parser.add_argument("--url", default="", help="Production HTTPS URL; defaults to site-sync-state.json.")
    parser.add_argument("--output", type=Path, help="Verification artifact path.")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        state = load_json(SITE_STATE) if SITE_STATE.exists() else {}
        url = args.url or str(state.get("deployment_url") or "")
        if not url:
            raise ValueError("production URL is required")
        payload = build_verification(url, timeout=args.timeout)
        output = args.output or DATA_DIR / f"production-verification-{payload['expected']['report_date']}.json"
        atomic_write_json(output, payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "passed" if payload["passed"] else "failed",
        "output": str(output),
        "deployment_url": url,
        "expected_content_hash": payload["expected"]["content_hash"],
        "errors": payload["errors"],
    }, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
