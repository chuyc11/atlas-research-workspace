#!/usr/bin/env python3
"""Fail-closed exchange-session age calculations for briefing market data."""

from __future__ import annotations

import json
import re
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from typing import Any


ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def strict_json_loads(text: str, *, source: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{source} contains non-standard numeric constant {value}.")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {source}: {exc}") from exc


def configured_path(root: Path, config: dict[str, Any], value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Paper-trading market calendar is unavailable: {field} is not configured.")
    path = Path(value.strip())
    return path if path.is_absolute() else root / path


def read_calendar_json(
    root: Path,
    config: dict[str, Any],
    field: str,
    *,
    cache: dict[str, Any] | None = None,
) -> Any:
    if cache is not None and field in cache:
        return cache[field]
    calendar = config.get("market_calendar")
    if not isinstance(calendar, dict):
        raise ValueError("Paper-trading market calendar is unavailable: market_calendar is not configured.")
    path = configured_path(root, config, calendar.get(field), field=f"market_calendar.{field}")
    if not path.is_file():
        raise ValueError(f"Paper-trading market calendar is unavailable: {path} does not exist.")
    payload = strict_json_loads(path.read_text(encoding="utf-8-sig"), source=str(path))
    if cache is not None:
        cache[field] = payload
    return payload


def calendar_exchange(market_type: str, exchange: str) -> str:
    market = str(market_type or "").strip().upper()
    venue = str(exchange or "").strip().upper()
    if market == "US":
        return "US"
    if market == "HK":
        return "HKEX"
    if market != "A_SHARE":
        raise ValueError(f"Paper-trading market calendar is unavailable for market type {market_type!r}.")
    if venue in {"SH", "SSE", "SHSE", "SHANGHAI"}:
        return "SSE"
    if venue in {"SZ", "SZSE", "SHENZHEN"}:
        return "SZSE"
    if venue in {"BJ", "BSE", "BEIJING"}:
        return "BSE"
    raise ValueError(f"Paper-trading market calendar is unavailable for A-share exchange {exchange!r}.")


def coverage_bounds(raw: Any, *, exchange: str, holidays: set[str]) -> tuple[Date, Date] | None:
    if isinstance(raw, dict):
        start = raw.get("start") or raw.get("coverage_start")
        end = raw.get("end") or raw.get("coverage_end")
        if start and end:
            return (
                Date.fromisoformat(strict_iso_date(start, f"{exchange} calendar coverage start")),
                Date.fromisoformat(strict_iso_date(end, f"{exchange} calendar coverage end")),
            )
    if holidays:
        years = [Date.fromisoformat(day).year for day in holidays]
        return Date(min(years), 1, 1), Date(max(years), 12, 31)
    return None


def exchange_session_status(
    day: Date,
    *,
    market_type: str,
    exchange: str,
    config: dict[str, Any],
    root: Path,
    cache: dict[str, Any] | None = None,
) -> bool:
    calendar_name = calendar_exchange(market_type, exchange)
    if calendar_name == "US":
        payload = read_calendar_json(root, config, "us_calendar_file", cache=cache)
        if not isinstance(payload, dict) or str(payload.get("market") or "").upper() != "US":
            raise ValueError("Paper-trading US market calendar is unavailable or malformed.")
        start = Date.fromisoformat(strict_iso_date(payload.get("coverage_start"), "US calendar coverage_start"))
        end = Date.fromisoformat(strict_iso_date(payload.get("coverage_end"), "US calendar coverage_end"))
        if day < start or day > end:
            raise ValueError(
                f"Paper-trading US market calendar is unavailable for {day.isoformat()}; "
                f"coverage is {start.isoformat()} through {end.isoformat()}."
            )
        closed = {
            strict_iso_date(value, "US calendar closed date")
            for value in payload.get("closed_dates", [])
        }
        return day.weekday() < 5 and day.isoformat() not in closed

    sessions_payload = read_calendar_json(root, config, "a_share_sessions_file", cache=cache)
    rows = sessions_payload.get("rows", sessions_payload) if isinstance(sessions_payload, dict) else sessions_payload
    if not isinstance(rows, list):
        raise ValueError("Paper-trading A/HK session calendar is unavailable or malformed.")
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_exchange = str(row.get("exchange") or "").strip().upper()
        row_day = str(row.get("date") or row.get("trade_date") or "")[:10]
        if row_exchange == calendar_name and row_day == day.isoformat():
            return bool(row.get("is_trading_day", row.get("is_open", False)))

    contract = read_calendar_json(root, config, "exchange_holiday_contract_file", cache=cache)
    if not isinstance(contract, dict):
        raise ValueError("Paper-trading exchange holiday calendar is unavailable or malformed.")
    holiday_map = contract.get("explicit_holiday_list")
    raw_holidays = holiday_map.get(calendar_name) if isinstance(holiday_map, dict) else None
    if not isinstance(raw_holidays, list):
        if day.weekday() >= 5:
            return False
        raise ValueError(
            f"Paper-trading market calendar is unavailable for {calendar_name} on {day.isoformat()}."
        )
    holidays = {strict_iso_date(value, f"{calendar_name} calendar holiday") for value in raw_holidays}
    coverage_map = contract.get("coverage") if isinstance(contract.get("coverage"), dict) else {}
    bounds = coverage_bounds(coverage_map.get(calendar_name), exchange=calendar_name, holidays=holidays)
    if bounds is None or day < bounds[0] or day > bounds[1]:
        raise ValueError(
            f"Paper-trading market calendar is unavailable for {calendar_name} on {day.isoformat()}."
        )
    return day.weekday() < 5 and day.isoformat() not in holidays


def market_session_age(
    price_date: str,
    valuation_date: str,
    *,
    market_type: str,
    exchange: str,
    config: dict[str, Any],
    root: Path,
) -> int:
    price_day = Date.fromisoformat(strict_iso_date(price_date, "Price date"))
    valuation_day = Date.fromisoformat(strict_iso_date(valuation_date, "Valuation date"))
    if price_day > valuation_day:
        raise ValueError(f"Price date {price_date} cannot follow valuation date {valuation_date}.")
    cache: dict[str, Any] = {}
    if not exchange_session_status(
        price_day,
        market_type=market_type,
        exchange=exchange,
        config=config,
        root=root,
        cache=cache,
    ):
        raise ValueError(
            f"Price date {price_date} is not a trading session for {market_type}:{exchange}."
        )
    age = 0
    cursor = price_day + timedelta(days=1)
    while cursor <= valuation_day:
        if exchange_session_status(
            cursor,
            market_type=market_type,
            exchange=exchange,
            config=config,
            root=root,
            cache=cache,
        ):
            age += 1
        cursor += timedelta(days=1)
    return age
