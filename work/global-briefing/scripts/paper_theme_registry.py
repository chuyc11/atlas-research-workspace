#!/usr/bin/env python3
"""Validate, version, and query the non-economic paper-position theme registry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "paper_trading.json"
DEFAULT_REGISTRY_PATH = ROOT / "work" / "global-briefing" / "config" / "paper_theme_registry.json"
DEFAULT_HISTORY_PATH = ROOT / "work" / "shared" / "atlas" / "paper_theme_registry" / "history.jsonl"
DEFAULT_SNAPSHOT_DIR = ROOT / "work" / "shared" / "atlas" / "paper_theme_registry" / "snapshots"


def parse_date(value: Any) -> str:
    canonical = datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()
    return canonical


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


@contextmanager
def exclusive_lock(path: Path, timeout_seconds: float = 5.0):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for paper theme registry lock {lock_path}") from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def position_key(account: Any, symbol: Any, exchange: Any) -> str:
    return f"{str(account or '').strip().upper()}|{str(exchange or '').strip().upper()}|{str(symbol or '').strip().upper()}"


def registry_path(config: dict[str, Any], root: Path = ROOT) -> Path:
    raw = str(config.get("theme_registry_file") or "work/global-briefing/config/paper_theme_registry.json")
    path = Path(raw)
    return path if path.is_absolute() else root / path


def history_path(config: dict[str, Any], root: Path = ROOT) -> Path:
    raw = str(config.get("theme_registry_history_file") or "work/shared/atlas/paper_theme_registry/history.jsonl")
    path = Path(raw)
    return path if path.is_absolute() else root / path


def snapshot_dir(config: dict[str, Any], root: Path = ROOT) -> Path:
    raw = str(config.get("theme_registry_snapshot_dir") or "work/shared/atlas/paper_theme_registry/snapshots")
    path = Path(raw)
    return path if path.is_absolute() else root / path


def relative_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def snapshot_path(config: dict[str, Any], registry_sha256: str, root: Path = ROOT) -> Path:
    return snapshot_dir(config, root) / f"{registry_sha256}.json"


def read_history(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"history line {line_number} is invalid JSON: {exc.msg}")
            continue
        if not isinstance(item, dict):
            errors.append(f"history line {line_number} must be a JSON object")
            continue
        records.append(item)
    return records, errors


def validate_registry(payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["registry must be a JSON object"]
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    themes = payload.get("allowed_themes")
    if not isinstance(themes, list) or not themes or not all(isinstance(value, str) and value.strip() for value in themes):
        errors.append("allowed_themes must be a non-empty string list")
        allowed: set[str] = set()
    else:
        allowed = {str(value) for value in themes}
        if len(allowed) != len(themes):
            errors.append("allowed_themes contains duplicates")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return [*errors, "entries must be a list"]
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        prefix = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        key = position_key(entry.get("account"), entry.get("symbol"), entry.get("exchange"))
        if not all(str(entry.get(field) or "").strip() for field in ("account", "symbol", "exchange")):
            errors.append(f"{prefix} requires account, symbol, and exchange")
        if str(entry.get("account") or "").upper() not in {"US", "CHINA"}:
            errors.append(f"{prefix}.account must be US or CHINA")
        if key in seen:
            errors.append(f"duplicate position key {key}")
        seen.add(key)
        primary = str(entry.get("primary_theme") or "")
        if primary not in allowed:
            errors.append(f"{prefix}.primary_theme is not allowed")
        secondary = entry.get("secondary_themes", [])
        if not isinstance(secondary, list) or not all(isinstance(value, str) for value in secondary):
            errors.append(f"{prefix}.secondary_themes must be a string list")
        else:
            if primary in secondary:
                errors.append(f"{prefix}.secondary_themes repeats primary_theme")
            unknown = sorted(set(secondary) - allowed)
            if unknown:
                errors.append(f"{prefix}.secondary_themes contains unknown themes: {', '.join(unknown)}")
        if str(entry.get("status") or "") not in {"verified", "draft", "retired"}:
            errors.append(f"{prefix}.status must be verified, draft, or retired")
        try:
            parse_date(entry.get("effective_from"))
        except (TypeError, ValueError):
            errors.append(f"{prefix}.effective_from must be YYYY-MM-DD")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(value, str) and value.strip() for value in evidence):
            errors.append(f"{prefix}.evidence must be a non-empty string list")
    return errors


def load_registry(config: dict[str, Any], root: Path = ROOT, *, strict: bool = True) -> dict[str, Any]:
    path = registry_path(config, root)
    payload = read_json(path, {"schema_version": 1, "allowed_themes": [], "entries": []})
    errors = validate_registry(payload)
    if strict and errors:
        raise ValueError(f"Invalid paper theme registry {path}: {'; '.join(errors)}")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["_path"] = str(path)
        payload["_errors"] = errors
        return payload
    return {"schema_version": 1, "allowed_themes": [], "entries": [], "_path": str(path), "_errors": errors}


def registry_index(payload: dict[str, Any], as_of: str | None = None) -> dict[str, dict[str, Any]]:
    cutoff = parse_date(as_of) if as_of else None
    result: dict[str, dict[str, Any]] = {}
    for entry in payload.get("entries", []) if isinstance(payload.get("entries"), list) else []:
        if not isinstance(entry, dict) or entry.get("status") != "verified":
            continue
        effective = parse_date(entry.get("effective_from"))
        if cutoff and effective > cutoff:
            continue
        result[position_key(entry.get("account"), entry.get("symbol"), entry.get("exchange"))] = entry
    return result


def assignment_for(
    payload: dict[str, Any], account: Any, symbol: Any, exchange: Any, as_of: str | None = None
) -> dict[str, Any] | None:
    return registry_index(payload, as_of).get(position_key(account, symbol, exchange))


def resolve_order_theme(
    payload: dict[str, Any],
    *,
    account: Any,
    symbol: Any,
    exchange: Any,
    date: str,
    explicit_theme: Any = None,
    required: bool = False,
) -> tuple[str | None, str | None]:
    explicit = str(explicit_theme or "").strip() or None
    allowed = {str(value) for value in payload.get("allowed_themes", [])}
    if explicit and allowed and explicit not in allowed:
        raise ValueError(f"Unknown paper theme {explicit!r} for {account}:{exchange}:{symbol}.")
    assignment = assignment_for(payload, account, symbol, exchange, date)
    registered = str(assignment.get("primary_theme")) if assignment else None
    if explicit and registered and explicit != registered:
        raise ValueError(
            f"Paper theme {explicit!r} conflicts with verified registry theme {registered!r} for {account}:{exchange}:{symbol}."
        )
    theme = explicit or registered
    source = "explicit_order" if explicit else "verified_registry" if registered else None
    if required and not theme:
        raise ValueError(f"BUY {account}:{exchange}:{symbol} requires a canonical paper theme.")
    return theme, source


def coverage(config: dict[str, Any], payload: dict[str, Any], root: Path, date: str) -> dict[str, Any]:
    index = registry_index(payload, date)
    accounts_out = []
    for account, account_config in config.get("accounts", {}).items():
        portfolio_path = Path(str(account_config.get("portfolio_file") or ""))
        if not portfolio_path.is_absolute():
            portfolio_path = root / portfolio_path
        portfolio = read_json(portfolio_path, {})
        positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else {}
        last_prices = portfolio.get("last_prices", {}) if isinstance(portfolio, dict) else {}
        total_value = 0.0
        covered_value = 0.0
        covered_count = 0
        missing = []
        for key, position in positions.items() if isinstance(positions, dict) else []:
            if not isinstance(position, dict) or float(position.get("quantity") or 0.0) <= 0:
                continue
            price_item = last_prices.get(key, {}) if isinstance(last_prices, dict) else {}
            price = float(price_item.get("price", position.get("avg_cost", 0.0)) or 0.0)
            fx = float(price_item.get("fx_to_base", position.get("fx_to_base", 1.0)) or 1.0)
            value = float(position.get("quantity") or 0.0) * price * fx
            total_value += value
            registry_key = position_key(account, position.get("symbol"), position.get("exchange"))
            if position.get("theme") or registry_key in index:
                covered_count += 1
                covered_value += value
            else:
                missing.append(registry_key)
        position_count = sum(1 for value in positions.values() if isinstance(value, dict) and float(value.get("quantity") or 0.0) > 0) if isinstance(positions, dict) else 0
        accounts_out.append({
            "account": account,
            "position_count": position_count,
            "covered_position_count": covered_count,
            "position_count_coverage_pct": round(covered_count / position_count * 100, 2) if position_count else 100.0,
            "position_value_coverage_pct": round(covered_value / total_value * 100, 2) if total_value else 100.0,
            "missing_position_keys": missing,
        })
    return {"accounts": accounts_out, "all_accounts_fully_covered": all(row["position_value_coverage_pct"] == 100.0 for row in accounts_out)}


def validation_payload(config: dict[str, Any], root: Path, date: str) -> dict[str, Any]:
    path = registry_path(config, root)
    registry = load_registry(config, root, strict=False)
    errors = list(registry.get("_errors", []))
    coverage_result = coverage(config, registry, root, date) if not errors else {"accounts": [], "all_accounts_fully_covered": False}
    return {
        "schema_version": 1,
        "date": date,
        "registry_path": str(path),
        "registry_sha256": file_hash(path),
        "valid": not errors,
        "errors": errors,
        "verified_entry_count": len(registry_index(registry, date)) if not errors else 0,
        "coverage": coverage_result,
        "economic_ledger_mutations": [],
        "paper_trading_only": True,
    }


def registry_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous = previous or {"allowed_themes": [], "entries": []}
    previous_entries = {
        position_key(item.get("account"), item.get("symbol"), item.get("exchange")): item
        for item in previous.get("entries", [])
        if isinstance(item, dict)
    }
    current_entries = {
        position_key(item.get("account"), item.get("symbol"), item.get("exchange")): item
        for item in current.get("entries", [])
        if isinstance(item, dict)
    }
    added_keys = sorted(set(current_entries) - set(previous_entries))
    removed_keys = sorted(set(previous_entries) - set(current_entries))
    changed_keys = sorted(
        key for key in set(previous_entries) & set(current_entries)
        if stable_json(previous_entries[key]) != stable_json(current_entries[key])
    )
    previous_themes = {str(value) for value in previous.get("allowed_themes", [])}
    current_themes = {str(value) for value in current.get("allowed_themes", [])}
    metadata_fields = ("schema_version", "updated", "method")
    metadata_changes = {
        field: {"before": previous.get(field), "after": current.get(field)}
        for field in metadata_fields
        if previous.get(field) != current.get(field)
    }
    return {
        "added": [{"key": key, "entry": current_entries[key]} for key in added_keys],
        "removed": [{"key": key, "entry": previous_entries[key]} for key in removed_keys],
        "changed": [
            {"key": key, "before": previous_entries[key], "after": current_entries[key]}
            for key in changed_keys
        ],
        "allowed_themes_added": sorted(current_themes - previous_themes),
        "allowed_themes_removed": sorted(previous_themes - current_themes),
        "metadata_changes": metadata_changes,
    }


def revision_core(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"revision_id", "chain_hash"}}


def expected_revision_identity(record: dict[str, Any]) -> tuple[str, str]:
    chain_hash = stable_hash(revision_core(record))
    registry_sha = str(record.get("registry_sha256") or "")
    revision_id = f"THEME-REG-{registry_sha[:12].upper()}-{chain_hash[:12].upper()}"
    return revision_id, chain_hash


def audit_history(
    config: dict[str, Any],
    root: Path,
    date: str,
    *,
    require_current: bool = True,
) -> dict[str, Any]:
    date = parse_date(date)
    validation = validation_payload(config, root, date)
    path = history_path(config, root)
    records, parse_errors = read_history(path)
    chain_errors = list(parse_errors)
    snapshot_errors: list[str] = []
    seen_revisions: set[str] = set()
    previous_revision_id: str | None = None
    previous_registry_sha: str | None = None
    previous_chain_hash: str | None = None
    expected_snapshot_root = snapshot_dir(config, root).resolve()

    if not path.is_file():
        chain_errors.append(f"history file does not exist: {path}")
    elif not records and not parse_errors:
        chain_errors.append("history contains no revisions")

    for index, record in enumerate(records):
        label = f"history[{index}]"
        if record.get("schema_version") != 1:
            chain_errors.append(f"{label}.schema_version must be 1")
        try:
            parse_date(record.get("revision_date"))
        except (TypeError, ValueError):
            chain_errors.append(f"{label}.revision_date must be YYYY-MM-DD")
        reason = str(record.get("reason") or "").strip()
        if len(reason) < 12:
            chain_errors.append(f"{label}.reason must contain at least 12 characters")
        registry_sha = str(record.get("registry_sha256") or "")
        if len(registry_sha) != 64 or any(character not in "0123456789abcdef" for character in registry_sha.lower()):
            chain_errors.append(f"{label}.registry_sha256 must be a SHA-256 hex digest")
        if record.get("previous_revision_id") != previous_revision_id:
            chain_errors.append(f"{label}.previous_revision_id breaks the append-only chain")
        if record.get("previous_registry_sha256") != previous_registry_sha:
            chain_errors.append(f"{label}.previous_registry_sha256 breaks the append-only chain")
        if record.get("previous_chain_hash") != previous_chain_hash:
            chain_errors.append(f"{label}.previous_chain_hash breaks the append-only chain")
        expected_revision_id, expected_chain_hash = expected_revision_identity(record)
        if record.get("chain_hash") != expected_chain_hash:
            chain_errors.append(f"{label}.chain_hash does not match record content")
        if record.get("revision_id") != expected_revision_id:
            chain_errors.append(f"{label}.revision_id does not match its content address")
        if expected_revision_id in seen_revisions:
            chain_errors.append(f"duplicate revision_id {expected_revision_id}")
        seen_revisions.add(expected_revision_id)

        expected_snapshot = snapshot_path(config, registry_sha, root).resolve()
        try:
            expected_snapshot.relative_to(expected_snapshot_root)
        except ValueError:
            snapshot_errors.append(f"{label} snapshot escapes configured snapshot directory")
        recorded_snapshot = str(record.get("snapshot_path") or "")
        if recorded_snapshot != relative_path(expected_snapshot, root):
            snapshot_errors.append(f"{label}.snapshot_path is not the content-addressed snapshot path")
        actual_snapshot_hash = file_hash(expected_snapshot)
        if actual_snapshot_hash != registry_sha or record.get("snapshot_sha256") != registry_sha:
            snapshot_errors.append(f"{label} snapshot is missing or its SHA-256 does not match")
        else:
            snapshot_payload = read_json(expected_snapshot, None)
            snapshot_registry_errors = validate_registry(snapshot_payload)
            snapshot_errors.extend(f"{label} snapshot: {error}" for error in snapshot_registry_errors)

        previous_revision_id = str(record.get("revision_id") or "") or None
        previous_registry_sha = registry_sha or None
        previous_chain_hash = str(record.get("chain_hash") or "") or None

    current_sha = validation.get("registry_sha256")
    latest = records[-1] if records else {}
    history_current = bool(records and latest.get("registry_sha256") == current_sha)
    current_errors: list[str] = []
    if require_current and not history_current:
        current_errors.append("current registry SHA-256 is not the latest recorded revision")
    coverage_result = validation.get("coverage", {})
    return {
        **validation,
        "history_path": str(path),
        "snapshot_dir": str(snapshot_dir(config, root)),
        "revision_count": len(records),
        "current_revision_id": latest.get("revision_id") if history_current else None,
        "current_registry_sha256": current_sha,
        "latest_recorded_registry_sha256": latest.get("registry_sha256"),
        "latest_revision_date": latest.get("revision_date"),
        "history_current": history_current,
        "chain_valid": not chain_errors,
        "snapshots_valid": not snapshot_errors,
        "history_errors": [*chain_errors, *current_errors],
        "snapshot_errors": snapshot_errors,
        "audit_passed": bool(
            validation.get("valid")
            and coverage_result.get("all_accounts_fully_covered")
            and not chain_errors
            and not current_errors
            and not snapshot_errors
            and (history_current or not require_current)
        ),
    }


def record_revision(
    config: dict[str, Any],
    root: Path,
    date: str,
    reason: str,
    *,
    allow_revert: bool = False,
) -> dict[str, Any]:
    date = parse_date(date)
    reason = str(reason or "").strip()
    if len(reason) < 12:
        raise ValueError("Revision reason must contain at least 12 characters.")
    validation = validation_payload(config, root, date)
    if not validation.get("valid"):
        raise ValueError("Cannot record an invalid paper theme registry: " + "; ".join(validation.get("errors", [])))
    if not validation.get("coverage", {}).get("all_accounts_fully_covered"):
        raise ValueError("Cannot record a paper theme registry that does not cover every open position.")

    path = history_path(config, root)
    registry = registry_path(config, root)
    current_sha = str(validation["registry_sha256"])
    with exclusive_lock(path):
        history_existed = path.is_file()
        original_history_bytes = path.read_bytes() if history_existed else b""
        records, parse_errors = read_history(path)
        if parse_errors:
            raise ValueError("Cannot append to invalid registry history: " + "; ".join(parse_errors))
        if records:
            existing_audit = audit_history(config, root, date, require_current=False)
            if not existing_audit.get("chain_valid") or not existing_audit.get("snapshots_valid"):
                raise ValueError(
                    "Cannot append to invalid registry history: "
                    + "; ".join([*existing_audit.get("history_errors", []), *existing_audit.get("snapshot_errors", [])])
                )
            if records[-1].get("registry_sha256") == current_sha:
                result = audit_history(config, root, date)
                return {**result, "recorded": False, "unchanged": True}
            if any(record.get("registry_sha256") == current_sha for record in records) and not allow_revert:
                raise ValueError("Registry content matches an older revision; use --allow-revert with an explicit reason.")

        previous = records[-1] if records else None
        previous_payload = None
        if previous:
            previous_snapshot = snapshot_path(config, str(previous.get("registry_sha256")), root)
            previous_payload = read_json(previous_snapshot, None)
        current_payload = read_json(registry, {})
        content_snapshot = snapshot_path(config, current_sha, root)
        snapshot_existed = content_snapshot.exists()
        if content_snapshot.exists() and file_hash(content_snapshot) != current_sha:
            raise ValueError(f"Existing content-addressed snapshot is corrupt: {content_snapshot}")
        if not content_snapshot.exists():
            atomic_write_bytes(content_snapshot, registry.read_bytes())
        if file_hash(content_snapshot) != current_sha:
            raise ValueError("Snapshot verification failed before revision append.")

        repeated_content = any(record.get("registry_sha256") == current_sha for record in records)
        record: dict[str, Any] = {
            "schema_version": 1,
            "revision_date": date,
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "change_type": "revert" if repeated_content else "change" if previous else "baseline",
            "registry_sha256": current_sha,
            "previous_registry_sha256": previous.get("registry_sha256") if previous else None,
            "previous_revision_id": previous.get("revision_id") if previous else None,
            "previous_chain_hash": previous.get("chain_hash") if previous else None,
            "reason": reason,
            "entry_count": len(current_payload.get("entries", [])),
            "verified_entry_count": len(registry_index(current_payload, date)),
            "diff": registry_diff(previous_payload, current_payload),
            "snapshot_path": relative_path(content_snapshot, root),
            "snapshot_sha256": current_sha,
            "economic_ledger_mutations": [],
            "paper_trading_only": True,
        }
        revision_id, chain_hash = expected_revision_identity(record)
        record["revision_id"] = revision_id
        record["chain_hash"] = chain_hash
        records.append(record)
        atomic_write_text(path, "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records))

        result = audit_history(config, root, date)
        if not result.get("audit_passed"):
            if history_existed:
                atomic_write_bytes(path, original_history_bytes)
            else:
                path.unlink(missing_ok=True)
            if not snapshot_existed:
                content_snapshot.unlink(missing_ok=True)
            raise ValueError(
                "Registry revision append failed verification: "
                + "; ".join([*result.get("history_errors", []), *result.get("snapshot_errors", [])])
            )
        return {**result, "recorded": True, "unchanged": False, "record": record}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and version the paper-position theme registry.")
    parser.add_argument("command", choices=["validate", "audit", "record-revision"])
    parser.add_argument("--date", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--allow-revert", action="store_true")
    args = parser.parse_args()
    parse_date(args.date)
    config = read_json(CONFIG_PATH, {})
    if args.command == "validate":
        result = validation_payload(config, ROOT, args.date)
        passed = result["valid"] and result["coverage"]["all_accounts_fully_covered"]
    elif args.command == "audit":
        result = audit_history(config, ROOT, args.date)
        passed = result["audit_passed"]
    else:
        result = record_revision(config, ROOT, args.date, args.reason or "", allow_revert=args.allow_revert)
        passed = result["audit_passed"]
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
