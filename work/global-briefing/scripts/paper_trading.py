#!/usr/bin/env python3
"""Virtual paper-trading ledger for the daily global briefing project."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime
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


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def validate_config(config: dict[str, Any]) -> None:
    for key in ("max_position_pct", "max_daily_turnover_pct"):
        value = float(config[key])
        if not 0 < value <= 1:
            raise ValueError(f"{key} must be greater than 0 and at most 1.")
    min_cash_pct = float(config["min_cash_pct"])
    if not 0 <= min_cash_pct < 1:
        raise ValueError("min_cash_pct must be at least 0 and less than 1.")
    if int(config["max_new_positions_per_day"]) < 1:
        raise ValueError("max_new_positions_per_day must be at least 1.")

    contract = config.get("order_contract", {}) if isinstance(config.get("order_contract"), dict) else {}
    for field in (
        "price_date_required_from_date",
        "theme_required_from_date",
        "maximum_theme_exposure_enforce_from_date",
        "theme_registry_history_required_from_date",
    ):
        if contract.get(field):
            datetime.strptime(str(contract[field])[:10], "%Y-%m-%d")
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
    invested_values = [float(invested[key]) for key in ("minimum", "preferred", "maximum")]
    if not 0 <= invested_values[0] <= invested_values[1] <= invested_values[2] <= 1:
        raise ValueError("strategy_profile target invested percentages are invalid.")

    cash = profile.get("target_cash_pct", {})
    cash_values = [float(cash[key]) for key in ("hard_minimum", "preferred_minimum", "preferred_maximum")]
    if not 0 <= cash_values[0] <= cash_values[1] <= cash_values[2] <= 1:
        raise ValueError("strategy_profile target cash percentages are invalid.")
    if abs(cash_values[0] - min_cash_pct) > 1e-9:
        raise ValueError("strategy_profile hard cash minimum must equal min_cash_pct.")

    signal = profile.get("signal_score", {})
    thresholds = [float(signal[key]) for key in ("exit_threshold", "reduce_threshold", "buy_threshold", "add_threshold")]
    if not 0 <= thresholds[0] < thresholds[1] < thresholds[2] < thresholds[3] <= 100:
        raise ValueError("strategy_profile signal thresholds must increase from exit to add.")
    component_total = sum(float(value) for value in signal.get("components", {}).values())
    if abs(component_total - 100) > 1e-9:
        raise ValueError("strategy_profile signal component weights must total 100.")
    risk_overlays = profile.get("risk_overlays", {}) if isinstance(profile.get("risk_overlays"), dict) else {}
    if "maximum_theme_exposure_pct" in risk_overlays:
        maximum_theme = float(risk_overlays["maximum_theme_exposure_pct"])
        if not 0 < maximum_theme <= 1:
            raise ValueError("strategy_profile maximum_theme_exposure_pct must be greater than 0 and at most 1.")


def load_config() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
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
        "last_updated": now_iso(),
        "base_unit": config["base_unit"],
        "base_currency": config.get("base_currency"),
        "fx_rates_to_base": dict(config.get("fx_rates_to_base", {})),
        "currency_schema_version": 2,
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
    legacy_state = json.loads(legacy_path.read_text(encoding="utf-8"))
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
        migrated_lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return "\n".join(migrated_lines) + ("\n" if migrated_lines else "")


def ensure_files(reset: bool = False, account: str | None = None) -> None:
    base_config = load_config()
    accounts = target_accounts(base_config, account)
    if len(accounts) > 1:
        for account_name in accounts:
            ensure_files(reset=reset, account=account_name)
        return

    config = account_config(base_config, accounts[0])
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


def load_state(account: str | None = None) -> dict[str, Any]:
    base_config = load_config()
    selected = target_accounts(base_config, account)
    if len(selected) != 1:
        raise ValueError("load_state requires a single paper-trading account.")
    ensure_files(account=selected[0])
    config = account_config(base_config, selected[0])
    return json.loads(resolve_path(config, "portfolio_file").read_text(encoding="utf-8"))


def save_state(state: dict[str, Any], account: str | None = None) -> None:
    base_config = load_config()
    selected = normalize_account(account) or normalize_account(state.get("account"))
    config = account_config(base_config, selected)
    state["last_updated"] = now_iso()
    atomic_write_json(resolve_path(config, "portfolio_file"), state)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def transaction_path(config: dict[str, Any]) -> Path:
    return resolve_path(config, "trades_file").with_suffix(".transaction.json")


def valuation_transaction_path(config: dict[str, Any]) -> Path:
    return resolve_path(config, "valuations_file").with_suffix(".transaction.json")


def lock_path(config: dict[str, Any]) -> Path:
    return resolve_path(config, "portfolio_file").with_suffix(".lock")


@contextmanager
def account_lock(config: dict[str, Any]):
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(f"{os.getpid()}:{now_iso()}:{config.get('account_id')}".encode("utf-8")).hexdigest()
    payload = {"pid": os.getpid(), "created_at": now_iso(), "token": token}
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(str(existing.get("created_at", "")))
                stale = (datetime.now() - created).total_seconds() > 3600
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                stale = True
            if stale:
                path.unlink(missing_ok=True)
                continue
            raise RuntimeError(f"Paper-trading account is locked: {config.get('account_id')}") from exc
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            break
    else:
        raise RuntimeError(f"Unable to acquire paper-trading lock: {path}")
    try:
        yield
    finally:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("token") == token:
            path.unlink(missing_ok=True)


def recover_transaction(config: dict[str, Any]) -> None:
    journal_path = transaction_path(config)
    if not journal_path.exists():
        return
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    state = journal.get("state")
    ledger_text = journal.get("ledger_text")
    if not isinstance(state, dict) or not isinstance(ledger_text, str):
        raise ValueError(f"Invalid paper-trading transaction journal: {journal_path}")
    atomic_write_text(resolve_path(config, "trades_file"), ledger_text)
    atomic_write_json(resolve_path(config, "portfolio_file"), state)
    journal_path.unlink(missing_ok=True)


def recover_valuation_transaction(config: dict[str, Any]) -> None:
    journal_path = valuation_transaction_path(config)
    if not journal_path.exists():
        return
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    state = journal.get("state")
    ledger_text = journal.get("ledger_text")
    if not isinstance(state, dict) or not isinstance(ledger_text, str):
        raise ValueError(f"Invalid paper-valuation transaction journal: {journal_path}")
    atomic_write_text(resolve_path(config, "valuations_file"), ledger_text)
    atomic_write_json(resolve_path(config, "portfolio_file"), state)
    journal_path.unlink(missing_ok=True)


def commit_account_transaction(config: dict[str, Any], state: dict[str, Any], ledger_rows: list[dict[str, Any]]) -> None:
    state["last_updated"] = now_iso()
    ledger_text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ledger_rows)
    journal = {
        "account": config.get("account"),
        "account_id": config.get("account_id"),
        "prepared_at": now_iso(),
        "state": state,
        "ledger_text": ledger_text,
    }
    journal_path = transaction_path(config)
    atomic_write_json(journal_path, journal)
    atomic_write_text(resolve_path(config, "trades_file"), ledger_text)
    atomic_write_json(resolve_path(config, "portfolio_file"), state)
    journal_path.unlink(missing_ok=True)


def commit_valuation_transaction(config: dict[str, Any], state: dict[str, Any], ledger_rows: list[dict[str, Any]]) -> None:
    state["last_updated"] = now_iso()
    ledger_text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ledger_rows)
    journal = {
        "account": config.get("account"),
        "account_id": config.get("account_id"),
        "prepared_at": now_iso(),
        "state": state,
        "ledger_text": ledger_text,
    }
    journal_path = valuation_transaction_path(config)
    atomic_write_json(journal_path, journal)
    atomic_write_text(resolve_path(config, "valuations_file"), ledger_text)
    atomic_write_json(resolve_path(config, "portfolio_file"), state)
    journal_path.unlink(missing_ok=True)


def order_idempotency_key(config: dict[str, Any], order: dict[str, Any], date: str) -> str:
    explicit = str(order.get("order_id") or order.get("idempotency_key") or "").strip()
    if explicit:
        return explicit
    identity = {
        "account_id": config.get("account_id"),
        "date": str(order.get("date") or date),
        "order": {
            key: value
            for key, value in order.items()
            if key not in {"timestamp", "order_id", "idempotency_key", "account", "paper_account"}
        },
    }
    return f"PAPER-{hashlib.sha256(stable_json(identity).encode('utf-8')).hexdigest()[:24].upper()}"


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
        rate = float(explicit)
    else:
        clean = str(currency or config.get("base_currency") or "").upper()
        configured_rates = config.get("fx_rates_to_base", {})
        if not config.get("base_currency") and not configured_rates:
            return 1.0
        rate = float(configured_rates.get(clean, 1.0 if clean == str(config.get("base_currency") or "").upper() else 0.0))
    if rate <= 0:
        raise ValueError(f"Missing positive fx_to_base conversion for currency {currency!r} in account {config.get('account')}.")
    return rate


def state_fx_rate(state: dict[str, Any], position: dict[str, Any], price_item: dict[str, Any] | None = None) -> float:
    price_item = price_item or {}
    currency = str(price_item.get("currency") or position.get("currency") or state.get("base_currency") or "").upper()
    explicit = price_item.get("fx_to_base", position.get("fx_to_base"))
    if explicit not in (None, ""):
        return float(explicit)
    rates = state.get("fx_rates_to_base", {})
    if not state.get("base_currency") and not rates:
        return 1.0
    rate = float(rates.get(currency, 1.0 if currency == str(state.get("base_currency") or "").upper() else 0.0))
    if rate <= 0:
        raise ValueError(f"Missing positive fx_to_base conversion for position currency {currency!r}.")
    return rate


def reconcile_currency_state(config: dict[str, Any], state: dict[str, Any], trades: list[dict[str, Any]]) -> bool:
    """One-time migration from nominal mixed-currency points to account base currency."""
    if int(state.get("currency_schema_version", 0) or 0) >= 2:
        return False
    state["base_currency"] = config.get("base_currency")
    state["fx_rates_to_base"] = dict(config.get("fx_rates_to_base", {}))
    cash = float(state.get("initial_cash", config.get("initial_cash", 0.0)))
    realized_base = 0.0
    for trade in trades:
        action = str(trade.get("action") or "").upper()
        if action not in {"BUY", "SELL"}:
            continue
        currency = trade.get("currency") or rules_for_market(str(trade.get("market_type") or ""), config).get("currency")
        rate = fx_rate_to_base(currency, config, trade.get("fx_to_base"))
        gross_base = float(trade.get("gross_value_base", float(trade.get("gross_value", 0.0)) * rate))
        fee_base = float(trade.get("fee_base", float(trade.get("fee", 0.0)) * rate))
        cash += gross_base - fee_base if action == "SELL" else -(gross_base + fee_base)
        realized_base += float(trade.get("realized_pnl_base", float(trade.get("realized_pnl", 0.0)) * rate))
    for key, position in state.get("positions", {}).items():
        price_item = state.get("last_prices", {}).get(key, {})
        rate = fx_rate_to_base(position.get("currency") or price_item.get("currency"), config, price_item.get("fx_to_base"))
        position["fx_to_base"] = rate
        price_item["fx_to_base"] = rate
    state["cash"] = cash
    state["realized_pnl"] = realized_base
    state["currency_schema_version"] = 2
    state.setdefault("notes", []).append("Migrated account values to explicit base-currency conversion schema v2.")
    return True


def calculate_fee(
    action: str,
    gross: float,
    market_rules: dict[str, Any],
    default_fee_rate: float = 0.0,
) -> tuple[float, dict[str, float]]:
    """Apply the configured broker-style commission and sell-side tax model."""
    commission_rate = float(market_rules.get("commission_rate", default_fee_rate))
    min_commission = float(market_rules.get("min_commission", 0.0))
    stamp_tax_rate = float(market_rules.get("stamp_tax_sell_rate", 0.0)) if action == "SELL" else 0.0
    commission = max(abs(gross) * commission_rate, min_commission) if gross and commission_rate else 0.0
    stamp_tax = abs(gross) * stamp_tax_rate
    breakdown = {"commission": commission, "stamp_tax": stamp_tax}
    return commission + stamp_tax, breakdown


def floor_to_lot(quantity: float, lot_size: int) -> float:
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
        elif not (isinstance(quantity_raw, str) and quantity_raw.upper() == "ALL"):
            quantity = floor_to_lot(quantity, int(rules.get("partial_sell_lot_size", 100)))
    elif rules.get("integer_quantity"):
        quantity = float(int(quantity))
    return quantity


def price_limit_for_symbol(symbol: str, order: dict[str, Any], rules: dict[str, Any]) -> float:
    explicit = order.get("price_limit_pct")
    if explicit is not None:
        return float(explicit)
    symbol_clean = symbol.strip().upper()
    name = str(order.get("name", "")).upper()
    if "ST" in name:
        return float(rules.get("st_price_limit_pct", rules.get("default_price_limit_pct", 0.1)))
    if symbol_clean.startswith("688"):
        return float(rules.get("star_market_price_limit_pct", rules.get("default_price_limit_pct", 0.1)))
    if symbol_clean.startswith("300"):
        return float(rules.get("chinext_price_limit_pct", rules.get("default_price_limit_pct", 0.1)))
    return float(rules.get("default_price_limit_pct", 0.1))


def validate_price_limit(symbol: str, price: float, order: dict[str, Any], market_type: str, rules: dict[str, Any]) -> None:
    if market_type != "A_SHARE":
        return
    previous_close = order.get("previous_close")
    if previous_close in (None, ""):
        return
    previous = float(previous_close)
    if previous <= 0:
        return
    limit = price_limit_for_symbol(symbol, order, rules)
    lower = previous * (1 - limit)
    upper = previous * (1 + limit)
    if price < lower - 1e-9 or price > upper + 1e-9:
        raise ValueError(f"A-share price {price} breaches configured price limit range {lower:.2f}-{upper:.2f}.")


def priced_positions_value(state: dict[str, Any]) -> float:
    total = 0.0
    for key, position in state.get("positions", {}).items():
        quantity = float(position.get("quantity", 0.0))
        if quantity <= 0:
            continue
        price_item = state.get("last_prices", {}).get(key, {})
        price = price_item.get("price")
        if price is None:
            price = position.get("avg_cost", 0.0)
        total += quantity * float(price) * state_fx_rate(state, position, price_item)
    return total


def equity(state: dict[str, Any]) -> float:
    return float(state.get("cash", 0.0)) + priced_positions_value(state)


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
            total += abs(float(record.get("gross_value_base", record.get("gross_value", 0.0))))
    return total


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
    raw = json.loads(input_path.read_text(encoding="utf-8"))
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
    currency = str(order.get("currency") or market_rules.get("currency") or config.get("base_currency") or "").upper()
    fx_to_base = fx_rate_to_base(currency, config, order.get("fx_to_base"))
    key = position_key(symbol, exchange)
    reason = str(order.get("reason", "")).strip()
    if not reason:
        raise ValueError(f"Order for {key} requires reason.")
    decision_date = str(order.get("date") or date)[:10]
    price_date_raw = order.get("price_date")
    contract = config.get("order_contract", {}) if isinstance(config.get("order_contract"), dict) else {}
    price_date_required_from = str(contract.get("price_date_required_from_date") or "9999-12-31")[:10]
    if order.get("price") not in (None, "") and decision_date >= price_date_required_from and not price_date_raw:
        raise ValueError(f"Order for {key} requires price_date from {price_date_required_from}.")
    price_date = str(price_date_raw or decision_date)[:10] if order.get("price") not in (None, "") else None
    if price_date and price_date > decision_date:
        raise ValueError(f"Order price_date {price_date} cannot follow decision date {decision_date} for {key}.")
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
        "date": decision_date,
        "price_date": price_date,
        "account": config.get("account"),
        "account_id": config.get("account_id"),
        "market_scope": config.get("market_scope"),
        "action": action,
        "symbol": symbol,
        "exchange": exchange,
        "market_type": market_type,
        "currency": order.get("currency") or market_rules.get("currency"),
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
        record.update({"quantity": 0.0, "price": order.get("price"), "gross_value": 0.0, "fee": 0.0})
        return record

    price = float(order.get("price", 0.0))
    if price <= 0:
        raise ValueError(f"{action} order for {key} requires positive price.")
    validate_price_limit(symbol, price, order, market_type, market_rules)

    quantity_raw = order.get("quantity")
    notional_raw = order.get("notional")
    if action == "SELL" and isinstance(quantity_raw, str) and quantity_raw.upper() == "ALL":
        quantity = float(state.get("positions", {}).get(key, {}).get("quantity", 0.0))
    elif quantity_raw is not None:
        quantity = float(quantity_raw)
    elif notional_raw is not None:
        quantity = float(notional_raw) / price
    else:
        raise ValueError(f"{action} order for {key} requires quantity or notional.")
    if quantity <= 0:
        raise ValueError(f"{action} order for {key} has non-positive quantity.")

    quantity = apply_market_quantity_rules(action, quantity, quantity_raw, market_type, market_rules)
    if not config.get("allow_fractional_shares", True):
        quantity = float(int(quantity))
    if quantity <= 0:
        raise ValueError(f"{action} order for {key} has zero quantity after market lot rules.")
    gross = quantity * price
    calculated_fee, fee_breakdown = calculate_fee(
        action,
        gross,
        market_rules,
        float(config.get("default_fee_rate", 0.0)),
    )
    if order.get("fee") is not None:
        fee = float(order["fee"])
        fee_breakdown = {"explicit_override": fee}
    else:
        fee = calculated_fee
    gross_base = gross * fx_to_base
    fee_base = fee * fx_to_base
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
            if isinstance(existing_position, dict) and float(existing_position.get("quantity", 0.0)) > 0:
                old_price_item = state.get("last_prices", {}).get(key, {})
                old_value = (
                    float(existing_position.get("quantity", 0.0))
                    * float(old_price_item.get("price", existing_position.get("avg_cost", 0.0)) or 0.0)
                    * state_fx_rate(state, existing_position, old_price_item)
                )
                repriced_value = float(existing_position.get("quantity", 0.0)) * price * fx_to_base
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
    if action == "BUY":
        total_cost = gross_base + fee_base
        min_cash = float(config["initial_cash"]) * float(config["min_cash_pct"])
        if float(state["cash"]) - total_cost < min_cash:
            raise ValueError(f"BUY {key} would breach minimum cash reserve.")

        post_position_value = gross_base + (
            float(position.get("quantity", 0.0)) * price * state_fx_rate(state, position)
            if position else 0.0
        )
        if post_position_value > portfolio_equity * float(config["max_position_pct"]):
            raise ValueError(
                f"BUY {key} exceeds max position limit: "
                f"{post_position_value:.2f} > {portfolio_equity * float(config['max_position_pct']):.2f}"
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
                "currency": order.get("currency") or market_rules.get("currency"),
                "fx_to_base": fx_to_base,
                "quantity": 0.0,
                "avg_cost": 0.0,
                "cost_basis": 0.0,
                "realized_pnl": 0.0,
                "theme": theme,
                "opened_at": now_iso(),
            }
            positions[key] = position
        old_qty = float(position["quantity"])
        old_cost = old_qty * float(position["avg_cost"])
        new_qty = old_qty + quantity
        position["quantity"] = new_qty
        position["avg_cost"] = (old_cost + gross + fee) / new_qty
        position["cost_basis"] = new_qty * float(position["avg_cost"])
        position["market_type"] = market_type
        position["currency"] = order.get("currency") or market_rules.get("currency")
        position["fx_to_base"] = fx_to_base
        if theme and position.get("theme") and position.get("theme") != theme:
            raise ValueError(f"BUY {key} theme {theme!r} conflicts with existing position theme {position.get('theme')!r}.")
        if theme and not position.get("theme"):
            position["theme"] = theme
        position["last_buy_date"] = str(record["date"])
        state["cash"] = float(state["cash"]) - total_cost
        realized = 0.0
    else:
        if not position or float(position.get("quantity", 0.0)) <= 0:
            raise ValueError(f"Cannot SELL {key}; no position.")
        if market_type == "A_SHARE" and not market_rules.get("allow_same_day_sell", False):
            last_buy_date = str(position.get("last_buy_date", ""))
            if last_buy_date == str(record["date"]):
                raise ValueError(f"Cannot SELL {key}; A-share same-day sell is blocked by T+1 paper-trading rule.")
        held_qty = float(position["quantity"])
        if quantity > held_qty + 1e-9:
            if not config.get("allow_short", False):
                raise ValueError(f"Cannot SELL {quantity} {key}; held quantity is {held_qty}.")
        quantity = min(quantity, held_qty)
        proceeds = gross_base - fee_base
        realized_native = (price - float(position["avg_cost"])) * quantity - fee
        realized = realized_native * fx_to_base
        position["quantity"] = held_qty - quantity
        position["cost_basis"] = float(position["quantity"]) * float(position["avg_cost"])
        position["realized_pnl"] = float(position.get("realized_pnl", 0.0)) + realized
        state["cash"] = float(state["cash"]) + proceeds
        state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + realized
        if position["quantity"] <= 1e-9:
            positions.pop(key, None)

    state.setdefault("last_prices", {})[key] = {
        "symbol": symbol,
        "exchange": exchange,
        "market_type": market_type,
        "currency": order.get("currency") or market_rules.get("currency"),
        "fx_to_base": fx_to_base,
        "price": price,
        "date": price_date or record["date"],
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
    base_config = load_config()
    orders_by_account = grouped_orders(base_config, load_orders(input_path), account=account)
    account_contexts: list[tuple[str | None, dict[str, Any], list[dict[str, Any]]]] = []
    for account_name in sorted(orders_by_account, key=lambda value: str(value or "")):
        config = account_config(base_config, account_name)
        ensure_files(account=account_name)
        account_contexts.append((account_name, config, orders_by_account[account_name]))

    returned_records: list[dict[str, Any]] = []
    prepared: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], bool]] = []
    with ExitStack() as stack:
        for _account_name, config, _orders in account_contexts:
            stack.enter_context(account_lock(config))

        for account_name, config, orders in account_contexts:
            recover_transaction(config)
            state = copy.deepcopy(load_state(account_name))
            trades_path = resolve_path(config, "trades_file")
            ledger_rows = read_jsonl(trades_path)
            migrated = reconcile_currency_state(config, state, ledger_rows)
            existing_by_id = {
                str(row["order_id"]): row
                for row in ledger_rows
                if row.get("order_id") not in (None, "")
            }
            pending_records: list[dict[str, Any]] = []

            for order in orders:
                order_id = order_idempotency_key(config, order, date)
                order_fingerprint = hashlib.sha256(
                    stable_json(
                        {
                            "account_id": config.get("account_id"),
                            "date": str(order.get("date") or date),
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
                    if existing_fingerprint and existing_fingerprint != order_fingerprint:
                        raise ValueError(f"Conflicting paper order reuses order_id {order_id}.")
                    replay = dict(existing)
                    replay["idempotent_replay"] = True
                    returned_records.append(replay)
                    continue

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
                returned_records.append(record)

            prepared.append((config, state, [*ledger_rows, *pending_records], bool(pending_records) or migrated))

        for config, state, ledger_rows, changed in prepared:
            if changed:
                commit_account_transaction(config, state, ledger_rows)

    return returned_records


def load_prices(input_path: Path) -> list[dict[str, Any]]:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        prices = raw.get("prices", raw.get("items", [raw]))
    elif isinstance(raw, list):
        prices = raw
    else:
        raise ValueError("Price input must be a JSON object, list, or object with prices/items.")
    return [price for price in prices if isinstance(price, dict)]


def grouped_prices(config: dict[str, Any], prices: list[dict[str, Any]], account: str | None = None) -> dict[str | None, list[dict[str, Any]]]:
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for item in prices:
        account_name = infer_order_account(config, item, requested_account=account)
        groups.setdefault(account_name, []).append(item)
    return groups


def mark_account_to_market(config: dict[str, Any], state: dict[str, Any], prices: list[dict[str, Any]], date: str) -> dict[str, Any]:
    previous_as_of_date = str(state.get("as_of_date") or "")
    state["as_of_date"] = date
    for item in prices:
        symbol = str(item.get("symbol", item.get("ticker", ""))).strip().upper()
        if not symbol:
            continue
        exchange = str(item.get("exchange", item.get("market", ""))).strip().upper()
        market_type = str(item.get("market_type") or classify_market(symbol, exchange, config)).upper()
        if market_type == "GLOBAL" and config.get("account") == "US":
            market_type = "US"
        validate_account_allows_market(config.get("account"), market_type, config)
        market_rules = rules_for_market(market_type, config)
        key = position_key(symbol, exchange)
        price_value = item.get("price", item.get("last_close", item.get("last")))
        if price_value is None:
            continue
        if float(price_value) <= 0:
            raise ValueError(f"Mark price for {key} must be positive.")
        price_date = str(item.get("date", date))[:10]
        if price_date > date:
            raise ValueError(f"Mark price for {key} is dated {price_date}, after valuation date {date}.")
        currency = str(item.get("currency") or market_rules.get("currency") or config.get("base_currency") or "").upper()
        fx_to_base = fx_rate_to_base(currency, config, item.get("fx_to_base"))
        state.setdefault("last_prices", {})[key] = {
            "symbol": symbol,
            "exchange": exchange,
            "market_type": market_type,
            "currency": currency,
            "fx_to_base": fx_to_base,
            "price": float(price_value),
            "date": price_date,
            "source": item.get("source"),
        }
    summary = summarize(state)
    valuation = {
        "timestamp": now_iso(),
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
        "price_snapshot": [
            {
                "key": key,
                "symbol": item.get("symbol"),
                "exchange": item.get("exchange"),
                "market_type": item.get("market_type"),
                "currency": item.get("currency"),
                "fx_to_base": item.get("fx_to_base"),
                "price": item.get("price"),
                "price_date": item.get("date"),
                "source": item.get("source"),
            }
            for key, item in sorted(state.get("last_prices", {}).items())
            if key in state.get("positions", {}) and isinstance(item, dict)
        ],
    }
    fingerprint_payload = {key: value for key, value in valuation.items() if key != "timestamp"}
    valuation["valuation_id"] = f"{config.get('account_id')}:{date}"
    valuation["valuation_fingerprint"] = hashlib.sha256(stable_json(fingerprint_payload).encode("utf-8")).hexdigest()
    valuations_path = resolve_path(config, "valuations_file")
    rows = read_jsonl(valuations_path)
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_date = str(row.get("date") or "")
        if row_date:
            by_date[row_date] = row
    existing = by_date.get(date)
    if existing and existing.get("valuation_fingerprint") == valuation["valuation_fingerprint"] and len(rows) == len(by_date) and previous_as_of_date == date:
        replay = dict(existing)
        replay["idempotent_replay"] = True
        return replay
    valuation["idempotent_replay"] = False
    by_date[date] = valuation
    commit_valuation_transaction(config, state, [by_date[key] for key in sorted(by_date)])
    return valuation


def mark_to_market(input_path: Path, date: str, account: str | None = None) -> list[dict[str, Any]]:
    base_config = load_config()
    prices_by_account = grouped_prices(base_config, load_prices(input_path), account=account)
    valuations: list[dict[str, Any]] = []
    for account_name, prices in sorted(prices_by_account.items(), key=lambda item: str(item[0] or "")):
        config = account_config(base_config, account_name)
        ensure_files(account=account_name)
        with account_lock(config):
            recover_transaction(config)
            recover_valuation_transaction(config)
            state = copy.deepcopy(load_state(account_name))
            reconcile_currency_state(config, state, read_jsonl(resolve_path(config, "trades_file")))
            valuations.append(mark_account_to_market(config, state, prices, date))
    return valuations


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    initial_cash = float(state.get("initial_cash", 0.0))
    positions_value = priced_positions_value(state)
    total_equity = float(state.get("cash", 0.0)) + positions_value
    positions = []
    for key, position in sorted(state.get("positions", {}).items()):
        price = state.get("last_prices", {}).get(key, {}).get("price", position.get("avg_cost", 0.0))
        quantity = float(position.get("quantity", 0.0))
        price_item = state.get("last_prices", {}).get(key, {})
        fx_to_base = state_fx_rate(state, position, price_item)
        market_value_native = quantity * float(price)
        market_value = market_value_native * fx_to_base
        cost_basis = float(position.get("cost_basis", 0.0))
        cost_basis_base = cost_basis * fx_to_base
        positions.append(
            {
                "key": key,
                "symbol": position.get("symbol"),
                "exchange": position.get("exchange"),
                "market_type": position.get("market_type"),
                "currency": position.get("currency"),
                "base_currency": state.get("base_currency"),
                "fx_to_base": fx_to_base,
                "quantity": quantity,
                "avg_cost": float(position.get("avg_cost", 0.0)),
                "last_price": float(price),
                "market_value": market_value,
                "market_value_native": market_value_native,
                "cost_basis_base": cost_basis_base,
                "unrealized_pnl": market_value - cost_basis_base,
                "weight_pct": (market_value / total_equity * 100) if total_equity else 0.0,
            }
        )
    return {
        "account_id": state.get("account_id"),
        "base_unit": state.get("base_unit"),
        "base_currency": state.get("base_currency"),
        "initial_cash": initial_cash,
        "cash": float(state.get("cash", 0.0)),
        "positions_value": positions_value,
        "equity": total_equity,
        "realized_pnl": float(state.get("realized_pnl", 0.0)),
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
    return [account_summary(account_name) for account_name in target_accounts(base_config, account)]


def print_previous(limit: int, account: str | None = None) -> None:
    base_config = load_config()
    for account_name in target_accounts(base_config, account):
        config = account_config(base_config, account_name)
        ensure_files(account=account_name)
        state = load_state(account_name)
        trades = read_jsonl(resolve_path(config, "trades_file"))
        valuations = read_jsonl(resolve_path(config, "valuations_file"))
        label = account_name or config.get("account_id", "DEFAULT")
        print(f"=== PAPER_TRADING_SUMMARY [{label}] ===")
        print(json.dumps(summarize(state), ensure_ascii=False, indent=2))
        print(f"\n=== RECENT_PAPER_TRADES [{label}] ===")
        for trade in trades[-limit:]:
            print(json.dumps(trade, ensure_ascii=False, sort_keys=True))
        if not trades:
            print("(none)")
        print(f"\n=== RECENT_VALUATIONS [{label}] ===")
        for valuation in valuations[-limit:]:
            print(json.dumps(valuation, ensure_ascii=False, sort_keys=True))
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
    orders_parser.add_argument("--date", required=True)
    orders_parser.add_argument("--input", required=True, type=Path)
    orders_parser.add_argument("--account", default="ALL", help="Optional forced paper account: US or CHINA. ALL routes by market.")

    mark_parser = subparsers.add_parser("mark", help="Mark portfolio to market from JSON prices.")
    mark_parser.add_argument("--date", required=True)
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
        records = apply_orders(args.input, args.date, account=args.account)
        print(json.dumps({"applied": records}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "mark":
        valuations = mark_to_market(args.input, args.date, account=args.account)
        print(json.dumps({"valuations": valuations}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "summary":
        print(json.dumps({"accounts": all_summaries(args.account)}, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
