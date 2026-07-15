#!/usr/bin/env python3
"""Build the audited alert payload surfaced by the daily Codex automation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "improvement_tracking.json"
RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def alert_fingerprint(date: str, destinations: list[str], findings: list[dict[str, Any]]) -> str:
    content = json.dumps(
        {"date": date, "destinations": destinations, "findings": findings},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def build_alert(
    date: str,
    *,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    runtime_root: Path = RUNTIME_ROOT,
) -> dict[str, Any]:
    config = read_json(config_path).get("external_alerting", {})
    destinations = config.get("destinations", []) if isinstance(config, dict) else []
    if not isinstance(config, dict) or config.get("enabled") is not True or not destinations:
        raise ValueError("external_alerting must be enabled with at least one destination")
    improvements = read_json(runtime_root / "improvements" / "latest.json")
    healing = read_json(runtime_root / "self_healing" / "latest.json")
    cycle = read_json(runtime_root / "cycle_state.json")
    findings: list[dict[str, Any]] = []
    for action in improvements.get("actions", []):
        status = str(action.get("status") or "")
        if status not in {"open", "overdue", "regressed", "requires_approval"}:
            continue
        spec = action.get("spec", {})
        findings.append({
            "kind": "improvement",
            "id": action.get("action_id"),
            "severity": spec.get("severity", "medium"),
            "status": status,
            "title": spec.get("title"),
            "summary": action.get("last_evaluation", {}).get("summary"),
        })
    healing_issues = healing.get("unresolved_issues", healing.get("issues", []))
    for issue in healing_issues if isinstance(healing_issues, list) else []:
        status = str(issue.get("status") or "")
        if status in {"resolved", "healthy"}:
            continue
        findings.append({
            "kind": "self_healing",
            "id": issue.get("issue_id"),
            "severity": issue.get("severity", "medium"),
            "status": status,
            "title": issue.get("title") or issue.get("probe"),
            "summary": issue.get("summary") or issue.get("detail"),
        })
    for blocker in cycle.get("blocking", []) if isinstance(cycle.get("blocking"), list) else []:
        findings.append({"kind": "cycle", "id": cycle.get("cycle_id"), "severity": "critical", "status": "blocked", "title": "ATLAS cycle blocked", "summary": blocker})
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda item: (severity_rank.get(str(item.get("severity")), 9), str(item.get("id"))))
    alerts_root = runtime_root / "alerts"
    previous = read_json(alerts_root / "latest.json")
    fingerprint = alert_fingerprint(date, list(destinations), findings)
    alert_id = f"ATLAS-ALERT-{date.replace('-', '')}-{fingerprint[:16].upper()}"
    same_alert = previous.get("alert_id") == alert_id
    generated_at = (
        str(previous.get("generated_at"))
        if same_alert and previous.get("generated_at")
        else datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    ack_required_severities = {
        str(value) for value in config.get("ack_required_severities", ["critical", "high"])
    }
    requires_ack = any(str(item.get("severity")) in ack_required_severities for item in findings)
    attempts = list(previous.get("delivery_attempts", [])) if same_alert else []
    if findings and not attempts:
        attempts = [
            {
                "attempt": 1,
                "prepared_at": generated_at,
                "destinations": list(destinations),
                "status": "pending_handoff",
            }
        ]
    ack_timeout = int(config.get("ack_timeout_minutes") or 60)
    payload = {
        "schema_version": 1,
        "alert_id": alert_id,
        "content_sha256": fingerprint,
        "date": date,
        "generated_at": generated_at,
        "status": "attention_required" if findings else "healthy",
        "destinations": destinations,
        "delivery_contract": "External automation must hand off every configured destination, record a receipt, and acknowledge critical/high findings.",
        "delivery_state": (
            str(previous.get("delivery_state"))
            if same_alert and previous.get("delivery_state")
            else "pending"
            if findings
            else "not_required"
        ),
        "delivery_attempts": attempts,
        "retry_limit": int(config.get("retry_limit") or 3),
        "requires_acknowledgement": requires_ack,
        "acknowledged_at": previous.get("acknowledged_at") if same_alert else None,
        "acknowledged_by": previous.get("acknowledged_by") if same_alert else None,
        "escalation_due_at": (
            (datetime.fromisoformat(generated_at.replace("Z", "+00:00")) + timedelta(minutes=ack_timeout))
            .isoformat()
            .replace("+00:00", "Z")
            if requires_ack
            else None
        ),
        "finding_count": len(findings),
        "findings": findings,
    }
    atomic_json(alerts_root / f"alert-{date}.json", payload)
    atomic_json(alerts_root / "latest.json", payload)
    return payload


def acknowledge_alert(date: str, acknowledged_by: str, *, runtime_root: Path = RUNTIME_ROOT) -> dict[str, Any]:
    path = runtime_root / "alerts" / f"alert-{date}.json"
    payload = read_json(path)
    if not payload or payload.get("date") != date:
        raise ValueError(f"no alert exists for {date}")
    actor = acknowledged_by.strip()
    if not actor:
        raise ValueError("acknowledged_by is required")
    payload["acknowledged_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload["acknowledged_by"] = actor
    payload["delivery_state"] = "acknowledged"
    atomic_json(path, payload)
    atomic_json(runtime_root / "alerts" / "latest.json", payload)
    return payload


def retry_alert(date: str, *, runtime_root: Path = RUNTIME_ROOT) -> dict[str, Any]:
    path = runtime_root / "alerts" / f"alert-{date}.json"
    payload = read_json(path)
    if not payload or payload.get("date") != date:
        raise ValueError(f"no alert exists for {date}")
    attempts = list(payload.get("delivery_attempts", []))
    retry_limit = int(payload.get("retry_limit") or 3)
    if len(attempts) >= retry_limit:
        payload["delivery_state"] = "escalation_required"
    else:
        attempts.append(
            {
                "attempt": len(attempts) + 1,
                "prepared_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "destinations": list(payload.get("destinations", [])),
                "status": "pending_handoff",
            }
        )
        payload["delivery_attempts"] = attempts
        payload["delivery_state"] = "pending"
    atomic_json(path, payload)
    atomic_json(runtime_root / "alerts" / "latest.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the daily ATLAS external alert payload.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ack-by")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if args.ack_by and args.retry:
        parser.error("--ack-by and --retry are mutually exclusive")
    payload = (
        acknowledge_alert(args.date, args.ack_by)
        if args.ack_by
        else retry_alert(args.date)
        if args.retry
        else build_alert(args.date)
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        ids = ", ".join(str(item.get("id")) for item in payload["findings"][:8]) or "none"
        print(f"alert_status={payload['status']} findings={payload['finding_count']} ids={ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
