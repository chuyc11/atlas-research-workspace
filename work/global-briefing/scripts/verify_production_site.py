"""Verify the deployed ATLAS site and emit fail-closed release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "work" / "global-briefing" / "data"
SITE_ROOT = ROOT / "src"
SITE_DATA = SITE_ROOT / "app" / "briefing.generated.json"
SITE_STATE = DATA_DIR / "site-sync-state.json"
CODE_SUFFIXES = {".js", ".jsx", ".mjs", ".ts", ".tsx"}
NETWORK_CALL_RE = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(")


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
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


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
    expected_content_hash: str,
    expected_report_date: str,
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
    for name, expected in exact_headers.items():
        if normalized.get(name, "").lower() != expected.lower():
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

    hash_match = re.search(r'data-atlas-content-hash=["\']([0-9a-f]{64})["\']', body)
    date_match = re.search(r'data-atlas-report-date=["\'](\d{4}-\d{2}-\d{2})["\']', body)
    if not hash_match or hash_match.group(1) != expected_content_hash:
        errors.append("production HTML content hash does not match the generated site payload")
    if not date_match or date_match.group(1) != expected_report_date:
        errors.append("production HTML report date does not match the generated site payload")
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
        "observed_content_hash": hash_match.group(1) if hash_match else "",
        "observed_report_date": date_match.group(1) if date_match else "",
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


def fetch_production(url: str, timeout: float) -> tuple[int, str, dict[str, str], str]:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("production verification URL must use HTTPS and include a hostname")
    request = Request(url, headers={"Accept": "text/html", "User-Agent": "ATLAS-Production-Verifier/1.0"})
    context = ssl.create_default_context()
    try:
        # The scheme and hostname are explicitly allowlisted above; redirects are
        # revalidated by evaluate_live_response before an artifact can pass.
        with urlopen(request, timeout=timeout, context=context) as response:  # nosec B310
            body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
            return response.status, response.geturl(), dict(response.headers.items()), body
    except HTTPError as exc:
        body = exc.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        return exc.code, exc.geturl(), dict(exc.headers.items()), body


def resolve_addresses(url: str) -> list[str]:
    hostname = urlparse(url).hostname
    if not hostname:
        return []
    return sorted({item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)})


def build_verification(url: str, *, timeout: float = 20.0, site_root: Path = SITE_ROOT) -> dict[str, Any]:
    payload_path = site_root / "app" / "briefing.generated.json"
    payload = load_json(payload_path)
    expected_hash = str(payload.get("contentHash") or "")
    expected_date = str(payload.get("reportDate") or "")
    payload_digest = payload_sha256(payload)
    isolation = audit_runtime_isolation(site_root)
    try:
        status, final_url, headers, body = fetch_production(url, timeout)
        live = evaluate_live_response(
            status=status,
            final_url=final_url,
            headers=headers,
            body=body,
            expected_content_hash=expected_hash,
            expected_report_date=expected_date,
        )
        dns_addresses = resolve_addresses(final_url)
        network_error = ""
    except (OSError, URLError, ssl.SSLError, socket.gaierror) as exc:
        live = {"passed": False, "errors": [f"production request failed: {type(exc).__name__}: {exc}"]}
        dns_addresses = []
        network_error = str(exc)
    errors = [*isolation.get("errors", []), *live.get("errors", [])]
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "deployment_url": url,
        "expected_content_hash": expected_hash,
        "expected_report_date": expected_date,
        "site_payload_sha256": payload_digest,
        "source_isolation": isolation,
        "live": live,
        "dns_addresses": dns_addresses,
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
        output = args.output or DATA_DIR / f"production-verification-{payload['expected_report_date']}.json"
        atomic_write_json(output, payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "passed" if payload["passed"] else "failed",
        "output": str(output),
        "deployment_url": url,
        "expected_content_hash": payload["expected_content_hash"],
        "errors": payload["errors"],
    }, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
