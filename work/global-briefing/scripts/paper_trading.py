#!/usr/bin/env python3
"""Virtual paper-trading ledger for the daily global briefing project."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import socket
import sys
from contextlib import ExitStack, contextmanager
from datetime import date as Date
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "paper_trading.json"
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from paper_theme_registry import assignment_for as registry_theme_assignment
from paper_theme_registry import audit_history as audit_theme_registry_history
from paper_theme_registry import load_registry as load_theme_registry
from paper_theme_registry import resolve_order_theme
from report_clock import report_date
from report_clock import report_now


ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def report_settings_path() -> Path:
    return ROOT / "work" / "global-briefing" / "config" / "settings.json"


def current_business_date(now: datetime | None = None) -> str:
    return report_date(now, settings_path=report_settings_path())


def now_iso(now: datetime | None = None) -> str:
    return report_now(now, settings_path=report_settings_path()).isoformat(timespec="seconds")


def utc_now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("UTC audit clock requires a timezone-aware datetime.")
    return current.astimezone(UTC).isoformat(timespec="seconds")


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def strict_json_loads(text: str, *, source: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{source} contains non-standard numeric constant {value}.")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {source}: {exc}") from exc


def finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number, not a boolean.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite.")
    return number


def positive_float(value: Any, field: str) -> float:
    number = finite_float(value, field)
    if number <= 0:
        raise ValueError(f"{field} must be positive.")
    return number


def non_negative_float(value: Any, field: str) -> float:
    number = finite_float(value, field)
    if number < 0:
        raise ValueError(f"{field} must be non-negative.")
    return number


def non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer, not a boolean.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer.") from exc
    if number < 0 or str(number) != str(value).strip():
        raise ValueError(f"{field} must be a non-negative integer.")
    return number


def strict_iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ISO_DATE_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must use strict ISO YYYY-MM-DD format.")
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO date.") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use strict ISO YYYY-MM-DD format.")
    return value


def require_monotonic_as_of(state: dict[str, Any], candidate_date: str, *, operation: str) -> str:
    candidate = strict_iso_date(candidate_date, f"{operation} date")
    current_raw = state.get("as_of_date")
    if current_raw in (None, ""):
        return candidate
    current = strict_iso_date(current_raw, "Portfolio as_of_date")
    if candidate < current:
        raise ValueError(
            f"{operation} date {candidate} cannot move portfolio as_of_date backward from {current}."
        )
    return candidate


def business_day_age(price_date: str, valuation_date: str) -> int:
    price_day = Date.fromisoformat(price_date)
    valuation_day = Date.fromisoformat(valuation_date)
    if price_day > valuation_day:
        raise ValueError(f"Price date {price_date} cannot follow valuation date {valuation_date}.")
    age = 0
    cursor = price_day + timedelta(days=1)
    while cursor <= valuation_day:
        if cursor.weekday() < 5:
            age += 1
        cursor += timedelta(days=1)
    return age


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2) + "\n",
    )


def validate_config(config: dict[str, Any]) -> None:
    for key in ("max_position_pct", "max_daily_turnover_pct"):
        value = finite_float(config[key], key)
        if not 0 < value <= 1:
            raise ValueError(f"{key} must be greater than 0 and at most 1.")
    min_cash_pct = finite_float(config["min_cash_pct"], "min_cash_pct")
    if not 0 <= min_cash_pct < 1:
        raise ValueError("min_cash_pct must be at least 0 and less than 1.")
    if int(config["max_new_positions_per_day"]) < 1:
        raise ValueError("max_new_positions_per_day must be at least 1.")
    positive_float(config["initial_cash"], "initial_cash")
    non_negative_float(config.get("default_fee_rate", 0.0), "default_fee_rate")

    for account_name, item in config.get("accounts", {}).items():
        if not isinstance(item, dict):
            raise ValueError(f"Account configuration {account_name} must be an object.")
        positive_float(item.get("initial_cash", config["initial_cash"]), f"accounts.{account_name}.initial_cash")
        for currency, rate in item.get("fx_rates_to_base", {}).items():
            positive_float(rate, f"accounts.{account_name}.fx_rates_to_base.{currency}")

    for market_name, rules in config.get("market_rules", {}).items():
        if not isinstance(rules, dict):
            raise ValueError(f"Market rules {market_name} must be an object.")
        for field in ("commission_rate", "min_commission", "stamp_tax_sell_rate"):
            if field in rules:
                non_negative_float(rules[field], f"market_rules.{market_name}.{field}")
        for field in (
            "default_price_limit_pct",
            "star_market_price_limit_pct",
            "chinext_price_limit_pct",
            "st_price_limit_pct",
        ):
            if field in rules:
                value = non_negative_float(rules[field], f"market_rules.{market_name}.{field}")
                if value > 1:
                    raise ValueError(f"market_rules.{market_name}.{field} must be at most 1.")

    valuation_policy = config.get("valuation_policy", {})
    if valuation_policy:
        if not isinstance(valuation_policy, dict):
            raise ValueError("valuation_policy must be an object.")
        stale_policy = str(valuation_policy.get("stale_price_policy", "fail")).strip().lower()
        if stale_policy not in {"fail", "partial"}:
            raise ValueError("valuation_policy.stale_price_policy must be fail or partial.")
        non_negative_int(
            valuation_policy.get("max_price_age_business_days", 1),
            "valuation_policy.max_price_age_business_days",
        )

    contract = config.get("order_contract", {}) if isinstance(config.get("order_contract"), dict) else {}
    if "max_price_age_business_days" in contract:
        non_negative_int(
            contract["max_price_age_business_days"],
            "order_contract.max_price_age_business_days",
        )
    for field in (
        "price_date_required_from_date",
        "prediction_reference_required_from_date",
        "theme_required_from_date",
        "maximum_theme_exposure_enforce_from_date",
        "theme_registry_history_required_from_date",
    ):
        if contract.get(field):
            datetime.strptime(str(contract[field])[:10], "%Y-%m-%d")
    if contract.get("prediction_reference_required_from_date"):
        prediction_ledger_file = config.get("prediction_ledger_file")
        if not isinstance(prediction_ledger_file, str) or not prediction_ledger_file.strip():
            raise ValueError(
                "prediction_ledger_file is required when prediction-reference enforcement is configured."
            )
    if contract.get("theme_registry_history_required_from_date") and not all(
        config.get(field) for field in ("theme_registry_history_file", "theme_registry_snapshot_dir")
    ):
        raise ValueError(
            "theme_registry_history_file and theme_registry_snapshot_dir are required when registry-history enforcement is configured."
        )

    profile = config.get("strategy_profile", {})
    if not isinstance(profile, dict) or not profile.get("enabled"):
        return
    if config.get("allow_leverage") or config.get("allow_short") or config.get("allow_options"):
        raise ValueError("Aggressive paper strategy cannot enable leverage, shorts, or options.")

    invested = profile.get("target_invested_pct", {})
    invested_values = [
        finite_float(invested[key], f"strategy_profile.target_invested_pct.{key}")
        for key in ("minimum", "preferred", "maximum")
    ]
    if not 0 <= invested_values[0] <= invested_values[1] <= invested_values[2] <= 1:
        raise ValueError("strategy_profile target invested percentages are invalid.")

    cash = profile.get("target_cash_pct", {})
    cash_values = [
        finite_float(cash[key], f"strategy_profile.target_cash_pct.{key}")
        for key in ("hard_minimum", "preferred_minimum", "preferred_maximum")
    ]
    if not 0 <= cash_values[0] <= cash_values[1] <= cash_values[2] <= 1:
        raise ValueError("strategy_profile target cash percentages are invalid.")
    if abs(cash_values[0] - min_cash_pct) > 1e-9:
        raise ValueError("strategy_profile hard cash minimum must equal min_cash_pct.")

    signal = profile.get("signal_score", {})
    thresholds = [
        finite_float(signal[key], f"strategy_profile.signal_score.{key}")
        for key in ("exit_threshold", "reduce_threshold", "buy_threshold", "add_threshold")
    ]
    if not 0 <= thresholds[0] < thresholds[1] < thresholds[2] < thresholds[3] <= 100:
        raise ValueError("strategy_profile signal thresholds must increase from exit to add.")
    component_total = sum(
        finite_float(value, f"strategy_profile.signal_score.components.{key}")
        for key, value in signal.get("components", {}).items()
    )
    if abs(component_total - 100) > 1e-9:
        raise ValueError("strategy_profile signal component weights must total 100.")
    risk_overlays = profile.get("risk_overlays", {}) if isinstance(profile.get("risk_overlays"), dict) else {}
    if "maximum_theme_exposure_pct" in risk_overlays:
        maximum_theme = finite_float(
            risk_overlays["maximum_theme_exposure_pct"],
            "strategy_profile.risk_overlays.maximum_theme_exposure_pct",
        )
        if not 0 < maximum_theme <= 1:
            raise ValueError("strategy_profile maximum_theme_exposure_pct must be greater than 0 and at most 1.")


def load_config() -> dict[str, Any]:
    config = strict_json_loads(
        CONFIG_PATH.read_text(encoding="utf-8"),
        source=str(CONFIG_PATH),
    )
    if not isinstance(config, dict):
        raise ValueError(f"Paper-trading configuration must be a JSON object: {CONFIG_PATH}")
    validate_config(config)
    return config


def resolve_path(config: dict[str, Any], key: str) -> Path:
    return ROOT / config[key]


def account_names(config: dict[str, Any]) -> list[str]:
    accounts = config.get("accounts", {})
    return sorted(accounts) if isinstance(accounts, dict) and accounts else []


def normalize_account(account: str | None) -> str | None:
    if account is None:
        return None
    clean = str(account).strip().upper()
    return None if clean in {"", "ALL"} else clean


def target_accounts(config: dict[str, Any], account: str | None = None) -> list[str | None]:
    names = account_names(config)
    normalized = normalize_account(account)
    if not names:
        return [None]
    if normalized is None:
        return names
    if normalized not in names:
        raise ValueError(f"Unknown paper-trading account: {account}. Available accounts: {', '.join(names)}")
    return [normalized]


def account_config(config: dict[str, Any], account: str | None = None) -> dict[str, Any]:
    names = account_names(config)
    if not names:
        return dict(config)
    normalized = normalize_account(account) or str(config.get("default_account", names[0])).upper()
    if normalized not in names:
        raise ValueError(f"Unknown paper-trading account: {account}. Available accounts: {', '.join(names)}")
    merged = dict(config)
    merged.pop("accounts", None)
    merged.update(config["accounts"][normalized])
    merged["account"] = normalized
    return merged


def empty_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": config["account_id"],
        "account": config.get("account"),
        "market_scope": config.get("market_scope"),
        "mode": "paper_trading",
        "created_at": now_iso(),
        "created_at_utc": utc_now_iso(),
        "last_updated": now_iso(),
        "last_updated_utc": utc_now_iso(),
        "base_unit": config["base_unit"],
        "base_currency": config.get("base_currency"),
        "fx_rates_to_base": dict(config.get("fx_rates_to_base", {})),
        "currency_schema_version": 3,
        "initial_cash": float(config["initial_cash"]),
        "cash": float(config["initial_cash"]),
        "realized_pnl": 0.0,
        "positions": {},
        "last_prices": {},
        "notes": [
            "Virtual paper-trading ledger only. No real broker orders are placed."
        ],
    }


def migrate_legacy_state(base_config: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    if config.get("account") != "US":
        return None
    legacy_file = base_config.get("portfolio_file")
    if not legacy_file:
        return None
    legacy_path = ROOT / legacy_file
    if not legacy_path.exists() or legacy_path == resolve_path(config, "portfolio_file"):
        return None
    legacy_state = strict_json_loads(
        legacy_path.read_text(encoding="utf-8"),
        source=str(legacy_path),
    )
    if not isinstance(legacy_state, dict):
        raise ValueError(f"Legacy paper portfolio must be a JSON object: {legacy_path}")
    legacy_state["account_id"] = config["account_id"]
    legacy_state["account"] = config.get("account")
    legacy_state["market_scope"] = config.get("market_scope")
    legacy_state["base_unit"] = config["base_unit"]
    legacy_state["base_currency"] = config.get("base_currency")
    legacy_state["fx_rates_to_base"] = dict(config.get("fx_rates_to_base", {}))
    legacy_state["initial_cash"] = float(config["initial_cash"])
    legacy_state.setdefault("notes", []).append("Migrated from legacy single-account paper portfolio.")
    for position in legacy_state.get("positions", {}).values():
        market_type = position.get("market_type") or classify_market(
            str(position.get("symbol", "")),
            str(position.get("exchange", "")),
            base_config,
        )
        position["market_type"] = "US" if market_type == "GLOBAL" else market_type
        position["currency"] = position.get("currency") or rules_for_market(position["market_type"], base_config).get("currency")
    for item in legacy_state.get("last_prices", {}).values():
        market_type = item.get("market_type") or classify_market(
            str(item.get("symbol", "")),
            str(item.get("exchange", "")),
            base_config,
        )
        item["market_type"] = "US" if market_type == "GLOBAL" else market_type
        item["currency"] = item.get("currency") or rules_for_market(item["market_type"], base_config).get("currency")
    return legacy_state


def migrate_legacy_trades(base_config: dict[str, Any], config: dict[str, Any]) -> str | None:
    if config.get("account") != "US":
        return None
    legacy_file = base_config.get("trades_file")
    if not legacy_file:
        return None
    legacy_path = ROOT / legacy_file
    if not legacy_path.exists() or legacy_path == resolve_path(config, "trades_file"):
        return None
    migrated_lines = []
    for record in read_jsonl(legacy_path):
        record["account"] = config.get("account")
        record["account_id"] = config["account_id"]
        record["market_scope"] = config.get("market_scope")
        market_type = record.get("market_type") or classify_market(
            str(record.get("symbol", "")),
            str(record.get("exchange", "")),
            base_config,
        )
        record["market_type"] = "US" if market_type == "GLOBAL" else market_type
        record["currency"] = record.get("currency") or rules_for_market(record["market_type"], base_config).get("currency")
        migrated_lines.append(
            json.dumps(
                record,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return "\n".join(migrated_lines) + ("\n" if migrated_lines else "")


def ensure_account_files_unlocked(
    base_config: dict[str, Any],
    config: dict[str, Any],
    *,
    reset: bool,
) -> None:
    portfolio_path = resolve_path(config, "portfolio_file")
    trades_path = resolve_path(config, "trades_file")
    valuations_path = resolve_path(config, "valuations_file")
    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    valuations_path.parent.mkdir(parents=True, exist_ok=True)

    if reset or not portfolio_path.exists():
        state = None if reset else migrate_legacy_state(base_config, config)
        if state is None:
            state = empty_state(config)
        atomic_write_json(portfolio_path, state)
    if reset or not trades_path.exists():
        trades = None if reset else migrate_legacy_trades(base_config, config)
        atomic_write_text(trades_path, trades or "")
    if reset or not valuations_path.exists():
        atomic_write_text(valuations_path, "")


def all_account_configs(base_config: dict[str, Any]) -> list[dict[str, Any]]:
    configs = [
        account_config(base_config, account_name)
        for account_name in target_accounts(base_config, "ALL")
    ]
    return sorted(
        configs,
        key=lambda config: (
            str(lock_path(config).resolve()).casefold(),
            str(config.get("account_id") or ""),
        ),
    )


def ensure_files(reset: bool = False, account: str | None = None) -> None:
    """Create/reset account files while holding every account lock in canonical order."""
    base_config = load_config()
    selected_accounts = target_accounts(base_config, account)
    selected_ids = {
        str(account_config(base_config, account_name).get("account_id"))
        for account_name in selected_accounts
    }
    configs = all_account_configs(base_config)
    with account_locks(configs):
        recover_transaction_batches(configs, valuation=False)
        recover_transaction_batches(configs, valuation=True)
        for config in configs:
            if str(config.get("account_id")) in selected_ids:
                ensure_account_files_unlocked(base_config, config, reset=reset)


def load_state_unlocked(config: dict[str, Any]) -> dict[str, Any]:
    portfolio_path = resolve_path(config, "portfolio_file")
    state = strict_json_loads(
        portfolio_path.read_text(encoding="utf-8"),
        source=str(portfolio_path),
    )
    if not isinstance(state, dict):
        raise ValueError(f"Paper portfolio must be a JSON object: {portfolio_path}")
    return state


def load_account_snapshots(
    account: str | None = None,
) -> list[tuple[str | None, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]]:
    base_config = load_config()
    selected = target_accounts(base_config, account)
    ensure_files(account=account)
    configs = all_account_configs(base_config)
    with account_locks(configs):
        recover_transaction_batches(configs, valuation=False)
        recover_transaction_batches(configs, valuation=True)
        snapshots = []
        for account_name in selected:
            config = account_config(base_config, account_name)
            snapshots.append(
                (
                    account_name,
                    load_state_unlocked(config),
                    read_jsonl(resolve_path(config, "trades_file")),
                    read_jsonl(resolve_path(config, "valuations_file")),
                )
            )
        return snapshots


def load_account_snapshot(
    account: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    snapshots = load_account_snapshots(account)
    if len(snapshots) != 1:
        raise ValueError("load_account_snapshot requires a single paper-trading account.")
    _account_name, state, trades, valuations = snapshots[0]
    return state, trades, valuations


def load_state(account: str | None = None) -> dict[str, Any]:
    state, _trades, _valuations = load_account_snapshot(account)
    return state


def save_state(state: dict[str, Any], account: str | None = None) -> None:
    base_config = load_config()
    selected = normalize_account(account) or normalize_account(state.get("account"))
    config = account_config(base_config, selected)
    configs = all_account_configs(base_config)
    with account_locks(configs):
        recover_transaction_batches(configs, valuation=False)
        recover_transaction_batches(configs, valuation=True)
        state["last_updated"] = now_iso()
        state["last_updated_utc"] = utc_now_iso()
        atomic_write_json(resolve_path(config, "portfolio_file"), state)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            row = strict_json_loads(line, source=str(path))
            if not isinstance(row, dict):
                raise ValueError(f"JSONL rows must be objects: {path}")
            records.append(row)
    return records


def transaction_path(config: dict[str, Any]) -> Path:
    return resolve_path(config, "trades_file").with_suffix(".transaction.json")


def valuation_transaction_path(config: dict[str, Any]) -> Path:
    return resolve_path(config, "valuations_file").with_suffix(".transaction.json")


def batch_coordinator_path(*, valuation: bool) -> Path:
    kind = "valuations" if valuation else "orders"
    return CONFIG_PATH.parent.parent / "data" / ".paper-transaction-control" / f"{kind}.batch.json"


def batch_commit_marker_path(*, valuation: bool) -> Path:
    kind = "valuations" if valuation else "orders"
    return CONFIG_PATH.parent.parent / "data" / ".paper-transaction-control" / f"{kind}.committed.json"


def lock_path(config: dict[str, Any]) -> Path:
    return resolve_path(config, "portfolio_file").with_suffix(".lock")


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_is_stale(path: Path, existing: dict[str, Any], *, maximum_age_seconds: int = 3600) -> bool:
    try:
        age = max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
    except OSError:
        return False
    pid = existing.get("pid")
    hostname = str(existing.get("hostname") or "").casefold()
    if hostname == socket.gethostname().casefold() and isinstance(pid, int):
        return not process_is_alive(pid)
    return age > maximum_age_seconds


@contextmanager
def account_lock(config: dict[str, Any]):
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(f"{os.getpid()}:{now_iso()}:{config.get('account_id')}".encode("utf-8")).hexdigest()
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": now_iso(),
        "created_at_utc": utc_now_iso(),
        "token": token,
    }
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            try:
                existing = strict_json_loads(
                    path.read_text(encoding="utf-8"),
                    source=str(path),
                )
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, ValueError):
                existing = {}
            stale = lock_is_stale(path, existing)
            if stale:
                path.unlink(missing_ok=True)
                continue
            raise RuntimeError(f"Paper-trading account is locked: {config.get('account_id')}") from exc
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    payload,
                    handle,
                    allow_nan=False,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            break
    else:
        raise RuntimeError(f"Unable to acquire paper-trading lock: {path}")
    try:
        yield
    finally:
        try:
            existing = strict_json_loads(
                path.read_text(encoding="utf-8"),
                source=str(path),
            )
        except (OSError, ValueError):
            existing = {}
        if existing.get("token") == token:
            path.unlink(missing_ok=True)


@contextmanager
def account_locks(configs: list[dict[str, Any]]):
    """Acquire a set of account locks in one stable, process-independent order."""
    ordered = sorted(
        configs,
        key=lambda config: (
            str(lock_path(config).resolve()).casefold(),
            str(config.get("account_id") or ""),
        ),
    )
    lock_keys = [str(lock_path(config).resolve()).casefold() for config in ordered]
    if len(lock_keys) != len(set(lock_keys)):
        raise ValueError("Paper-trading accounts must use distinct portfolio lock paths.")
    with ExitStack() as stack:
        for config in ordered:
            stack.enter_context(account_lock(config))
        yield


def load_transaction_journal(journal_path: Path, label: str) -> dict[str, Any]:
    journal = strict_json_loads(
        journal_path.read_text(encoding="utf-8"),
        source=str(journal_path),
    )
    if not isinstance(journal, dict):
        raise ValueError(f"Invalid {label} transaction journal: {journal_path}")
    state = journal.get("state")
    ledger_text = journal.get("ledger_text")
    if not isinstance(state, dict) or not isinstance(ledger_text, str):
        raise ValueError(f"Invalid {label} transaction journal: {journal_path}")
    state_sha256 = str(journal.get("state_sha256") or "")
    ledger_sha256 = str(journal.get("ledger_sha256") or "")
    if int(journal.get("schema_version", 1)) >= 3 and (not state_sha256 or not ledger_sha256):
        raise ValueError(f"{label} transaction journal requires target checksums: {journal_path}")
    if state_sha256 and state_sha256 != hashlib.sha256(stable_json(state).encode("utf-8")).hexdigest():
        raise ValueError(f"{label} transaction state checksum mismatch: {journal_path}")
    if ledger_sha256 and ledger_sha256 != hashlib.sha256(ledger_text.encode("utf-8")).hexdigest():
        raise ValueError(f"{label} transaction ledger checksum mismatch: {journal_path}")
    return journal


def apply_transaction_journal(
    config: dict[str, Any],
    journal: dict[str, Any],
    *,
    ledger_key: str,
) -> None:
    state = journal["state"]
    ledger_text = journal["ledger_text"]
    atomic_write_text(resolve_path(config, ledger_key), ledger_text)
    atomic_write_json(resolve_path(config, "portfolio_file"), state)


def recover_transaction(config: dict[str, Any]) -> None:
    del config  # Recovery is global because a journal can belong to a multi-account generation.
    base_config = load_config()
    configs = all_account_configs(base_config)
    with account_locks(configs):
        recover_transaction_batches(configs, valuation=False)


def recover_valuation_transaction(config: dict[str, Any]) -> None:
    del config  # Recovery is global because a journal can belong to a multi-account generation.
    base_config = load_config()
    configs = all_account_configs(base_config)
    with account_locks(configs):
        recover_transaction_batches(configs, valuation=True)


def transaction_journal_path(config: dict[str, Any], *, valuation: bool) -> Path:
    return valuation_transaction_path(config) if valuation else transaction_path(config)


def transaction_ledger_key(*, valuation: bool) -> str:
    return "valuations_file" if valuation else "trades_file"


def target_file_checksum(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cleanup_atomic_temps(path: Path) -> None:
    for temporary in path.parent.glob(f".{path.name}.*.tmp"):
        if temporary.is_file():
            temporary.unlink(missing_ok=True)


def load_batch_coordinator(path: Path, *, valuation: bool) -> dict[str, Any]:
    label = "paper-valuation" if valuation else "paper-trading"
    coordinator = strict_json_loads(path.read_text(encoding="utf-8"), source=str(path))
    if not isinstance(coordinator, dict) or int(coordinator.get("schema_version", 0)) != 1:
        raise ValueError(f"Invalid {label} batch coordinator: {path}")
    expected_kind = "valuations" if valuation else "orders"
    if coordinator.get("kind") != expected_kind:
        raise ValueError(f"Mismatched {label} batch coordinator kind: {path}")
    if coordinator.get("phase") not in {"preparing", "prepared", "committed"}:
        raise ValueError(f"Invalid {label} batch coordinator phase: {path}")
    batch_id = str(coordinator.get("batch_id") or "")
    entries = coordinator.get("entries")
    if not batch_id or not isinstance(entries, list) or not entries:
        raise ValueError(f"Invalid {label} batch coordinator metadata: {path}")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError(f"Invalid {label} batch coordinator entries: {path}")
    account_ids = [str(entry.get("account_id") or "") for entry in entries]
    if any(not value for value in account_ids) or len(account_ids) != len(set(account_ids)):
        raise ValueError(f"Duplicate or empty account IDs in {label} batch coordinator: {path}")
    if int(coordinator.get("batch_size", 0)) != len(entries):
        raise ValueError(f"Invalid {label} batch coordinator size: {path}")
    return coordinator


def coordinator_entry_matches_targets(
    config: dict[str, Any],
    entry: dict[str, Any],
    *,
    valuation: bool,
) -> bool:
    ledger_path = resolve_path(config, transaction_ledger_key(valuation=valuation))
    state_path = resolve_path(config, "portfolio_file")
    return (
        target_file_checksum(ledger_path) == str(entry.get("ledger_file_sha256") or "")
        and target_file_checksum(state_path) == str(entry.get("state_file_sha256") or "")
    )


def recover_coordinated_batch(
    configs: list[dict[str, Any]],
    *,
    valuation: bool,
) -> bool:
    coordinator_path = batch_coordinator_path(valuation=valuation)
    if not coordinator_path.exists():
        return False
    label = "paper-valuation" if valuation else "paper-trading"
    ledger_key = transaction_ledger_key(valuation=valuation)
    coordinator = load_batch_coordinator(coordinator_path, valuation=valuation)
    batch_id = str(coordinator["batch_id"])
    phase = str(coordinator["phase"])
    configs_by_id = {str(config.get("account_id")): config for config in configs}
    entries = coordinator["entries"]
    expected_ids = {str(entry["account_id"]) for entry in entries}
    missing_configs = sorted(expected_ids - set(configs_by_id))
    if missing_configs:
        raise ValueError(
            f"Cannot recover {label} batch {batch_id}; configured accounts are missing: "
            + ", ".join(missing_configs)
        )

    journals: list[tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        config = configs_by_id[str(entry["account_id"])]
        path = transaction_journal_path(config, valuation=valuation)
        if path.exists():
            journal = load_transaction_journal(path, label)
            if str(journal.get("batch_id") or "") != batch_id:
                raise ValueError(f"{label} journal generation conflicts with coordinator {batch_id}: {path}")
            if str(journal.get("account_id") or "") != str(config.get("account_id") or ""):
                raise ValueError(f"{label} journal account conflicts with coordinator {batch_id}: {path}")
            if str(journal.get("state_sha256") or "") != str(entry.get("state_payload_sha256") or ""):
                raise ValueError(f"{label} journal state checksum conflicts with coordinator {batch_id}: {path}")
            if str(journal.get("ledger_sha256") or "") != str(entry.get("ledger_payload_sha256") or ""):
                raise ValueError(f"{label} journal ledger checksum conflicts with coordinator {batch_id}: {path}")
            journals.append((config, path, journal, entry))
        elif phase == "prepared":
            raise ValueError(f"Incomplete {label} committed-intent batch {batch_id}; missing journal {path}.")

    if phase == "preparing":
        # No commit decision was made. A crash while staging is an abort, never a partial publish.
        for _config, path, _journal, _entry in journals:
            path.unlink(missing_ok=True)
        for entry in entries:
            cleanup_atomic_temps(
                transaction_journal_path(
                    configs_by_id[str(entry["account_id"])],
                    valuation=valuation,
                )
            )
        coordinator_path.unlink(missing_ok=True)
        return True

    if phase == "prepared":
        if len(journals) != len(entries):
            raise ValueError(f"Incomplete {label} committed-intent batch {batch_id}.")
        for config, _path, journal, _entry in journals:
            apply_transaction_journal(config, journal, ledger_key=ledger_key)
        commit_marker = {
            **coordinator,
            "phase": "committed",
            "committed_at": now_iso(),
            "committed_at_utc": utc_now_iso(),
        }
        atomic_write_json(batch_commit_marker_path(valuation=valuation), commit_marker)
        atomic_write_json(coordinator_path, commit_marker)
        coordinator = commit_marker
        phase = "committed"

    if phase == "committed":
        journals_by_id = {
            str(config.get("account_id")): (config, path, journal)
            for config, path, journal, _entry in journals
        }
        for entry in entries:
            config = configs_by_id[str(entry["account_id"])]
            if coordinator_entry_matches_targets(config, entry, valuation=valuation):
                continue
            staged = journals_by_id.get(str(entry["account_id"]))
            if staged is None:
                raise ValueError(
                    f"Committed {label} batch {batch_id} target checksum mismatch without recovery journal."
                )
            apply_transaction_journal(config, staged[2], ledger_key=ledger_key)
            if not coordinator_entry_matches_targets(config, entry, valuation=valuation):
                raise ValueError(f"Unable to restore committed {label} batch {batch_id} targets.")
        marker_path = batch_commit_marker_path(valuation=valuation)
        if marker_path.exists():
            marker = load_batch_coordinator(marker_path, valuation=valuation)
            if (
                marker.get("phase") != "committed"
                or marker.get("batch_id") != batch_id
                or stable_json(marker.get("entries")) != stable_json(entries)
            ):
                raise ValueError(f"{label} durable commit marker conflicts with batch {batch_id}.")
        else:
            atomic_write_json(marker_path, coordinator)
        for _config, path, _journal, _entry in journals:
            path.unlink(missing_ok=True)
            cleanup_atomic_temps(path)
        coordinator_path.unlink(missing_ok=True)
    return True


def recover_transaction_batches(
    configs: list[dict[str, Any]],
    *,
    valuation: bool,
) -> None:
    if recover_coordinated_batch(configs, valuation=valuation):
        return

    # Backward-compatible recovery for schema-v1/v2 journals created before the
    # durable coordinator existed. These are accepted only when the whole batch is present.
    label = "paper-valuation" if valuation else "paper-trading"
    ledger_key = transaction_ledger_key(valuation=valuation)
    pending: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    for config in configs:
        path = transaction_journal_path(config, valuation=valuation)
        if path.exists():
            pending.append((config, path, load_transaction_journal(path, label)))
    if not pending:
        return

    groups: dict[str, list[tuple[dict[str, Any], Path, dict[str, Any]]]] = {}
    for config, path, journal in pending:
        batch_id = str(journal.get("batch_id") or f"legacy:{config.get('account_id')}:{path}")
        groups.setdefault(batch_id, []).append((config, path, journal))
    for batch_id, entries in groups.items():
        first = entries[0][2]
        expected_size = int(first.get("batch_size", len(entries)))
        expected_accounts = {
            str(value) for value in first.get("batch_account_ids", []) if value not in (None, "")
        }
        actual_accounts = {str(config.get("account_id")) for config, _path, _journal in entries}
        if len(entries) != expected_size or (expected_accounts and actual_accounts != expected_accounts):
            raise ValueError(
                f"Incomplete legacy {label} transaction batch {batch_id}: "
                f"found {len(entries)} of {expected_size} journals."
            )
        if any(str(journal.get("batch_id") or batch_id) != batch_id for _config, _path, journal in entries):
            raise ValueError(f"Inconsistent legacy {label} transaction batch metadata: {batch_id}")
    for entries in groups.values():
        for config, _path, journal in entries:
            apply_transaction_journal(config, journal, ledger_key=ledger_key)
        for _config, path, _journal in entries:
            path.unlink(missing_ok=True)


def transaction_journal(
    config: dict[str, Any],
    state: dict[str, Any],
    ledger_rows: list[dict[str, Any]],
    *,
    batch_id: str,
    batch_account_ids: list[str],
    kind: str,
) -> dict[str, Any]:
    state["last_updated"] = now_iso()
    state["last_updated_utc"] = utc_now_iso()
    state.setdefault("transaction_generations", {})[kind] = batch_id
    ledger_text = "".join(
        json.dumps(
            row,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
        for row in ledger_rows
    )
    journal: dict[str, Any] = {
        "schema_version": 3,
        "kind": kind,
        "batch_id": batch_id,
        "batch_size": len(batch_account_ids),
        "batch_account_ids": batch_account_ids,
        "account": config.get("account"),
        "account_id": config.get("account_id"),
        "prepared_at": now_iso(),
        "prepared_at_utc": utc_now_iso(),
        "state": state,
        "ledger_text": ledger_text,
        "state_sha256": hashlib.sha256(stable_json(state).encode("utf-8")).hexdigest(),
        "ledger_sha256": hashlib.sha256(ledger_text.encode("utf-8")).hexdigest(),
    }
    stable_json(journal)
    return journal


def commit_prepared_transactions(
    prepared: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
    *,
    valuation: bool,
) -> None:
    if not prepared:
        return
    kind = "valuations" if valuation else "orders"
    batch_account_ids = [str(config.get("account_id")) for config, _state, _rows in prepared]
    if len(batch_account_ids) != len(set(batch_account_ids)):
        raise ValueError(f"Duplicate accounts in paper {kind} transaction batch.")
    batch_id = hashlib.sha256(
        stable_json(
            {
                "kind": kind,
                "pid": os.getpid(),
                "prepared_at": now_iso(),
                "accounts": batch_account_ids,
            }
        ).encode("utf-8")
    ).hexdigest()
    journals = [
        (
            config,
            transaction_journal_path(config, valuation=valuation),
            transaction_journal(
                config,
                state,
                ledger_rows,
                batch_id=batch_id,
                batch_account_ids=batch_account_ids,
                kind=kind,
            ),
        )
        for config, state, ledger_rows in prepared
    ]
    entries = []
    for config, _path, journal in journals:
        state_file_text = json.dumps(
            journal["state"],
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        entries.append(
            {
                "account": config.get("account"),
                "account_id": config.get("account_id"),
                "state_payload_sha256": journal["state_sha256"],
                "ledger_payload_sha256": journal["ledger_sha256"],
                "state_file_sha256": hashlib.sha256(state_file_text.encode("utf-8")).hexdigest(),
                "ledger_file_sha256": journal["ledger_sha256"],
            }
        )
    coordinator = {
        "schema_version": 1,
        "kind": kind,
        "phase": "preparing",
        "batch_id": batch_id,
        "batch_size": len(entries),
        "batch_account_ids": batch_account_ids,
        "prepared_at": now_iso(),
        "prepared_at_utc": utc_now_iso(),
        "entries": entries,
    }
    coordinator_path = batch_coordinator_path(valuation=valuation)
    if coordinator_path.exists():
        raise RuntimeError(f"Pending paper {kind} coordinator must be recovered before a new commit: {coordinator_path}")
    atomic_write_json(coordinator_path, coordinator)
    created: list[Path] = []
    try:
        for _config, path, journal in journals:
            atomic_write_json(path, journal)
            created.append(path)
        coordinator["phase"] = "prepared"
        coordinator["prepared_complete_at"] = now_iso()
        coordinator["prepared_complete_at_utc"] = utc_now_iso()
        atomic_write_json(coordinator_path, coordinator)
    except Exception:
        # The durable decision is phase=prepared. Before that point the intent is abortable.
        for path in created:
            path.unlink(missing_ok=True)
        coordinator_path.unlink(missing_ok=True)
        raise
    recover_transaction_batches(
        [config for config, _path, _journal in journals],
        valuation=valuation,
    )


def commit_account_transactions(
    prepared: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
) -> None:
    commit_prepared_transactions(prepared, valuation=False)


def commit_account_transaction(config: dict[str, Any], state: dict[str, Any], ledger_rows: list[dict[str, Any]]) -> None:
    commit_account_transactions([(config, state, ledger_rows)])


def commit_valuation_transactions(
    prepared: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
) -> None:
    commit_prepared_transactions(prepared, valuation=True)


def commit_valuation_transaction(config: dict[str, Any], state: dict[str, Any], ledger_rows: list[dict[str, Any]]) -> None:
    commit_valuation_transactions([(config, state, ledger_rows)])


def order_idempotency_key(config: dict[str, Any], order: dict[str, Any], date: str) -> str:
    explicit = str(order.get("order_id") or order.get("idempotency_key") or "").strip()
    if explicit:
        return explicit
    identity = {
        "account_id": config.get("account_id"),
        "date": date,
        "order": {
            key: value
            for key, value in order.items()
            if key not in {"timestamp", "order_id", "idempotency_key", "account", "paper_account"}
        },
    }
    return f"PAPER-{hashlib.sha256(stable_json(identity).encode('utf-8')).hexdigest()[:24].upper()}"


def order_decision_date(order: dict[str, Any], run_date: str) -> str:
    canonical_run_date = strict_iso_date(run_date, "Run date")
    raw_order_date = order.get("date")
    if raw_order_date in (None, ""):
        return canonical_run_date
    canonical_order_date = strict_iso_date(raw_order_date, "Order date")
    if canonical_order_date != canonical_run_date:
        raise ValueError(
            f"Order date {canonical_order_date} must equal run date {canonical_run_date}; backdated orders are not allowed."
        )
    return canonical_order_date


def prediction_reference_required(config: dict[str, Any], decision_date: str) -> bool:
    contract = config.get("order_contract", {}) if isinstance(config.get("order_contract"), dict) else {}
    required_from = str(contract.get("prediction_reference_required_from_date") or "9999-12-31")[:10]
    return decision_date >= required_from


def load_original_prediction_dates(config: dict[str, Any]) -> dict[str, list[str]]:
    ledger_path = resolve_path(config, "prediction_ledger_file")
    if not ledger_path.exists():
        raise ValueError(f"Prediction ledger required for new paper orders does not exist: {ledger_path}")

    originals: dict[str, list[str]] = {}
    for index, row in enumerate(read_jsonl(ledger_path), start=1):
        if isinstance(row.get("review"), dict):
            continue
        prediction_id = str(row.get("prediction_id") or "").strip()
        if not prediction_id:
            continue
        record_date = strict_iso_date(
            row.get("date"),
            f"Prediction ledger row {index} date for {prediction_id}",
        )
        if prediction_id in originals:
            raise ValueError(
                "Prediction ledger contains duplicate original prediction_id "
                f"{prediction_id}; new paper orders fail closed."
            )
        originals.setdefault(prediction_id, []).append(record_date)
    return originals


def required_prediction_id(order: dict[str, Any], decision_date: str) -> str:
    prediction_id_raw = order.get("prediction_id")
    if not isinstance(prediction_id_raw, str) or not prediction_id_raw.strip():
        raise ValueError(
            f"New paper order on {decision_date} requires prediction_id under the prediction-reference contract."
        )
    if prediction_id_raw != prediction_id_raw.strip():
        raise ValueError("New paper order prediction_id must not contain leading or trailing whitespace.")
    return prediction_id_raw.strip()


def validate_prediction_reference(
    prediction_id: str,
    decision_date: str,
    original_dates: dict[str, list[str]],
) -> None:
    dates = original_dates.get(prediction_id, [])
    if not dates:
        raise ValueError(
            f"New paper order references prediction_id {prediction_id}, but no original prediction record exists."
        )
    if not any(record_date <= decision_date for record_date in dates):
        earliest = min(dates)
        raise ValueError(
            f"New paper order references prediction_id {prediction_id} dated {earliest}, "
            f"which follows order date {decision_date}."
        )


def position_key(symbol: str, exchange: str | None = None) -> str:
    symbol_clean = symbol.strip().upper()
    exchange_clean = (exchange or "").strip().upper()
    return f"{exchange_clean}:{symbol_clean}" if exchange_clean else symbol_clean


def classify_market(symbol: str, exchange: str | None, config: dict[str, Any]) -> str:
    symbol_clean = symbol.strip().upper()
    exchange_clean = (exchange or "").strip().upper()
    if symbol_clean.endswith((".SH", ".SZ", ".BJ")):
        return "A_SHARE"
    if symbol_clean.endswith(".HK"):
        return "HK"
    for market, rules in config.get("market_rules", {}).items():
        if exchange_clean in {str(value).upper() for value in rules.get("exchanges", [])}:
            return market
    if exchange_clean in {"NASDAQ", "NYSE", "NYSE ARCA", "AMEX", "ARCA", "US", "USA"}:
        return "US"
    return "GLOBAL"


def account_for_market_type(market_type: str, config: dict[str, Any]) -> str | None:
    names = account_names(config)
    if not names:
        return None
    clean_market = market_type.upper()
    if clean_market == "GLOBAL":
        clean_market = str(config.get("default_account", "US")).upper()
    for account_name in names:
        allowed = {
            str(value).upper()
            for value in config["accounts"][account_name].get("allowed_market_types", [])
        }
        if clean_market in allowed:
            return account_name
    raise ValueError(f"No paper-trading account allows market type {market_type}.")


def infer_order_account(config: dict[str, Any], order: dict[str, Any], requested_account: str | None = None) -> str | None:
    forced = normalize_account(requested_account) or normalize_account(order.get("account") or order.get("paper_account"))
    if forced:
        if forced not in account_names(config):
            raise ValueError(f"Unknown paper-trading account: {forced}.")
        return forced
    symbol = str(order.get("symbol", "")).strip().upper()
    exchange = str(order.get("exchange", order.get("market", ""))).strip().upper()
    market_type = str(order.get("market_type") or classify_market(symbol, exchange, config)).upper()
    return account_for_market_type(market_type, config)


def validate_account_allows_market(account: str | None, market_type: str, config: dict[str, Any]) -> None:
    if account is None:
        return
    allowed = {
        str(value).upper()
        for value in config.get("allowed_market_types", [])
    }
    if not allowed:
        return
    clean_market = market_type.upper()
    if clean_market == "GLOBAL" and account == "US":
        return
    if allowed and clean_market not in allowed:
        raise ValueError(f"Account {account} does not allow market type {market_type}.")


def rules_for_market(market_type: str, config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("market_rules", {}).get(market_type, {}))


def fx_rate_to_base(currency: str | None, config: dict[str, Any], explicit: Any = None) -> float:
    if explicit not in (None, ""):
        rate = finite_float(explicit, "fx_to_base")
    else:
        clean = str(currency or config.get("base_currency") or "").upper()
        configured_rates = config.get("fx_rates_to_base", {})
        if not config.get("base_currency") and not configured_rates:
            return 1.0
        rate = finite_float(
            configured_rates.get(
                clean,
                1.0 if clean == str(config.get("base_currency") or "").upper() else 0.0,
            ),
            f"fx_to_base[{clean}]",
        )
    if rate <= 0:
        raise ValueError(f"Missing positive fx_to_base conversion for currency {currency!r} in account {config.get('account')}.")
    return rate


def instrument_currency(
    config: dict[str, Any],
    market_type: str,
    explicit: Any = None,
    *,
    field: str,
) -> str:
    rules = rules_for_market(market_type, config)
    expected = str(rules.get("currency") or config.get("base_currency") or "").strip().upper()
    supplied = str(explicit or "").strip().upper()
    if supplied and expected and supplied != expected:
        raise ValueError(f"{field} currency {supplied} conflicts with {market_type} instrument currency {expected}.")
    return supplied or expected


def require_matching_currency(expected: str, actual: Any, *, field: str) -> None:
    supplied = str(actual or "").strip().upper()
    if supplied and expected and supplied != expected:
        raise ValueError(f"{field} currency {supplied} conflicts with instrument currency {expected}.")


def state_fx_rate(state: dict[str, Any], position: dict[str, Any], price_item: dict[str, Any] | None = None) -> float:
    price_item = price_item or {}
    position_currency = str(position.get("currency") or "").strip().upper()
    price_currency = str(price_item.get("currency") or "").strip().upper()
    if position_currency and price_currency and position_currency != price_currency:
        raise ValueError(
            f"Mark currency {price_currency} conflicts with position currency {position_currency}."
        )
    currency = price_currency or position_currency or str(state.get("base_currency") or "").upper()
    explicit = price_item.get("fx_to_base", position.get("fx_to_base"))
    if explicit not in (None, ""):
        return positive_float(explicit, f"fx_to_base for {currency or 'position'}")
    rates = state.get("fx_rates_to_base", {})
    if not state.get("base_currency") and not rates:
        return 1.0
    rate = finite_float(
        rates.get(
            currency,
            1.0 if currency == str(state.get("base_currency") or "").upper() else 0.0,
        ),
        f"fx_to_base for {currency or 'position'}",
    )
    if rate <= 0:
        raise ValueError(f"Missing positive fx_to_base conversion for position currency {currency!r}.")
    return rate


def position_native_cost_basis(position: dict[str, Any]) -> float:
    quantity = non_negative_float(position.get("quantity", 0.0), "position quantity")
    fallback = quantity * non_negative_float(position.get("avg_cost", 0.0), "position avg_cost")
    return non_negative_float(position.get("cost_basis", fallback), "position cost_basis")


def position_entry_fx_rate(state: dict[str, Any], position: dict[str, Any]) -> float:
    explicit = position.get("entry_fx_to_base", position.get("fx_to_base"))
    if explicit not in (None, ""):
        return positive_float(explicit, "position entry_fx_to_base")
    currency = str(position.get("currency") or state.get("base_currency") or "").upper()
    rates = state.get("fx_rates_to_base", {})
    if not state.get("base_currency") and not rates:
        return 1.0
    rate = rates.get(
        currency,
        1.0 if currency == str(state.get("base_currency") or "").upper() else 0.0,
    )
    return positive_float(rate, f"position entry fx_to_base for {currency}")


def position_base_cost_basis(state: dict[str, Any], position: dict[str, Any]) -> float:
    explicit = position.get("cost_basis_base")
    if explicit not in (None, ""):
        return non_negative_float(explicit, "position cost_basis_base")
    return position_native_cost_basis(position) * position_entry_fx_rate(state, position)


def reconstructed_open_cost_bases(
    trades: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, dict[str, float]]:
    reconstructed: dict[str, dict[str, float]] = {}
    for trade in trades:
        action = str(trade.get("action") or "").upper()
        if action not in {"BUY", "SELL"}:
            continue
        symbol = str(trade.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        key = position_key(symbol, str(trade.get("exchange") or ""))
        quantity = positive_float(trade.get("quantity"), f"trade {key} quantity")
        market_type = str(trade.get("market_type") or classify_market(symbol, str(trade.get("exchange") or ""), config)).upper()
        currency = instrument_currency(
            config,
            market_type,
            trade.get("currency"),
            field=f"historical trade {key}",
        )
        rate = fx_rate_to_base(currency, config, trade.get("fx_to_base"))
        gross_native = non_negative_float(trade.get("gross_value", 0.0), f"trade {key} gross_value")
        fee_native = non_negative_float(trade.get("fee", 0.0), f"trade {key} fee")
        gross_base = non_negative_float(
            trade.get("gross_value_base", gross_native * rate),
            f"trade {key} gross_value_base",
        )
        fee_base = non_negative_float(
            trade.get("fee_base", fee_native * rate),
            f"trade {key} fee_base",
        )
        bucket = reconstructed.setdefault(
            key,
            {"quantity": 0.0, "cost_basis": 0.0, "cost_basis_base": 0.0},
        )
        if action == "BUY":
            bucket["quantity"] += quantity
            bucket["cost_basis"] += gross_native + fee_native
            bucket["cost_basis_base"] += gross_base + fee_base
            continue
        held = bucket["quantity"]
        if held <= 0:
            continue
        sold = min(quantity, held)
        remaining_ratio = max(0.0, (held - sold) / held)
        bucket["quantity"] = held - sold
        bucket["cost_basis"] *= remaining_ratio
        bucket["cost_basis_base"] *= remaining_ratio
    return reconstructed


def reconcile_currency_state(config: dict[str, Any], state: dict[str, Any], trades: list[dict[str, Any]]) -> bool:
    """Migrate cash and open-position cost bases into the account base currency."""
    schema_version = int(state.get("currency_schema_version", 0) or 0)
    changed = False
    state["base_currency"] = config.get("base_currency")
    state["fx_rates_to_base"] = dict(config.get("fx_rates_to_base", {}))
    if schema_version < 2:
        cash = finite_float(
            state.get("initial_cash", config.get("initial_cash", 0.0)),
            "initial_cash",
        )
        realized_base = 0.0
        for trade in trades:
            action = str(trade.get("action") or "").upper()
            if action not in {"BUY", "SELL"}:
                continue
            symbol = str(trade.get("symbol") or "").strip().upper()
            market_type = str(
                trade.get("market_type")
                or classify_market(symbol, str(trade.get("exchange") or ""), config)
            ).upper()
            currency = instrument_currency(
                config,
                market_type,
                trade.get("currency"),
                field=f"historical trade {position_key(symbol, str(trade.get('exchange') or ''))}",
            )
            rate = fx_rate_to_base(currency, config, trade.get("fx_to_base"))
            gross_native = non_negative_float(trade.get("gross_value", 0.0), "trade gross_value")
            fee_native = non_negative_float(trade.get("fee", 0.0), "trade fee")
            gross_base = non_negative_float(
                trade.get("gross_value_base", gross_native * rate),
                "trade gross_value_base",
            )
            fee_base = non_negative_float(
                trade.get("fee_base", fee_native * rate),
                "trade fee_base",
            )
            cash += gross_base - fee_base if action == "SELL" else -(gross_base + fee_base)
            realized_native = finite_float(trade.get("realized_pnl", 0.0), "trade realized_pnl")
            realized_base += finite_float(
                trade.get("realized_pnl_base", realized_native * rate),
                "trade realized_pnl_base",
            )
        state["cash"] = cash
        state["realized_pnl"] = realized_base
        state.setdefault("notes", []).append("Migrated account values to explicit base-currency conversion schema v2.")
        changed = True

    reconstructed = reconstructed_open_cost_bases(trades, config) if schema_version < 3 else {}
    for key, position in state.get("positions", {}).items():
        if not isinstance(position, dict):
            raise ValueError(f"Position {key} must be an object.")
        price_item = state.get("last_prices", {}).get(key, {})
        position_market_type = str(
            position.get("market_type")
            or classify_market(
                str(position.get("symbol") or ""),
                str(position.get("exchange") or ""),
                config,
            )
        ).upper()
        expected_currency = instrument_currency(
            config,
            position_market_type,
            position.get("currency"),
            field=f"position {key}",
        )
        if expected_currency and not position.get("currency"):
            position["currency"] = expected_currency
            changed = True
        if isinstance(price_item, dict):
            require_matching_currency(
                expected_currency,
                price_item.get("currency"),
                field=f"last price {key}",
            )
            if expected_currency and not price_item.get("currency"):
                price_item["currency"] = expected_currency
                changed = True
        current_rate = fx_rate_to_base(
            expected_currency,
            config,
            price_item.get("fx_to_base"),
        )
        if isinstance(price_item, dict):
            price_item["fx_to_base"] = current_rate

        if schema_version < 3:
            quantity = non_negative_float(position.get("quantity", 0.0), f"position {key} quantity")
            rebuilt = reconstructed.get(key)
            if rebuilt and abs(rebuilt["quantity"] - quantity) <= max(1e-7, quantity * 1e-7):
                native_basis = rebuilt["cost_basis"]
                base_basis = rebuilt["cost_basis_base"]
                position["cost_basis"] = native_basis
                position["avg_cost"] = native_basis / quantity if quantity else 0.0
            else:
                native_basis = position_native_cost_basis(position)
                entry_rate = position_entry_fx_rate(state, position)
                base_basis = native_basis * entry_rate
            position["cost_basis_base"] = base_basis
            blended_rate = base_basis / native_basis if native_basis > 0 else position_entry_fx_rate(state, position)
            position["entry_fx_to_base"] = blended_rate
            position["fx_to_base"] = blended_rate
            changed = True

    if schema_version < 3:
        state["currency_schema_version"] = 3
        state.setdefault("notes", []).append(
            "Migrated open positions to explicit base-currency cost-basis schema v3."
        )
        changed = True
    return changed


def calculate_fee(
    action: str,
    gross: float,
    market_rules: dict[str, Any],
    default_fee_rate: float = 0.0,
) -> tuple[float, dict[str, float]]:
    """Apply the configured broker-style commission and sell-side tax model."""
    gross = non_negative_float(gross, "gross notional")
    commission_rate = non_negative_float(
        market_rules.get("commission_rate", default_fee_rate),
        "commission_rate",
    )
    min_commission = non_negative_float(
        market_rules.get("min_commission", 0.0),
        "min_commission",
    )
    stamp_tax_rate = (
        non_negative_float(
            market_rules.get("stamp_tax_sell_rate", 0.0),
            "stamp_tax_sell_rate",
        )
        if action == "SELL"
        else 0.0
    )
    commission = (
        non_negative_float(
            max(abs(gross) * commission_rate, min_commission),
            "commission",
        )
        if gross and commission_rate
        else 0.0
    )
    stamp_tax = non_negative_float(abs(gross) * stamp_tax_rate, "stamp_tax")
    breakdown = {"commission": commission, "stamp_tax": stamp_tax}
    return non_negative_float(commission + stamp_tax, "total fee"), breakdown


def floor_to_lot(quantity: float, lot_size: int) -> float:
    quantity = finite_float(quantity, "quantity")
    if lot_size <= 1:
        return float(int(quantity))
    return float(int(quantity // lot_size) * lot_size)


def apply_market_quantity_rules(
    action: str,
    quantity: float,
    quantity_raw: Any,
    market_type: str,
    rules: dict[str, Any],
) -> float:
    if market_type == "A_SHARE":
        if action == "BUY":
            quantity = floor_to_lot(quantity, int(rules.get("buy_lot_size", 100)))
        elif not (isinstance(quantity_raw, str) and quantity_raw.strip().upper() == "ALL"):
            quantity = floor_to_lot(quantity, int(rules.get("partial_sell_lot_size", 100)))
    elif rules.get("integer_quantity"):
        quantity = float(int(quantity))
    return quantity


def price_limit_for_symbol(symbol: str, order: dict[str, Any], rules: dict[str, Any]) -> float:
    explicit = order.get("price_limit_pct")
    if explicit is not None:
        limit = non_negative_float(explicit, "price_limit_pct")
        if limit > 1:
            raise ValueError("price_limit_pct must be at most 1.")
        return limit
    symbol_clean = symbol.strip().upper()
    name = str(order.get("name", "")).upper()
    if "ST" in name:
        return non_negative_float(
            rules.get("st_price_limit_pct", rules.get("default_price_limit_pct", 0.1)),
            "st_price_limit_pct",
        )
    if symbol_clean.startswith("688"):
        return non_negative_float(
            rules.get("star_market_price_limit_pct", rules.get("default_price_limit_pct", 0.1)),
            "star_market_price_limit_pct",
        )
    if symbol_clean.startswith("300"):
        return non_negative_float(
            rules.get("chinext_price_limit_pct", rules.get("default_price_limit_pct", 0.1)),
            "chinext_price_limit_pct",
        )
    return non_negative_float(
        rules.get("default_price_limit_pct", 0.1),
        "default_price_limit_pct",
    )


def validate_price_limit(symbol: str, price: float, order: dict[str, Any], market_type: str, rules: dict[str, Any]) -> None:
    if market_type != "A_SHARE":
        return
    previous_close = order.get("previous_close")
    if previous_close in (None, ""):
        return
    previous = finite_float(previous_close, "previous_close")
    if previous <= 0:
        raise ValueError("previous_close must be positive when supplied.")
    limit = price_limit_for_symbol(symbol, order, rules)
    lower = previous * (1 - limit)
    upper = previous * (1 + limit)
    if price < lower - 1e-9 or price > upper + 1e-9:
        raise ValueError(f"A-share price {price} breaches configured price limit range {lower:.2f}-{upper:.2f}.")


def priced_positions_value(state: dict[str, Any]) -> float:
    total = 0.0
    for key, position in state.get("positions", {}).items():
        quantity = non_negative_float(position.get("quantity", 0.0), f"position {key} quantity")
        if quantity <= 0:
            continue
        price_item = state.get("last_prices", {}).get(key, {})
        price = price_item.get("price")
        if price is None:
            price = position.get("avg_cost", 0.0)
        total += quantity * positive_float(price, f"position {key} price") * state_fx_rate(
            state,
            position,
            price_item,
        )
    return finite_float(total, "positions value")


def equity(state: dict[str, Any]) -> float:
    return finite_float(state.get("cash", 0.0), "cash") + priced_positions_value(state)


def theme_registry_for_config(
    config: dict[str, Any],
    *,
    date: str | None = None,
    require_recorded_revision: bool = False,
) -> dict[str, Any]:
    if not config.get("theme_registry_file"):
        return {"schema_version": 1, "allowed_themes": [], "entries": []}
    registry = load_theme_registry(config, ROOT, strict=True)
    if require_recorded_revision:
        if not date:
            raise ValueError("A decision date is required to verify the paper theme registry revision.")
        audit = audit_theme_registry_history(config, ROOT, date)
        if not audit.get("audit_passed") or not audit.get("history_current"):
            errors = [*audit.get("history_errors", []), *audit.get("snapshot_errors", [])]
            detail = "; ".join(errors) or "current registry revision is not audited"
            raise ValueError(f"BUY blocked by paper theme registry history gate: {detail}")
        registry["_revision_id"] = audit.get("current_revision_id")
        registry["_registry_sha256"] = audit.get("current_registry_sha256")
    return registry


def classified_theme_exposure(
    state: dict[str, Any],
    config: dict[str, Any],
    registry: dict[str, Any],
    date: str,
) -> tuple[dict[str, float], list[str]]:
    exposures: dict[str, float] = {}
    missing: list[str] = []
    account = config.get("account")
    for key, position in state.get("positions", {}).items():
        if not isinstance(position, dict) or float(position.get("quantity", 0.0)) <= 0:
            continue
        assignment = registry_theme_assignment(
            registry,
            account,
            position.get("symbol"),
            position.get("exchange"),
            date,
        )
        registered = str(assignment.get("primary_theme")) if assignment else None
        explicit = str(position.get("theme") or "").strip() or None
        if explicit and registered and explicit != registered:
            raise ValueError(f"Position {account}:{key} theme {explicit!r} conflicts with verified registry theme {registered!r}.")
        theme = explicit or registered
        if not theme:
            missing.append(f"{account}:{key}")
            continue
        price_item = state.get("last_prices", {}).get(key, {})
        price = float(price_item.get("price", position.get("avg_cost", 0.0)) or 0.0)
        value = float(position.get("quantity", 0.0)) * price * state_fx_rate(state, position, price_item)
        exposures[theme] = exposures.get(theme, 0.0) + value
    return exposures, missing


def turnover_for_date(
    date: str,
    trades_path: Path,
    pending_records: list[dict[str, Any]] | None = None,
) -> float:
    total = 0.0
    for record in [*read_jsonl(trades_path), *(pending_records or [])]:
        if record.get("date") == date and record.get("action") in {"BUY", "SELL"}:
            gross = non_negative_float(
                record.get("gross_value_base", record.get("gross_value", 0.0)),
                f"historical turnover gross value for {record.get('order_id') or record.get('symbol') or 'trade'}",
            )
            total = finite_float(total + gross, "daily turnover")
    return finite_float(total, "daily turnover")


def new_positions_for_date(
    date: str,
    trades_path: Path,
    pending_records: list[dict[str, Any]] | None = None,
) -> int:
    count = 0
    for record in [*read_jsonl(trades_path), *(pending_records or [])]:
        if record.get("date") == date and record.get("action") == "BUY" and record.get("opened_new_position"):
            count += 1
    return count


def actions_for_date(
    date: str,
    trades_path: Path,
    pending_records: list[dict[str, Any]] | None = None,
) -> int:
    return sum(
        1
        for record in [*read_jsonl(trades_path), *(pending_records or [])]
        if record.get("date") == date and record.get("action") in {"BUY", "SELL"}
    )


def load_orders(input_path: Path) -> list[dict[str, Any]]:
    raw = strict_json_loads(
        input_path.read_text(encoding="utf-8"),
        source=str(input_path),
    )
    if isinstance(raw, dict):
        orders = raw.get("orders", [raw])
    elif isinstance(raw, list):
        orders = raw
    else:
        raise ValueError("Order input must be a JSON object, list, or object with an orders list.")
    if not all(isinstance(order, dict) for order in orders):
        raise ValueError("Each order must be a JSON object.")
    return orders


def apply_order(
    state: dict[str, Any],
    config: dict[str, Any],
    order: dict[str, Any],
    date: str,
    pending_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = str(order.get("action", "")).upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        raise ValueError(f"Unsupported action: {action}")

    symbol = str(order.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("Order requires symbol.")
    exchange = str(order.get("exchange", order.get("market", ""))).strip().upper()
    market_type = str(order.get("market_type") or classify_market(symbol, exchange, config)).upper()
    if market_type == "GLOBAL" and config.get("account") == "US":
        market_type = "US"
    validate_account_allows_market(config.get("account"), market_type, config)
    market_rules = rules_for_market(market_type, config)
    currency = instrument_currency(
        config,
        market_type,
        order.get("currency"),
        field=f"Order {position_key(symbol, exchange)}",
    )
    fx_to_base = fx_rate_to_base(currency, config, order.get("fx_to_base"))
    key = position_key(symbol, exchange)
    existing_position = state.get("positions", {}).get(key)
    if isinstance(existing_position, dict):
        require_matching_currency(currency, existing_position.get("currency"), field=f"Position {key}")
        existing_market_type = str(existing_position.get("market_type") or "").strip().upper()
        if existing_market_type and existing_market_type != market_type:
            raise ValueError(
                f"Order market type {market_type} conflicts with existing position market type {existing_market_type} for {key}."
            )
    existing_mark = state.get("last_prices", {}).get(key)
    if isinstance(existing_mark, dict):
        require_matching_currency(currency, existing_mark.get("currency"), field=f"Last price {key}")
    reason = str(order.get("reason", "")).strip()
    if not reason:
        raise ValueError(f"Order for {key} requires reason.")
    decision_date = order_decision_date(order, date)
    require_monotonic_as_of(state, decision_date, operation="Order")
    price_raw = order.get("price")
    has_price = price_raw not in (None, "")
    validated_price = finite_float(price_raw, f"{action} order price for {key}") if has_price else None
    if validated_price is not None and validated_price <= 0:
        raise ValueError(f"{action} order for {key} requires positive price.")
    quantity_raw = order.get("quantity")
    validated_quantity: float | None = None
    if quantity_raw not in (None, "") and not (
        action == "SELL"
        and isinstance(quantity_raw, str)
        and quantity_raw.strip().upper() == "ALL"
    ):
        validated_quantity = finite_float(quantity_raw, f"{action} order quantity for {key}")
        if action in {"BUY", "SELL"} and validated_quantity <= 0:
            raise ValueError(f"{action} order for {key} has non-positive quantity.")
        if action == "HOLD" and validated_quantity < 0:
            raise ValueError(f"HOLD order for {key} has negative quantity.")
    notional_raw = order.get("notional")
    validated_notional: float | None = None
    if notional_raw not in (None, ""):
        validated_notional = finite_float(notional_raw, f"{action} order notional for {key}")
        if action in {"BUY", "SELL"} and validated_notional <= 0:
            raise ValueError(f"{action} order for {key} has non-positive notional.")
        if action == "HOLD" and validated_notional < 0:
            raise ValueError(f"HOLD order for {key} has negative notional.")
    explicit_fee: float | None = None
    explicit_fee_breakdown: dict[str, float] | None = None
    if order.get("fee") not in (None, ""):
        explicit_fee = non_negative_float(order["fee"], f"{action} order fee for {key}")
        explicit_fee_breakdown = {"explicit_override": explicit_fee}
    explicit_components: dict[str, float] = {}
    for field in ("commission", "tax", "stamp_tax"):
        if order.get(field) not in (None, ""):
            explicit_components[field] = non_negative_float(
                order[field],
                f"{action} order {field} for {key}",
            )
    if explicit_components:
        if explicit_fee is not None:
            raise ValueError(
                f"{action} order for {key} cannot provide both total fee and fee/tax components."
            )
        explicit_fee = sum(explicit_components.values())
        explicit_fee_breakdown = explicit_components

    price_date_raw = order.get("price_date")
    contract = config.get("order_contract", {}) if isinstance(config.get("order_contract"), dict) else {}
    price_date_required_from = str(contract.get("price_date_required_from_date") or "9999-12-31")[:10]
    if has_price and decision_date >= price_date_required_from and not price_date_raw:
        raise ValueError(f"Order for {key} requires price_date from {price_date_required_from}.")
    price_date = (
        strict_iso_date(price_date_raw, f"Order price_date for {key}")
        if price_date_raw not in (None, "")
        else decision_date if has_price else None
    )
    if price_date and price_date > decision_date:
        raise ValueError(f"Order price_date {price_date} cannot follow decision date {decision_date} for {key}.")
    if price_date:
        valuation_policy = config.get("valuation_policy", {})
        if not isinstance(valuation_policy, dict):
            valuation_policy = {}
        maximum_price_age = non_negative_int(
            contract.get(
                "max_price_age_business_days",
                valuation_policy.get("max_price_age_business_days", 1),
            ),
            "order price max_price_age_business_days",
        )
        price_age = business_day_age(price_date, decision_date)
        if price_age > maximum_price_age:
            raise ValueError(
                f"Order price for {key} is stale: price_date={price_date}, "
                f"business_day_age={price_age}, maximum={maximum_price_age}."
            )
    history_required_from = str(contract.get("theme_registry_history_required_from_date") or "9999-12-31")[:10]
    require_recorded_revision = action == "BUY" and decision_date >= history_required_from
    theme_registry = theme_registry_for_config(
        config,
        date=decision_date,
        require_recorded_revision=require_recorded_revision,
    )
    theme_required_from = str(contract.get("theme_required_from_date") or "9999-12-31")[:10]
    theme, theme_source = resolve_order_theme(
        theme_registry,
        account=config.get("account"),
        symbol=symbol,
        exchange=exchange,
        date=decision_date,
        explicit_theme=order.get("theme"),
        required=action == "BUY" and decision_date >= theme_required_from,
    )

    record: dict[str, Any] = {
        "timestamp": now_iso(),
        "timestamp_utc": utc_now_iso(),
        "date": decision_date,
        "price_date": price_date,
        "account": config.get("account"),
        "account_id": config.get("account_id"),
        "market_scope": config.get("market_scope"),
        "action": action,
        "symbol": symbol,
        "exchange": exchange,
        "market_type": market_type,
        "currency": currency,
        "prediction_id": order.get("prediction_id"),
        "theme": theme,
        "theme_source": theme_source,
        "theme_registry_revision_id": theme_registry.get("_revision_id") if action == "BUY" else None,
        "theme_registry_sha256": theme_registry.get("_registry_sha256") if action == "BUY" else None,
        "scenario": order.get("scenario"),
        "reason": reason,
        "risk": order.get("risk"),
        "source": order.get("source"),
        "paper_trading_only": True,
    }
    state["as_of_date"] = str(record["date"])[:10]

    if action == "HOLD":
        profile = config.get("strategy_profile", {})
        policy = profile.get("decision_policy", {}) if isinstance(profile, dict) else {}
        if profile.get("enabled") and policy.get("hold_requires_explicit_blocker"):
            blocker = str(order.get("blocker", "")).strip()
            allowed = {str(value) for value in policy.get("allowed_hold_blockers", [])}
            if blocker not in allowed:
                raise ValueError(
                    f"Aggressive-mode HOLD for {key} requires an allowed blocker: {', '.join(sorted(allowed))}"
                )
            record["blocker"] = blocker
        record.update(
            {
                "quantity": 0.0,
                "price": validated_price,
                "gross_value": 0.0,
                "fee": 0.0,
            }
        )
        return record

    if validated_price is None:
        raise ValueError(f"{action} order for {key} requires positive price.")
    price = validated_price
    validate_price_limit(symbol, price, order, market_type, market_rules)

    if action == "SELL" and isinstance(quantity_raw, str) and quantity_raw.strip().upper() == "ALL":
        quantity = non_negative_float(
            state.get("positions", {}).get(key, {}).get("quantity", 0.0),
            f"held quantity for {key}",
        )
    elif validated_quantity is not None:
        quantity = validated_quantity
    elif validated_notional is not None:
        quantity = validated_notional / price
    else:
        raise ValueError(f"{action} order for {key} requires quantity or notional.")
    quantity = finite_float(quantity, f"{action} order quantity for {key}")
    if quantity <= 0:
        raise ValueError(f"{action} order for {key} has non-positive quantity.")

    quantity = apply_market_quantity_rules(action, quantity, quantity_raw, market_type, market_rules)
    if not config.get("allow_fractional_shares", True):
        quantity = float(int(quantity))
    quantity = finite_float(quantity, f"{action} order adjusted quantity for {key}")
    if quantity <= 0:
        raise ValueError(f"{action} order for {key} has zero quantity after market lot rules.")
    gross = quantity * price
    calculated_fee, fee_breakdown = calculate_fee(
        action,
        gross,
        market_rules,
        non_negative_float(config.get("default_fee_rate", 0.0), "default_fee_rate"),
    )
    if explicit_fee is not None:
        fee = explicit_fee
        fee_breakdown = explicit_fee_breakdown or {"explicit_override": fee}
    else:
        fee = calculated_fee
    gross = positive_float(gross, f"{action} order gross value for {key}")
    fee = non_negative_float(fee, f"{action} order fee for {key}")
    gross_base = finite_float(gross * fx_to_base, f"{action} order base gross value for {key}")
    fee_base = non_negative_float(fee * fx_to_base, f"{action} order base fee for {key}")
    portfolio_equity = max(equity(state), 1.0)
    profile = config.get("strategy_profile", {})
    theme_exposure_after = None
    theme_exposure_pct_after = None
    theme_limit = None
    if action == "BUY":
        enforce_from = str(contract.get("maximum_theme_exposure_enforce_from_date") or "9999-12-31")[:10]
        if decision_date >= enforce_from:
            exposures, missing_themes = classified_theme_exposure(state, config, theme_registry, decision_date)
            if missing_themes:
                raise ValueError(
                    "Cannot enforce maximum theme exposure while open positions lack a canonical theme: "
                    + ", ".join(sorted(missing_themes))
                )
            if not theme:
                raise ValueError(f"BUY {key} requires a canonical paper theme before theme-cap enforcement.")
            theme_limit = portfolio_equity * float(
                profile.get("risk_overlays", {}).get("maximum_theme_exposure_pct", 0.0)
            )
            theme_exposure_after = float(exposures.get(theme, 0.0)) + gross_base
            existing_position = state.get("positions", {}).get(key)
            if isinstance(existing_position, dict) and non_negative_float(
                existing_position.get("quantity", 0.0),
                f"position {key} quantity",
            ) > 0:
                old_price_item = state.get("last_prices", {}).get(key, {})
                old_value = (
                    non_negative_float(existing_position.get("quantity", 0.0), f"position {key} quantity")
                    * positive_float(
                        old_price_item.get("price", existing_position.get("avg_cost", 0.0)),
                        f"position {key} price",
                    )
                    * state_fx_rate(state, existing_position, old_price_item)
                )
                repriced_value = (
                    non_negative_float(existing_position.get("quantity", 0.0), f"position {key} quantity")
                    * price
                    * fx_to_base
                )
                theme_exposure_after += repriced_value - old_value
            theme_exposure_pct_after = theme_exposure_after / portfolio_equity
            if theme_exposure_after > theme_limit + 1e-9:
                raise ValueError(
                    f"BUY {key} would exceed maximum theme exposure for {theme}: "
                    f"{theme_exposure_after:.2f} > {theme_limit:.2f}."
                )

    trades_path = resolve_path(config, "trades_file")
    policy = profile.get("decision_policy", {}) if isinstance(profile, dict) else {}
    max_actions = int(policy.get("maximum_actions_per_account_per_day", 0))
    if profile.get("enabled") and max_actions > 0 and actions_for_date(
        str(record["date"]), trades_path, pending_records
    ) >= max_actions:
        raise ValueError(f"Daily aggressive-strategy action count exceeds configured limit of {max_actions}.")
    today_turnover = turnover_for_date(str(record["date"]), trades_path, pending_records)
    if today_turnover + gross_base > portfolio_equity * float(config["max_daily_turnover_pct"]):
        raise ValueError(
            f"{action} {key} exceeds daily turnover limit: "
            f"{today_turnover + gross_base:.2f} > {portfolio_equity * float(config['max_daily_turnover_pct']):.2f}"
        )

    positions = state.setdefault("positions", {})
    position = positions.get(key)
    opened_new_position = False
    base_cost_released = 0.0
    realized_price_pnl_base = 0.0
    realized_fx_pnl_base = 0.0
    if action == "BUY":
        total_cost = positive_float(
            gross_base + fee_base,
            f"BUY total base cost for {key}",
        )
        min_cash = finite_float(config["initial_cash"], "initial_cash") * finite_float(
            config["min_cash_pct"],
            "min_cash_pct",
        )
        if finite_float(state["cash"], "cash") - total_cost < min_cash:
            raise ValueError(f"BUY {key} would breach minimum cash reserve.")

        post_position_value = gross_base + (
            non_negative_float(position.get("quantity", 0.0), f"position {key} quantity")
            * price
            * fx_to_base
            if position else 0.0
        )
        if post_position_value > portfolio_equity * finite_float(
            config["max_position_pct"],
            "max_position_pct",
        ):
            raise ValueError(
                f"BUY {key} exceeds max position limit: "
                f"{post_position_value:.2f} > "
                f"{portfolio_equity * finite_float(config['max_position_pct'], 'max_position_pct'):.2f}"
            )

        opened_new_position = position is None
        if opened_new_position and new_positions_for_date(
            str(record["date"]), trades_path, pending_records
        ) >= int(config["max_new_positions_per_day"]):
            raise ValueError("New position count exceeds configured limit.")

        if opened_new_position:
            position = {
                "symbol": symbol,
                "exchange": exchange,
                "market_type": market_type,
                "currency": currency,
                "fx_to_base": fx_to_base,
                "entry_fx_to_base": fx_to_base,
                "quantity": 0.0,
                "avg_cost": 0.0,
                "cost_basis": 0.0,
                "cost_basis_base": 0.0,
                "realized_pnl": 0.0,
                "realized_price_pnl_base": 0.0,
                "realized_fx_pnl_base": 0.0,
                "theme": theme,
                "opened_at": now_iso(),
                "opened_at_utc": utc_now_iso(),
            }
            positions[key] = position
        old_qty = non_negative_float(position["quantity"], f"position {key} quantity")
        old_cost = position_native_cost_basis(position)
        old_cost_base = position_base_cost_basis(state, position)
        new_qty = positive_float(old_qty + quantity, f"position {key} quantity after BUY")
        new_native_cost = positive_float(
            old_cost + gross + fee,
            f"position {key} native cost basis after BUY",
        )
        new_base_cost = positive_float(
            old_cost_base + total_cost,
            f"position {key} base cost basis after BUY",
        )
        position["quantity"] = new_qty
        position["avg_cost"] = new_native_cost / new_qty
        position["cost_basis"] = new_native_cost
        position["cost_basis_base"] = new_base_cost
        blended_entry_fx = new_base_cost / new_native_cost if new_native_cost > 0 else fx_to_base
        position["entry_fx_to_base"] = blended_entry_fx
        position["market_type"] = market_type
        position["currency"] = currency
        position["fx_to_base"] = blended_entry_fx
        if theme and position.get("theme") and position.get("theme") != theme:
            raise ValueError(f"BUY {key} theme {theme!r} conflicts with existing position theme {position.get('theme')!r}.")
        if theme and not position.get("theme"):
            position["theme"] = theme
        position["last_buy_date"] = str(record["date"])
        state["cash"] = finite_float(
            finite_float(state["cash"], "cash") - total_cost,
            "cash after BUY",
        )
        realized = 0.0
    else:
        if not position or non_negative_float(position.get("quantity", 0.0), f"position {key} quantity") <= 0:
            raise ValueError(f"Cannot SELL {key}; no position.")
        if market_type == "A_SHARE" and not market_rules.get("allow_same_day_sell", False):
            last_buy_date = str(position.get("last_buy_date", ""))
            if last_buy_date == str(record["date"]):
                raise ValueError(f"Cannot SELL {key}; A-share same-day sell is blocked by T+1 paper-trading rule.")
        held_qty = positive_float(position["quantity"], f"position {key} quantity")
        if quantity > held_qty + 1e-9:
            if not config.get("allow_short", False):
                raise ValueError(f"Cannot SELL {quantity} {key}; held quantity is {held_qty}.")
        quantity = min(quantity, held_qty)
        gross = positive_float(quantity * price, f"SELL order gross value for {key}")
        gross_base = finite_float(gross * fx_to_base, f"SELL order base gross value for {key}")
        calculated_fee, fee_breakdown = calculate_fee(
            action,
            gross,
            market_rules,
            non_negative_float(config.get("default_fee_rate", 0.0), "default_fee_rate"),
        )
        if explicit_fee is not None:
            fee = explicit_fee
            fee_breakdown = explicit_fee_breakdown or {"explicit_override": fee}
        else:
            fee = calculated_fee
        fee = non_negative_float(fee, f"SELL order fee for {key}")
        fee_base = non_negative_float(fee * fx_to_base, f"SELL order base fee for {key}")
        proceeds = gross_base - fee_base
        native_cost_before = position_native_cost_basis(position)
        base_cost_before = position_base_cost_basis(state, position)
        sold_ratio = quantity / held_qty
        native_cost_released = native_cost_before * sold_ratio
        base_cost_released = base_cost_before * sold_ratio
        entry_fx = (
            base_cost_released / native_cost_released
            if native_cost_released > 0
            else position_entry_fx_rate(state, position)
        )
        net_proceeds_native = gross - fee
        realized_price_pnl_base = finite_float(
            (net_proceeds_native - native_cost_released) * entry_fx,
            f"SELL realized price PnL for {key}",
        )
        realized = finite_float(
            proceeds - base_cost_released,
            f"SELL realized PnL for {key}",
        )
        realized_fx_pnl_base = finite_float(
            realized - realized_price_pnl_base,
            f"SELL realized FX PnL for {key}",
        )
        position["quantity"] = held_qty - quantity
        position["cost_basis"] = max(0.0, native_cost_before - native_cost_released)
        position["cost_basis_base"] = max(0.0, base_cost_before - base_cost_released)
        remaining_native_basis = position["cost_basis"]
        position["avg_cost"] = (
            remaining_native_basis / position["quantity"]
            if position["quantity"] > 1e-9
            else 0.0
        )
        remaining_base_basis = position["cost_basis_base"]
        remaining_entry_fx = (
            remaining_base_basis / remaining_native_basis
            if remaining_native_basis > 0
            else entry_fx
        )
        position["entry_fx_to_base"] = remaining_entry_fx
        position["fx_to_base"] = remaining_entry_fx
        position["realized_pnl"] = finite_float(position.get("realized_pnl", 0.0), "position realized_pnl") + realized
        position["realized_price_pnl_base"] = finite_float(
            position.get("realized_price_pnl_base", 0.0),
            "position realized_price_pnl_base",
        ) + realized_price_pnl_base
        position["realized_fx_pnl_base"] = finite_float(
            position.get("realized_fx_pnl_base", 0.0),
            "position realized_fx_pnl_base",
        ) + realized_fx_pnl_base
        state["cash"] = finite_float(
            finite_float(state["cash"], "cash") + proceeds,
            "cash after SELL",
        )
        state["realized_pnl"] = finite_float(
            finite_float(state.get("realized_pnl", 0.0), "realized_pnl") + realized,
            "realized_pnl after SELL",
        )
        if position["quantity"] <= 1e-9:
            positions.pop(key, None)

    state.setdefault("last_prices", {})[key] = {
        "symbol": symbol,
        "exchange": exchange,
        "market_type": market_type,
        "currency": currency,
        "fx_to_base": fx_to_base,
        "price": price,
        "date": price_date or record["date"],
        "price_date": price_date or record["date"],
        "source": order.get("source"),
    }
    record.update(
        {
            "quantity": quantity,
            "price": price,
            "gross_value": gross,
            "gross_value_base": gross_base,
            "fee": fee,
            "fee_base": fee_base,
            "fee_breakdown": fee_breakdown,
            "realized_pnl": realized,
            "realized_pnl_base": realized,
            "realized_price_pnl_base": realized_price_pnl_base,
            "realized_fx_pnl_base": realized_fx_pnl_base,
            "cost_basis_base_released": base_cost_released,
            "cost_basis_base_after": (
                position_base_cost_basis(state, positions[key])
                if key in positions
                else 0.0
            ),
            "fx_to_base": fx_to_base,
            "base_currency": config.get("base_currency"),
            "opened_new_position": opened_new_position,
            "market_lot_adjusted": True if action != "HOLD" and (quantity_raw is not None or notional_raw is not None) else False,
            "cash_after": float(state["cash"]),
            "equity_after": equity(state),
            "theme_exposure_after": theme_exposure_after,
            "theme_exposure_pct_after": round(theme_exposure_pct_after, 8) if theme_exposure_pct_after is not None else None,
            "theme_limit_value": theme_limit,
        }
    )
    return record


def grouped_orders(config: dict[str, Any], orders: list[dict[str, Any]], account: str | None = None) -> dict[str | None, list[dict[str, Any]]]:
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for order in orders:
        account_name = infer_order_account(config, order, requested_account=account)
        groups.setdefault(account_name, []).append(order)
    return groups


def apply_orders(input_path: Path, date: str, account: str | None = None) -> list[dict[str, Any]]:
    date = strict_iso_date(date, "Run date")
    base_config = load_config()
    orders_by_account = grouped_orders(base_config, load_orders(input_path), account=account)
    account_contexts: list[tuple[str | None, dict[str, Any], list[dict[str, Any]]]] = []
    for account_name in sorted(orders_by_account, key=lambda value: str(value or "")):
        config = account_config(base_config, account_name)
        ensure_files(account=account_name)
        account_contexts.append((account_name, config, orders_by_account[account_name]))

    returned_records: list[dict[str, Any]] = []
    prepared: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], bool]] = []
    original_prediction_dates: dict[str, list[str]] | None = None
    recovery_contexts = [
        (account_name, account_config(base_config, account_name))
        for account_name in target_accounts(base_config, "ALL")
    ]
    recovery_configs = [config for _account_name, config in recovery_contexts]
    with account_locks(recovery_configs):
        recover_transaction_batches(recovery_configs, valuation=False)
        recover_transaction_batches(recovery_configs, valuation=True)

        for _account_name, config, orders in account_contexts:
            state = copy.deepcopy(load_state_unlocked(config))
            trades_path = resolve_path(config, "trades_file")
            ledger_rows = read_jsonl(trades_path)
            migrated = reconcile_currency_state(config, state, ledger_rows)
            existing_by_id: dict[str, dict[str, Any]] = {}
            existing_by_fingerprint: dict[str, str] = {}
            for row in ledger_rows:
                historical_id = str(row.get("order_id") or "").strip()
                historical_fingerprint = str(row.get("order_fingerprint") or "").strip()
                if historical_id:
                    if historical_id in existing_by_id:
                        raise ValueError(f"Duplicate order_id {historical_id} in paper trade ledger.")
                    existing_by_id[historical_id] = row
                if historical_fingerprint:
                    prior_id = existing_by_fingerprint.get(historical_fingerprint)
                    if prior_id is not None:
                        raise ValueError(
                            f"Duplicate order_fingerprint in paper trade ledger: "
                            f"{prior_id or '<legacy>'} and {historical_id or '<legacy>'}."
                        )
                    existing_by_fingerprint[historical_fingerprint] = historical_id
            pending_records: list[dict[str, Any]] = []
            input_order_ids: set[str] = set()

            for order in orders:
                order_decision_date(order, date)
                order_id = order_idempotency_key(config, order, date)
                if order_id in input_order_ids:
                    raise ValueError(f"Duplicate order_id {order_id} in the same input batch.")
                input_order_ids.add(order_id)
                order_fingerprint = hashlib.sha256(
                    stable_json(
                        {
                            "account_id": config.get("account_id"),
                            "date": date,
                            "order": {
                                key: value
                                for key, value in order.items()
                                if key not in {"timestamp", "order_id", "idempotency_key", "account", "paper_account"}
                            },
                        }
                    ).encode("utf-8")
                ).hexdigest()
                existing = existing_by_id.get(order_id)
                if existing is not None:
                    existing_fingerprint = str(existing.get("order_fingerprint") or "")
                    if not existing_fingerprint:
                        raise ValueError(
                            f"Legacy paper order {order_id} lacks order_fingerprint; safe idempotent replay is impossible."
                        )
                    if existing_fingerprint != order_fingerprint:
                        raise ValueError(f"Conflicting paper order reuses order_id {order_id}.")
                    replay = dict(existing)
                    replay["idempotent_replay"] = True
                    returned_records.append(replay)
                    continue
                fingerprint_owner = existing_by_fingerprint.get(order_fingerprint)
                if fingerprint_owner is not None:
                    raise ValueError(
                        f"Paper order payload duplicates order_fingerprint owned by "
                        f"{fingerprint_owner or '<legacy>'} with a different order_id."
                    )

                if prediction_reference_required(config, date):
                    prediction_id = required_prediction_id(order, date)
                    if original_prediction_dates is None:
                        original_prediction_dates = load_original_prediction_dates(config)
                    validate_prediction_reference(prediction_id, date, original_prediction_dates)

                record = apply_order(
                    state,
                    config,
                    order,
                    date,
                    pending_records=pending_records,
                )
                record["order_id"] = order_id
                record["order_fingerprint"] = order_fingerprint
                record["idempotent_replay"] = False
                pending_records.append(record)
                existing_by_id[order_id] = record
                existing_by_fingerprint[order_fingerprint] = order_id
                returned_records.append(record)

            prepared.append((config, state, [*ledger_rows, *pending_records], bool(pending_records) or migrated))

        commit_account_transactions(
            [
                (config, state, ledger_rows)
                for config, state, ledger_rows, changed in prepared
                if changed
            ]
        )

    return returned_records


def load_prices(input_path: Path) -> list[dict[str, Any]]:
    raw = strict_json_loads(
        input_path.read_text(encoding="utf-8"),
        source=str(input_path),
    )
    if isinstance(raw, dict):
        prices = raw.get("prices", raw.get("items", [raw]))
    elif isinstance(raw, list):
        prices = raw
    else:
        raise ValueError("Price input must be a JSON object, list, or object with prices/items.")
    if not all(isinstance(price, dict) for price in prices):
        raise ValueError("Each price item must be a JSON object.")
    return prices


def grouped_prices(config: dict[str, Any], prices: list[dict[str, Any]], account: str | None = None) -> dict[str | None, list[dict[str, Any]]]:
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for item in prices:
        account_name = infer_order_account(config, item, requested_account=account)
        groups.setdefault(account_name, []).append(item)
    return groups


def mark_price_date(item: dict[str, Any], key: str) -> str:
    explicit_raw = item.get("price_date")
    legacy_raw = item.get("date")
    explicit = (
        strict_iso_date(explicit_raw, f"Mark price_date for {key}")
        if explicit_raw not in (None, "")
        else None
    )
    legacy = (
        strict_iso_date(legacy_raw, f"Mark date for {key}")
        if legacy_raw not in (None, "")
        else None
    )
    if explicit and legacy and explicit != legacy:
        raise ValueError(
            f"Mark price_date {explicit} conflicts with date {legacy} for {key}."
        )
    selected = explicit or legacy
    if not selected:
        raise ValueError(
            f"Mark price for {key} requires an explicit price_date (legacy date is also accepted)."
        )
    return selected


def valuation_price_issues(
    state: dict[str, Any],
    valuation_date: str,
    *,
    maximum_age_business_days: int,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for key, position in sorted(state.get("positions", {}).items()):
        if not isinstance(position, dict):
            issues.append({"key": key, "issue": "invalid_position"})
            continue
        quantity = non_negative_float(position.get("quantity", 0.0), f"position {key} quantity")
        if quantity <= 0:
            continue
        item = state.get("last_prices", {}).get(key)
        if not isinstance(item, dict):
            issues.append({"key": key, "issue": "missing_price"})
            continue
        price = item.get("price")
        if price in (None, ""):
            issues.append({"key": key, "issue": "missing_price"})
            continue
        try:
            positive_float(price, f"position {key} mark price")
        except ValueError as exc:
            issues.append({"key": key, "issue": "invalid_price", "detail": str(exc)})
            continue
        raw_price_date = item.get("price_date") or item.get("date")
        try:
            price_date = strict_iso_date(raw_price_date, f"position {key} price_date")
        except ValueError as exc:
            issues.append({"key": key, "issue": "missing_or_invalid_price_date", "detail": str(exc)})
            continue
        if price_date > valuation_date:
            issues.append(
                {
                    "key": key,
                    "issue": "future_price",
                    "price_date": price_date,
                    "valuation_date": valuation_date,
                }
            )
            continue
        age = business_day_age(price_date, valuation_date)
        if age > maximum_age_business_days:
            issues.append(
                {
                    "key": key,
                    "issue": "stale_price",
                    "price_date": price_date,
                    "valuation_date": valuation_date,
                    "business_day_age": age,
                    "maximum_business_day_age": maximum_age_business_days,
                }
            )
    return issues


def prepare_mark_account_to_market(
    config: dict[str, Any],
    state: dict[str, Any],
    prices: list[dict[str, Any]],
    date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    date = strict_iso_date(date, "Valuation date")
    previous_as_of_date = str(state.get("as_of_date") or "")
    require_monotonic_as_of(state, date, operation="Valuation")
    for item in prices:
        symbol = str(item.get("symbol", item.get("ticker", ""))).strip().upper()
        if not symbol:
            raise ValueError("Each mark price requires symbol or ticker.")
        exchange = str(item.get("exchange", item.get("market", ""))).strip().upper()
        market_type = str(item.get("market_type") or classify_market(symbol, exchange, config)).upper()
        if market_type == "GLOBAL" and config.get("account") == "US":
            market_type = "US"
        validate_account_allows_market(config.get("account"), market_type, config)
        key = position_key(symbol, exchange)
        price_value = item.get("price", item.get("last_close", item.get("last")))
        if price_value in (None, ""):
            raise ValueError(f"Mark price for {key} must not be empty.")
        price = positive_float(price_value, f"Mark price for {key}")
        price_date = mark_price_date(item, key)
        if price_date > date:
            raise ValueError(f"Mark price for {key} is dated {price_date}, after valuation date {date}.")
        currency = instrument_currency(
            config,
            market_type,
            item.get("currency"),
            field=f"Mark {key}",
        )
        position = state.get("positions", {}).get(key)
        if isinstance(position, dict):
            require_matching_currency(currency, position.get("currency"), field=f"Position {key}")
            position_market_type = str(position.get("market_type") or "").strip().upper()
            if position_market_type and position_market_type != market_type:
                raise ValueError(
                    f"Mark market type {market_type} conflicts with position market type {position_market_type} for {key}."
                )
        prior_mark = state.get("last_prices", {}).get(key)
        if isinstance(prior_mark, dict):
            require_matching_currency(currency, prior_mark.get("currency"), field=f"Prior mark {key}")
        fx_to_base = fx_rate_to_base(currency, config, item.get("fx_to_base"))
        state.setdefault("last_prices", {})[key] = {
            "symbol": symbol,
            "exchange": exchange,
            "market_type": market_type,
            "currency": currency,
            "fx_to_base": fx_to_base,
            "price": price,
            "date": price_date,
            "price_date": price_date,
            "source": item.get("source"),
        }

    policy = config.get("valuation_policy", {})
    if not isinstance(policy, dict):
        policy = {}
    stale_policy = str(policy.get("stale_price_policy", "fail")).strip().lower()
    maximum_age = non_negative_int(
        policy.get("max_price_age_business_days", 1),
        "valuation_policy.max_price_age_business_days",
    )
    issues = valuation_price_issues(
        state,
        date,
        maximum_age_business_days=maximum_age,
    )
    if issues and stale_policy == "fail":
        detail = "; ".join(
            f"{item.get('key')}:{item.get('issue')}"
            + (
                f"(price_date={item.get('price_date')},age={item.get('business_day_age')})"
                if item.get("price_date")
                else ""
            )
            for item in issues
        )
        raise ValueError(f"Valuation blocked by missing, invalid, or stale open-position prices: {detail}")

    state["as_of_date"] = date
    summary = summarize(state)
    valuation_status = "partial" if issues else "complete"
    valuation = {
        "timestamp": now_iso(),
        "timestamp_utc": utc_now_iso(),
        "date": date,
        "account": config.get("account"),
        "account_id": config.get("account_id"),
        "market_scope": config.get("market_scope"),
        "cash": summary["cash"],
        "positions_value": summary["positions_value"],
        "equity": summary["equity"],
        "realized_pnl": summary["realized_pnl"],
        "total_return_pct": summary["total_return_pct"],
        "base_currency": config.get("base_currency"),
        "paper_trading_only": True,
        "valuation_status": valuation_status,
        "valuation_healthy": not issues,
        "valuation_issues": issues,
        "stale_price_policy": stale_policy,
        "max_price_age_business_days": maximum_age,
        "price_snapshot": [
            {
                "key": key,
                "symbol": item.get("symbol"),
                "exchange": item.get("exchange"),
                "market_type": item.get("market_type"),
                "currency": item.get("currency"),
                "fx_to_base": item.get("fx_to_base"),
                "price": item.get("price"),
                "price_date": item.get("price_date") or item.get("date"),
                "source": item.get("source"),
            }
            for key, item in sorted(state.get("last_prices", {}).items())
            if key in state.get("positions", {}) and isinstance(item, dict)
        ],
    }
    fingerprint_payload = {
        key: value
        for key, value in valuation.items()
        if key not in {"timestamp", "timestamp_utc"}
    }
    valuation["valuation_id"] = f"{config.get('account_id')}:{date}"
    valuation["valuation_fingerprint"] = hashlib.sha256(stable_json(fingerprint_payload).encode("utf-8")).hexdigest()
    valuations_path = resolve_path(config, "valuations_file")
    rows = read_jsonl(valuations_path)
    by_date: dict[str, dict[str, Any]] = {}
    valuation_fingerprints: set[str] = set()
    for row in rows:
        row_date = str(row.get("date") or "")
        if row_date:
            if row_date in by_date:
                raise ValueError(f"Duplicate valuation date {row_date} in paper valuation ledger.")
            by_date[row_date] = row
        historical_fingerprint = str(row.get("valuation_fingerprint") or "")
        if historical_fingerprint:
            if historical_fingerprint in valuation_fingerprints:
                raise ValueError("Duplicate valuation_fingerprint in paper valuation ledger.")
            valuation_fingerprints.add(historical_fingerprint)
    existing = by_date.get(date)
    if existing and not existing.get("valuation_fingerprint"):
        raise ValueError(
            f"Legacy valuation for {date} lacks valuation_fingerprint; safe replay is impossible."
        )
    if existing and existing.get("valuation_fingerprint") == valuation["valuation_fingerprint"] and len(rows) == len(by_date) and previous_as_of_date == date:
        replay = dict(existing)
        replay["idempotent_replay"] = True
        return replay, [by_date[key] for key in sorted(by_date)], False
    valuation["idempotent_replay"] = False
    by_date[date] = valuation
    return valuation, [by_date[key] for key in sorted(by_date)], True


def mark_account_to_market(config: dict[str, Any], state: dict[str, Any], prices: list[dict[str, Any]], date: str) -> dict[str, Any]:
    valuation, rows, changed = prepare_mark_account_to_market(
        config,
        state,
        prices,
        date,
    )
    if changed:
        commit_valuation_transaction(config, state, rows)
    return valuation


def mark_to_market(input_path: Path, date: str, account: str | None = None) -> list[dict[str, Any]]:
    date = strict_iso_date(date, "Valuation date")
    base_config = load_config()
    prices_by_account = grouped_prices(base_config, load_prices(input_path), account=account)
    valuations: list[dict[str, Any]] = []
    account_contexts = [
        (account_name, account_config(base_config, account_name), prices)
        for account_name, prices in sorted(
            prices_by_account.items(),
            key=lambda item: str(item[0] or ""),
        )
    ]
    for account_name, _config, _prices in account_contexts:
        ensure_files(account=account_name)
    recovery_contexts = [
        (account_name, account_config(base_config, account_name))
        for account_name in target_accounts(base_config, "ALL")
    ]
    prepared: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    recovery_configs = [config for _account_name, config in recovery_contexts]
    with account_locks(recovery_configs):
        recover_transaction_batches(recovery_configs, valuation=False)
        recover_transaction_batches(recovery_configs, valuation=True)
        for _account_name, config, prices in account_contexts:
            state = copy.deepcopy(load_state_unlocked(config))
            migrated = reconcile_currency_state(
                config,
                state,
                read_jsonl(resolve_path(config, "trades_file")),
            )
            valuation, rows, changed = prepare_mark_account_to_market(
                config,
                state,
                prices,
                date,
            )
            valuations.append(valuation)
            if changed or migrated:
                prepared.append((config, state, rows))
        commit_valuation_transactions(prepared)
    return valuations


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    initial_cash = non_negative_float(state.get("initial_cash", 0.0), "initial_cash")
    positions_value = priced_positions_value(state)
    cash = finite_float(state.get("cash", 0.0), "cash")
    total_equity = finite_float(cash + positions_value, "equity")
    positions = []
    for key, position in sorted(state.get("positions", {}).items()):
        price = state.get("last_prices", {}).get(key, {}).get("price", position.get("avg_cost", 0.0))
        quantity = non_negative_float(position.get("quantity", 0.0), f"position {key} quantity")
        price_item = state.get("last_prices", {}).get(key, {})
        fx_to_base = state_fx_rate(state, position, price_item)
        mark_price = positive_float(price, f"position {key} price")
        market_value_native = finite_float(quantity * mark_price, f"position {key} market value")
        market_value = finite_float(
            market_value_native * fx_to_base,
            f"position {key} base market value",
        )
        cost_basis = position_native_cost_basis(position)
        cost_basis_base = position_base_cost_basis(state, position)
        entry_fx_to_base = (
            cost_basis_base / cost_basis
            if cost_basis > 0
            else position_entry_fx_rate(state, position)
        )
        unrealized_pnl = market_value - cost_basis_base
        unrealized_price_pnl_base = (market_value_native - cost_basis) * entry_fx_to_base
        unrealized_fx_pnl_base = unrealized_pnl - unrealized_price_pnl_base
        positions.append(
            {
                "key": key,
                "symbol": position.get("symbol"),
                "exchange": position.get("exchange"),
                "market_type": position.get("market_type"),
                "currency": position.get("currency"),
                "base_currency": state.get("base_currency"),
                "fx_to_base": fx_to_base,
                "entry_fx_to_base": entry_fx_to_base,
                "quantity": quantity,
                "avg_cost": non_negative_float(position.get("avg_cost", 0.0), f"position {key} avg_cost"),
                "last_price": mark_price,
                "market_value": market_value,
                "market_value_native": market_value_native,
                "cost_basis": cost_basis,
                "cost_basis_base": cost_basis_base,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_base": unrealized_pnl,
                "unrealized_price_pnl_base": unrealized_price_pnl_base,
                "unrealized_fx_pnl_base": unrealized_fx_pnl_base,
                "weight_pct": (market_value / total_equity * 100) if total_equity else 0.0,
            }
        )
    return {
        "account_id": state.get("account_id"),
        "base_unit": state.get("base_unit"),
        "base_currency": state.get("base_currency"),
        "initial_cash": initial_cash,
        "cash": cash,
        "positions_value": positions_value,
        "equity": total_equity,
        "realized_pnl": finite_float(state.get("realized_pnl", 0.0), "realized_pnl"),
        "total_return_pct": ((total_equity / initial_cash) - 1.0) * 100 if initial_cash else 0.0,
        "positions": positions,
        "paper_trading_only": True,
    }


def account_summary(account: str | None = None) -> dict[str, Any]:
    base_config = load_config()
    selected = target_accounts(base_config, account)
    if len(selected) != 1:
        raise ValueError("account_summary requires a single paper-trading account.")
    state = load_state(selected[0])
    summary = summarize(state)
    summary["account"] = selected[0]
    summary["market_scope"] = account_config(base_config, selected[0]).get("market_scope")
    return summary


def all_summaries(account: str | None = None) -> list[dict[str, Any]]:
    base_config = load_config()
    summaries = []
    for account_name, state, _trades, _valuations in load_account_snapshots(account):
        summary = summarize(state)
        summary["account"] = account_name
        summary["market_scope"] = account_config(base_config, account_name).get("market_scope")
        summaries.append(summary)
    return summaries


def print_previous(limit: int, account: str | None = None) -> None:
    base_config = load_config()
    for account_name, state, trades, valuations in load_account_snapshots(account):
        config = account_config(base_config, account_name)
        label = account_name or config.get("account_id", "DEFAULT")
        print(f"=== PAPER_TRADING_SUMMARY [{label}] ===")
        print(json.dumps(summarize(state), allow_nan=False, ensure_ascii=False, indent=2))
        print(f"\n=== RECENT_PAPER_TRADES [{label}] ===")
        for trade in trades[-limit:]:
            print(json.dumps(trade, allow_nan=False, ensure_ascii=False, sort_keys=True))
        if not trades:
            print("(none)")
        print(f"\n=== RECENT_VALUATIONS [{label}] ===")
        for valuation in valuations[-limit:]:
            print(json.dumps(valuation, allow_nan=False, ensure_ascii=False, sort_keys=True))
        if not valuations:
            print("(none)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the paper-trading account for daily briefings.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create paper-trading files if missing.")
    init_parser.add_argument("--reset", action="store_true", help="Reset the virtual portfolio to initial cash.")
    init_parser.add_argument("--account", default="ALL", help="Paper account to initialize: US, CHINA, or ALL.")

    previous_parser = subparsers.add_parser("previous", help="Print account summary and recent virtual trades.")
    previous_parser.add_argument("--limit", type=int, default=20)
    previous_parser.add_argument("--account", default="ALL", help="Paper account to read: US, CHINA, or ALL.")

    orders_parser = subparsers.add_parser("apply-orders", help="Apply virtual BUY/SELL/HOLD orders from JSON.")
    orders_parser.add_argument(
        "--date",
        help="Business date in YYYY-MM-DD; defaults to the configured report timezone date.",
    )
    orders_parser.add_argument("--input", required=True, type=Path)
    orders_parser.add_argument("--account", default="ALL", help="Optional forced paper account: US or CHINA. ALL routes by market.")

    mark_parser = subparsers.add_parser("mark", help="Mark portfolio to market from JSON prices.")
    mark_parser.add_argument(
        "--date",
        help="Valuation date in YYYY-MM-DD; defaults to the configured report timezone date.",
    )
    mark_parser.add_argument("--input", required=True, type=Path)
    mark_parser.add_argument("--account", default="ALL", help="Optional forced paper account: US or CHINA. ALL routes by market.")

    summary_parser = subparsers.add_parser("summary", help="Print current account summary.")
    summary_parser.add_argument("--account", default="ALL", help="Paper account to summarize: US, CHINA, or ALL.")

    args = parser.parse_args(argv)
    if args.command == "init":
        ensure_files(reset=args.reset, account=args.account)
        base_config = load_config()
        for account_name in target_accounts(base_config, args.account):
            config = account_config(base_config, account_name)
            label = account_name or config.get("account_id", "DEFAULT")
            print(f"[{label}]")
            print(resolve_path(config, "portfolio_file"))
            print(resolve_path(config, "trades_file"))
            print(resolve_path(config, "valuations_file"))
        return 0
    if args.command == "previous":
        print_previous(args.limit, account=args.account)
        return 0
    if args.command == "apply-orders":
        records = apply_orders(
            args.input,
            args.date or current_business_date(),
            account=args.account,
        )
        print(json.dumps({"applied": records}, allow_nan=False, ensure_ascii=False, indent=2))
        return 0
    if args.command == "mark":
        valuations = mark_to_market(
            args.input,
            args.date or current_business_date(),
            account=args.account,
        )
        print(json.dumps({"valuations": valuations}, allow_nan=False, ensure_ascii=False, indent=2))
        return 0
    if args.command == "summary":
        print(json.dumps({"accounts": all_summaries(args.account)}, allow_nan=False, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
