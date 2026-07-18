#!/usr/bin/env python3
"""Build the audited alert payload surfaced by the daily Codex automation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
DEFAULT_RETRY_BACKOFF_MINUTES = (5, 15, 60)


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
    def wrapper(*args: Any, **kwargs: Any):
        runtime_root = kwargs.get("runtime_root", RUNTIME_ROOT)
        with alert_transaction_lock(Path(runtime_root)):
            return function(*args, **kwargs)

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


def validated_alert_date(value: str) -> str:
    """Return a strict ISO day suitable for a compatibility alias filename."""
    if not isinstance(value, str):
        raise ValueError("alert date must be an ISO date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("alert date must be an ISO date") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("alert date must be an ISO date")
    return value


def alert_alias_path(runtime_root: Path, date: str) -> Path:
    """Compatibility pointer for date-only callers; it always names the latest revision."""
    return runtime_root / "alerts" / f"alert-{validated_alert_date(date)}.json"


def alert_record_path(runtime_root: Path, alert_id: str) -> Path:
    """Path for an immutable alert revision, guarded against path traversal."""
    if not isinstance(alert_id, str) or not re.fullmatch(
        r"ATLAS-ALERT-\d{8}-[A-F0-9]{16}", alert_id
    ):
        raise ValueError("invalid alert_id")
    return runtime_root / "alerts" / "records" / f"{alert_id}.json"


def load_alert_records(runtime_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Load revision records, ignoring malformed or untrusted filenames/payloads."""
    records_root = runtime_root / "alerts" / "records"
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(records_root.glob("*.json")):
        payload = read_json(path)
        try:
            expected = alert_record_path(runtime_root, str(payload.get("alert_id") or ""))
        except ValueError:
            continue
        if path != expected or not payload.get("date"):
            continue
        result.append((path, payload))
    return result


def _revision_number(payload: dict[str, Any]) -> int:
    """Treat pre-revision alert payloads as the first revision."""
    return positive_int(payload.get("revision"), 1)


def _select_latest_date_record(
    runtime_root: Path,
    date: str,
) -> tuple[dict[str, Any], Path | None]:
    """Resolve the current revision for a day without relying on global latest.json."""
    alias_path = alert_alias_path(runtime_root, date)
    alias = read_json(alias_path)
    if alias.get("date") == date and alias.get("alert_id"):
        try:
            return alias, alert_record_path(runtime_root, str(alias["alert_id"]))
        except ValueError:
            # A malformed compatibility pointer must not hide a valid retained
            # revision record for this date.
            pass

    candidates = [
        (payload, path)
        for path, payload in load_alert_records(runtime_root)
        if payload.get("date") == date
    ]
    if not candidates:
        return {}, None
    return max(
        candidates,
        key=lambda item: (_revision_number(item[0]), str(item[0].get("generated_at") or "")),
    )


def load_alert_revision(
    date: str,
    *,
    runtime_root: Path,
    alert_id: str | None = None,
) -> tuple[dict[str, Any], Path | None]:
    """Load a revision selected by date, optionally narrowed to an exact alert id.

    Date-only operations intentionally resolve to the latest revision for that
    day.  Mutating operations can name ``alert_id`` to work on a superseded
    escalation without moving the current date/latest pointers.
    """
    validated_alert_date(date)
    if not alert_id:
        return _select_latest_date_record(runtime_root, date)

    try:
        record_path = alert_record_path(runtime_root, alert_id)
    except ValueError as exc:
        raise ValueError("invalid alert_id") from exc
    payload = read_json(record_path)
    # Support an on-disk pre-revision alert during the first migration.  It is
    # still constrained to the requested date and identifier before use.
    if not payload:
        alias = read_json(alert_alias_path(runtime_root, date))
        if alias.get("alert_id") == alert_id:
            payload = alias
            record_path = None
    if not payload or payload.get("date") != date or payload.get("alert_id") != alert_id:
        raise ValueError(f"no alert revision exists for {date}: {alert_id}")

    # The date alias is the backwards-compatible working copy for the current
    # revision.  Prefer it when it points to the requested record so legacy
    # recovery tooling that edits the alias is migrated rather than ignored.
    alias = read_json(alert_alias_path(runtime_root, date))
    if alias.get("date") == date and alias.get("alert_id") == alert_id:
        return alias, record_path
    return payload, record_path


def persist_alert_revision(
    payload: dict[str, Any],
    *,
    runtime_root: Path,
    make_current: bool = False,
) -> None:
    """Persist one revision and update pointers only when they name that revision.

    ``alert-YYYY-MM-DD.json`` and ``latest.json`` remain compatibility views;
    revision records are retained independently under ``alerts/records``.
    This prevents a changed same-day alert from overwriting an unresolved
    earlier escalation.
    """
    date = validated_alert_date(str(payload.get("date") or ""))
    alert_id = str(payload.get("alert_id") or "")
    record_path = alert_record_path(runtime_root, alert_id)
    atomic_json(record_path, payload)

    alias_path = alert_alias_path(runtime_root, date)
    current_alias = read_json(alias_path)
    if make_current or not current_alias or current_alias.get("alert_id") == alert_id:
        atomic_json(alias_path, payload)

    latest_path = runtime_root / "alerts" / "latest.json"
    current_latest = read_json(latest_path)
    if make_current or not current_latest or current_latest.get("alert_id") == alert_id:
        atomic_json(latest_path, payload)


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


def utc_now(now: datetime | None = None) -> datetime:
    """Normalize injected/test time and wall-clock time to UTC."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    return utc_now(value).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def positive_int(value: Any, default: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return default
    return candidate if candidate > 0 else default


def normalized_retry_backoff(value: Any, retry_limit: int) -> list[int]:
    raw = value if isinstance(value, list) else list(DEFAULT_RETRY_BACKOFF_MINUTES)
    result: list[int] = []
    for item in raw:
        try:
            minutes = int(item)
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            result.append(minutes)
    if not result:
        result = list(DEFAULT_RETRY_BACKOFF_MINUTES)
    # The final interval is also the grace period after the final permitted
    # handoff before escalation.  Extend short configurations predictably.
    minimum_intervals = max(retry_limit, 1)
    while len(result) < minimum_intervals:
        result.append(result[-1])
    return result


def retry_due_at(
    attempts: list[dict[str, Any]],
    backoff_minutes: list[int],
    *,
    now: datetime,
) -> str | None:
    if not attempts:
        return None
    prepared_at = parse_utc(attempts[-1].get("prepared_at")) or now
    index = min(len(attempts) - 1, len(backoff_minutes) - 1)
    return iso_utc(prepared_at + timedelta(minutes=backoff_minutes[index]))


def mark_escalation(payload: dict[str, Any], *, now: datetime, reason: str) -> bool:
    """Persist an explicit, durable escalation rather than silently timing out."""
    changed = payload.get("delivery_state") != "escalation_required"
    payload["delivery_state"] = "escalation_required"
    payload["retry_due_at"] = None
    existing = payload.get("escalation")
    if not isinstance(existing, dict) or not existing.get("triggered_at"):
        payload["escalation"] = {
            "triggered_at": iso_utc(now),
            "reason": reason,
            "status": "requires_human_follow_up",
        }
        changed = True
    return changed


def advance_alert_lifecycle(payload: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Advance one persisted alert when its acknowledgement/retry deadline is due.

    This function never claims an external delivery.  It only records a new
    ``pending_handoff`` retry or a durable escalation; connectors must still
    submit a receipt through ``record_delivery_receipt``.
    """
    current = utc_now(now)
    findings = payload.get("findings")
    if not isinstance(findings, list) or not findings:
        changed = payload.get("delivery_state") != "not_required"
        payload["delivery_state"] = "not_required"
        payload["retry_due_at"] = None
        return changed
    if payload.get("acknowledged_at") and all_destinations_delivered(payload):
        changed = payload.get("delivery_state") != "acknowledged" or payload.get("retry_due_at") is not None
        payload["delivery_state"] = "acknowledged"
        payload["retry_due_at"] = None
        return changed
    # Escalation is a durable terminal control state.  Delivery receipts can
    # still be recorded and a human can acknowledge the alert, but a scheduler
    # or retry must never quietly demote it back to pending.
    if payload.get("delivery_state") == "escalation_required":
        return False
    if payload.get("requires_acknowledgement") is True and not payload.get("acknowledged_at"):
        due = parse_utc(payload.get("escalation_due_at"))
        if due is None:
            return mark_escalation(payload, now=current, reason="invalid_acknowledgement_deadline")
        if current >= due:
            return mark_escalation(payload, now=current, reason="acknowledgement_overdue")
    if all_destinations_delivered(payload):
        changed = payload.get("delivery_state") != "delivered" or payload.get("retry_due_at") is not None
        payload["delivery_state"] = "delivered"
        payload["retry_due_at"] = None
        return changed

    attempts = payload.get("delivery_attempts")
    attempts = list(attempts) if isinstance(attempts, list) else []
    retry_limit = positive_int(payload.get("retry_limit"), 3)
    backoff = normalized_retry_backoff(payload.get("retry_backoff_minutes"), retry_limit)
    payload["retry_limit"] = retry_limit
    payload["retry_backoff_minutes"] = backoff
    # Older or interrupted writes can leave an attention-required alert in a
    # pending state without a prepared handoff or deadline.  Recover that
    # lifecycle record explicitly.  A pending handoff is not delivery proof:
    # only record_delivery_receipt can add a durable receipt.
    if not attempts:
        attempts.append(
            {
                "attempt": 1,
                "prepared_at": iso_utc(current),
                "destinations": list(payload.get("destinations", [])),
                "status": "pending_handoff",
                "reason": "recovered_missing_delivery_attempt",
            }
        )
        payload["delivery_attempts"] = attempts
        payload["delivery_state"] = "pending"
        payload["retry_due_at"] = retry_due_at(attempts, backoff, now=current)
        return True
    due = parse_utc(payload.get("retry_due_at"))
    if due is None:
        if payload.get("retry_due_at") not in (None, ""):
            return mark_escalation(payload, now=current, reason="invalid_retry_deadline")
        scheduled = retry_due_at(attempts, backoff, now=current)
        payload["retry_due_at"] = scheduled
        payload["delivery_state"] = "pending"
        return scheduled is not None
    if current < due:
        return False
    if len(attempts) >= retry_limit:
        return mark_escalation(payload, now=current, reason="delivery_retry_limit_exhausted")

    attempts.append(
        {
            "attempt": len(attempts) + 1,
            "prepared_at": iso_utc(current),
            "destinations": list(payload.get("destinations", [])),
            "status": "pending_handoff",
            "reason": "automatic_retry_after_backoff",
        }
    )
    payload["delivery_attempts"] = attempts
    payload["delivery_state"] = "pending"
    payload["retry_due_at"] = retry_due_at(attempts, backoff, now=current)
    return True


def _process_due_alerts_locked(
    *,
    runtime_root: Path,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Apply lifecycle transitions to persisted alert files while holding the transaction lock."""
    alerts_root = runtime_root / "alerts"
    seen_alert_ids: set[str] = set()
    candidates: list[tuple[dict[str, Any], Path | None]] = []
    # Revision records are authoritative for historical alerts.  If a record
    # is also the current date pointer, load that compatibility copy so an
    # interrupted legacy write is repaired into the record rather than lost.
    for record_path, stored in load_alert_records(runtime_root):
        alert_id = str(stored.get("alert_id") or "")
        date = stored.get("date")
        if not isinstance(date, str):
            continue
        try:
            alias = read_json(alert_alias_path(runtime_root, date))
        except ValueError:
            alias = {}
        payload = alias if alias.get("alert_id") == alert_id else stored
        candidates.append((payload, record_path))
        seen_alert_ids.add(alert_id)

    # Migrate date-only legacy payloads once.  Do not scan latest.json: it is
    # a pointer and would otherwise process the same alert twice.
    for path in sorted(alerts_root.glob("alert-*.json")):
        payload = read_json(path)
        alert_id = str(payload.get("alert_id") or "")
        if not payload or not alert_id or alert_id in seen_alert_ids:
            continue
        try:
            expected_alias = alert_alias_path(runtime_root, str(payload.get("date") or ""))
            record_path = alert_record_path(runtime_root, alert_id)
        except ValueError:
            continue
        if path != expected_alias:
            continue
        candidates.append((payload, record_path))
        seen_alert_ids.add(alert_id)

    changed: list[dict[str, Any]] = []
    for payload, record_path in candidates:
        if not advance_alert_lifecycle(payload, now=now):
            # Create a revision record even when no deadline fired.  This
            # one-time migration prevents a subsequent same-day content
            # change from erasing a pre-revision date-only alert.
            if record_path is None or not record_path.is_file():
                persist_alert_revision(payload, runtime_root=runtime_root)
            continue
        persist_alert_revision(payload, runtime_root=runtime_root)
        changed.append(
            {
                "date": str(payload.get("date") or ""),
                "alert_id": str(payload.get("alert_id") or ""),
                "delivery_state": str(payload.get("delivery_state") or ""),
            }
        )
    return changed


@synchronized_alert_operation
def process_due_alerts(
    *,
    runtime_root: Path = RUNTIME_ROOT,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Consume retry/acknowledgement deadlines for scheduled automation."""
    return _process_due_alerts_locked(runtime_root=runtime_root, now=now)


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
    now: datetime | None = None,
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
    # A normal daily alert invocation is also a lifecycle consumer, so an
    # expired acknowledgement/retry deadline cannot remain inert forever.
    _process_due_alerts_locked(runtime_root=runtime_root, now=now)
    previous, _previous_record = _select_latest_date_record(runtime_root, date)
    fingerprint = alert_fingerprint(date, list(destinations), findings)
    alert_id = f"ATLAS-ALERT-{date.replace('-', '')}-{fingerprint[:16].upper()}"
    same_alert = previous.get("alert_id") == alert_id
    # A deployment can encounter a legacy date-only payload before its first
    # revision write.  Materialize it before moving that date pointer so the
    # changed content cannot erase an unresolved historical alert.
    if previous and not same_alert:
        persist_alert_revision(previous, runtime_root=runtime_root)
    current = utc_now(now)
    previous_generated_at = str(previous.get("generated_at") or "") if same_alert else ""
    generated_at = previous_generated_at if parse_utc(previous_generated_at) else iso_utc(current)
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
    retry_limit = positive_int(config.get("retry_limit"), 3)
    retry_backoff = normalized_retry_backoff(config.get("retry_backoff_minutes"), retry_limit)
    ack_timeout = positive_int(config.get("ack_timeout_minutes"), 60)
    previous_retry_due_at = previous.get("retry_due_at") if same_alert else None
    if previous_retry_due_at is not None and parse_utc(previous_retry_due_at) is None:
        previous_retry_due_at = None
    scheduled_retry_due_at = previous_retry_due_at
    if findings and not delivered and delivery_state != "escalation_required" and scheduled_retry_due_at is None:
        scheduled_retry_due_at = retry_due_at(attempts, retry_backoff, now=current)
    prior_revision = _revision_number(previous) if previous else 0
    payload = {
        "schema_version": 2,
        "alert_id": alert_id,
        "content_sha256": fingerprint,
        "date": date,
        "revision": prior_revision if same_alert else prior_revision + 1,
        "supersedes_alert_id": (
            str(previous.get("alert_id")) if previous and not same_alert else previous.get("supersedes_alert_id")
        ),
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
        "retry_limit": retry_limit,
        "retry_backoff_minutes": retry_backoff,
        "retry_due_at": scheduled_retry_due_at,
        "requires_acknowledgement": requires_ack,
        "acknowledged_at": acknowledged_at,
        "acknowledged_by": previous.get("acknowledged_by") if same_alert else None,
        "escalation_due_at": (
            iso_utc((parse_utc(generated_at) or current) + timedelta(minutes=ack_timeout))
            if requires_ack
            else None
        ),
        "escalation": previous.get("escalation") if same_alert and isinstance(previous.get("escalation"), dict) else None,
        "finding_count": len(findings),
        "findings": findings,
    }
    persist_alert_revision(payload, runtime_root=runtime_root, make_current=True)
    return payload


@synchronized_alert_operation
def record_delivery_receipt(
    date: str,
    destination: str,
    receipt_id: str,
    *,
    runtime_root: Path = RUNTIME_ROOT,
    alert_id: str | None = None,
) -> dict[str, Any]:
    try:
        payload, _record_path = load_alert_revision(
            date,
            runtime_root=runtime_root,
            alert_id=alert_id,
        )
    except ValueError as exc:
        if alert_id:
            raise
        raise ValueError(f"no alert exists for {date}") from exc
    if not payload:
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
    current = utc_now()
    if not existing_id:
        receipts[target] = {
            "receipt_id": receipt,
            "recorded_at": iso_utc(current),
        }
    payload["delivery_receipts"] = receipts
    advance_alert_lifecycle(payload, now=current)
    persist_alert_revision(payload, runtime_root=runtime_root)
    update_channel_health(payload, target, receipts[target], runtime_root=runtime_root)
    return payload


@synchronized_alert_operation
def acknowledge_alert(
    date: str,
    acknowledged_by: str,
    *,
    runtime_root: Path = RUNTIME_ROOT,
    alert_id: str | None = None,
) -> dict[str, Any]:
    try:
        payload, _record_path = load_alert_revision(
            date,
            runtime_root=runtime_root,
            alert_id=alert_id,
        )
    except ValueError as exc:
        if alert_id:
            raise
        raise ValueError(f"no alert exists for {date}") from exc
    if not payload:
        raise ValueError(f"no alert exists for {date}")
    actor = acknowledged_by.strip()
    if not actor:
        raise ValueError("acknowledged_by is required")
    if payload.get("findings") and not all_destinations_delivered(payload):
        raise ValueError("cannot acknowledge an alert before every destination has a delivery receipt")
    payload["acknowledged_at"] = iso_utc(utc_now())
    payload["acknowledged_by"] = actor
    payload["delivery_state"] = "acknowledged"
    payload["retry_due_at"] = None
    persist_alert_revision(payload, runtime_root=runtime_root)
    return payload


@synchronized_alert_operation
def retry_alert(
    date: str,
    *,
    runtime_root: Path = RUNTIME_ROOT,
    alert_id: str | None = None,
) -> dict[str, Any]:
    try:
        payload, _record_path = load_alert_revision(
            date,
            runtime_root=runtime_root,
            alert_id=alert_id,
        )
    except ValueError as exc:
        if alert_id:
            raise
        raise ValueError(f"no alert exists for {date}") from exc
    if not payload:
        raise ValueError(f"no alert exists for {date}")
    # A direct manual retry must not bypass an acknowledgement deadline merely
    # because the scheduled consumer has not run yet.  Persist the escalation
    # before rejecting the operation so the state cannot appear downgraded.
    current = utc_now()
    advance_alert_lifecycle(payload, now=current)
    if payload.get("delivery_state") == "escalation_required":
        persist_alert_revision(payload, runtime_root=runtime_root)
        raise ValueError(
            "cannot retry an alert that requires human escalation; "
            "record delivery evidence and acknowledge it instead"
        )
    if payload.get("delivery_state") in {"delivered", "acknowledged"}:
        raise ValueError("cannot retry an alert that has already been delivered")
    attempts = list(payload.get("delivery_attempts", []))
    retry_limit = positive_int(payload.get("retry_limit"), 3)
    backoff = normalized_retry_backoff(payload.get("retry_backoff_minutes"), retry_limit)
    if len(attempts) >= retry_limit:
        mark_escalation(payload, now=current, reason="delivery_retry_limit_exhausted")
    else:
        attempts.append(
            {
                "attempt": len(attempts) + 1,
                "prepared_at": iso_utc(current),
                "destinations": list(payload.get("destinations", [])),
                "status": "pending_handoff",
                "reason": "manual_retry",
            }
        )
        payload["delivery_attempts"] = attempts
        payload["delivery_state"] = "pending"
        payload["retry_limit"] = retry_limit
        payload["retry_backoff_minutes"] = backoff
        payload["retry_due_at"] = retry_due_at(attempts, backoff, now=current)
    persist_alert_revision(payload, runtime_root=runtime_root)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the daily ATLAS external alert payload.")
    parser.add_argument("--date")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ack-by")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--receipt-destination")
    parser.add_argument("--receipt-id")
    parser.add_argument(
        "--alert-id",
        help="Target one exact historical alert revision for acknowledgement, retry, or receipt recording.",
    )
    parser.add_argument(
        "--process-due",
        action="store_true",
        help="consume persisted retry and acknowledgement deadlines without generating a new alert",
    )
    args = parser.parse_args()
    receipt_mode = bool(args.receipt_destination or args.receipt_id)
    if receipt_mode and not (args.receipt_destination and args.receipt_id):
        parser.error("--receipt-destination and --receipt-id must be provided together")
    if sum(bool(value) for value in (args.ack_by, args.retry, receipt_mode, args.process_due)) > 1:
        parser.error("acknowledge, retry, and delivery-receipt modes are mutually exclusive")
    if args.alert_id and not (args.ack_by or args.retry or receipt_mode):
        parser.error("--alert-id only applies to acknowledgement, retry, or delivery-receipt modes")
    if args.alert_id:
        try:
            alert_record_path(RUNTIME_ROOT, args.alert_id)
        except ValueError:
            parser.error("--alert-id must be a valid ATLAS alert identifier")
    if not args.process_due and not args.date:
        parser.error("--date is required unless --process-due is used")
    if args.process_due:
        processed = process_due_alerts()
        if args.json:
            print(json.dumps({"processed": processed}, ensure_ascii=False, indent=2))
        else:
            print(f"processed_due_alerts={len(processed)}")
        return 0
    payload = (
        acknowledge_alert(args.date, args.ack_by, alert_id=args.alert_id)
        if args.ack_by
        else retry_alert(args.date, alert_id=args.alert_id)
        if args.retry
        else record_delivery_receipt(
            args.date,
            args.receipt_destination,
            args.receipt_id,
            alert_id=args.alert_id,
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
