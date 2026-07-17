#!/usr/bin/env python3
"""Unified command surface for the ATLAS briefing and research workspace."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date as Date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
TRADING_ROOT = ROOT / "work" / "trading-core"
SITE_ROOT = ROOT / "src"
BRIEFING_SETTINGS_PATH = BRIEFING_ROOT / "config" / "settings.json"
OUTPUTS_ROOT = ROOT / "outputs"
ATLAS_RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas"
VIRTUAL_LEDGER_ROOT = ATLAS_RUNTIME_ROOT / "virtual_execution"
VIRTUAL_LEDGER_PATH = VIRTUAL_LEDGER_ROOT / "atlas_virtual_execution_ledger.jsonl"
VIRTUAL_LEDGER_STATE_PATH = VIRTUAL_LEDGER_ROOT / "atlas_virtual_execution_state.json"
VIRTUAL_LEDGER_AUDIT_PATH = VIRTUAL_LEDGER_ROOT / "atlas_virtual_execution_audit.json"
RUN_AUDIT_ROOT = ATLAS_RUNTIME_ROOT / "run_audits"
CYCLE_STATE_PATH = ATLAS_RUNTIME_ROOT / "cycle_state.json"
REPORT_PATTERN = re.compile(r"每日全球晨间简报-(\d{4}-\d{2}-\d{2})\.md$")
REPLAY_EVALUATION_PATTERN = re.compile(
    r"global_briefing_replay_evaluation-(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})\.json$"
)
LEDGER_ID = "atlas-virtual-execution-ledger-v1"
LEDGER_SCHEMA_VERSION = 1
GIT_REMOTE_PROBE_TIMEOUT_SECONDS = 5
MINIMUM_NODE_VERSION = (22, 15, 0)
RELEASE_REQUIRED_STAGES = (
    "doctor",
    "sync",
    "canonical_virtual_ledger_audit",
    "replay_shadow_gate",
    "targeted_integration_tests",
    "canonical_virtual_ledger_commit",
)
FORBIDDEN_LEDGER_KEYS = {
    "broker_order_id",
    "broker_account",
    "account_number",
    "live_order_id",
    "external_order_id",
    "real_order_id",
}
SAFETY_FLAGS_REQUIRED_TRUE = {
    "paper_trading_only",
    "no_real_broker_order",
}
SAFETY_FLAGS_REQUIRED_FALSE = {
    "external_broker_connection",
    "live_trading",
    "real_broker_orders_allowed",
}
NORMALIZED_FORBIDDEN_LEDGER_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in FORBIDDEN_LEDGER_KEYS
}
NORMALIZED_SAFETY_FLAGS_REQUIRED_TRUE = {
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in SAFETY_FLAGS_REQUIRED_TRUE
}
NORMALIZED_SAFETY_FLAGS_REQUIRED_FALSE = {
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in SAFETY_FLAGS_REQUIRED_FALSE
}
NORMALIZED_LEDGER_NUMERIC_KEYS = {
    "cash",
    "cashafter",
    "equity",
    "equityafter",
    "fee",
    "filledprice",
    "filledquantity",
    "grossvalue",
    "initialcash",
    "notional",
    "price",
    "quantity",
    "realizedpnl",
    "tax",
}
NORMALIZED_NON_NEGATIVE_LEDGER_NUMERIC_KEYS = {
    "fee",
    "filledprice",
    "filledquantity",
    "grossvalue",
    "notional",
    "price",
    "quantity",
    "tax",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    required: bool = True


def command_env(*, trading_core: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if trading_core:
        source = str(TRADING_ROOT / "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source if not existing else f"{source}{os.pathsep}{existing}"
    return env


def run_command(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    trading_core: bool = False,
    quiet: bool = False,
) -> int:
    if not quiet:
        print(f"\n> {' '.join(command)}", flush=True)
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=command_env(trading_core=trading_core),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode


def capture_command(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    trading_core: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=command_env(trading_core=trading_core),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def probe_git_remotes(
    git: str,
    path: Path,
    remote_names: Sequence[str],
    *,
    head_commit: str,
) -> dict[str, Any]:
    """Verify that the exact local HEAD is advertised by at least one configured remote."""
    probes: list[dict[str, Any]] = []
    for remote in remote_names:
        try:
            result = capture_command(
                [git, "-c", "credential.interactive=never", "ls-remote", remote],
                cwd=path,
                timeout=GIT_REMOTE_PROBE_TIMEOUT_SECONDS,
            )
            advertised_commits = {
                line.split()[0]
                for line in result.stdout.splitlines()
                if len(line.split()) >= 2 and re.fullmatch(r"[0-9a-fA-F]{40}", line.split()[0])
            }
            head_advertised = bool(head_commit and head_commit in advertised_commits)
            fetchable = result.returncode == 0 and head_advertised
            probes.append(
                {
                    "remote": remote,
                    "fetchable": fetchable,
                    "head_advertised": head_advertised,
                    "returncode": result.returncode,
                    "detail": (
                        "head_advertised"
                        if fetchable
                        else "head_not_advertised"
                        if result.returncode == 0
                        else "probe_failed"
                    ),
                }
            )
        except subprocess.TimeoutExpired:
            probes.append(
                {
                    "remote": remote,
                    "fetchable": False,
                    "returncode": None,
                    "detail": "probe_timeout",
                }
            )
    fetchable_remote_names = sorted(
        str(probe["remote"]) for probe in probes if probe["fetchable"] is True
    )
    return {
        "remote_fetchable": bool(fetchable_remote_names),
        "fetchable_remote_names": fetchable_remote_names,
        "remote_probes": probes,
    }


def component_repository_check(name: str, path: Path) -> Check:
    """Report whether a separately governed workspace component is reproducible."""
    git = shutil.which("git")
    if not path.exists():
        return Check(f"{name} repository", "warn", "component path is missing", required=False)
    if not git:
        return Check(f"{name} repository", "warn", "git is unavailable", required=False)
    if not (path / ".git").exists():
        return Check(
            f"{name} repository",
            "warn",
            "component has no independent .git metadata",
            required=False,
        )

    head = capture_command([git, "rev-parse", "HEAD"], cwd=path)
    branch = capture_command([git, "branch", "--show-current"], cwd=path)
    status = capture_command([git, "status", "--porcelain", "--untracked-files=normal"], cwd=path)
    remotes = capture_command([git, "remote"], cwd=path)
    if any(result.returncode != 0 for result in (head, branch, status, remotes)):
        return Check(f"{name} repository", "warn", "git metadata could not be inspected", required=False)

    dirty_count = len([line for line in status.stdout.splitlines() if line.strip()])
    remote_names = [line.strip() for line in remotes.stdout.splitlines() if line.strip()]
    head_commit = head.stdout.strip()
    remote_status = probe_git_remotes(git, path, remote_names, head_commit=head_commit)
    details = [
        f"commit={head_commit[:12] or 'unknown'}",
        f"branch={branch.stdout.strip() or 'detached'}",
        f"worktree={'clean' if dirty_count == 0 else f'dirty({dirty_count})'}",
        f"remotes={','.join(remote_names) if remote_names else 'missing'}",
        (
            "fetchable_remotes="
            + (
                ",".join(remote_status["fetchable_remote_names"])
                if remote_status["fetchable_remote_names"]
                else "none"
            )
        ),
    ]
    repository_ready = dirty_count == 0 and remote_status["remote_fetchable"]
    return Check(
        f"{name} repository",
        "ok" if repository_ready else "warn",
        "; ".join(details),
        required=False,
    )


def git_repository_provenance(name: str, path: Path) -> dict[str, Any]:
    git = shutil.which("git")
    if not git or not (path / ".git").exists():
        return {"name": name, "path": relative_path(path), "available": False, "release_ready": False}
    head = capture_command([git, "rev-parse", "HEAD"], cwd=path)
    branch = capture_command([git, "branch", "--show-current"], cwd=path)
    status = capture_command([git, "status", "--porcelain", "--untracked-files=normal"], cwd=path)
    remotes = capture_command([git, "remote"], cwd=path)
    commands_passed = all(result.returncode == 0 for result in (head, branch, status, remotes))
    dirty_paths = [line for line in status.stdout.splitlines() if line.strip()] if status.returncode == 0 else []
    remote_names = sorted(line.strip() for line in remotes.stdout.splitlines() if line.strip()) if remotes.returncode == 0 else []
    head_commit = head.stdout.strip() if head.returncode == 0 else ""
    remote_status = probe_git_remotes(git, path, remote_names, head_commit=head_commit)
    payload = {
        "name": name,
        "path": relative_path(path),
        "available": commands_passed,
        "commit": head_commit or None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "clean": commands_passed and not dirty_paths,
        "dirty_path_count": len(dirty_paths),
        "remote_names": remote_names,
        "remote_count": len(remote_names),
        **remote_status,
    }
    payload["release_ready"] = bool(
        payload["available"]
        and payload["clean"]
        and payload["commit"]
        and payload["remote_fetchable"]
    )
    return payload


def build_workspace_lock() -> dict[str, Any]:
    repositories = [
        git_repository_provenance("root", ROOT),
        git_repository_provenance("site", SITE_ROOT),
        git_repository_provenance("trading-core", TRADING_ROOT),
    ]
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "repositories": repositories,
        "release_reproducible": all(repository["release_ready"] for repository in repositories),
    }
    payload["content_sha256"] = stable_hash(
        {key: value for key, value in payload.items() if key != "generated_at"}
    )
    return payload


def latest_report() -> tuple[str, Path]:
    candidates: list[tuple[str, Path]] = []
    if OUTPUTS_ROOT.exists():
        for path in OUTPUTS_ROOT.glob("每日全球晨间简报-*.md"):
            match = REPORT_PATTERN.match(path.name)
            if not match:
                continue
            try:
                report_date = Date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                continue
            candidates.append((report_date, path))
    if not candidates:
        raise FileNotFoundError(f"No dated briefing report found under {OUTPUTS_ROOT}")
    return max(candidates, key=lambda item: item[0])


def valid_iso_date(value: str) -> str:
    """Return a canonical ISO date or raise an argparse-friendly error."""
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc
    canonical = parsed.isoformat()
    if value != canonical:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected {canonical}.")
    return canonical


def parse_version(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", text)
    if not match:
        return ()
    return tuple(int(value or 0) for value in match.groups())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stable_json(payload: Any) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(row, allow_nan=False, ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    )
    atomic_write_text(path, text)


def strict_json_loads(text: str, *, source: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{source} contains non-standard numeric constant {value}")

    return json.loads(text, parse_constant=reject_constant)


def read_json_file(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return strict_json_loads(path.read_text(encoding="utf-8"), source=str(path))


def configured_report_date(
    now: datetime | None = None,
    *,
    settings_path: Path = BRIEFING_SETTINGS_PATH,
) -> Date:
    settings = read_json_file(settings_path, default={})
    timezone_name = (
        str(settings.get("timezone") or "Asia/Shanghai")
        if isinstance(settings, dict)
        else "Asia/Shanghai"
    )
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid briefing timezone: {timezone_name}") from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("report date requires a timezone-aware datetime")
    return current.astimezone(timezone).date()


def audit_record_hash(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    chain = normalized.get("audit_chain")
    if isinstance(chain, dict):
        normalized["audit_chain"] = {key: value for key, value in chain.items() if key != "entry_sha256"}
    return stable_hash(normalized)


def audit_cycle_history(history_root: Path) -> dict[str, Any]:
    errors: list[str] = []
    legacy_record_count = 0
    chained_record_count = 0
    previous_hash: str | None = None
    previous_sequence = 0
    for path in sorted(history_root.glob("*.json")) if history_root.exists() else []:
        try:
            payload = read_json_file(path)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: unreadable audit JSON: {exc}")
            continue
        chain = payload.get("audit_chain") if isinstance(payload, dict) else None
        if not isinstance(chain, dict):
            if chained_record_count:
                errors.append(f"{path.name}: unchained record appears after hash-chain genesis")
            legacy_record_count += 1
            continue
        chained_record_count += 1
        actual_hash = audit_record_hash(payload)
        if chain.get("entry_sha256") != actual_hash:
            errors.append(f"{path.name}: entry hash mismatch")
        expected_sequence = previous_sequence + 1
        if chain.get("sequence") != expected_sequence:
            errors.append(f"{path.name}: expected sequence {expected_sequence}, got {chain.get('sequence')}")
        if chain.get("previous_audit_sha256") != previous_hash:
            errors.append(f"{path.name}: previous audit hash mismatch")
        previous_hash = actual_hash
        previous_sequence = expected_sequence
    return {
        "passed": not errors,
        "errors": errors,
        "legacy_record_count": legacy_record_count,
        "chained_record_count": chained_record_count,
        "last_audit_sha256": previous_hash,
        "next_sequence": previous_sequence + 1,
    }


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = strict_json_loads(text, source=f"{path}:{line_number}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
        rows.append(payload)
    return rows


def maybe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid ledger numbers")
    if isinstance(value, str) and not value.strip():
        raise ValueError("empty strings are not valid ledger numbers")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid ledger number: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite ledger number: {value!r}")
    return number


def normalized_ledger_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def nested_forbidden_keys(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if normalized_ledger_key(key) in NORMALIZED_FORBIDDEN_LEDGER_KEYS:
                found.append(path)
            found.extend(nested_forbidden_keys(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_forbidden_keys(value, f"{prefix}[{index}]"))
    return found


def nested_unsafe_safety_declarations(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized_key = normalized_ledger_key(key)
            if normalized_key in NORMALIZED_SAFETY_FLAGS_REQUIRED_TRUE and value is not True:
                found.append(f"{path}={value!r}")
            if normalized_key in NORMALIZED_SAFETY_FLAGS_REQUIRED_FALSE and value is not False:
                found.append(f"{path}={value!r}")
            found.extend(nested_unsafe_safety_declarations(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_unsafe_safety_declarations(value, f"{prefix}[{index}]"))
    return found


def nested_nonfinite_values(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.extend(nested_nonfinite_values(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_nonfinite_values(value, f"{prefix}[{index}]"))
    elif isinstance(payload, float) and not math.isfinite(payload):
        found.append(f"{prefix}={payload!r}")
    elif isinstance(payload, str) and payload.strip().casefold() in {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }:
        found.append(f"{prefix}={payload!r}")
    return found


def nested_invalid_numeric_values(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized_key = normalized_ledger_key(key)
            if normalized_key in NORMALIZED_LEDGER_NUMERIC_KEYS and value is not None:
                try:
                    number = maybe_float(value)
                except ValueError as exc:
                    found.append(f"{path}={value!r} ({exc})")
                else:
                    if normalized_key in NORMALIZED_NON_NEGATIVE_LEDGER_NUMERIC_KEYS and number < 0:
                        found.append(f"{path}={value!r} (must be non-negative)")
            found.extend(nested_invalid_numeric_values(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_invalid_numeric_values(value, f"{prefix}[{index}]"))
    return found


def raw_virtual_source_safety_errors(payload: Any, source_path: Path, record: str) -> list[str]:
    locator = f"{relative_path(source_path)}:{record}"
    errors: list[str] = []
    forbidden = sorted(nested_forbidden_keys(payload))
    if forbidden:
        errors.append(f"{locator}: raw virtual source contains forbidden broker/live keys {forbidden}")
    unsafe_declarations = sorted(nested_unsafe_safety_declarations(payload))
    if unsafe_declarations:
        errors.append(f"{locator}: raw virtual source contains unsafe safety declarations {unsafe_declarations}")
    nonfinite = sorted(nested_nonfinite_values(payload))
    if nonfinite:
        errors.append(f"{locator}: raw virtual source contains non-finite numeric values {nonfinite}")
    invalid_numeric = sorted(nested_invalid_numeric_values(payload))
    if invalid_numeric:
        errors.append(f"{locator}: raw virtual source contains invalid numeric values {invalid_numeric}")
    return errors


def is_iso_date(value: Any) -> bool:
    try:
        return Date.fromisoformat(str(value)).isoformat() == str(value)
    except ValueError:
        return False


def source_identity(path: Path, line_number: int | None, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": relative_path(path),
        "record_id": payload.get("trade_id") or payload.get("order_id") or payload.get("replay_id"),
        "sha256": stable_hash(payload),
    }


def ledger_event_id(prefix: str, identity: dict[str, Any]) -> str:
    return f"{prefix}-{stable_hash(identity)[:24].upper()}"


def discover_briefing_temp_files(kind: str) -> list[Path]:
    """Return both legacy underscore and current hyphenated daily temp files."""
    if kind not in {"orders", "prices"}:
        raise ValueError(f"unsupported briefing temp file kind: {kind}")
    briefing_data = BRIEFING_ROOT / "data"
    if not briefing_data.exists():
        return []
    patterns = (f"temp_{kind}_*.json", f"temp-{kind}-*.json")
    return sorted(
        {path for pattern in patterns for path in briefing_data.glob(pattern)},
        key=lambda path: str(path),
    )


def virtual_ledger_source_files() -> list[Path]:
    files = [
        BRIEFING_ROOT / "data" / "paper_trades_us.jsonl",
        BRIEFING_ROOT / "data" / "paper_trades_china.jsonl",
        BRIEFING_ROOT / "data" / "paper_portfolio_us.json",
        BRIEFING_ROOT / "data" / "paper_portfolio_china.json",
    ]
    files.extend(discover_briefing_temp_files("orders"))
    files.extend(discover_briefing_temp_files("prices"))
    replay_root = TRADING_ROOT / "data" / "replays" / "global_briefing"
    if replay_root.exists():
        files.extend(sorted((replay_root / "trades").glob("*.jsonl")))
        files.extend(sorted((replay_root / "accounts").glob("*.json")))
        files.extend(sorted(replay_root.glob("global_briefing_replay_evaluation-*.json")))
    return [path for path in files if path.exists()]


def build_cycle_fingerprint(date: str) -> dict[str, Any]:
    files = [
        OUTPUTS_ROOT / f"每日全球晨间简报-{date}.md",
        BRIEFING_ROOT / "data" / f"macro_signals-{date}.jsonl",
        TRADING_ROOT / "data" / "macro_signals" / f"macro_signals-{date}.jsonl",
        SITE_ROOT / "app" / "briefing.generated.json",
        BRIEFING_ROOT / "data" / f"temp_orders_{date}.json",
        BRIEFING_ROOT / "data" / f"temp-orders-{date}.json",
        BRIEFING_ROOT / "data" / f"temp_prices_{date}.json",
        BRIEFING_ROOT / "data" / f"temp-prices-{date}.json",
        *virtual_ledger_source_files(),
    ]
    records = [
        {"path": relative_path(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
        for path in sorted(set(files), key=lambda item: str(item))
        if path.exists()
    ]
    return {
        "date": date,
        "files": records,
        "fingerprint": stable_hash(records),
    }


def canonicalize_global_paper_trade(row: dict[str, Any], source_path: Path, line_number: int, source_ledger: str) -> dict[str, Any]:
    identity = source_identity(source_path, line_number, row)
    action = str(row.get("action") or row.get("side") or "UNKNOWN").upper()
    quantity = maybe_float(row.get("quantity"))
    price = maybe_float(row.get("price"))
    notional = maybe_float(row.get("gross_value"), price * quantity)
    account = str(row.get("account") or row.get("market_scope") or "GLOBAL").upper()
    account_id = str(row.get("account_id") or f"global-briefing-{account.lower()}-paper-trading")
    status = "FILLED" if action in {"BUY", "SELL"} and quantity > 0 else "HELD"
    event_id = ledger_event_id("ATLASGBTRADE", identity)
    timestamp = str(row.get("timestamp") or f"{row.get('date', '1970-01-01')}T00:00:00")
    return {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_event_id": event_id,
        "event_type": "virtual_trade" if status == "FILLED" else "virtual_hold",
        "idempotency_key": event_id,
        "source_system": "global-briefing",
        "source_ledger": source_ledger,
        "source_path": relative_path(source_path),
        "source_line": line_number,
        "source_hash": identity["sha256"],
        "date": str(row.get("date") or timestamp[:10]),
        "timestamp": timestamp,
        "account_id": account_id,
        "virtual_account_scope": account,
        "symbol": str(row.get("symbol") or ""),
        "exchange": row.get("exchange"),
        "market_type": row.get("market_type"),
        "currency": row.get("currency"),
        "side": action,
        "filled_price": price,
        "filled_quantity": quantity,
        "notional": notional,
        "fee": maybe_float(row.get("fee")),
        "tax": maybe_float(row.get("tax")),
        "cash_after": maybe_float(row.get("cash_after")) if row.get("cash_after") is not None else None,
        "equity_after": maybe_float(row.get("equity_after")) if row.get("equity_after") is not None else None,
        "status": status,
        "trade_id": str(row.get("trade_id") or event_id),
        "order_id": str(row.get("order_id") or event_id.replace("TRADE", "ORDER")),
        "prediction_id": row.get("prediction_id"),
        "scenario": row.get("scenario"),
        "reason": row.get("reason"),
        "risk": row.get("risk"),
        "paper_trading_only": row.get("paper_trading_only", True) is True,
        "isolated_replay": False,
        "no_real_broker_order": row.get("no_real_broker_order", True) is True,
    }


def canonicalize_replay_trade(row: dict[str, Any], source_path: Path, line_number: int) -> dict[str, Any]:
    identity = source_identity(source_path, line_number, row)
    event_id = ledger_event_id("ATLASREPLAYTRADE", identity)
    replay_id = str(row.get("replay_id") or "unknown-replay")
    price = maybe_float(row.get("price"))
    quantity = maybe_float(row.get("quantity"))
    return {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_event_id": event_id,
        "event_type": "isolated_replay_trade",
        "idempotency_key": event_id,
        "source_system": "trading-core",
        "source_ledger": "global_briefing_isolated_replay",
        "source_path": relative_path(source_path),
        "source_line": line_number,
        "source_hash": identity["sha256"],
        "date": str(row.get("date") or ""),
        "timestamp": str(row.get("date") or ""),
        "account_id": replay_id,
        "virtual_account_scope": "REPLAY",
        "symbol": str(row.get("symbol") or ""),
        "exchange": None,
        "market_type": "REPLAY",
        "currency": "CNY",
        "side": str(row.get("side") or "UNKNOWN").upper(),
        "filled_price": price,
        "filled_quantity": quantity,
        "notional": maybe_float(row.get("notional"), price * quantity),
        "fee": maybe_float(row.get("fee")),
        "tax": maybe_float(row.get("tax")),
        "status": "FILLED",
        "trade_id": str(row.get("trade_id") or event_id),
        "order_id": str(row.get("order_id") or event_id.replace("TRADE", "ORDER")),
        "prediction_id": None,
        "scenario": "isolated historical replay",
        "reason": row.get("reason"),
        "risk": None,
        "paper_trading_only": row.get("paper_trading_only", True) is True,
        "isolated_replay": bool(row.get("isolated", True)),
        "no_real_broker_order": row.get("no_real_broker_order", True) is True,
    }


def load_temp_orders(path: Path) -> list[dict[str, Any]]:
    raw = read_json_file(path, default={})
    if isinstance(raw, dict):
        orders = raw.get("orders", [])
    elif isinstance(raw, list):
        orders = raw
    else:
        raise ValueError(f"{path}: temp order input must be an object or list")
    if not isinstance(orders, list) or not all(isinstance(order, dict) for order in orders):
        raise ValueError(f"{path}: temp order input must contain JSON object orders")
    return orders


def temp_order_date(path: Path, row: dict[str, Any]) -> str:
    if row.get("date"):
        return str(row["date"])
    match = re.search(r"temp(?:_|-)orders(?:_|-)(\d{4}-\d{2}-\d{2})\.json$", path.name)
    return match.group(1) if match else ""


def canonicalize_temp_order_intent(row: dict[str, Any], source_path: Path, order_index: int) -> dict[str, Any]:
    identity = source_identity(source_path, order_index, row)
    event_id = ledger_event_id("ATLASORDERINTENT", identity)
    action = str(row.get("action") or row.get("side") or "HOLD").upper()
    account = str(row.get("account") or row.get("paper_account") or row.get("market_scope") or "AUTO").upper()
    date = temp_order_date(source_path, row)
    quantity = maybe_float(row.get("quantity"))
    price = maybe_float(row.get("price"))
    return {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_event_id": event_id,
        "event_type": "virtual_order_intent",
        "idempotency_key": event_id,
        "source_system": "global-briefing",
        "source_ledger": "global_briefing_temp_orders",
        "source_path": relative_path(source_path),
        "source_line": order_index,
        "source_hash": identity["sha256"],
        "date": date,
        "timestamp": str(row.get("timestamp") or f"{date}T00:00:00"),
        "account_id": str(row.get("account_id") or f"global-briefing-{account.lower()}-paper-trading"),
        "virtual_account_scope": account,
        "symbol": str(row.get("symbol") or ""),
        "exchange": row.get("exchange"),
        "market_type": row.get("market_type"),
        "currency": row.get("currency"),
        "side": action,
        "filled_price": price,
        "filled_quantity": quantity,
        "notional": maybe_float(row.get("gross_value"), price * quantity),
        "fee": maybe_float(row.get("fee")),
        "tax": maybe_float(row.get("tax")),
        "status": "INTENT_RECORDED" if action in {"BUY", "SELL"} and quantity > 0 else "HELD",
        "trade_id": event_id,
        "order_id": str(row.get("order_id") or event_id),
        "prediction_id": row.get("prediction_id"),
        "scenario": row.get("scenario"),
        "reason": row.get("reason"),
        "risk": row.get("risk"),
        "paper_trading_only": row.get("paper_trading_only", True) is True,
        "isolated_replay": False,
        "no_real_broker_order": row.get("no_real_broker_order", True) is True,
    }


def load_virtual_account_snapshots(*, source_errors: list[str] | None = None) -> dict[str, Any]:
    accounts: dict[str, Any] = {}
    for account, path in {
        "US": BRIEFING_ROOT / "data" / "paper_portfolio_us.json",
        "CHINA": BRIEFING_ROOT / "data" / "paper_portfolio_china.json",
    }.items():
        payload = read_json_file(path, default=None)
        if isinstance(payload, dict):
            safety_errors = raw_virtual_source_safety_errors(payload, path, "document")
            if safety_errors:
                if source_errors is None:
                    raise ValueError("; ".join(safety_errors))
                source_errors.extend(safety_errors)
                continue
            account_id = str(payload.get("account_id") or f"global-briefing-{account.lower()}-paper-trading")
            accounts[account_id] = {
                "source_system": "global-briefing",
                "source_path": relative_path(path),
                "virtual_account_scope": account,
                "account_id": account_id,
                "cash": maybe_float(payload.get("cash")),
                "initial_cash": maybe_float(payload.get("initial_cash")),
                "realized_pnl": maybe_float(payload.get("realized_pnl")),
                "positions": payload.get("positions", {}),
                "last_prices": payload.get("last_prices", {}),
                "paper_trading_only": (
                    payload.get("mode") == "paper_trading"
                    and payload.get("paper_trading_only", True) is True
                ),
                "no_real_broker_order": payload.get("no_real_broker_order", True) is True,
            }
    replay_account_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "accounts"
    if replay_account_root.exists():
        for path in sorted(replay_account_root.glob("account-*.json")):
            payload = read_json_file(path, default=None)
            if isinstance(payload, dict):
                safety_errors = raw_virtual_source_safety_errors(payload, path, "document")
                if safety_errors:
                    if source_errors is None:
                        raise ValueError("; ".join(safety_errors))
                    source_errors.extend(safety_errors)
                    continue
                account_id = str(payload.get("replay_id") or path.stem)
                accounts[account_id] = {
                    "source_system": "trading-core",
                    "source_path": relative_path(path),
                    "virtual_account_scope": "REPLAY",
                    "account_id": account_id,
                    "cash": maybe_float(payload.get("cash")),
                    "equity": maybe_float(payload.get("equity")),
                    "positions": payload.get("positions", []),
                    "isolated_replay": payload.get("isolated") is True,
                    "paper_trading_only": payload.get("paper_trading_only", True) is True,
                    "no_real_broker_order": payload.get("no_real_broker_order", True) is True,
                }
    return accounts


def event_source_locator(event: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(event.get("source_ledger") or ""),
        str(event.get("source_path") or ""),
        int(event.get("source_line") or 0),
    )


def audit_ledger_continuity(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Fail closed when a previously committed source row disappears or mutates."""
    if not VIRTUAL_LEDGER_PATH.exists():
        return {
            "previous_ledger_present": False,
            "previous_event_count": 0,
            "preserved_event_count": 0,
            "added_event_count": len(events),
            "mutated_locators": [],
            "deleted_locators": [],
            "blocking_reasons": [],
        }
    try:
        previous_events = read_jsonl_file(VIRTUAL_LEDGER_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "previous_ledger_present": True,
            "previous_event_count": None,
            "preserved_event_count": 0,
            "added_event_count": 0,
            "mutated_locators": [],
            "deleted_locators": [],
            "blocking_reasons": [f"cannot verify previous canonical ledger: {type(exc).__name__}: {exc}"],
        }
    previous_by_locator = {event_source_locator(event): event for event in previous_events}
    current_by_locator = {event_source_locator(event): event for event in events}
    deleted = sorted(set(previous_by_locator) - set(current_by_locator))
    mutated = sorted(
        locator
        for locator in set(previous_by_locator) & set(current_by_locator)
        if previous_by_locator[locator].get("source_hash") != current_by_locator[locator].get("source_hash")
        or previous_by_locator[locator].get("ledger_event_id") != current_by_locator[locator].get("ledger_event_id")
    )
    blocking = [f"previous canonical event source deleted: {locator}" for locator in deleted]
    blocking.extend(f"previous canonical event source mutated: {locator}" for locator in mutated)
    preserved = len(set(previous_by_locator) & set(current_by_locator)) - len(mutated)
    return {
        "previous_ledger_present": True,
        "previous_event_count": len(previous_events),
        "preserved_event_count": preserved,
        "added_event_count": len(set(current_by_locator) - set(previous_by_locator)),
        "mutated_locators": [list(locator) for locator in mutated],
        "deleted_locators": [list(locator) for locator in deleted],
        "blocking_reasons": blocking,
    }


def normalized_position_key(exchange: Any, symbol: Any) -> str:
    exchange_text = str(exchange or "").strip().upper()
    symbol_text = str(symbol or "").strip().upper()
    return f"{exchange_text}:{symbol_text}" if exchange_text else symbol_text


def reconcile_virtual_accounts(
    events: Sequence[dict[str, Any]], account_state: dict[str, Any]
) -> dict[str, Any]:
    """Replay filled trades and compare quantities/cash with account snapshots."""
    blocking: list[str] = []
    accounts: list[dict[str, Any]] = []
    for account_id, snapshot in sorted(account_state.items()):
        filled = [
            event
            for event in events
            if event.get("account_id") == account_id
            and event.get("event_type") in {"virtual_trade", "isolated_replay_trade"}
        ]
        replayed: dict[str, float] = {}
        for event in filled:
            key = normalized_position_key(
                event.get("exchange") if snapshot.get("virtual_account_scope") != "REPLAY" else None,
                event.get("symbol"),
            )
            direction = 1.0 if event.get("side") == "BUY" else -1.0
            replayed[key] = replayed.get(key, 0.0) + direction * maybe_float(event.get("filled_quantity"))
        raw_positions = snapshot.get("positions", {})
        if isinstance(raw_positions, dict):
            position_rows = list(raw_positions.values())
        elif isinstance(raw_positions, list):
            position_rows = raw_positions
        else:
            position_rows = []
            blocking.append(f"account {account_id}: positions must be an object or list")
        actual: dict[str, float] = {}
        for position in position_rows:
            if not isinstance(position, dict):
                blocking.append(f"account {account_id}: position row must be an object")
                continue
            key = normalized_position_key(
                position.get("exchange") if snapshot.get("virtual_account_scope") != "REPLAY" else None,
                position.get("symbol"),
            )
            actual[key] = actual.get(key, 0.0) + maybe_float(position.get("quantity"))
        mismatches: list[dict[str, Any]] = []
        for key in sorted(set(replayed) | set(actual)):
            expected_quantity = replayed.get(key, 0.0)
            actual_quantity = actual.get(key, 0.0)
            tolerance = max(1e-8, abs(expected_quantity) * 1e-9)
            if abs(expected_quantity - actual_quantity) > tolerance:
                mismatch = {
                    "position": key,
                    "replayed_quantity": expected_quantity,
                    "snapshot_quantity": actual_quantity,
                }
                mismatches.append(mismatch)
                blocking.append(f"account {account_id}: position reconciliation failed {mismatch}")
        cash_events = [event for event in filled if event.get("cash_after") is not None]
        cash_match: bool | None = None
        replayed_cash: float | None = None
        if cash_events and snapshot.get("virtual_account_scope") != "REPLAY":
            replayed_cash = maybe_float(cash_events[-1].get("cash_after"))
            snapshot_cash = maybe_float(snapshot.get("cash"))
            cash_match = abs(replayed_cash - snapshot_cash) <= max(1e-6, abs(replayed_cash) * 1e-9)
            if not cash_match:
                blocking.append(
                    f"account {account_id}: cash reconciliation failed "
                    f"replayed={replayed_cash} snapshot={snapshot_cash}"
                )
        accounts.append(
            {
                "account_id": account_id,
                "filled_trade_count": len(filled),
                "position_count": len(actual),
                "position_mismatches": mismatches,
                "cash_replayed": replayed_cash,
                "cash_snapshot": snapshot.get("cash"),
                "cash_match": cash_match,
            }
        )
    return {"accounts": accounts, "blocking_reasons": blocking, "passed": not blocking}


def audit_virtual_execution_ledger(
    events: Sequence[dict[str, Any]],
    account_state: dict[str, Any],
    *,
    warnings: Sequence[str] = (),
    source_expectations: Sequence[dict[str, Any]] = (),
    source_errors: Sequence[str] = (),
) -> dict[str, Any]:
    blocking: list[str] = list(source_errors)
    ids = [str(event.get("ledger_event_id", "")) for event in events]
    if len(ids) != len(set(ids)):
        blocking.append("duplicate ledger_event_id detected")
    allowed_sides = {"BUY", "SELL", "HOLD"}
    required_trade_fields = {"trade_id", "order_id", "date", "account_id", "symbol", "side", "filled_price", "filled_quantity", "status"}
    required_non_empty = {"ledger_event_id", "event_type", "date", "account_id", "side", "status"}
    source_counts: dict[str, int] = {}
    source_path_counts: dict[str, int] = {}
    for event in events:
        event_id = str(event.get("ledger_event_id") or "missing-event-id")
        source_counts[str(event.get("source_ledger"))] = source_counts.get(str(event.get("source_ledger")), 0) + 1
        source_path = str(event.get("source_path") or "")
        source_path_counts[source_path] = source_path_counts.get(source_path, 0) + 1
        missing = sorted(field for field in required_trade_fields if field not in event)
        if missing:
            blocking.append(f"{event_id}: missing required fields {missing}")
        empty = sorted(
            field
            for field in required_non_empty
            if event.get(field) is None or event.get(field) == ""
        )
        if empty:
            blocking.append(f"{event_id}: empty required fields {empty}")
        if event.get("ledger_id") != LEDGER_ID:
            blocking.append(f"{event_id}: wrong ledger_id")
        if event.get("paper_trading_only") is not True:
            blocking.append(f"{event_id}: paper_trading_only is not true")
        if event.get("no_real_broker_order") is not True:
            blocking.append(f"{event_id}: no_real_broker_order is not true")
        if str(event.get("side")).upper() not in allowed_sides:
            blocking.append(f"{event_id}: unsupported side {event.get('side')!r}")
        numeric_values: dict[str, float | None] = {}
        for field in (
            "filled_quantity",
            "filled_price",
            "notional",
            "fee",
            "tax",
            "cash_after",
            "equity_after",
        ):
            if field not in event or event.get(field) is None:
                numeric_values[field] = None
                continue
            try:
                numeric_values[field] = maybe_float(event.get(field))
            except ValueError as exc:
                numeric_values[field] = None
                blocking.append(f"{event_id}: invalid {field}: {exc}")
        quantity = numeric_values.get("filled_quantity")
        price = numeric_values.get("filled_price")
        if quantity is not None and quantity < 0:
            blocking.append(f"{event_id}: negative quantity")
        if price is not None and price < 0:
            blocking.append(f"{event_id}: negative price")
        for field in ("notional", "fee", "tax"):
            value = numeric_values.get(field)
            if value is not None and value < 0:
                blocking.append(f"{event_id}: negative {field}")
        if not is_iso_date(event.get("date")):
            blocking.append(f"{event_id}: invalid date {event.get('date')!r}")
        event_type = event.get("event_type")
        if event_type not in {"virtual_trade", "virtual_hold", "virtual_order_intent", "isolated_replay_trade"}:
            blocking.append(f"{event_id}: unsupported event_type {event_type!r}")
        if event_type in {"virtual_trade", "isolated_replay_trade"}:
            if str(event.get("side")).upper() not in {"BUY", "SELL"}:
                blocking.append(f"{event_id}: filled trade must be BUY or SELL")
            if quantity is None or price is None or quantity <= 0 or price <= 0:
                blocking.append(f"{event_id}: filled trade requires positive quantity and price")
        if event_type == "isolated_replay_trade" and event.get("isolated_replay") is not True:
            blocking.append(f"{event_id}: replay trade is not isolated")
        if event_type == "virtual_hold":
            if str(event.get("side")).upper() != "HOLD" or event.get("status") != "HELD":
                blocking.append(f"{event_id}: virtual hold must use HOLD/HELD")
            if quantity is None or quantity != 0:
                blocking.append(f"{event_id}: virtual hold quantity must be zero")
        if event_type == "virtual_order_intent" and str(event.get("side")).upper() in {"BUY", "SELL"}:
            if quantity is None or price is None or quantity <= 0 or price <= 0:
                blocking.append(f"{event_id}: order intent requires positive quantity and price")
        forbidden = sorted(nested_forbidden_keys(event))
        if forbidden:
            blocking.append(f"{event_id}: forbidden broker/live keys {forbidden}")
    if not account_state:
        blocking.append("no virtual account snapshots found")
    for account_id, account in account_state.items():
        if account.get("paper_trading_only") is not True:
            blocking.append(f"account {account_id}: paper_trading_only is not true")
        if account.get("no_real_broker_order") is not True:
            blocking.append(f"account {account_id}: no_real_broker_order is not true")
        if account.get("virtual_account_scope") == "REPLAY" and account.get("isolated_replay") is not True:
            blocking.append(f"account {account_id}: replay account is not isolated")
        forbidden = sorted(nested_forbidden_keys(account))
        if forbidden:
            blocking.append(f"account {account_id}: forbidden broker/live keys {forbidden}")
    for expectation in source_expectations:
        path = str(expectation.get("path") or "")
        if expectation.get("required") and not expectation.get("present"):
            blocking.append(f"required virtual ledger source missing: {path}")
        expected_count = expectation.get("expected_event_count")
        if expected_count is not None and source_path_counts.get(path, 0) != expected_count:
            blocking.append(
                f"virtual ledger source count mismatch: {path} "
                f"expected={expected_count} actual={source_path_counts.get(path, 0)}"
            )
    snapshot_paths = {str(account.get("source_path") or "") for account in account_state.values()}
    for expectation in source_expectations:
        if expectation.get("kind") == "account_snapshot" and expectation.get("present"):
            if str(expectation.get("path") or "") not in snapshot_paths:
                blocking.append(f"account snapshot was not loaded: {expectation.get('path')}")
    reconciliation = reconcile_virtual_accounts(events, account_state)
    blocking.extend(reconciliation["blocking_reasons"])
    continuity = audit_ledger_continuity(events)
    blocking.extend(continuity["blocking_reasons"])
    audit_content = {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "blocking_reasons": blocking,
        "warnings": list(warnings),
        "event_hash": stable_hash(list(events)),
        "account_hash": stable_hash(account_state),
    }
    audit_content_hash = stable_hash(audit_content)
    payload = {
        "audit_id": f"ATLAS-VIRTUAL-LEDGER-AUDIT-{audit_content_hash[:24].upper()}",
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "content_hash": audit_content_hash,
        "overall_passed": not blocking,
        "blocking_reasons": blocking,
        "warnings": list(warnings),
        "canonical_ledger_path": relative_path(VIRTUAL_LEDGER_PATH),
        "canonical_state_path": relative_path(VIRTUAL_LEDGER_STATE_PATH),
        "event_count": len(events),
        "account_count": len(account_state),
        "source_counts": source_counts,
        "source_path_counts": source_path_counts,
        "source_expectations": list(source_expectations),
        "reconciliation": reconciliation,
        "continuity": continuity,
        "legacy_sources_read_only": [
            relative_path(BRIEFING_ROOT / "data" / "paper_trades_us.jsonl"),
            relative_path(BRIEFING_ROOT / "data" / "paper_trades_china.jsonl"),
            relative_path(TRADING_ROOT / "data" / "replays" / "global_briefing" / "trades"),
        ],
        "boundary": {
            "single_canonical_virtual_ledger": True,
            "legacy_ledgers_are_read_only_sources": True,
            "paper_trading_only": True,
            "real_broker_orders_allowed": False,
            "external_broker_connection": False,
            "live_trading": False,
            "historical_source_mutation_fails_closed": True,
            "account_snapshots_reconciled": reconciliation["passed"],
        },
    }
    return payload


def build_virtual_execution_ledger(*, write_files: bool = True) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_errors: list[str] = []
    source_expectations: list[dict[str, Any]] = []
    for source_ledger, path in {
        "global_briefing_paper_us": BRIEFING_ROOT / "data" / "paper_trades_us.jsonl",
        "global_briefing_paper_china": BRIEFING_ROOT / "data" / "paper_trades_china.jsonl",
    }.items():
        expectation = {
            "kind": "event_ledger",
            "source_ledger": source_ledger,
            "path": relative_path(path),
            "required": True,
            "present": path.exists(),
            "expected_event_count": None,
        }
        source_expectations.append(expectation)
        if not path.exists():
            warnings.append(f"missing source ledger: {relative_path(path)}")
            continue
        rows = read_jsonl_file(path)
        expectation["expected_event_count"] = len(rows)
        for line_number, row in enumerate(rows, 1):
            safety_errors = raw_virtual_source_safety_errors(row, path, f"line {line_number}")
            if safety_errors:
                source_errors.extend(safety_errors)
                continue
            events.append(canonicalize_global_paper_trade(row, path, line_number, source_ledger))

    temp_order_dates: dict[str, list[Path]] = {}
    for path in discover_briefing_temp_files("orders"):
        rows = load_temp_orders(path)
        source_expectations.append(
            {
                "kind": "event_ledger",
                "source_ledger": "global_briefing_temp_orders",
                "path": relative_path(path),
                "required": True,
                "present": True,
                "expected_event_count": len(rows),
            }
        )
        date = temp_order_date(path, rows[0] if rows else {})
        if date:
            temp_order_dates.setdefault(date, []).append(path)
        for order_index, row in enumerate(rows, 1):
            safety_errors = raw_virtual_source_safety_errors(row, path, f"order {order_index}")
            if safety_errors:
                source_errors.extend(safety_errors)
                continue
            events.append(canonicalize_temp_order_intent(row, path, order_index))
    for date, paths in temp_order_dates.items():
        non_empty = [path for path in paths if load_temp_orders(path)]
        if len(non_empty) > 1:
            source_errors.append(
                f"multiple non-empty temp order files for {date}: "
                + ", ".join(relative_path(path) for path in non_empty)
            )

    replay_trade_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "trades"
    if replay_trade_root.exists():
        for path in sorted(replay_trade_root.glob("*.jsonl")):
            rows = read_jsonl_file(path)
            source_expectations.append(
                {
                    "kind": "event_ledger",
                    "source_ledger": "global_briefing_isolated_replay",
                    "path": relative_path(path),
                    "required": True,
                    "present": True,
                    "expected_event_count": len(rows),
                }
            )
            for line_number, row in enumerate(rows, 1):
                safety_errors = raw_virtual_source_safety_errors(row, path, f"line {line_number}")
                if safety_errors:
                    source_errors.extend(safety_errors)
                    continue
                events.append(canonicalize_replay_trade(row, path, line_number))
    else:
        warnings.append(f"missing replay trade source directory: {relative_path(replay_trade_root)}")
        source_errors.append(f"required replay trade source directory missing: {relative_path(replay_trade_root)}")

    events = sorted(
        events,
        key=lambda event: (
            str(event.get("date", "")),
            str(event.get("timestamp", "")),
            str(event.get("source_ledger", "")),
            int(event.get("source_line") or 0),
            str(event.get("ledger_event_id", "")),
        ),
    )
    account_state = load_virtual_account_snapshots(source_errors=source_errors)
    for path in (
        BRIEFING_ROOT / "data" / "paper_portfolio_us.json",
        BRIEFING_ROOT / "data" / "paper_portfolio_china.json",
    ):
        source_expectations.append(
            {
                "kind": "account_snapshot",
                "path": relative_path(path),
                "required": True,
                "present": path.exists(),
                "expected_event_count": None,
            }
        )
    replay_account_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "accounts"
    for path in sorted(replay_account_root.glob("account-*.json")) if replay_account_root.exists() else []:
        source_expectations.append(
            {
                "kind": "account_snapshot",
                "path": relative_path(path),
                "required": True,
                "present": True,
                "expected_event_count": None,
            }
        )
    content_hash = stable_hash({"events": events, "accounts": account_state})
    previous_state = read_json_file(VIRTUAL_LEDGER_STATE_PATH, default={})
    generated_at = utc_now()
    if isinstance(previous_state, dict) and previous_state.get("content_hash") == content_hash:
        generated_at = str(previous_state.get("generated_at") or generated_at)
    state_payload = {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "generated_at": generated_at,
        "content_hash": content_hash,
        "canonical_ledger_path": relative_path(VIRTUAL_LEDGER_PATH),
        "accounts": account_state,
        "event_count": len(events),
    }
    audit = audit_virtual_execution_ledger(
        events,
        account_state,
        warnings=warnings,
        source_expectations=source_expectations,
        source_errors=source_errors,
    )
    write_performed = bool(write_files and audit["overall_passed"])
    if write_performed:
        atomic_write_jsonl(VIRTUAL_LEDGER_PATH, events)
        atomic_write_json(VIRTUAL_LEDGER_STATE_PATH, state_payload)
        # Re-audit against the just-established canonical baseline so the first
        # successful write and every idempotent replay produce identical audit bytes.
        audit = audit_virtual_execution_ledger(
            events,
            account_state,
            warnings=warnings,
            source_expectations=source_expectations,
            source_errors=source_errors,
        )
        atomic_write_json(VIRTUAL_LEDGER_AUDIT_PATH, audit)
        atomic_write_text(
            VIRTUAL_LEDGER_AUDIT_PATH.with_suffix(".md"),
            build_virtual_ledger_audit_markdown(audit),
        )
    return {
        "ledger_path": str(VIRTUAL_LEDGER_PATH),
        "state_path": str(VIRTUAL_LEDGER_STATE_PATH),
        "audit_path": str(VIRTUAL_LEDGER_AUDIT_PATH),
        "event_count": len(events),
        "account_count": len(account_state),
        "content_hash": content_hash,
        "write_requested": write_files,
        "write_performed": write_performed,
        "events": events,
        "state": state_payload,
        "audit": audit,
    }


def build_virtual_ledger_audit_markdown(audit: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# ATLAS Virtual Execution Ledger Audit",
            "",
            "## Verdict",
            f"- overall_passed={str(audit['overall_passed']).lower()}",
            f"- blocking_reasons={audit['blocking_reasons']}",
            "",
            "## Canonical Ledger",
            f"- ledger_id={audit['ledger_id']}",
            f"- path={audit['canonical_ledger_path']}",
            f"- event_count={audit['event_count']}",
            f"- account_count={audit['account_count']}",
            "",
            "## Source Counts",
            *[f"- {name}: {count}" for name, count in sorted(audit["source_counts"].items())],
            "",
            "## Boundary",
            "- single canonical virtual ledger",
            "- legacy US/CHINA/replay ledgers are read-only sources",
            "- paper trading only",
            "- no broker connection",
            "- no live trading",
            "",
        ]
    )


def latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    candidates = sorted(directory.glob(pattern), key=lambda path: (path.stat().st_mtime_ns, path.name))
    return candidates[-1] if candidates else None


def select_replay_evaluation(replay_dir: Path, date: str) -> tuple[Path | None, str | None, str | None]:
    cutoff = Date.fromisoformat(date)
    candidates: list[tuple[Date, Date, Path]] = []
    if replay_dir.exists():
        for path in replay_dir.glob("global_briefing_replay_evaluation-*.json"):
            match = REPLAY_EVALUATION_PATTERN.fullmatch(path.name)
            if not match:
                continue
            try:
                start = Date.fromisoformat(match.group(1))
                end = Date.fromisoformat(match.group(2))
            except ValueError:
                continue
            if start <= end <= cutoff:
                candidates.append((end, start, path))
    if not candidates:
        return None, None, None
    end, start, path = max(candidates, key=lambda item: (item[0], item[1], item[2].name))
    return path, start.isoformat(), end.isoformat()


def run_replay_shadow_validation(date: str) -> dict[str, Any]:
    replay_dir = TRADING_ROOT / "data" / "replays" / "global_briefing"
    replay_path, replay_start, replay_end = select_replay_evaluation(replay_dir, date)
    replay_payload = read_json_file(replay_path, default={}) if replay_path else {}
    integrity = replay_payload.get("integrity", {}) if isinstance(replay_payload, dict) else {}
    execution = replay_payload.get("execution", {}) if isinstance(replay_payload, dict) else {}
    replay_execution_passed = bool(
        replay_payload
        and replay_payload.get("overall_status") == "research_review_ready"
        and integrity.get("isolated_ledger_complete") is True
        and integrity.get("isolated_replay") is True
        and integrity.get("main_ledger_written") is False
        and integrity.get("run_daily_called") is False
        and execution.get("mode") == "isolated"
        and execution.get("isolated_outputs_present") is True
        and execution.get("no_trade_fallback") is False
    )
    strategy_validation = replay_payload.get("strategy_validation", {}) if isinstance(replay_payload, dict) else {}
    strategy_evidence_passed = bool(
        isinstance(strategy_validation, dict)
        and strategy_validation.get("passed") is True
        and integrity.get("labels_used") is True
        and (integrity.get("ml_shadow_used") is True or integrity.get("experiments_used") is True)
    )

    promotion_code = (
        "import json\n"
        "from trading_core.evolution.promotion_evidence import evaluate_verified_shadow_promotion\n"
        f"print(json.dumps(evaluate_verified_shadow_promotion('momentum_shadow_v1', {date!r}), ensure_ascii=False))\n"
    )
    promotion_command = capture_command([sys.executable, "-c", promotion_code], cwd=TRADING_ROOT, trading_core=True)
    promotion_payload: dict[str, Any]
    if promotion_command.returncode == 0:
        try:
            promotion_payload = json.loads(promotion_command.stdout.strip())
        except json.JSONDecodeError:
            promotion_payload = {"parse_error": promotion_command.stdout.strip()}
    else:
        promotion_payload = {"command_error": promotion_command.stderr.strip() or promotion_command.stdout.strip()}
    promotion_passed = bool(
        promotion_command.returncode == 0
        and promotion_payload.get("auto_applied") is False
        and promotion_payload.get("recommended_state") in {"shadow", "active_small"}
        and promotion_payload.get("recommended_state") != "active_normal"
    )

    return {
        "overall_passed": replay_execution_passed and promotion_passed,
        "safety_gate_passed": replay_execution_passed and promotion_passed,
        "strategy_evidence_passed": strategy_evidence_passed,
        "replay": {
            "passed": replay_execution_passed,
            "execution_safety_passed": replay_execution_passed,
            "strategy_evidence_passed": strategy_evidence_passed,
            "validation_scope": "execution_isolation_plus_explicit_strategy_evidence" if strategy_evidence_passed else "execution_isolation_only",
            "path": str(replay_path) if replay_path else None,
            "period_start": replay_start,
            "period_end": replay_end,
            "selected_as_of": date,
            "overall_status": replay_payload.get("overall_status") if isinstance(replay_payload, dict) else None,
            "integrity": integrity,
            "execution": execution,
        },
        "shadow_promotion_gate": {
            "passed": promotion_passed,
            "safety_passed": promotion_passed,
            "evidence_passed": strategy_evidence_passed,
            "payload": promotion_payload,
            "returncode": promotion_command.returncode,
        },
        "boundary": {
            "replay_isolated": replay_execution_passed,
            "promotion_auto_apply_allowed": False,
            "active_normal_requires_manual_review": True,
            "execution_safety_is_not_strategy_validation": True,
        },
    }


def build_cycle_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# ATLAS Cycle Run Audit",
        "",
        "## Verdict",
        f"- cycle_id={payload['cycle_id']}",
        f"- run_id={payload.get('run_id')}",
        f"- date={payload['date']}",
        f"- gate_scope={payload.get('gate_scope', 'legacy')}",
        f"- operational_gate_passed={str(payload.get('operational_gate_passed', payload['overall_passed'])).lower()}",
        f"- release_candidate_passed={str(payload.get('release_candidate_passed', False)).lower()}",
        f"- research_promotion_passed={str(payload.get('research_promotion_passed', False)).lower()}",
        f"- blocking_reasons={payload['blocking_reasons']}",
        "",
        "## Stages",
    ]
    for stage in payload["stages"]:
        lines.append(f"- {stage['name']}: status={stage['status']} detail={stage.get('detail')}")
    lines.extend(
        [
            "",
            "## Ledger",
            f"- canonical_ledger={payload['ledger']['ledger_path']}",
            f"- events={payload['ledger']['event_count']}",
            f"- accounts={payload['ledger']['account_count']}",
            f"- content_hash={payload['ledger'].get('content_hash')}",
            f"- write_performed={str(payload['ledger'].get('write_performed', False)).lower()}",
            "",
            "## Replay / Shadow Gate",
            f"- overall_passed={str(payload['replay_shadow_validation']['overall_passed']).lower()}",
            f"- replay_passed={str(payload['replay_shadow_validation']['replay']['passed']).lower()}",
            f"- shadow_promotion_gate_passed={str(payload['replay_shadow_validation']['shadow_promotion_gate']['passed']).lower()}",
            "",
            "## Boundary",
            "- virtual execution only",
            "- single canonical virtual ledger",
            "- legacy ledgers are read-only sources",
            "- no real broker orders",
            "- no automatic active-normal promotion",
            "",
        ]
    )
    return "\n".join(lines)


def doctor_checks() -> list[Check]:
    checks: list[Check] = []
    checks.append(
        Check(
            "Python",
            "ok" if sys.version_info >= (3, 11) else "error",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    node = shutil.which("node")
    if node:
        completed = capture_command([node, "--version"])
        version = completed.stdout.strip() or completed.stderr.strip()
        node_ok = completed.returncode == 0 and parse_version(version) >= MINIMUM_NODE_VERSION
        detail = f"{version or 'version unavailable'}; required>=22.15.0"
        checks.append(Check("Node.js", "ok" if node_ok else "error", detail))
    else:
        checks.append(Check("Node.js", "error", "not found"))

    required_paths = {
        "global-briefing": BRIEFING_ROOT / "scripts" / "briefing_store.py",
        "trading-core": TRADING_ROOT / "src" / "trading_core" / "cli.py",
        "ATLAS site": SITE_ROOT / "package.json",
        "prediction adapter": BRIEFING_ROOT / "scripts" / "export_trading_core_signals.py",
        "research quality gate": BRIEFING_ROOT / "scripts" / "research_quality.py",
    }
    for name, path in required_paths.items():
        checks.append(Check(name, "ok" if path.exists() else "error", str(path.relative_to(ROOT))))

    checks.extend(
        [
            component_repository_check("root", ROOT),
            component_repository_check("trading-core", TRADING_ROOT),
            component_repository_check("ATLAS site", SITE_ROOT),
        ]
    )

    missing_modules = [
        module
        for module in ("yfinance", "akshare", "baostock", "tushare")
        if importlib.util.find_spec(module) is None
    ]
    checks.append(
        Check(
            "Python data dependencies",
            "ok" if not missing_modules else "error",
            "installed" if not missing_modules else f"missing: {', '.join(missing_modules)}",
        )
    )

    if TRADING_ROOT.exists():
        core_version = capture_command(
            [sys.executable, "-m", "trading_core.entrypoint", "--version"],
            cwd=TRADING_ROOT,
            trading_core=True,
        )
        checks.append(
            Check(
                "trading-core CLI",
                "ok" if core_version.returncode == 0 else "error",
                (core_version.stdout or core_version.stderr).strip(),
            )
        )
    else:
        checks.append(Check("trading-core CLI", "error", "component path is missing"))

    try:
        report_date, report_path = latest_report()
        report_age_days = (configured_report_date() - Date.fromisoformat(report_date)).days
        report_fresh = 0 <= report_age_days <= 3
        checks.append(Check(
            "latest briefing",
            "ok" if report_fresh else "error",
            f"{report_date} ({report_path.name}); age_days={report_age_days}; max_age_days=3",
        ))
    except (FileNotFoundError, ValueError) as exc:
        report_date = ""
        checks.append(Check("latest briefing", "error", str(exc)))

    generated_path = SITE_ROOT / "app" / "briefing.generated.json"
    try:
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        generated_date = str(generated.get("reportDate", ""))
        site_status = "ok" if generated_date and generated_date == report_date else "warn"
        checks.append(Check("site data", site_status, f"reportDate={generated_date or 'missing'}", required=False))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(Check("site data", "warn", str(exc), required=False))

    signal_path = BRIEFING_ROOT / "data" / f"macro_signals-{report_date}.jsonl"
    checks.append(
        Check(
            "briefing → trading-core bridge",
            "ok" if report_date and signal_path.exists() else "warn",
            str(signal_path.relative_to(ROOT)) if report_date else "latest date unavailable",
            required=False,
        )
    )
    local_signal_path = TRADING_ROOT / "data" / "macro_signals" / signal_path.name
    if signal_path.exists() and local_signal_path.exists():
        bridge_fresh = file_sha256(signal_path) == file_sha256(local_signal_path)
        bridge_detail = "source and local hashes match" if bridge_fresh else "local copy is stale; run atlas sync"
    else:
        bridge_fresh = False
        bridge_detail = "source or local macro signal file is missing"
    checks.append(Check("macro bridge freshness", "ok" if bridge_fresh else "warn", bridge_detail, required=False))
    checks.append(
        Check(
            "site dependencies",
            "ok" if (SITE_ROOT / "node_modules").exists() else "warn",
            "installed" if (SITE_ROOT / "node_modules").exists() else "run npm install under src",
            required=False,
        )
    )
    source_policy = SITE_ROOT / "security" / "dependency-source-policy.mjs"
    if node and source_policy.exists():
        source_check = capture_command([node, str(source_policy)], cwd=SITE_ROOT)
        source_detail = (source_check.stdout or source_check.stderr).strip()
        checks.append(
            Check(
                "site dependency sources",
                "ok" if source_check.returncode == 0 else "error",
                source_detail or "dependency source policy returned no output",
                required=False,
            )
        )
    return checks


def command_doctor(args: argparse.Namespace) -> int:
    checks = doctor_checks()
    if args.json:
        print(json.dumps({"checks": [asdict(item) for item in checks]}, ensure_ascii=False, indent=2))
    else:
        labels = {"ok": "[OK]", "warn": "[WARN]", "error": "[ERROR]"}
        print("ATLAS workspace doctor")
        for item in checks:
            print(f"{labels[item.status]:7} {item.name}: {item.detail}")
    return 1 if any(item.required and item.status == "error" for item in checks) else 0


def command_sync(args: argparse.Namespace) -> int:
    try:
        date = args.date or latest_report()[0]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    quality_args = argparse.Namespace(date=date, dry_run=args.dry_run, strict=False)
    if command_quality(quality_args) != 0:
        return 1

    exporter = BRIEFING_ROOT / "scripts" / "export_trading_core_signals.py"
    export_command = [sys.executable, str(exporter), "--date", date]
    if args.dry_run:
        export_command.append("--dry-run")
    if run_command(export_command) != 0:
        return 1

    validation = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "evolution.py"),
        "validate-records",
        "--period",
        "day",
        "--date",
        date,
    ]
    if run_command(validation) != 0:
        return 1

    evolution_policy = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "evolution.py"),
        "update-policy",
        "--period",
        "month",
        "--date",
        date,
    ]
    if args.dry_run:
        evolution_policy.append("--dry-run")
    if run_command(evolution_policy) != 0:
        return 1

    load_macro = [sys.executable, "-m", "trading_core.entrypoint", "load-macro", "--date", date]
    if args.dry_run:
        load_macro.append("--dry-run")
    if run_command(load_macro, cwd=TRADING_ROOT, trading_core=True) != 0:
        return 1

    site_sync = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "sync_briefing_site.py"),
        "--date",
        date,
    ]
    if args.dry_run:
        site_sync.append("--dry-run")
    if args.force_site:
        site_sync.append("--force")
    if run_command(site_sync) != 0:
        return 1

    action = "validation" if args.dry_run else "sync"
    print(f"\nATLAS {action} complete for {date}.")
    return 0


def command_quality(args: argparse.Namespace) -> int:
    try:
        date = args.date or latest_report()[0]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    command = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "research_quality.py"),
        "--date",
        date,
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.strict:
        command.append("--strict")
    return run_command(command)


def command_heal(args: argparse.Namespace) -> int:
    """Run the isolated self-healing control plane."""
    command = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "self_healing.py"),
    ]
    if args.status:
        command.append("--status")
    else:
        try:
            date = args.date or latest_report()[0]
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        command.extend(["--date", date])
        if args.apply_safe:
            command.append("--apply-safe")
        if args.deep:
            command.append("--deep")
        if args.strict:
            command.append("--strict")
    if args.json:
        command.append("--json")
    return run_command(command)


def command_improvements(args: argparse.Namespace) -> int:
    """Track retrospective recommendations through cross-run verification."""
    command = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "improvement_tracker.py"),
    ]
    if args.status:
        command.append("--status")
    else:
        try:
            date = args.date or latest_report()[0]
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        command.extend(["--date", date])
        if args.apply_safe:
            command.append("--apply-safe")
        if args.strict:
            command.append("--strict")
    if args.json:
        command.append("--json")
    return run_command(command)


def command_backup(args: argparse.Namespace) -> int:
    command = [sys.executable, str(BRIEFING_ROOT / "scripts" / "disaster_recovery.py"), "--date", args.date]
    if args.json:
        command.append("--json")
    return run_command(command)


def command_alerts(args: argparse.Namespace) -> int:
    command = [sys.executable, str(BRIEFING_ROOT / "scripts" / "alert_dispatch.py"), "--date", args.date]
    if args.json:
        command.append("--json")
    if args.ack_by:
        command.extend(["--ack-by", args.ack_by])
    if args.retry:
        command.append("--retry")
    if args.receipt_destination:
        command.extend(["--receipt-destination", args.receipt_destination])
    if args.receipt_id:
        command.extend(["--receipt-id", args.receipt_id])
    return run_command(command)


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def cycle_lock(date: str):
    lock_path = ATLAS_RUNTIME_ROOT / "cycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = stable_hash({"pid": os.getpid(), "date": date, "started_at": utc_now()})
    payload = {"pid": os.getpid(), "date": date, "started_at": utc_now(), "token": token}
    for _attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            try:
                existing = read_json_file(lock_path, default={})
            except (OSError, ValueError):
                existing = {}
            existing_pid = int(existing.get("pid", 0)) if isinstance(existing, dict) else 0
            try:
                started_at = datetime.fromisoformat(str(existing.get("started_at", "")).replace("Z", "+00:00"))
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                started_at = datetime.now(UTC) - timedelta(days=2)
            stale = not process_is_running(existing_pid) or datetime.now(UTC) - started_at > timedelta(hours=24)
            if stale:
                lock_path.unlink(missing_ok=True)
                continue
            raise RuntimeError(f"ATLAS cycle already running: {existing}") from exc
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            break
    else:
        raise RuntimeError(f"Unable to acquire ATLAS cycle lock: {lock_path}")
    try:
        yield
    finally:
        existing = read_json_file(lock_path, default={})
        if isinstance(existing, dict) and existing.get("token") == token:
            lock_path.unlink(missing_ok=True)


def command_cycle(args: argparse.Namespace) -> int:
    try:
        with cycle_lock(str(args.date or "latest")):
            return _command_cycle_locked(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def required_cycle_stages_passed(stages: Sequence[dict[str, Any]]) -> bool:
    statuses = {str(stage.get("name")): stage.get("status") for stage in stages}
    return all(statuses.get(name) == "passed" for name in RELEASE_REQUIRED_STAGES)


def _command_cycle_locked(args: argparse.Namespace) -> int:
    try:
        date = args.date or latest_report()[0]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cycle_id = f"ATLAS-CYCLE-{date.replace('-', '')}"
    started_at = utc_now()
    fingerprint_before = build_cycle_fingerprint(date)
    stages: list[dict[str, Any]] = []
    blocking: list[str] = []

    try:
        checks = doctor_checks()
    except Exception as exc:
        checks = [Check("doctor", "error", f"{type(exc).__name__}: {exc}")]
    doctor_blocking = [item for item in checks if item.required and item.status == "error"]
    stages.append(
        {
            "name": "doctor",
            "status": "passed" if not doctor_blocking else "failed",
            "detail": [asdict(item) for item in checks],
        }
    )
    if doctor_blocking:
        blocking.extend(f"doctor:{item.name}:{item.detail}" for item in doctor_blocking)

    sync_rc: int | None = None
    if args.skip_sync:
        stages.append({"name": "sync", "status": "skipped", "detail": "skip_sync=true"})
        blocking.append("sync stage was skipped")
    elif doctor_blocking:
        stages.append({"name": "sync", "status": "blocked", "detail": "doctor gate failed"})
    else:
        sync_args = argparse.Namespace(date=date, dry_run=args.dry_run, force_site=args.force_site)
        try:
            sync_rc = command_sync(sync_args)
        except Exception as exc:
            sync_rc = 1
            blocking.append(f"sync raised {type(exc).__name__}: {exc}")
        stages.append({"name": "sync", "status": "passed" if sync_rc == 0 else "failed", "detail": {"returncode": sync_rc}})
        if sync_rc != 0:
            blocking.append(f"sync failed with returncode {sync_rc}")

    ledger_write_allowed = False
    try:
        ledger_result = build_virtual_execution_ledger(write_files=False)
    except Exception as exc:
        reason = f"ledger build raised {type(exc).__name__}: {exc}"
        ledger_result = {
            "ledger_path": str(VIRTUAL_LEDGER_PATH),
            "state_path": str(VIRTUAL_LEDGER_STATE_PATH),
            "audit_path": str(VIRTUAL_LEDGER_AUDIT_PATH),
            "event_count": 0,
            "account_count": 0,
            "content_hash": None,
            "write_requested": ledger_write_allowed,
            "write_performed": False,
            "events": [],
            "state": {},
            "audit": {
                "audit_id": "ATLAS-VIRTUAL-LEDGER-AUDIT-FAILED",
                "ledger_id": LEDGER_ID,
                "schema_version": LEDGER_SCHEMA_VERSION,
                "overall_passed": False,
                "blocking_reasons": [reason],
                "warnings": [],
                "canonical_ledger_path": relative_path(VIRTUAL_LEDGER_PATH),
                "canonical_state_path": relative_path(VIRTUAL_LEDGER_STATE_PATH),
                "event_count": 0,
                "account_count": 0,
                "source_counts": {},
                "boundary": {"single_canonical_virtual_ledger": True, "real_broker_orders_allowed": False},
            },
        }
    ledger_audit = ledger_result["audit"]
    stages.append(
        {
            "name": "canonical_virtual_ledger_audit",
            "status": "passed" if ledger_audit["overall_passed"] else "failed",
            "detail": {
                "event_count": ledger_result["event_count"],
                "account_count": ledger_result["account_count"],
                "audit_path": ledger_result["audit_path"],
                "write_requested": ledger_result["write_requested"],
                "write_performed": ledger_result["write_performed"],
            },
        }
    )
    if not ledger_audit["overall_passed"]:
        blocking.extend(f"ledger:{reason}" for reason in ledger_audit["blocking_reasons"])

    try:
        replay_shadow = run_replay_shadow_validation(date)
    except Exception as exc:
        replay_shadow = {
            "overall_passed": False,
            "safety_gate_passed": False,
            "strategy_evidence_passed": False,
            "replay": {"passed": False, "execution_safety_passed": False, "strategy_evidence_passed": False, "error": f"{type(exc).__name__}: {exc}"},
            "shadow_promotion_gate": {"passed": False, "safety_passed": False, "evidence_passed": False, "payload": {}, "returncode": None},
            "boundary": {"replay_isolated": False, "promotion_auto_apply_allowed": False},
        }
    stages.append(
        {
            "name": "replay_shadow_gate",
            "status": "passed" if replay_shadow["overall_passed"] else "failed",
            "detail": {
                "replay_passed": replay_shadow["replay"]["passed"],
                "strategy_evidence_passed": replay_shadow["replay"].get("strategy_evidence_passed", False),
                "validation_scope": replay_shadow["replay"].get("validation_scope", "execution_isolation_only"),
                "shadow_promotion_gate_passed": replay_shadow["shadow_promotion_gate"]["passed"],
            },
        }
    )
    if not replay_shadow["overall_passed"]:
        blocking.append("replay/shadow validation gate failed")

    tests_rc: int | None = None
    full_tests_requested = bool(getattr(args, "full_tests", False))
    if args.skip_tests:
        stages.append({"name": "targeted_integration_tests", "status": "skipped", "detail": {"skip_tests": True, "full_suite": False}})
        blocking.append("targeted integration tests were skipped")
    else:
        test_args = argparse.Namespace(
            skip_site=args.skip_site,
            skip_trading_core=args.skip_trading_core,
            full=full_tests_requested,
        )
        try:
            tests_rc = command_test(test_args)
        except Exception as exc:
            tests_rc = 1
            blocking.append(f"continuous tests raised {type(exc).__name__}: {exc}")
        test_scope = {
            "kind": "full_regression_suite" if full_tests_requested else "targeted_integration_suite",
            "full_suite": full_tests_requested,
            "root_unittest_discovery": True,
            "briefing_unittest_discovery": True,
            "trading_core_selected_file_count": 0 if args.skip_trading_core else None if full_tests_requested else 8,
            "site_test_included": not args.skip_site,
        }
        stages.append({"name": "targeted_integration_tests", "status": "passed" if tests_rc == 0 else "failed", "detail": {"returncode": tests_rc, "scope": test_scope}})
        if tests_rc != 0:
            blocking.append(f"targeted integration tests failed with returncode {tests_rc}")

    history_root = RUN_AUDIT_ROOT / "history" / date
    history_integrity_before = audit_cycle_history(history_root)
    if not history_integrity_before["passed"]:
        blocking.extend(f"run audit history integrity: {error}" for error in history_integrity_before["errors"])

    ledger_write_allowed = bool(
        not args.dry_run
        and not blocking
        and not args.skip_sync
        and sync_rc == 0
        and tests_rc == 0
        and replay_shadow.get("safety_gate_passed", replay_shadow.get("overall_passed")) is True
        and ledger_audit.get("overall_passed") is True
    )
    if ledger_write_allowed:
        try:
            ledger_result = build_virtual_execution_ledger(write_files=True)
            ledger_audit = ledger_result["audit"]
        except Exception as exc:
            blocking.append(f"canonical ledger commit raised {type(exc).__name__}: {exc}")
            ledger_result["write_performed"] = False
        if not ledger_result.get("write_performed"):
            blocking.append("canonical ledger write was not performed after all upstream gates passed")
        if not ledger_audit.get("overall_passed"):
            blocking.extend(
                f"ledger commit:{reason}" for reason in ledger_audit.get("blocking_reasons", [])
            )
    stages.append({
        "name": "canonical_virtual_ledger_commit",
        "status": "passed" if ledger_result.get("write_performed") else "dry_run" if args.dry_run else "blocked",
        "detail": {
            "all_required_gates_passed": ledger_write_allowed,
            "write_performed": ledger_result.get("write_performed", False),
            "blocked_by": list(blocking),
        },
    })

    fingerprint_after = build_cycle_fingerprint(date)
    workspace_lock = build_workspace_lock()
    workspace_lock_content_sha256 = str(workspace_lock.get("content_sha256") or "")
    previous_state = read_json_file(CYCLE_STATE_PATH, default={})
    previous_key = previous_state.get("idempotency_key") if isinstance(previous_state, dict) else None
    previous_workspace_lock_sha256 = (
        str(previous_state.get("workspace_lock_content_sha256") or "")
        if isinstance(previous_state, dict)
        else ""
    )
    execution_profile = {
        "dry_run": bool(args.dry_run),
        "skip_sync": bool(args.skip_sync),
        "skip_tests": bool(args.skip_tests),
        "skip_site": bool(args.skip_site),
        "skip_trading_core": bool(args.skip_trading_core),
        "force_site": bool(args.force_site),
        "full_tests": full_tests_requested,
    }
    idempotency_key = stable_hash(
        {
            "cycle_id": cycle_id,
            "date": date,
            "fingerprint": fingerprint_after["fingerprint"],
            "ledger_content_hash": ledger_result.get("content_hash"),
            "execution_profile": execution_profile,
            "workspace_lock_content_sha256": workspace_lock_content_sha256,
        }
    )
    previous_passed = bool(previous_state.get("overall_passed")) if isinstance(previous_state, dict) else False
    idempotent_replay = bool(
        previous_key == idempotency_key
        and previous_workspace_lock_sha256 == workspace_lock_content_sha256
        and previous_passed
        and not args.force
    )
    finished_at = utc_now()
    run_id = f"{cycle_id}-RUN-{finished_at.replace(':', '').replace('-', '').replace('.', '')}"
    operational_gate_passed = not blocking
    release_required_stages_passed = required_cycle_stages_passed(stages)
    research_promotion_passed = bool(
        replay_shadow.get("strategy_evidence_passed") is True
        and replay_shadow.get("shadow_promotion_gate", {}).get("evidence_passed") is True
    )
    release_candidate_passed = bool(
        operational_gate_passed
        and idempotent_replay
        and full_tests_requested
        and not args.skip_site
        and not args.skip_trading_core
        and sync_rc == 0
        and ledger_result.get("write_performed") is True
        and release_required_stages_passed
        and workspace_lock["release_reproducible"]
    )

    audit_payload = {
        "cycle_id": cycle_id,
        "run_id": run_id,
        "date": date,
        "started_at": started_at,
        "finished_at": finished_at,
        "dry_run": bool(args.dry_run),
        "execution_profile": execution_profile,
        "idempotency_key": idempotency_key,
        "idempotent_replay": idempotent_replay,
        "workspace_lock_content_sha256": workspace_lock_content_sha256,
        "gate_scope": "daily_operational",
        "overall_passed": operational_gate_passed,
        "operational_gate_passed": operational_gate_passed,
        "release_candidate_passed": release_candidate_passed,
        "release_candidate_evidence": {
            "sync_passed": sync_rc == 0,
            "canonical_ledger_write_performed": ledger_result.get("write_performed") is True,
            "required_stages": list(RELEASE_REQUIRED_STAGES),
            "required_stages_passed": release_required_stages_passed,
        },
        "research_promotion_passed": research_promotion_passed,
        "blocking_reasons": blocking,
        "stages": stages,
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
        "ledger": {
            "ledger_path": ledger_result["ledger_path"],
            "state_path": ledger_result["state_path"],
            "audit_path": ledger_result["audit_path"],
            "event_count": ledger_result["event_count"],
            "account_count": ledger_result["account_count"],
            "content_hash": ledger_result.get("content_hash"),
            "write_requested": ledger_result.get("write_requested", False),
            "write_performed": ledger_result.get("write_performed", False),
        },
        "ledger_audit": ledger_audit,
        "replay_shadow_validation": replay_shadow,
        "test_returncode": tests_rc,
        "sync_returncode": sync_rc,
        "boundary": {
            "unified_cycle_entry": True,
            "single_virtual_execution_ledger": ledger_audit.get("boundary", {}).get("single_canonical_virtual_ledger") is True,
            "idempotent_outputs": idempotent_replay,
            "idempotency_verified_by_repeated_fingerprint": idempotent_replay,
            "safe_fail_closed_gates": bool(
                (ledger_result.get("write_performed") and ledger_write_allowed)
                or (not ledger_result.get("write_performed") and not ledger_write_allowed)
            ),
            "canonical_write_performed": ledger_result.get("write_performed", False),
            "canonical_write_blocked_by_upstream_gate": bool(not ledger_write_allowed and not args.dry_run),
            "release_required_stages_passed": release_required_stages_passed,
            "replay_and_shadow_validation": replay_shadow["overall_passed"],
            "targeted_integration_test_gate": tests_rc == 0,
            "full_test_suite_executed": full_tests_requested and tests_rc == 0,
            "run_audit_written": not args.dry_run,
            "immutable_run_history": history_integrity_before["passed"] and not args.dry_run,
            "real_broker_orders_allowed": False,
        },
        "history_integrity_before": history_integrity_before,
        "workspace_lock": workspace_lock,
    }
    audit_payload["audit_chain"] = {
        "schema_version": 1,
        "sequence": history_integrity_before["next_sequence"],
        "previous_audit_sha256": history_integrity_before["last_audit_sha256"],
        "legacy_record_count_at_genesis": history_integrity_before["legacy_record_count"],
    }
    audit_payload["audit_chain"]["entry_sha256"] = audit_record_hash(audit_payload)
    audit_json_path = RUN_AUDIT_ROOT / f"atlas-cycle-{date}.json"
    audit_md_path = RUN_AUDIT_ROOT / f"ATLAS_CYCLE_RUN_AUDIT-{date}.md"
    history_json_path = history_root / f"{run_id}.json"
    history_md_path = history_root / f"{run_id}.md"
    if not args.dry_run:
        atomic_write_json(history_json_path, audit_payload)
        history_integrity_after = audit_cycle_history(history_root)
        if not history_integrity_after["passed"]:
            raise RuntimeError("run audit history verification failed: " + "; ".join(history_integrity_after["errors"]))
        atomic_write_text(history_md_path, build_cycle_audit_markdown(audit_payload))
        atomic_write_json(audit_json_path, audit_payload)
        atomic_write_text(audit_md_path, build_cycle_audit_markdown(audit_payload))
        atomic_write_json(ATLAS_RUNTIME_ROOT / "workspace-lock.json", workspace_lock)
        atomic_write_json(
            CYCLE_STATE_PATH,
            {
                "cycle_id": cycle_id,
                "date": date,
                "idempotency_key": idempotency_key,
                "execution_profile": execution_profile,
                "fingerprint": fingerprint_after["fingerprint"],
                "ledger_content_hash": ledger_result.get("content_hash"),
                "workspace_lock_content_sha256": workspace_lock_content_sha256,
                "last_audit_json": str(audit_json_path),
                "last_audit_markdown": str(audit_md_path),
                "last_history_json": str(history_json_path),
                "last_history_markdown": str(history_md_path),
                "updated_at": audit_payload["finished_at"],
                "overall_passed": audit_payload["overall_passed"],
                "operational_gate_passed": audit_payload["operational_gate_passed"],
                "release_candidate_passed": audit_payload["release_candidate_passed"],
                "blocking_reasons": audit_payload["blocking_reasons"],
            },
        )

    summary = {
        "cycle_id": cycle_id,
        "date": date,
        "overall_passed": audit_payload["overall_passed"],
        "blocking_reasons": blocking,
        "ledger_path": ledger_result["ledger_path"],
        "event_count": ledger_result["event_count"],
        "run_audit": None if args.dry_run else str(audit_json_path),
        "run_history": None if args.dry_run else str(history_json_path),
        "idempotent_replay": idempotent_replay,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if audit_payload["overall_passed"] else 1


def npm_command() -> str:
    candidate = "npm.cmd" if os.name == "nt" else "npm"
    return shutil.which(candidate) or candidate


def command_test(args: argparse.Namespace) -> int:
    root_tests = ROOT / "tests"
    if root_tests.exists():
        atlas_tests = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            str(root_tests),
            "-p",
            "test_*.py",
        ]
        if run_command(atlas_tests) != 0:
            return 1

    adapter_tests = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        str(BRIEFING_ROOT / "tests"),
        "-p",
        "test_*.py",
    ]
    if run_command(adapter_tests) != 0:
        return 1

    if not args.skip_trading_core:
        core_tests = [sys.executable, "-m", "pytest"]
        if not getattr(args, "full", False):
            core_tests.extend(
                [
                    "tests/test_global_briefing_integration.py",
                    "tests/test_global_briefing_integration_robustness.py",
                    "tests/test_signal_schema.py",
                    "tests/test_entrypoint.py",
                    "tests/test_macro_signal_refresh.py",
                    "tests/test_promotion_gate.py",
                    "tests/test_promotion_evidence.py",
                    "tests/test_cli_stage_boundaries.py",
                ]
            )
        if run_command(core_tests, cwd=TRADING_ROOT, trading_core=True) != 0:
            return 1

    if not args.skip_site:
        if run_command([npm_command(), "test"], cwd=SITE_ROOT) != 0:
            return 1
    return 0


def command_build_site(_args: argparse.Namespace) -> int:
    return run_command([npm_command(), "test"], cwd=SITE_ROOT)


def command_serve(_args: argparse.Namespace) -> int:
    return run_command([npm_command(), "run", "dev"], cwd=SITE_ROOT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ATLAS unified briefing and research workspace.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check the unified workspace and dependencies.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    doctor.set_defaults(handler=command_doctor)

    sync = subparsers.add_parser("sync", help="Bridge the latest briefing into trading-core and the site.")
    sync.add_argument("--date", type=valid_iso_date, help="Briefing date; defaults to the newest dated report.")
    sync.add_argument("--dry-run", action="store_true", help="Validate the bridge without writing files.")
    sync.add_argument("--force-site", action="store_true", help="Regenerate site data even if already deployed.")
    sync.set_defaults(handler=command_sync)

    quality = subparsers.add_parser("quality", help="Audit report content and proper forecast calibration readiness.")
    quality.add_argument("--date", type=valid_iso_date, help="Briefing date; defaults to the newest dated report.")
    quality.add_argument("--dry-run", action="store_true", help="Run the audit without writing its JSON artifact.")
    quality.add_argument("--strict", action="store_true", help="Fail unless all research-readiness gates pass.")
    quality.set_defaults(handler=command_quality)

    heal = subparsers.add_parser("heal", help="Detect, register, and safely repair allowlisted ATLAS issues.")
    heal.add_argument("--date", type=valid_iso_date, help="Briefing date; defaults to the newest dated report.")
    heal.add_argument("--apply-safe", action="store_true", help="Apply only allowlisted low-risk repairs with rollback.")
    heal.add_argument("--deep", action="store_true", help="Also run briefing and website quality suites.")
    heal.add_argument("--strict", action="store_true", help="Fail when a critical issue remains unresolved.")
    heal.add_argument("--status", action="store_true", help="Show the latest self-healing report without running probes.")
    heal.add_argument("--json", action="store_true", help="Print full machine-readable output.")
    heal.set_defaults(handler=command_heal)

    improvements = subparsers.add_parser("improvements", help="Track and verify retrospective improvement actions.")
    improvements.add_argument("--date", type=valid_iso_date, help="Review date; defaults to the newest dated report.")
    improvements.add_argument("--apply-safe", action="store_true", help="Repair allowlisted low-risk derived artifacts.")
    improvements.add_argument("--strict", action="store_true", help="Fail on overdue or regressed critical actions.")
    improvements.add_argument("--status", action="store_true", help="Show the latest improvement report.")
    improvements.add_argument("--json", action="store_true", help="Print full machine-readable output.")
    improvements.set_defaults(handler=command_improvements)

    backup = subparsers.add_parser("backup", help="Create and restore-verify an external disaster-recovery snapshot.")
    backup.add_argument("--date", type=valid_iso_date, required=True)
    backup.add_argument("--json", action="store_true")
    backup.set_defaults(handler=command_backup)

    alerts = subparsers.add_parser("alerts", help="Prepare the audited Codex task-inbox alert payload.")
    alerts.add_argument("--date", type=valid_iso_date, required=True)
    alerts.add_argument("--json", action="store_true")
    alerts.add_argument("--ack-by", help="Record who acknowledged the date-aligned alert payload.")
    alerts.add_argument("--retry", action="store_true", help="Prepare the next delivery attempt or escalate.")
    alerts.add_argument(
        "--receipt-destination",
        help="Configured destination that returned a durable delivery receipt.",
    )
    alerts.add_argument("--receipt-id", help="Provider or connector receipt identifier.")
    alerts.set_defaults(handler=command_alerts)

    cycle = subparsers.add_parser("cycle", help="Run the gated ATLAS virtual trading/evolution cycle.")
    cycle.add_argument("--date", type=valid_iso_date, help="Cycle date; defaults to the newest dated report.")
    cycle.add_argument("--dry-run", action="store_true", help="Validate the cycle without writing cycle/ledger artifacts.")
    cycle.add_argument("--force", action="store_true", help="Re-run even when the previous idempotency key matches.")
    cycle.add_argument("--force-site", action="store_true", help="Regenerate site data during sync even if already deployed.")
    cycle.add_argument("--skip-sync", action="store_true", help="Skip briefing/trading-core/site sync stage.")
    cycle.add_argument("--skip-tests", action="store_true", help="Skip the continuous test gate.")
    cycle.add_argument("--skip-site", action="store_true", help="Skip site tests inside the continuous test gate.")
    cycle.add_argument("--skip-trading-core", action="store_true", help="Skip trading-core tests inside the continuous test gate.")
    cycle.add_argument("--full-tests", action="store_true", help="Run the complete regression suite and record release-candidate evidence.")
    cycle.set_defaults(handler=command_cycle)

    tests = subparsers.add_parser("test", help="Run the cross-project integration test suite.")
    tests.add_argument("--skip-site", action="store_true")
    tests.add_argument("--skip-trading-core", action="store_true")
    tests.add_argument("--full", action="store_true", help="Run the complete trading-core regression suite.")
    tests.set_defaults(handler=command_test)

    build_site = subparsers.add_parser("build-site", help="Build and test the ATLAS web surface.")
    build_site.set_defaults(handler=command_build_site)

    serve = subparsers.add_parser("serve", help="Start the ATLAS site in development mode.")
    serve.set_defaults(handler=command_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
