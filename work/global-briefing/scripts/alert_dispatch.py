#!/usr/bin/env python3
"""Build the audited alert payload surfaced by the daily Codex automation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "improvement_tracking.json"
RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas"


@contextmanager
def alert_transaction_lock(runtime_root: Path, timeout_seconds: float = 10.0):
    """Serialize alert, receipt, acknowledgement, retry, and channel-health updates."""
    lock_path = runtime_root / "alerts" / ".transaction.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring alert transaction lock: {lock_path}") from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def synchronized_alert_operation(function):
    @wraps(function)
    def wrapper(date: str, *args: Any, **kwargs: Any):
        runtime_root = kwargs.get("runtime_root", RUNTIME_ROOT)
        with alert_transaction_lock(Path(runtime_root)):
            return function(date, *args, **kwargs)

    return wrapper


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def alert_fingerprint(date: str, destinations: list[str], findings: list[dict[str, Any]]) -> str:
    content = json.dumps(
        {"date": date, "destinations": destinations, "findings": findings},
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def configured_destinations(config: dict[str, Any]) -> list[str]:
    raw = config.get("destinations", [])
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(value.strip() for value in raw if isinstance(value, str) and value.strip()))


def receipt_destinations(payload: dict[str, Any]) -> set[str]:
    receipts = payload.get("delivery_receipts", {})
    if not isinstance(receipts, dict):
        return set()
    return {
        str(destination)
        for destination, receipt in receipts.items()
        if isinstance(receipt, dict) and str(receipt.get("receipt_id") or "").strip()
    }


def all_destinations_delivered(payload: dict[str, Any]) -> bool:
    destinations = {
        str(destination)
        for destination in payload.get("destinations", [])
        if isinstance(destination, str) and destination
    }
    return bool(destinations) and destinations.issubset(receipt_destinations(payload))


def update_channel_health(
    payload: dict[str, Any],
    destination: str,
    receipt: dict[str, Any],
    *,
    runtime_root: Path,
) -> dict[str, Any]:
    """Persist connector delivery proof independently from the daily alert lifecycle."""
    path = runtime_root / "alerts" / "channel_health.json"
    health = read_json(path)
    destinations = health.get("destinations", {}) if isinstance(health, dict) else {}
    destinations = dict(destinations) if isinstance(destinations, dict) else {}
    recorded_at = str(receipt.get("recorded_at") or "")
    receipt_id = str(receipt.get("receipt_id") or "").strip()
    destinations[destination] = {
        "status": "healthy",
        "destination": destination,
        "provider_message_id": receipt_id,
        "receipt_id": receipt_id,
        "verified_at": recorded_at,
        "source_alert_id": str(payload.get("alert_id") or ""),
        "source_alert_date": str(payload.get("date") or ""),
        "source_alert_sha256": str(payload.get("content_sha256") or ""),
    }
    result = {
        "schema_version": 1,
        "updated_at": recorded_at,
        "destinations": destinations,
    }
    atomic_json(path, result)
    return result


@synchronized_alert_operation
def build_alert(
    date: str,
    *,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    runtime_root: Path = RUNTIME_ROOT,
) -> dict[str, Any]:
    config = read_json(config_path).get("external_alerting", {})
    destinations = configured_destinations(config) if isinstance(config, dict) else []
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
    blockers = cycle.get("blocking_reasons")
    if not isinstance(blockers, list):
        blockers = cycle.get("blocking")
    blockers = blockers if isinstance(blockers, list) else []
    if cycle.get("operational_gate_passed") is False and not blockers:
        blockers = ["operational gate failed without a recorded blocking reason"]
    for blocker in dict.fromkeys(str(value) for value in blockers if str(value).strip()):
        findings.append(
            {
                "kind": "cycle",
                "id": cycle.get("cycle_id"),
                "severity": "critical",
                "status": "blocked",
                "title": "ATLAS cycle blocked",
                "summary": blocker,
            }
        )
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
    receipts = dict(previous.get("delivery_receipts", {})) if same_alert else {}
    if findings and not attempts:
        attempts = [
            {
                "attempt": 1,
                "prepared_at": generated_at,
                "destinations": list(destinations),
                "status": "pending_handoff",
            }
        ]
    previous_state = str(previous.get("delivery_state") or "") if same_alert else ""
    delivered = bool(findings) and all_destinations_delivered(
        {"destinations": destinations, "delivery_receipts": receipts}
    )
    acknowledged_at = previous.get("acknowledged_at") if same_alert else None
    if not findings:
        delivery_state = "not_required"
    elif acknowledged_at and delivered:
        delivery_state = "acknowledged"
    elif delivered:
        delivery_state = "delivered"
    elif previous_state == "escalation_required":
        delivery_state = "escalation_required"
    else:
        delivery_state = "pending"
    ack_timeout = int(config.get("ack_timeout_minutes") or 60)
    payload = {
        "schema_version": 1,
        "alert_id": alert_id,
        "content_sha256": fingerprint,
        "date": date,
        "generated_at": generated_at,
        "status": "attention_required" if findings else "healthy",
        "destinations": destinations,
        "delivery_contract": (
            "Every configured destination must return a durable receipt before delivery is "
            "considered complete; critical/high findings must then be acknowledged."
        ),
        "delivery_state": delivery_state,
        "delivery_attempts": attempts,
        "delivery_receipts": receipts,
        "retry_limit": int(config.get("retry_limit") or 3),
        "requires_acknowledgement": requires_ack,
        "acknowledged_at": acknowledged_at,
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


@synchronized_alert_operation
def record_delivery_receipt(
    date: str,
    destination: str,
    receipt_id: str,
    *,
    runtime_root: Path = RUNTIME_ROOT,
) -> dict[str, Any]:
    path = runtime_root / "alerts" / f"alert-{date}.json"
    payload = read_json(path)
    if not payload or payload.get("date") != date:
        raise ValueError(f"no alert exists for {date}")
    target = destination.strip()
    receipt = receipt_id.strip()
    destinations = {
        str(value) for value in payload.get("destinations", []) if isinstance(value, str)
    }
    if target not in destinations:
        raise ValueError(f"destination is not configured for this alert: {target}")
    if not receipt:
        raise ValueError("receipt_id is required")
    if not payload.get("findings"):
        raise ValueError("healthy alerts do not require delivery receipts")
    receipts = payload.get("delivery_receipts", {})
    receipts = dict(receipts) if isinstance(receipts, dict) else {}
    existing = receipts.get(target, {})
    existing_id = (
        str(existing.get("receipt_id") or "").strip() if isinstance(existing, dict) else ""
    )
    if existing_id and existing_id != receipt:
        raise ValueError(f"delivery receipt is immutable for destination: {target}")
    if not existing_id:
        receipts[target] = {
            "receipt_id": receipt,
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    payload["delivery_receipts"] = receipts
    payload["delivery_state"] = "delivered" if all_destinations_delivered(payload) else "pending"
    atomic_json(path, payload)
    atomic_json(runtime_root / "alerts" / "latest.json", payload)
    update_channel_health(payload, target, receipts[target], runtime_root=runtime_root)
    return payload


@synchronized_alert_operation
def acknowledge_alert(date: str, acknowledged_by: str, *, runtime_root: Path = RUNTIME_ROOT) -> dict[str, Any]:
    path = runtime_root / "alerts" / f"alert-{date}.json"
    payload = read_json(path)
    if not payload or payload.get("date") != date:
        raise ValueError(f"no alert exists for {date}")
    actor = acknowledged_by.strip()
    if not actor:
        raise ValueError("acknowledged_by is required")
    if payload.get("findings") and not all_destinations_delivered(payload):
        raise ValueError("cannot acknowledge an alert before every destination has a delivery receipt")
    payload["acknowledged_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload["acknowledged_by"] = actor
    payload["delivery_state"] = "acknowledged"
    atomic_json(path, payload)
    atomic_json(runtime_root / "alerts" / "latest.json", payload)
    return payload


@synchronized_alert_operation
def retry_alert(date: str, *, runtime_root: Path = RUNTIME_ROOT) -> dict[str, Any]:
    path = runtime_root / "alerts" / f"alert-{date}.json"
    payload = read_json(path)
    if not payload or payload.get("date") != date:
        raise ValueError(f"no alert exists for {date}")
    if payload.get("delivery_state") in {"delivered", "acknowledged"}:
        raise ValueError("cannot retry an alert that has already been delivered")
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
    parser.add_argument("--receipt-destination")
    parser.add_argument("--receipt-id")
    args = parser.parse_args()
    receipt_mode = bool(args.receipt_destination or args.receipt_id)
    if receipt_mode and not (args.receipt_destination and args.receipt_id):
        parser.error("--receipt-destination and --receipt-id must be provided together")
    if sum(bool(value) for value in (args.ack_by, args.retry, receipt_mode)) > 1:
        parser.error("acknowledge, retry, and delivery-receipt modes are mutually exclusive")
    payload = (
        acknowledge_alert(args.date, args.ack_by)
        if args.ack_by
        else retry_alert(args.date)
        if args.retry
        else record_delivery_receipt(
            args.date,
            args.receipt_destination,
            args.receipt_id,
        )
        if receipt_mode
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
