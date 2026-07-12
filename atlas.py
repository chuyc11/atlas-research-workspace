#!/usr/bin/env python3
"""Unified command surface for the ATLAS briefing and research workspace."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
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


ROOT = Path(__file__).resolve().parent
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
TRADING_ROOT = ROOT / "work" / "trading-core"
SITE_ROOT = ROOT / "src"
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
FORBIDDEN_LEDGER_KEYS = {
    "broker_order_id",
    "broker_account",
    "account_number",
    "live_order_id",
    "external_order_id",
    "real_order_id",
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
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=command_env(trading_core=trading_core),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


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
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def atomic_write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    atomic_write_text(path, text)


def read_json_file(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
        rows.append(payload)
    return rows


def maybe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def nested_forbidden_keys(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in FORBIDDEN_LEDGER_KEYS:
                found.append(path)
            found.extend(nested_forbidden_keys(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_forbidden_keys(value, f"{prefix}[{index}]"))
    return found


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


def virtual_ledger_source_files() -> list[Path]:
    files = [
        BRIEFING_ROOT / "data" / "paper_trades_us.jsonl",
        BRIEFING_ROOT / "data" / "paper_trades_china.jsonl",
        BRIEFING_ROOT / "data" / "paper_portfolio_us.json",
        BRIEFING_ROOT / "data" / "paper_portfolio_china.json",
    ]
    briefing_data = BRIEFING_ROOT / "data"
    if briefing_data.exists():
        files.extend(sorted(briefing_data.glob("temp_orders_*.json")))
        files.extend(sorted(briefing_data.glob("temp_prices_*.json")))
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
        BRIEFING_ROOT / "data" / f"temp_prices_{date}.json",
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
        "status": status,
        "trade_id": str(row.get("trade_id") or event_id),
        "order_id": str(row.get("order_id") or event_id.replace("TRADE", "ORDER")),
        "prediction_id": row.get("prediction_id"),
        "scenario": row.get("scenario"),
        "reason": row.get("reason"),
        "risk": row.get("risk"),
        "paper_trading_only": bool(row.get("paper_trading_only", True)),
        "isolated_replay": False,
        "no_real_broker_order": True,
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
        "paper_trading_only": True,
        "isolated_replay": bool(row.get("isolated", True)),
        "no_real_broker_order": True,
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
    match = re.search(r"temp_orders_(\d{4}-\d{2}-\d{2})\.json$", path.name)
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
        "paper_trading_only": True,
        "isolated_replay": False,
        "no_real_broker_order": True,
    }


def load_virtual_account_snapshots() -> dict[str, Any]:
    accounts: dict[str, Any] = {}
    for account, path in {
        "US": BRIEFING_ROOT / "data" / "paper_portfolio_us.json",
        "CHINA": BRIEFING_ROOT / "data" / "paper_portfolio_china.json",
    }.items():
        payload = read_json_file(path, default=None)
        if isinstance(payload, dict):
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
                "paper_trading_only": payload.get("mode") == "paper_trading",
            }
    replay_account_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "accounts"
    if replay_account_root.exists():
        for path in sorted(replay_account_root.glob("account-*.json")):
            payload = read_json_file(path, default=None)
            if isinstance(payload, dict):
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
                    "paper_trading_only": True,
                }
    return accounts


def audit_virtual_execution_ledger(
    events: Sequence[dict[str, Any]],
    account_state: dict[str, Any],
    *,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    blocking: list[str] = []
    ids = [str(event.get("ledger_event_id", "")) for event in events]
    if len(ids) != len(set(ids)):
        blocking.append("duplicate ledger_event_id detected")
    allowed_sides = {"BUY", "SELL", "HOLD"}
    required_trade_fields = {"trade_id", "order_id", "date", "account_id", "symbol", "side", "filled_price", "filled_quantity", "status"}
    required_non_empty = {"ledger_event_id", "event_type", "date", "account_id", "side", "status"}
    source_counts: dict[str, int] = {}
    for event in events:
        event_id = str(event.get("ledger_event_id") or "missing-event-id")
        source_counts[str(event.get("source_ledger"))] = source_counts.get(str(event.get("source_ledger")), 0) + 1
        missing = sorted(field for field in required_trade_fields if field not in event)
        if missing:
            blocking.append(f"{event_id}: missing required fields {missing}")
        empty = sorted(field for field in required_non_empty if event.get(field) in {None, ""})
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
        if maybe_float(event.get("filled_quantity")) < 0:
            blocking.append(f"{event_id}: negative quantity")
        if maybe_float(event.get("filled_price")) < 0:
            blocking.append(f"{event_id}: negative price")
        if not is_iso_date(event.get("date")):
            blocking.append(f"{event_id}: invalid date {event.get('date')!r}")
        event_type = event.get("event_type")
        if event_type not in {"virtual_trade", "virtual_hold", "virtual_order_intent", "isolated_replay_trade"}:
            blocking.append(f"{event_id}: unsupported event_type {event_type!r}")
        if event_type in {"virtual_trade", "isolated_replay_trade"}:
            if str(event.get("side")).upper() not in {"BUY", "SELL"}:
                blocking.append(f"{event_id}: filled trade must be BUY or SELL")
            if maybe_float(event.get("filled_quantity")) <= 0 or maybe_float(event.get("filled_price")) <= 0:
                blocking.append(f"{event_id}: filled trade requires positive quantity and price")
        if event_type == "isolated_replay_trade" and event.get("isolated_replay") is not True:
            blocking.append(f"{event_id}: replay trade is not isolated")
        if event_type == "virtual_hold":
            if str(event.get("side")).upper() != "HOLD" or event.get("status") != "HELD":
                blocking.append(f"{event_id}: virtual hold must use HOLD/HELD")
            if maybe_float(event.get("filled_quantity")) != 0:
                blocking.append(f"{event_id}: virtual hold quantity must be zero")
        if event_type == "virtual_order_intent" and str(event.get("side")).upper() in {"BUY", "SELL"}:
            if maybe_float(event.get("filled_quantity")) <= 0 or maybe_float(event.get("filled_price")) <= 0:
                blocking.append(f"{event_id}: order intent requires positive quantity and price")
        forbidden = sorted(nested_forbidden_keys(event))
        if forbidden:
            blocking.append(f"{event_id}: forbidden broker/live keys {forbidden}")
    if not account_state:
        blocking.append("no virtual account snapshots found")
    for account_id, account in account_state.items():
        if account.get("paper_trading_only") is not True:
            blocking.append(f"account {account_id}: paper_trading_only is not true")
        if account.get("virtual_account_scope") == "REPLAY" and account.get("isolated_replay") is not True:
            blocking.append(f"account {account_id}: replay account is not isolated")
        forbidden = sorted(nested_forbidden_keys(account))
        if forbidden:
            blocking.append(f"account {account_id}: forbidden broker/live keys {forbidden}")
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
        },
    }
    return payload


def build_virtual_execution_ledger(*, write_files: bool = True) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    for source_ledger, path in {
        "global_briefing_paper_us": BRIEFING_ROOT / "data" / "paper_trades_us.jsonl",
        "global_briefing_paper_china": BRIEFING_ROOT / "data" / "paper_trades_china.jsonl",
    }.items():
        if not path.exists():
            warnings.append(f"missing source ledger: {relative_path(path)}")
            continue
        for line_number, row in enumerate(read_jsonl_file(path), 1):
            events.append(canonicalize_global_paper_trade(row, path, line_number, source_ledger))

    briefing_data = BRIEFING_ROOT / "data"
    if briefing_data.exists():
        for path in sorted(briefing_data.glob("temp_orders_*.json")):
            for order_index, row in enumerate(load_temp_orders(path), 1):
                events.append(canonicalize_temp_order_intent(row, path, order_index))

    replay_trade_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "trades"
    if replay_trade_root.exists():
        for path in sorted(replay_trade_root.glob("*.jsonl")):
            for line_number, row in enumerate(read_jsonl_file(path), 1):
                events.append(canonicalize_replay_trade(row, path, line_number))
    else:
        warnings.append(f"missing replay trade source directory: {relative_path(replay_trade_root)}")

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
    account_state = load_virtual_account_snapshots()
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
    audit = audit_virtual_execution_ledger(events, account_state, warnings=warnings)
    write_performed = bool(write_files and audit["overall_passed"])
    if write_performed:
        atomic_write_jsonl(VIRTUAL_LEDGER_PATH, events)
        atomic_write_json(VIRTUAL_LEDGER_STATE_PATH, state_payload)
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
        f"- overall_passed={str(payload['overall_passed']).lower()}",
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
        node_ok = completed.returncode == 0 and parse_version(version) >= (22, 13, 0)
        checks.append(Check("Node.js", "ok" if node_ok else "error", version or "version unavailable"))
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

    try:
        report_date, report_path = latest_report()
        report_age_days = (Date.today() - Date.fromisoformat(report_date)).days
        report_fresh = 0 <= report_age_days <= 3
        checks.append(Check(
            "latest briefing",
            "ok" if report_fresh else "error",
            f"{report_date} ({report_path.name}); age_days={report_age_days}; max_age_days=3",
        ))
    except FileNotFoundError as exc:
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
        except FileExistsError:
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
            raise RuntimeError(f"ATLAS cycle already running: {existing}")
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
    if args.skip_tests:
        stages.append({"name": "targeted_integration_tests", "status": "skipped", "detail": {"skip_tests": True, "full_suite": False}})
        blocking.append("targeted integration tests were skipped")
    else:
        test_args = argparse.Namespace(skip_site=args.skip_site, skip_trading_core=args.skip_trading_core)
        try:
            tests_rc = command_test(test_args)
        except Exception as exc:
            tests_rc = 1
            blocking.append(f"continuous tests raised {type(exc).__name__}: {exc}")
        test_scope = {
            "kind": "targeted_integration_suite",
            "full_suite": False,
            "root_unittest_discovery": True,
            "briefing_unittest_discovery": True,
            "trading_core_selected_file_count": 0 if args.skip_trading_core else 8,
            "site_test_included": not args.skip_site,
        }
        stages.append({"name": "targeted_integration_tests", "status": "passed" if tests_rc == 0 else "failed", "detail": {"returncode": tests_rc, "scope": test_scope}})
        if tests_rc != 0:
            blocking.append(f"targeted integration tests failed with returncode {tests_rc}")

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
    previous_state = read_json_file(CYCLE_STATE_PATH, default={})
    previous_key = previous_state.get("idempotency_key") if isinstance(previous_state, dict) else None
    execution_profile = {
        "dry_run": bool(args.dry_run),
        "skip_sync": bool(args.skip_sync),
        "skip_tests": bool(args.skip_tests),
        "skip_site": bool(args.skip_site),
        "skip_trading_core": bool(args.skip_trading_core),
        "force_site": bool(args.force_site),
    }
    idempotency_key = stable_hash(
        {
            "cycle_id": cycle_id,
            "date": date,
            "fingerprint": fingerprint_after["fingerprint"],
            "ledger_content_hash": ledger_result.get("content_hash"),
            "execution_profile": execution_profile,
        }
    )
    previous_passed = bool(previous_state.get("overall_passed")) if isinstance(previous_state, dict) else False
    idempotent_replay = previous_key == idempotency_key and previous_passed and not args.force
    finished_at = utc_now()
    run_id = f"{cycle_id}-RUN-{finished_at.replace(':', '').replace('-', '').replace('.', '')}"

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
        "overall_passed": not blocking,
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
            "replay_and_shadow_validation": replay_shadow["overall_passed"],
            "targeted_integration_test_gate": tests_rc == 0,
            "full_test_suite_executed": False,
            "run_audit_written": not args.dry_run,
            "immutable_run_history": not args.dry_run,
            "real_broker_orders_allowed": False,
        },
    }
    audit_json_path = RUN_AUDIT_ROOT / f"atlas-cycle-{date}.json"
    audit_md_path = RUN_AUDIT_ROOT / f"ATLAS_CYCLE_RUN_AUDIT-{date}.md"
    history_root = RUN_AUDIT_ROOT / "history" / date
    history_json_path = history_root / f"{run_id}.json"
    history_md_path = history_root / f"{run_id}.md"
    if not args.dry_run:
        atomic_write_json(audit_json_path, audit_payload)
        atomic_write_text(audit_md_path, build_cycle_audit_markdown(audit_payload))
        atomic_write_json(history_json_path, audit_payload)
        atomic_write_text(history_md_path, build_cycle_audit_markdown(audit_payload))
        atomic_write_json(
            CYCLE_STATE_PATH,
            {
                "cycle_id": cycle_id,
                "date": date,
                "idempotency_key": idempotency_key,
                "execution_profile": execution_profile,
                "fingerprint": fingerprint_after["fingerprint"],
                "ledger_content_hash": ledger_result.get("content_hash"),
                "last_audit_json": str(audit_json_path),
                "last_audit_markdown": str(audit_md_path),
                "last_history_json": str(history_json_path),
                "last_history_markdown": str(history_md_path),
                "updated_at": audit_payload["finished_at"],
                "overall_passed": audit_payload["overall_passed"],
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
        core_tests = [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_global_briefing_integration.py",
            "tests/test_global_briefing_integration_robustness.py",
            "tests/test_signal_schema.py",
            "tests/test_entrypoint.py",
            "tests/test_macro_signal_refresh.py",
            "tests/test_promotion_gate.py",
            "tests/test_promotion_evidence.py",
            "tests/test_cli_stage_boundaries.py",
        ]
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

    cycle = subparsers.add_parser("cycle", help="Run the gated ATLAS virtual trading/evolution cycle.")
    cycle.add_argument("--date", type=valid_iso_date, help="Cycle date; defaults to the newest dated report.")
    cycle.add_argument("--dry-run", action="store_true", help="Validate the cycle without writing cycle/ledger artifacts.")
    cycle.add_argument("--force", action="store_true", help="Re-run even when the previous idempotency key matches.")
    cycle.add_argument("--force-site", action="store_true", help="Regenerate site data during sync even if already deployed.")
    cycle.add_argument("--skip-sync", action="store_true", help="Skip briefing/trading-core/site sync stage.")
    cycle.add_argument("--skip-tests", action="store_true", help="Skip the continuous test gate.")
    cycle.add_argument("--skip-site", action="store_true", help="Skip site tests inside the continuous test gate.")
    cycle.add_argument("--skip-trading-core", action="store_true", help="Skip trading-core tests inside the continuous test gate.")
    cycle.set_defaults(handler=command_cycle)

    tests = subparsers.add_parser("test", help="Run the cross-project integration test suite.")
    tests.add_argument("--skip-site", action="store_true")
    tests.add_argument("--skip-trading-core", action="store_true")
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
