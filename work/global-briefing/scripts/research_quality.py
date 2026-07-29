#!/usr/bin/env python3
"""Research-grade quality gates for ATLAS forecasts and briefing content.

The module deliberately separates three concepts that used to be conflated:

* operational validity: can the daily pipeline safely consume the artifact?
* editorial quality: is the report auditable and decision-dense?
* forecast validity: can resolved predictions support proper scoring/calibration?

Legacy categorical forecasts remain readable, but never enter proper scoring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
DEFAULT_SETTINGS = BRIEFING_ROOT / "config" / "settings.json"
DEFAULT_PREDICTIONS = BRIEFING_ROOT / "data" / "predictions.jsonl"
DEFAULT_DATA_DIR = BRIEFING_ROOT / "data"
DEFAULT_OUTPUTS = ROOT / "outputs"
DEFAULT_A_SHARE_CALENDAR = ROOT / "work" / "trading-core" / "data" / "equity_universe" / "trading_calendar.json"
DEFAULT_EXCHANGE_CALENDAR_CONTRACT = (
    ROOT / "work" / "trading-core" / "data" / "system" / "ashare_trading_calendar_contract.json"
)
DEFAULT_MARKET_PRICE_RECOMPUTE_ENFORCE_FROM = "2026-07-16"

HORIZON_DAYS = {"1d": 1, "1w": 7, "1m": 30}
CLOSED_STATUSES = {"validated", "partial", "wrong", "expired"}
LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
PREDICTION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-P\d{2,}$")
SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
DIRECT_MARKET_SOURCE_TERMS = (
    "market data", "quote", "snapshot", "tencent", "yahoo", "finance",
    "行情", "报价", "收盘", "快照",
)


def parse_date(value: Any) -> date_type:
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def date_on_or_after(value: Any, threshold: date_type) -> bool:
    try:
        return parse_date(value) >= threshold
    except (TypeError, ValueError):
        return False


def strict_json_loads(text: str, *, source: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{source} contains non-standard numeric constant {value}")

    return json.loads(text, parse_constant=reject_constant)


def read_json(path: Path) -> dict[str, Any]:
    value = strict_json_loads(path.read_text(encoding="utf-8-sig"), source=str(path))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def prediction_family_indexes(registry: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    """Return prediction and mapping lookup tables from the append-only family sidecar."""
    event_index: dict[str, str] = {}
    market_index: dict[str, str] = {}
    for family_id, prediction_ids in (registry or {}).get("event_families", {}).items():
        if isinstance(prediction_ids, list):
            for prediction_id in prediction_ids:
                event_index[str(prediction_id)] = str(family_id)
    for thesis_id, mapping_ids in (registry or {}).get("market_theses", {}).items():
        if isinstance(mapping_ids, list):
            for mapping_id in mapping_ids:
                market_index[str(mapping_id).upper()] = str(thesis_id)
    return event_index, market_index


def normalize_hostname(value: str) -> str:
    clean = value.rstrip(".").casefold()
    try:
        return clean.encode("idna").decode("ascii").removeprefix("www.")
    except UnicodeError:
        return ""


def auditable_url_hostname(value: Any) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or parsed.path in {"", "/"}:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    if port not in {None, default_port}:
        return ""
    return normalize_hostname(parsed.hostname or "")


def direct_auditable_url(value: Any) -> bool:
    return bool(auditable_url_hostname(value))


def canonical_original_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Use the first immutable original consistently across every consumer."""
    originals: dict[str, dict[str, Any]] = {}
    for row in records:
        prediction_id = str(row.get("prediction_id") or "")
        if prediction_id and not isinstance(row.get("review"), dict) and prediction_id not in originals:
            originals[prediction_id] = row
    return originals


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = strict_json_loads(line, source=f"{path}:{line_number}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: row is not an object")
        rows.append(value)
    return rows


def atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    text = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return True


def numeric_probability(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    probability = float(value)
    return probability if 0.0 < probability < 1.0 else None


def observed_outcome(review: dict[str, Any]) -> int | None:
    value = review.get("observed_outcome")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and float(value) in {0.0, 1.0}:
        return int(value)
    return None


def review_scope(review_record: dict[str, Any]) -> str:
    """Return the review dimension without treating asset results as event outcomes.

    Historical reviews did not declare a scope and are event reviews. New market-only
    records use ``resolution_scope=market`` and must never close or score the event.
    """
    review = review_record.get("review")
    if not isinstance(review, dict):
        return "none"
    explicit = str(review.get("resolution_scope") or "").strip().lower()
    if explicit in {"event", "market", "combined"}:
        return explicit
    has_event = observed_outcome(review) is not None
    has_market = isinstance(review.get("market_resolution"), list) and bool(review.get("market_resolution"))
    if has_event and has_market:
        return "combined"
    if has_market and not has_event:
        return "market"
    return "event"


def market_mapping_key(prediction_id: str, mapping: dict[str, Any]) -> str:
    return "|".join(
        (
            prediction_id,
            str(mapping.get("symbol") or "").strip().upper(),
            str(mapping.get("benchmark") or "").strip().upper(),
            str(mapping.get("evaluation_deadline") or "").strip(),
        )
    )


def canonical_market_thesis_key(
    prediction_id: str,
    mapping: dict[str, Any],
    market_family_index: dict[str, str] | None = None,
) -> str:
    """Collapse reciprocal rows and rolling re-statements into one independent thesis."""
    symbol = str(mapping.get("symbol") or "").strip().upper()
    benchmark = str(mapping.get("benchmark") or "").strip().upper()
    explicit = str(mapping.get("market_thesis_id") or "").strip()
    registered = (market_family_index or {}).get(f"{prediction_id}|{symbol}".upper())
    if explicit or registered:
        return explicit or str(registered)
    evaluation = mapping.get("evaluation") if isinstance(mapping.get("evaluation"), dict) else {}
    return "|".join(
        (
            prediction_id,
            *sorted((symbol, benchmark)),
            str(mapping.get("evaluation_deadline") or "").strip(),
            str(evaluation.get("window_start") or "").strip(),
            str(evaluation.get("metric") or "total_return").strip().lower(),
        )
    )


def is_asset_only_prediction(record: dict[str, Any]) -> bool:
    """Detect event records whose resolution is only a benchmark-relative return test."""
    mappings = record.get("market_mapping")
    if not isinstance(mappings, list) or not mappings:
        return False
    resolution = record.get("resolution") if isinstance(record.get("resolution"), dict) else {}
    event_text = " ".join(
        str(value or "")
        for value in (
            record.get("scenario"),
            resolution.get("question"),
            resolution.get("success_criteria"),
            resolution.get("failure_criteria"),
        )
    ).upper()
    comparison_terms = (
        "RETURN", "TOTAL RETURN", "OUTPERFORM", "UNDERPERFORM",
        "回报", "收益", "涨幅", "跌幅", "跑赢", "跑输",
    )
    if not any(term in event_text for term in comparison_terms):
        return False
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        symbol = str(mapping.get("symbol") or "").strip().upper()
        benchmark = str(mapping.get("benchmark") or "").strip().upper()
        if symbol and benchmark and symbol in event_text and benchmark in event_text:
            return True
    return False


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def instrument_market(symbol: Any) -> str:
    value = str(symbol or "").strip().upper()
    if value.endswith((".SH", ".SZ", ".BJ")):
        return "A_SHARE"
    if value.endswith(".HK"):
        return "HK"
    return "US_OR_GLOBAL"


def load_exchange_holidays(
    path: Path = DEFAULT_EXCHANGE_CALENDAR_CONTRACT,
) -> dict[str, set[str]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw = payload.get("explicit_holiday_list", {}) if isinstance(payload, dict) else {}
    return {
        str(exchange).upper(): {str(day)[:10] for day in days}
        for exchange, days in raw.items()
        if isinstance(days, list)
    }


def load_a_share_sessions(path: Path = DEFAULT_A_SHARE_CALENDAR) -> dict[tuple[str, str], bool]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    sessions: dict[tuple[str, str], bool] = {}
    if not isinstance(rows, list):
        return sessions
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("date") or row.get("trade_date") or "")[:10]
        exchange = str(row.get("exchange") or "").strip().upper()
        if day and exchange:
            sessions[(exchange, day)] = bool(row.get("is_trading_day", row.get("is_open", False)))
    return sessions


def market_session_status(day: date_type, symbol: Any) -> tuple[bool, str]:
    value = str(symbol or "").strip().upper()
    market = instrument_market(value)
    if market == "US_OR_GLOBAL":
        return day.weekday() < 5, "weekday_calendar"
    exchange = (
        "SSE"
        if value.endswith(".SH")
        else "SZSE"
        if value.endswith(".SZ")
        else "BSE"
        if value.endswith(".BJ")
        else "HKEX"
    )
    sessions = load_a_share_sessions()
    key = (exchange, day.isoformat())
    if key in sessions:
        return sessions[key], "official_exchange_session_file"
    holidays = load_exchange_holidays()
    if exchange in holidays:
        return day.weekday() < 5 and day.isoformat() not in holidays[exchange], "official_exchange_holiday_contract"
    return False, "official_exchange_calendar_unavailable"


def is_comparable_market_session(day: date_type, symbol: Any, benchmark: Any) -> bool:
    return market_session_status(day, symbol)[0] and market_session_status(day, benchmark)[0]


def previous_comparable_market_session(
    boundary: date_type,
    symbol: Any,
    benchmark: Any,
) -> date_type | None:
    candidate = boundary - timedelta(days=1)
    for _ in range(10):
        if is_comparable_market_session(candidate, symbol, benchmark):
            return candidate
        candidate -= timedelta(days=1)
    return None


def validate_market_resolution_item(
    prediction_id: str,
    item: dict[str, Any],
    review_record: dict[str, Any],
    *,
    original: dict[str, Any] | None = None,
    expected_mapping: dict[str, Any] | None = None,
    price_recompute_enforce_from_date: str = DEFAULT_MARKET_PRICE_RECOMPUTE_ENFORCE_FROM,
) -> list[str]:
    errors: list[str] = []
    prefix = f"{prediction_id}: review.market_resolution"
    for field in ("symbol", "benchmark", "evaluation_deadline", "status"):
        if not str(item.get(field) or "").strip():
            errors.append(f"{prefix}.{field} is required")
    status = str(item.get("status") or "").lower()
    if status not in {"resolved", "unresolved"}:
        errors.append(f"{prefix}.status must be resolved or unresolved")
        return errors
    try:
        evaluation_day = parse_date(item.get("evaluation_deadline"))
    except (TypeError, ValueError):
        errors.append(f"{prefix}.evaluation_deadline must be YYYY-MM-DD")
        return errors
    if status == "unresolved":
        blockers = item.get("blockers")
        if not _nonempty_list(blockers):
            errors.append(f"{prefix}.blockers must explain why the mapping is unresolved")
        return errors

    if observed_outcome(item) is None:
        errors.append(f"{prefix}.observed_outcome must be binary 0 or 1")
    for field in ("symbol_return_pct", "benchmark_return_pct", "excess_return_pct"):
        if _numeric(item.get(field)) is None:
            errors.append(f"{prefix}.{field} must be a finite number")
    price_days: dict[str, date_type] = {}
    for field in ("start_price_date", "end_price_date"):
        try:
            price_days[field] = parse_date(item.get(field))
        except (TypeError, ValueError):
            errors.append(f"{prefix}.{field} must be YYYY-MM-DD")
    start_day = price_days.get("start_price_date")
    end_day = price_days.get("end_price_date")
    symbol = str(item.get("symbol") or "").strip().upper()
    benchmark = str(item.get("benchmark") or "").strip().upper()
    if start_day and end_day:
        if start_day >= end_day:
            errors.append(f"{prefix} start_price_date must precede end_price_date")
        if not is_comparable_market_session(start_day, symbol, benchmark):
            errors.append(f"{prefix}.start_price_date is not a comparable market session")
        if not is_comparable_market_session(end_day, symbol, benchmark):
            errors.append(f"{prefix}.end_price_date is not a comparable market session")
        if end_day != evaluation_day:
            errors.append(f"{prefix}.end_price_date must equal the pre-registered evaluation deadline")
        if expected_mapping is not None and original is not None:
            evaluation = expected_mapping.get("evaluation")
            verification_rule = str(expected_mapping.get("verification_rule") or "")
            single_session_rule = any(term in verification_rule.lower() for term in ("single day", "single-day", "单日"))
            if isinstance(evaluation, dict) and evaluation.get("window_start"):
                boundary_value = evaluation.get("window_start")
            elif single_session_rule:
                boundary_value = expected_mapping.get("evaluation_deadline")
            else:
                boundary_value = original.get("date")
            try:
                boundary = parse_date(boundary_value)
            except (TypeError, ValueError):
                errors.append(f"{prefix} cannot determine the pre-registered window start")
            else:
                expected_start = previous_comparable_market_session(boundary, symbol, benchmark)
                if expected_start is None:
                    errors.append(f"{prefix} cannot resolve a comparable baseline session")
                elif start_day != expected_start:
                    errors.append(
                        f"{prefix}.start_price_date must be the last comparable session before "
                        f"the pre-registered window ({expected_start.isoformat()})"
                    )
    evidence = item.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{prefix}.evidence must be a non-empty list")
    review = review_record.get("review") if isinstance(review_record.get("review"), dict) else {}
    try:
        resolved_day = parse_date(review.get("review_date") or review_record.get("date"))
    except (TypeError, ValueError):
        errors.append(f"{prefix} requires a valid review_date")
    else:
        if resolved_day <= evaluation_day and item.get("terminal_evidence") is not True:
            errors.append(f"{prefix} is premature before the evaluation deadline has fully elapsed")
        try:
            recompute_enforce_day = parse_date(price_recompute_enforce_from_date)
        except (TypeError, ValueError):
            recompute_enforce_day = date_type.max
        if resolved_day >= recompute_enforce_day:
            raw_prices: dict[str, float] = {}
            for field in (
                "symbol_start_price",
                "symbol_end_price",
                "benchmark_start_price",
                "benchmark_end_price",
            ):
                value = _numeric(item.get(field))
                if value is None or value <= 0:
                    errors.append(f"{prefix}.{field} must be a positive finite number")
                else:
                    raw_prices[field] = value
            expected_price_field = None
            if expected_mapping is not None and isinstance(expected_mapping.get("evaluation"), dict):
                expected_price_field = str(expected_mapping["evaluation"].get("price_field") or "")
            if expected_price_field and str(item.get("price_field") or "") != expected_price_field:
                errors.append(f"{prefix}.price_field must match the pre-registered evaluation")
            if len(raw_prices) == 4:
                computed_symbol = (raw_prices["symbol_end_price"] / raw_prices["symbol_start_price"] - 1.0) * 100.0
                computed_benchmark = (
                    raw_prices["benchmark_end_price"] / raw_prices["benchmark_start_price"] - 1.0
                ) * 100.0
                computed_excess = computed_symbol - computed_benchmark
                reported = {
                    "symbol_return_pct": computed_symbol,
                    "benchmark_return_pct": computed_benchmark,
                    "excess_return_pct": computed_excess,
                }
                for field, computed in reported.items():
                    value = _numeric(item.get(field))
                    if value is not None and abs(value - computed) > 1e-5:
                        errors.append(f"{prefix}.{field} conflicts with recomputed raw-price return")
    return errors


def resolved_market_mapping_index(
    records: list[dict[str, Any]],
    *,
    price_recompute_enforce_from_date: str = DEFAULT_MARKET_PRICE_RECOMPUTE_ENFORCE_FROM,
) -> dict[str, dict[str, Any]]:
    """Index independently resolved asset mappings across every appended review."""
    resolved: dict[str, dict[str, Any]] = {}
    originals = canonical_original_index(records)
    for row in records:
        prediction_id = str(row.get("prediction_id") or "")
        review = row.get("review")
        if not prediction_id or not isinstance(review, dict):
            continue
        items = review.get("market_resolution")
        if not isinstance(items, list):
            continue
        review_day = str(review.get("review_date") or row.get("date") or "")
        for item in items:
            if not isinstance(item, dict) or str(item.get("status") or "").lower() != "resolved":
                continue
            original = originals.get(prediction_id)
            expected_mappings = {
                market_mapping_key(prediction_id, mapping): mapping
                for mapping in (original or {}).get("market_mapping", [])
                if isinstance(mapping, dict)
            }
            expected_mapping = expected_mappings.get(market_mapping_key(prediction_id, item))
            if validate_market_resolution_item(
                prediction_id,
                item,
                row,
                original=original,
                expected_mapping=expected_mapping,
                price_recompute_enforce_from_date=price_recompute_enforce_from_date,
            ):
                continue
            key = market_mapping_key(prediction_id, item)
            previous = resolved.get(key)
            previous_day = str((previous or {}).get("review_date") or "")
            if previous is None or review_day >= previous_day:
                resolved[key] = {"review_date": review_day, "record": row, "result": item}
    return resolved


def maturity_date(record: dict[str, Any]) -> date_type:
    """Return the final calendar day covered by the forecast.

    Date-only deadlines are interpreted as end-of-day in the report timezone.
    A forecast is therefore mature for a daily run only after this date has
    fully elapsed, unless terminal evidence permits an early resolution.
    """
    if record.get("deadline"):
        return parse_date(record["deadline"])
    horizon = str(record.get("horizon") or "1d").lower()
    return parse_date(record.get("date")) + timedelta(days=HORIZON_DAYS.get(horizon, 1))


def is_matured_as_of(record: dict[str, Any], cutoff: str | date_type) -> bool:
    cutoff_day = cutoff if isinstance(cutoff, date_type) else parse_date(cutoff)
    return maturity_date(record) < cutoff_day


def review_is_valid_for_resolution(original: dict[str, Any], review_record: dict[str, Any] | None) -> bool:
    if not review_record or str(review_record.get("status") or "").lower() not in CLOSED_STATUSES:
        return False
    review = review_record.get("review")
    if not isinstance(review, dict):
        return False
    if review_scope(review_record) not in {"event", "combined"}:
        return False
    if review.get("terminal_evidence") is True:
        return True
    try:
        resolved_day = parse_date(review.get("review_date") or review_record.get("date"))
    except (TypeError, ValueError):
        return False
    return resolved_day > maturity_date(original)


def original_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in records if not isinstance(row.get("review"), dict)]


def latest_reviews(
    records: list[dict[str, Any]],
    *,
    as_of: date_type | None = None,
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in records:
        prediction_id = str(row.get("prediction_id") or "")
        status = str(row.get("status") or "").lower()
        if (
            not prediction_id
            or not isinstance(row.get("review"), dict)
            or status not in CLOSED_STATUSES
            or review_scope(row) not in {"event", "combined"}
        ):
            continue
        current = latest.get(prediction_id)
        current_day = str((current or {}).get("review", {}).get("review_date") or (current or {}).get("date") or "")
        candidate_day = str(row.get("review", {}).get("review_date") or row.get("date") or "")
        if as_of is not None:
            try:
                if parse_date(candidate_day) > as_of:
                    continue
            except (TypeError, ValueError):
                continue
        if current is None or candidate_day >= current_day:
            latest[prediction_id] = row
    return latest


def _nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def validate_v2_prediction(
    record: dict[str, Any],
    *,
    machine_evaluation_enforce_from_date: str | None = None,
    event_asset_separation_enforce_from_date: str | None = None,
    independence_enforce_from_date: str | None = None,
    evidence_reproducibility_enforce_from_date: str | None = None,
) -> list[str]:
    """Validate the pre-registered v2 prediction contract."""
    errors: list[str] = []
    prediction_id = str(record.get("prediction_id") or "<missing-id>")
    if record.get("schema_version") != 2:
        errors.append(f"{prediction_id}: schema_version must be 2")
    if not PREDICTION_ID_RE.fullmatch(prediction_id):
        errors.append(f"{prediction_id}: prediction_id must match YYYY-MM-DD-PNN")
    try:
        prediction_day = parse_date(record.get("date"))
    except (TypeError, ValueError):
        prediction_day = None
        errors.append(f"{prediction_id}: date must be YYYY-MM-DD")
    enforce_independence = bool(
        independence_enforce_from_date
        and prediction_day
        and date_on_or_after(prediction_day, parse_date(independence_enforce_from_date))
    )
    enforce_reproducibility = bool(
        evidence_reproducibility_enforce_from_date
        and prediction_day
        and date_on_or_after(prediction_day, parse_date(evidence_reproducibility_enforce_from_date))
    )
    if enforce_independence:
        for field in ("event_family_id", "baseline_state", "novelty_delta", "independence_rationale"):
            if len(str(record.get(field) or "").strip()) < (6 if field == "event_family_id" else 20):
                errors.append(f"{prediction_id}: {field} is required for independent-sample governance")
    if str(record.get("horizon") or "").lower() not in HORIZON_DAYS:
        errors.append(f"{prediction_id}: horizon must be one of {sorted(HORIZON_DAYS)}")
    try:
        deadline = parse_date(record.get("deadline"))
        if prediction_day and deadline <= prediction_day:
            errors.append(f"{prediction_id}: deadline must be after date")
    except (TypeError, ValueError):
        errors.append(f"{prediction_id}: deadline must be an explicit YYYY-MM-DD date")
    if numeric_probability(record.get("probability")) is None:
        errors.append(f"{prediction_id}: probability must be numeric and strictly between 0 and 1")
    for field in ("scenario", "trigger"):
        if not str(record.get(field) or "").strip():
            errors.append(f"{prediction_id}: {field} is required")
    for field in ("verification_signals", "falsification_signals"):
        if not _nonempty_list(record.get(field)):
            errors.append(f"{prediction_id}: {field} must be a non-empty list")
    resolution = record.get("resolution")
    if not isinstance(resolution, dict):
        errors.append(f"{prediction_id}: resolution object is required")
    else:
        for field in ("question", "success_criteria", "failure_criteria"):
            if not str(resolution.get(field) or "").strip():
                errors.append(f"{prediction_id}: resolution.{field} is required")
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{prediction_id}: evidence must contain at least one source snapshot")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict) or not str(item.get("source") or "").strip() or not str(item.get("url") or "").startswith("http"):
                errors.append(f"{prediction_id}: evidence[{index}] requires source and http(s) url")
                continue
            if enforce_reproducibility and not direct_auditable_url(item.get("url")):
                errors.append(f"{prediction_id}: evidence[{index}].url must identify a direct auditable page")
            if enforce_reproducibility and not str(item.get("retrieved_at") or "").strip():
                errors.append(f"{prediction_id}: evidence[{index}].retrieved_at is required")
            source_name = str(item.get("source") or "").lower()
            is_market_snapshot = any(term in source_name for term in DIRECT_MARKET_SOURCE_TERMS)
            if enforce_reproducibility and is_market_snapshot:
                if not str(item.get("artifact_path") or "").strip():
                    errors.append(f"{prediction_id}: evidence[{index}].artifact_path is required for market data")
                if not SHA256_RE.fullmatch(str(item.get("artifact_sha256") or "")):
                    errors.append(f"{prediction_id}: evidence[{index}].artifact_sha256 must be a SHA-256 digest")
                if not str(item.get("query") or "").strip():
                    errors.append(f"{prediction_id}: evidence[{index}].query is required for market data")
    mappings = record.get("market_mapping")
    tickers = record.get("tickers")
    ticker_symbols = {
        str(item.get("symbol") if isinstance(item, dict) else item).strip().upper()
        for item in tickers if str(item.get("symbol") if isinstance(item, dict) else item).strip()
    } if isinstance(tickers, list) else set()
    enforce_event_asset_separation = False
    if event_asset_separation_enforce_from_date and prediction_day:
        try:
            enforce_event_asset_separation = prediction_day >= parse_date(event_asset_separation_enforce_from_date)
        except (TypeError, ValueError):
            pass
    if enforce_event_asset_separation and is_asset_only_prediction(record):
        errors.append(f"{prediction_id}: event resolution duplicates a market-mapping outcome")
    if ticker_symbols and (not isinstance(mappings, list) or not mappings):
        errors.append(f"{prediction_id}: non-empty market_mapping is required when tickers are named")
    elif isinstance(mappings, list):
        mapped_symbols: set[str] = set()
        canonical_mapping_keys: set[str] = set()
        for index, item in enumerate(mappings):
            if not isinstance(item, dict):
                errors.append(f"{prediction_id}: market_mapping[{index}] must be an object")
                continue
            for field in ("symbol", "direction", "benchmark", "verification_rule", "evaluation_deadline"):
                if not str(item.get(field) or "").strip():
                    errors.append(f"{prediction_id}: market_mapping[{index}].{field} is required")
            if enforce_independence and len(str(item.get("market_thesis_id") or "").strip()) < 6:
                errors.append(f"{prediction_id}: market_mapping[{index}].market_thesis_id is required")
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol:
                mapped_symbols.add(symbol)
            if enforce_event_asset_separation:
                canonical_key = canonical_market_thesis_key(prediction_id, item)
                if canonical_key in canonical_mapping_keys:
                    errors.append(
                        f"{prediction_id}: market_mapping[{index}] duplicates a reciprocal market thesis"
                    )
                canonical_mapping_keys.add(canonical_key)
            if str(item.get("direction") or "") not in {"up", "down", "outperform", "underperform", "neutral"}:
                errors.append(f"{prediction_id}: market_mapping[{index}].direction is invalid")
            mapping_deadline = None
            try:
                mapping_deadline = parse_date(item.get("evaluation_deadline"))
                if prediction_day and mapping_deadline <= prediction_day:
                    errors.append(f"{prediction_id}: market_mapping[{index}].evaluation_deadline must be after date")
                if not is_comparable_market_session(mapping_deadline, item.get("symbol"), item.get("benchmark")):
                    errors.append(
                        f"{prediction_id}: market_mapping[{index}].evaluation_deadline must be a comparable market session"
                    )
            except (TypeError, ValueError):
                errors.append(f"{prediction_id}: market_mapping[{index}].evaluation_deadline must be YYYY-MM-DD")
            evaluation = item.get("evaluation")
            require_machine_evaluation = False
            if machine_evaluation_enforce_from_date and prediction_day:
                try:
                    require_machine_evaluation = prediction_day >= parse_date(machine_evaluation_enforce_from_date)
                except (TypeError, ValueError):
                    pass
            if require_machine_evaluation and not isinstance(evaluation, dict):
                errors.append(f"{prediction_id}: market_mapping[{index}].evaluation is required for machine resolution")
            if isinstance(evaluation, dict):
                if str(evaluation.get("metric") or "") != "total_return":
                    errors.append(f"{prediction_id}: market_mapping[{index}].evaluation.metric must be total_return")
                if str(evaluation.get("price_field") or "") not in {"adjusted_close", "close"}:
                    errors.append(f"{prediction_id}: market_mapping[{index}].evaluation.price_field is invalid")
                comparisons = {
                    "symbol_gt_benchmark", "symbol_lt_benchmark", "symbol_return_gt",
                    "symbol_return_lt", "abs_symbol_return_lte",
                }
                if str(evaluation.get("comparison") or "") not in comparisons:
                    errors.append(f"{prediction_id}: market_mapping[{index}].evaluation.comparison is invalid")
                expected_comparison = {
                    "outperform": "symbol_gt_benchmark",
                    "underperform": "symbol_lt_benchmark",
                    "up": "symbol_return_gt",
                    "down": "symbol_return_lt",
                    "neutral": "abs_symbol_return_lte",
                }.get(str(item.get("direction") or ""))
                if expected_comparison and str(evaluation.get("comparison") or "") != expected_comparison:
                    errors.append(
                        f"{prediction_id}: market_mapping[{index}].evaluation.comparison conflicts with direction"
                    )
                if expected_comparison == "abs_symbol_return_lte" and _numeric(evaluation.get("threshold_pct")) is None:
                    errors.append(f"{prediction_id}: neutral mapping requires numeric evaluation.threshold_pct")
                try:
                    window_start = parse_date(evaluation.get("window_start"))
                    if prediction_day and window_start < prediction_day:
                        errors.append(f"{prediction_id}: market_mapping[{index}].evaluation.window_start cannot precede date")
                    if mapping_deadline and window_start > mapping_deadline:
                        errors.append(f"{prediction_id}: market_mapping[{index}].evaluation.window_start exceeds deadline")
                except (TypeError, ValueError):
                    errors.append(f"{prediction_id}: market_mapping[{index}].evaluation.window_start must be YYYY-MM-DD")
        for missing_symbol in sorted(ticker_symbols - mapped_symbols):
            errors.append(f"{prediction_id}: ticker {missing_symbol} has no market_mapping")
    return errors


def validate_v2_review(
    record: dict[str, Any],
    original: dict[str, Any] | None,
    *,
    price_recompute_enforce_from_date: str = DEFAULT_MARKET_PRICE_RECOMPUTE_ENFORCE_FROM,
) -> list[str]:
    """Validate a resolution record against its immutable original forecast."""
    prediction_id = str(record.get("prediction_id") or "<missing-id>")
    errors: list[str] = []
    if original is None:
        return [f"{prediction_id}: review has no original prediction"]
    review = record.get("review")
    if not isinstance(review, dict):
        return [f"{prediction_id}: review object is required"]
    scope = review_scope(record)
    if scope not in {"event", "market", "combined"}:
        errors.append(f"{prediction_id}: review.resolution_scope must be event, market, or combined")
    status = str(record.get("status") or "").lower()
    if scope in {"event", "combined"} and status not in CLOSED_STATUSES:
        errors.append(f"{prediction_id}: event review status must be one of {sorted(CLOSED_STATUSES)}")
    if scope == "market" and status != "active":
        errors.append(f"{prediction_id}: market-only review status must be active")
    try:
        resolved_day = parse_date(review.get("review_date") or record.get("date"))
    except (TypeError, ValueError):
        errors.append(f"{prediction_id}: review.review_date must be YYYY-MM-DD")
        resolved_day = None
    if scope in {"event", "combined"}:
        if observed_outcome(review) is None:
            errors.append(f"{prediction_id}: review.observed_outcome must be binary 0 or 1")
        evidence = review.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{prediction_id}: review.evidence must be a non-empty list")
        if resolved_day and resolved_day <= maturity_date(original) and review.get("terminal_evidence") is not True:
            errors.append(f"{prediction_id}: premature review requires terminal_evidence=true")
    if scope in {"market", "combined"}:
        market_results = review.get("market_resolution")
        if not isinstance(market_results, list) or not market_results:
            errors.append(f"{prediction_id}: review.market_resolution must be a non-empty list")
        else:
            original_mappings = {
                market_mapping_key(prediction_id, item): item
                for item in original.get("market_mapping", [])
                if isinstance(item, dict)
            }
            for item in market_results:
                if not isinstance(item, dict):
                    errors.append(f"{prediction_id}: review.market_resolution entries must be objects")
                    continue
                item_key = market_mapping_key(prediction_id, item)
                expected_mapping = original_mappings.get(item_key)
                errors.extend(
                    validate_market_resolution_item(
                        prediction_id,
                        item,
                        record,
                        original=original,
                        expected_mapping=expected_mapping,
                        price_recompute_enforce_from_date=price_recompute_enforce_from_date,
                    )
                )
                if expected_mapping is None:
                    errors.append(f"{prediction_id}: market resolution does not match a pre-registered mapping")
                    continue
                if str(item.get("status") or "").lower() != "resolved":
                    continue
                symbol_return = _numeric(item.get("symbol_return_pct"))
                benchmark_return = _numeric(item.get("benchmark_return_pct"))
                excess_return = _numeric(item.get("excess_return_pct"))
                if (
                    symbol_return is not None
                    and benchmark_return is not None
                    and excess_return is not None
                    and abs(excess_return - (symbol_return - benchmark_return)) > 1e-5
                ):
                    errors.append(f"{prediction_id}: market resolution excess return is arithmetically inconsistent")
                evaluation = expected_mapping.get("evaluation")
                if isinstance(evaluation, dict) and symbol_return is not None and benchmark_return is not None:
                    threshold = _numeric(evaluation.get("threshold_pct")) or 0.0
                    excess = symbol_return - benchmark_return
                    comparison = str(evaluation.get("comparison") or "")
                    computed = {
                        "symbol_gt_benchmark": excess > threshold,
                        "symbol_lt_benchmark": excess < -threshold,
                        "symbol_return_gt": symbol_return > threshold,
                        "symbol_return_lt": symbol_return < -threshold,
                        "abs_symbol_return_lte": abs(symbol_return) <= threshold,
                    }.get(comparison)
                    if computed is not None and observed_outcome(item) != int(computed):
                        errors.append(f"{prediction_id}: market resolution outcome conflicts with pre-registered rule")
    return errors


def invalid_market_review_is_superseded(
    review_row: dict[str, Any],
    all_review_rows: list[dict[str, Any]],
    original: dict[str, Any],
    *,
    price_recompute_enforce_from_date: str = DEFAULT_MARKET_PRICE_RECOMPUTE_ENFORCE_FROM,
    as_of: date_type | None = None,
) -> bool:
    """Keep append-only audit history without permanently blocking a corrected mapping.

    Only a strictly later, fully valid market/combined review can supersede an invalid
    market-only review, and it must replace every mapping contained in the invalid row.
    The original errors remain visible as diagnostics; they are merely removed from the
    current operational gate once an auditable correction exists.
    """
    if review_scope(review_row) != "market":
        return False
    review = review_row.get("review")
    if not isinstance(review, dict):
        return False
    invalid_items = review.get("market_resolution")
    if not isinstance(invalid_items, list) or not invalid_items:
        return False
    prediction_id = str(review_row.get("prediction_id") or "")
    invalid_keys = {
        market_mapping_key(prediction_id, item)
        for item in invalid_items
        if isinstance(item, dict)
    }
    if not invalid_keys:
        return False
    try:
        invalid_day = parse_date(review.get("review_date") or review_row.get("date"))
    except (TypeError, ValueError):
        return False

    corrected_keys: set[str] = set()
    for candidate in all_review_rows:
        if candidate is review_row or str(candidate.get("prediction_id") or "") != prediction_id:
            continue
        if review_scope(candidate) not in {"market", "combined"}:
            continue
        candidate_review = candidate.get("review")
        if not isinstance(candidate_review, dict):
            continue
        try:
            candidate_day = parse_date(candidate_review.get("review_date") or candidate.get("date"))
        except (TypeError, ValueError):
            continue
        if candidate_day <= invalid_day or (as_of is not None and candidate_day > as_of):
            continue
        if validate_v2_review(
            candidate,
            original,
            price_recompute_enforce_from_date=price_recompute_enforce_from_date,
        ):
            continue
        candidate_items = candidate_review.get("market_resolution")
        if not isinstance(candidate_items, list):
            continue
        corrected_keys.update(
            market_mapping_key(prediction_id, item)
            for item in candidate_items
            if isinstance(item, dict) and str(item.get("status") or "").lower() == "resolved"
        )
    return invalid_keys.issubset(corrected_keys)


def proper_scoring_metrics(
    records: list[dict[str, Any]],
    *,
    cutoff: str,
    minimum_sample: int = 30,
    maximum_brier: float = 0.25,
    maximum_ece: float = 0.15,
    family_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cutoff_day = parse_date(cutoff)
    originals = original_records(records)
    original_by_id: dict[str, dict[str, Any]] = {}
    for row in originals:
        prediction_id = str(row.get("prediction_id") or "")
        if prediction_id and prediction_id not in original_by_id:
            original_by_id[prediction_id] = row
    reviews = latest_reviews(records, as_of=cutoff_day)
    all_matured = {key: row for key, row in original_by_id.items() if is_matured_as_of(row, cutoff_day)}
    matured_v2 = {
        key: row
        for key, row in all_matured.items()
        if row.get("schema_version") == 2
    }
    asset_only_matured = {
        key: row for key, row in matured_v2.items() if is_asset_only_prediction(row)
    }
    matured = {
        key: row for key, row in matured_v2.items() if key not in asset_only_matured
    }
    legacy_matured_count = len(all_matured) - len(matured_v2)
    samples: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    event_family_index, _market_family_index = prediction_family_indexes(family_registry)
    if asset_only_matured:
        exclusion_counts["asset_only_prediction"] = len(asset_only_matured)

    for prediction_id, original in matured.items():
        review_row = reviews.get(prediction_id)
        if not review_row:
            exclusion_counts["missing_review"] += 1
            continue
        probability = numeric_probability(original.get("probability"))
        if probability is None:
            exclusion_counts["non_numeric_probability"] += 1
            continue
        review = review_row.get("review", {})
        outcome = observed_outcome(review)
        if outcome is None:
            exclusion_counts["missing_binary_observed_outcome"] += 1
            continue
        try:
            resolved_day = parse_date(review.get("review_date") or review_row.get("date"))
        except (TypeError, ValueError):
            exclusion_counts["invalid_review_date"] += 1
            continue
        terminal = review.get("terminal_evidence") is True
        if resolved_day <= maturity_date(original) and not terminal:
            exclusion_counts["early_closure_without_terminal_evidence"] += 1
            continue
        if not _nonempty_list(review.get("evidence")):
            exclusion_counts["missing_resolution_evidence"] += 1
            continue
        clipped = min(max(probability, 1e-12), 1 - 1e-12)
        brier = (probability - outcome) ** 2
        log_loss = -(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped))
        samples.append(
            {
                "prediction_id": prediction_id,
                "event_family_id": str(original.get("event_family_id") or event_family_index.get(prediction_id) or prediction_id),
                "probability": probability,
                "observed_outcome": outcome,
                "brier": round(brier, 6),
                "log_loss": round(log_loss, 6),
            }
        )

    family_sample_counts = Counter(item["event_family_id"] for item in samples)
    for item in samples:
        item["independence_weight"] = round(1.0 / family_sample_counts[item["event_family_id"]], 8)
    independent_family_count = len(family_sample_counts)
    matured_family_ids = {
        str(row.get("event_family_id") or event_family_index.get(prediction_id) or prediction_id)
        for prediction_id, row in matured.items()
    }
    repeated_sample_count = len(samples) - independent_family_count
    if repeated_sample_count:
        exclusion_counts["rolling_family_restatement_downweighted"] = repeated_sample_count

    calibration_bins = []
    ece = None
    if samples:
        weighted_gap = 0.0
        total_weight = sum(float(item["independence_weight"]) for item in samples)
        for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
            upper = lower + 0.2
            members = [
                item for item in samples
                if lower <= item["probability"] < upper or (upper == 1.0 and item["probability"] == 1.0)
            ]
            if not members:
                continue
            bin_weight = sum(float(item["independence_weight"]) for item in members)
            mean_probability = sum(item["probability"] * float(item["independence_weight"]) for item in members) / bin_weight
            outcome_rate = sum(item["observed_outcome"] * float(item["independence_weight"]) for item in members) / bin_weight
            gap = abs(mean_probability - outcome_rate)
            weighted_gap += gap * bin_weight / total_weight
            calibration_bins.append(
                {
                    "range": [round(lower, 1), round(upper, 1)],
                    "count": len(members),
                    "independence_weight": round(bin_weight, 6),
                    "mean_probability": round(mean_probability, 4),
                    "observed_rate": round(outcome_rate, 4),
                    "absolute_gap": round(gap, 4),
                }
            )
        ece = round(weighted_gap, 6)

    total_weight = sum(float(item.get("independence_weight", 0.0)) for item in samples)
    mean_brier = round(sum(item["brier"] * float(item["independence_weight"]) for item in samples) / total_weight, 6) if total_weight else None
    mean_log_loss = round(sum(item["log_loss"] * float(item["independence_weight"]) for item in samples) / total_weight, 6) if total_weight else None
    resolved_coverage = independent_family_count / len(matured_family_ids) if matured_family_ids else 0.0
    gates = {
        "minimum_sample": independent_family_count >= minimum_sample,
        "resolved_coverage_at_least_80pct": resolved_coverage >= 0.8,
        "brier_at_or_below_threshold": mean_brier is not None and mean_brier <= maximum_brier,
        "ece_at_or_below_threshold": ece is not None and ece <= maximum_ece,
    }
    return {
        "metric_standard": "binary proper scoring with unit weight per independent event family; lower is better",
        "cutoff": cutoff,
        "matured_prediction_count": len(matured),
        "matured_independent_event_family_count": len(matured_family_ids),
        "matured_v2_prediction_count": len(matured_v2),
        "asset_only_matured_prediction_count_excluded": len(asset_only_matured),
        "legacy_matured_prediction_count_excluded": legacy_matured_count,
        "total_matured_prediction_count": len(all_matured),
        "eligible_sample_count": len(samples),
        "independent_event_family_count": independent_family_count,
        "rolling_restatement_count_downweighted": repeated_sample_count,
        "resolved_coverage_pct": round(resolved_coverage * 100, 2),
        "brier_score": mean_brier,
        "log_loss": mean_log_loss,
        "expected_calibration_error": ece,
        "calibration_bins": calibration_bins,
        "exclusion_counts": dict(sorted(exclusion_counts.items())),
        "thresholds": {
            "minimum_sample": minimum_sample,
            "minimum_sample_unit": "independent_event_family",
            "maximum_brier": maximum_brier,
            "maximum_expected_calibration_error": maximum_ece,
            "minimum_resolved_coverage_pct": 80.0,
        },
        "gates": gates,
        "is_research_ready": all(gates.values()),
        "samples": samples,
    }


def calibration_recovery_guidance(
    metrics: dict[str, Any],
    recovery_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn proper-scoring state into bounded, non-mutating next-run guidance."""

    config = recovery_config or {}
    thresholds = metrics.get("thresholds") if isinstance(metrics.get("thresholds"), dict) else {}
    minimum_sample = max(1, int(thresholds.get("minimum_sample") or 30))
    independent_families = max(0, int(metrics.get("independent_event_family_count") or 0))
    family_gap = max(0, minimum_sample - independent_families)
    configured_novel_target = max(
        0,
        int(config.get("minimum_novel_event_families_per_daily_batch") or 0),
    )
    novel_target = min(configured_novel_target, family_gap)

    signed_gaps: list[tuple[float, float]] = []
    for item in metrics.get("calibration_bins") or []:
        if not isinstance(item, dict):
            continue
        try:
            weight = float(item.get("independence_weight") or 0.0)
            signed_gap = float(item.get("observed_rate")) - float(item.get("mean_probability"))
        except (TypeError, ValueError):
            continue
        if weight > 0:
            signed_gaps.append((signed_gap, weight))
    total_weight = sum(weight for _gap, weight in signed_gaps)
    weighted_signed_gap = (
        sum(gap * weight for gap, weight in signed_gaps) / total_weight
        if total_weight
        else None
    )
    minimum_feedback_families = max(
        1,
        int(config.get("minimum_feedback_independent_event_families") or 8),
    )
    if weighted_signed_gap is None or independent_families < minimum_feedback_families:
        bias = "insufficient_evidence"
    elif weighted_signed_gap > 0:
        bias = "underconfident"
    elif weighted_signed_gap < 0:
        bias = "overconfident"
    else:
        bias = "balanced"

    gates = metrics.get("gates") if isinstance(metrics.get("gates"), dict) else {}
    status = "research_ready" if metrics.get("is_research_ready") is True else "shadow"
    return {
        "status": status,
        "gate_mode": str(config.get("gate_mode") or "shadow"),
        "cutoff": metrics.get("cutoff"),
        "automatic_probability_rewrite_allowed": False,
        "historical_prediction_rewrite_allowed": False,
        "independent_event_family_count": independent_families,
        "minimum_independent_event_families": minimum_sample,
        "independent_event_family_gap": family_gap,
        "minimum_novel_event_families_per_daily_batch": novel_target,
        "maximum_rolling_restatements_per_daily_batch": max(
            0,
            int(config.get("maximum_rolling_restatements_per_daily_batch") or 0),
        ),
        "estimated_daily_batches_to_sample_gate": (
            math.ceil(family_gap / novel_target) if family_gap and novel_target else None
        ),
        "weighted_signed_calibration_gap": (
            round(weighted_signed_gap, 6) if weighted_signed_gap is not None else None
        ),
        "calibration_bias": bias,
        "feedback_confidence": (
            "low"
            if independent_families < minimum_sample
            else "moderate"
        ),
        "failed_gates": sorted(name for name, passed in gates.items() if passed is not True),
        "operating_rules": [
            "Prefer genuinely decision-useful new event families while the independent-family gate is short.",
            "Keep rolled deadlines, paraphrases, and unchanged causal propositions in their existing event family.",
            "Do not create filler forecasts merely to increase the sample count.",
            "Treat calibration bias as advisory until the configured independent-family threshold is met.",
            "Never rewrite historical probabilities or lower promotion thresholds to clear shadow status.",
        ],
    }


def market_mapping_metrics(
    records: list[dict[str, Any]],
    *,
    cutoff: str,
    family_registry: dict[str, Any] | None = None,
    price_recompute_enforce_from_date: str = DEFAULT_MARKET_PRICE_RECOMPUTE_ENFORCE_FROM,
) -> dict[str, Any]:
    """Score asset mappings independently from probabilistic event forecasts."""
    cutoff_day = parse_date(cutoff)
    originals = original_records(records)
    matured_groups: dict[str, list[dict[str, Any]]] = {}
    _event_family_index, market_family_index = prediction_family_indexes(family_registry)
    for original in originals:
        if original.get("schema_version") != 2:
            continue
        prediction_id = str(original.get("prediction_id") or "")
        for mapping in original.get("market_mapping", []):
            if not prediction_id or not isinstance(mapping, dict):
                continue
            try:
                evaluation_day = parse_date(mapping.get("evaluation_deadline"))
            except (TypeError, ValueError):
                continue
            if evaluation_day < cutoff_day:
                canonical_key = canonical_market_thesis_key(prediction_id, mapping, market_family_index)
                matured_groups.setdefault(canonical_key, []).append({
                    "prediction_id": prediction_id,
                    "mapping": mapping,
                    "mapping_id": market_mapping_key(prediction_id, mapping),
                })

    resolved_index = resolved_market_mapping_index(
        records,
        price_recompute_enforce_from_date=price_recompute_enforce_from_date,
    )
    samples: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    reciprocal_duplicate_count = sum(max(0, len(group) - 1) for group in matured_groups.values())
    if reciprocal_duplicate_count:
        exclusions["reciprocal_duplicate_mapping"] = reciprocal_duplicate_count
    direction_counts: Counter[str] = Counter()
    direction_hits: Counter[str] = Counter()
    for group in matured_groups.values():
        expected = next((item for item in group if item["mapping_id"] in resolved_index), group[0])
        mapping_id = expected["mapping_id"]
        resolved = resolved_index.get(mapping_id)
        if not resolved:
            exclusions["missing_market_resolution"] += 1
            continue
        result = resolved["result"]
        outcome = observed_outcome(result)
        symbol_return = _numeric(result.get("symbol_return_pct"))
        benchmark_return = _numeric(result.get("benchmark_return_pct"))
        excess_return = _numeric(result.get("excess_return_pct"))
        if outcome is None or symbol_return is None or benchmark_return is None or excess_return is None:
            exclusions["invalid_market_resolution"] += 1
            continue
        mapping = expected["mapping"]
        direction = str(mapping.get("direction") or "unknown")
        signed_performance = None
        if direction == "outperform":
            signed_performance = excess_return
        elif direction == "underperform":
            signed_performance = -excess_return
        elif direction == "up":
            signed_performance = symbol_return
        elif direction == "down":
            signed_performance = -symbol_return
        direction_counts[direction] += 1
        direction_hits[direction] += outcome
        samples.append({
            "mapping_id": mapping_id,
            "prediction_id": expected["prediction_id"],
            "symbol": mapping.get("symbol"),
            "benchmark": mapping.get("benchmark"),
            "direction": direction,
            "evaluation_deadline": mapping.get("evaluation_deadline"),
            "observed_outcome": outcome,
            "symbol_return_pct": round(symbol_return, 6),
            "benchmark_return_pct": round(benchmark_return, 6),
            "excess_return_pct": round(excess_return, 6),
            "signed_performance_pct": round(signed_performance, 6) if signed_performance is not None else None,
            "review_date": resolved.get("review_date"),
        })

    resolved_count = len(samples)
    matured_count = len(matured_groups)
    signed = [item["signed_performance_pct"] for item in samples if item["signed_performance_pct"] is not None]
    by_direction = {
        direction: {
            "resolved_count": count,
            "hit_rate_pct": round(direction_hits[direction] / count * 100, 2) if count else None,
        }
        for direction, count in sorted(direction_counts.items())
    }
    return {
        "metric_standard": "independent benchmark-relative mapping evaluation; event probability is not reused",
        "cutoff": cutoff,
        "matured_mapping_count": matured_count,
        "resolved_mapping_count": resolved_count,
        "reciprocal_duplicate_mapping_count_excluded": reciprocal_duplicate_count,
        "resolved_coverage_pct": round(resolved_count / matured_count * 100, 2) if matured_count else 0.0,
        "hit_rate_pct": round(sum(item["observed_outcome"] for item in samples) / resolved_count * 100, 2) if resolved_count else None,
        "mean_signed_performance_pct": round(sum(signed) / len(signed), 6) if signed else None,
        "by_direction": by_direction,
        "exclusion_counts": dict(sorted(exclusions.items())),
        "samples": samples,
    }


def audit_prediction_records(
    records: list[dict[str, Any]],
    *,
    cutoff: str,
    enforce_from_date: str,
    evaluation_config: dict[str, Any] | None = None,
    review_policy: dict[str, Any] | None = None,
    prediction_contract: dict[str, Any] | None = None,
    family_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    originals = original_records(records)
    reviews = latest_reviews(records, as_of=parse_date(cutoff))
    all_review_rows = [row for row in records if isinstance(row.get("review"), dict)]
    counts = Counter(str(row.get("prediction_id") or "") for row in originals)
    duplicate_original_ids = sorted(key for key, count in counts.items() if key and count > 1)
    original_by_id: dict[str, dict[str, Any]] = {}
    for row in originals:
        prediction_id = str(row.get("prediction_id") or "")
        if prediction_id and prediction_id not in original_by_id:
            original_by_id[prediction_id] = row
    orphan_reviews = sorted({
        str(row.get("prediction_id") or "")
        for row in all_review_rows
        if str(row.get("prediction_id") or "") not in original_by_id
    } - {""})
    current_rows = [row for row in originals if str(row.get("date") or "") == cutoff]
    enforce_day = parse_date(enforce_from_date)
    v2_errors: list[str] = []
    v2_review_errors: list[str] = []
    superseded_v2_review_errors: list[str] = []
    v2_applicable = []
    for row in originals:
        try:
            row_day = parse_date(row.get("date"))
        except (TypeError, ValueError):
            continue
        if row_day >= enforce_day:
            v2_applicable.append(row)
            v2_errors.extend(validate_v2_prediction(
                row,
                machine_evaluation_enforce_from_date=str(
                    (review_policy or {}).get("machine_evaluation_enforce_from_date") or ""
                ) or None,
                event_asset_separation_enforce_from_date=str(
                    (review_policy or {}).get("event_asset_separation_enforce_from_date") or ""
                ) or None,
                independence_enforce_from_date=str(
                    (prediction_contract or {}).get("independence_enforce_from_date") or ""
                ) or None,
                evidence_reproducibility_enforce_from_date=str(
                    (prediction_contract or {}).get("evidence_reproducibility_enforce_from_date") or ""
                ) or None,
            ))
    review_validation_results: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
    price_recompute_enforce_from_date = str(
        (review_policy or {}).get("market_resolution_price_recompute_enforce_from_date")
        or DEFAULT_MARKET_PRICE_RECOMPUTE_ENFORCE_FROM
    )
    for review_row in all_review_rows:
        prediction_id = str(review_row.get("prediction_id") or "")
        original = original_by_id.get(prediction_id)
        if not original:
            continue
        try:
            original_day = parse_date(original.get("date"))
        except (TypeError, ValueError):
            continue
        if original_day >= enforce_day:
            review_validation_results.append(
                (
                    review_row,
                    original,
                    validate_v2_review(
                        review_row,
                        original,
                        price_recompute_enforce_from_date=price_recompute_enforce_from_date,
                    ),
                )
            )
    for review_row, original, errors in review_validation_results:
        if errors and invalid_market_review_is_superseded(
            review_row,
            all_review_rows,
            original,
            price_recompute_enforce_from_date=price_recompute_enforce_from_date,
            as_of=parse_date(cutoff),
        ):
            superseded_v2_review_errors.extend(errors)
        else:
            v2_review_errors.extend(errors)
    v2_duplicate_ids = sorted(
        prediction_id
        for prediction_id in duplicate_original_ids
        if any(
            str(row.get("prediction_id") or "") == prediction_id
            and date_on_or_after(row.get("date"), enforce_day)
            for row in originals
        )
    )
    v2_orphan_reviews = sorted(
        prediction_id
        for prediction_id in orphan_reviews
        if any(
            str(row.get("prediction_id") or "") == prediction_id
            and date_on_or_after(
                row.get("review", {}).get("review_date") or row.get("date"),
                enforce_day,
            )
            for row in all_review_rows
        )
    )

    early_closed = []
    for prediction_id, review_row in reviews.items():
        original = original_by_id.get(prediction_id)
        if not original:
            continue
        review = review_row.get("review", {})
        try:
            review_day = parse_date(review.get("review_date") or review_row.get("date"))
        except (TypeError, ValueError):
            continue
        if review_day <= maturity_date(original) and review.get("terminal_evidence") is not True:
            early_closed.append(prediction_id)

    matured_v2 = {
        prediction_id: original
        for prediction_id, original in original_by_id.items()
        if original.get("schema_version") == 2 and is_matured_as_of(original, cutoff)
    }
    unresolved_matured_v2 = sorted(
        prediction_id
        for prediction_id, original in matured_v2.items()
        if not review_is_valid_for_resolution(original, reviews.get(prediction_id))
    )
    mapping_metrics = market_mapping_metrics(
        records,
        cutoff=cutoff,
        family_registry=family_registry,
        price_recompute_enforce_from_date=price_recompute_enforce_from_date,
    )
    resolved_mapping_keys = set(
        resolved_market_mapping_index(
            records,
            price_recompute_enforce_from_date=price_recompute_enforce_from_date,
        )
    )
    unresolved_matured_v2_mappings: list[str] = []
    for prediction_id, original in original_by_id.items():
        if original.get("schema_version") != 2:
            continue
        for mapping in original.get("market_mapping", []):
            if not isinstance(mapping, dict):
                continue
            try:
                is_due = parse_date(mapping.get("evaluation_deadline")) < parse_date(cutoff)
            except (TypeError, ValueError):
                continue
            key = market_mapping_key(prediction_id, mapping)
            if is_due and key not in resolved_mapping_keys:
                unresolved_matured_v2_mappings.append(key)
    unresolved_matured_v2_mappings.sort()

    eval_config = evaluation_config or {}
    metrics = proper_scoring_metrics(
        records,
        cutoff=cutoff,
        minimum_sample=int(eval_config.get("minimum_sample", 30)),
        maximum_brier=float(eval_config.get("maximum_brier", 0.25)),
        maximum_ece=float(eval_config.get("maximum_expected_calibration_error", 0.15)),
        family_registry=family_registry,
    )
    operational_errors = []
    if v2_duplicate_ids:
        operational_errors.append("duplicate v2 original prediction IDs")
    if v2_orphan_reviews:
        operational_errors.append("orphan v2 review records")
    operational_errors.extend(v2_errors)
    operational_errors.extend(v2_review_errors)
    if (review_policy or {}).get("block_deployment_on_unresolved_due_v2") is True and unresolved_matured_v2:
        operational_errors.append(
            "matured v2 predictions require valid resolution reviews: " + ", ".join(unresolved_matured_v2)
        )
    market_block_from = str(
        (review_policy or {}).get("block_deployment_on_unresolved_due_v2_market_from_date") or "0001-01-01"
    )
    enforce_market_debt = date_on_or_after(cutoff, parse_date(market_block_from))
    if (
        (review_policy or {}).get("block_deployment_on_unresolved_due_v2_market") is True
        and enforce_market_debt
        and unresolved_matured_v2_mappings
    ):
        operational_errors.append(
            "matured v2 market mappings require valid independent resolutions: "
            + ", ".join(unresolved_matured_v2_mappings)
        )
    numeric_count = sum(numeric_probability(row.get("probability")) is not None for row in originals)
    return {
        "cutoff": cutoff,
        "record_count": len(records),
        "original_prediction_count": len(originals),
        "reviewed_prediction_count": len(reviews),
        "current_prediction_count": len(current_rows),
        "duplicate_original_prediction_ids": duplicate_original_ids,
        "orphan_review_prediction_ids": orphan_reviews,
        "legacy_integrity_warnings": [
            message
            for condition, message in (
                (bool(duplicate_original_ids), "legacy store contains duplicate original IDs; first record is canonical"),
                (bool(orphan_reviews), "legacy store contains orphan review records"),
            )
            if condition
        ],
        "early_closed_without_terminal_evidence": sorted(early_closed),
        "matured_v2_prediction_ids": sorted(matured_v2),
        "unresolved_matured_v2_prediction_ids": unresolved_matured_v2,
        "numeric_probability_coverage_pct": round(numeric_count / len(originals) * 100, 2) if originals else None,
        "v2_contract": {
            "enforce_from_date": enforce_from_date,
            "applicable_prediction_count": len(v2_applicable),
            "valid_prediction_count": len(v2_applicable) - len({error.split(":", 1)[0] for error in v2_errors}),
            "errors": v2_errors,
            "review_errors": v2_review_errors,
            "superseded_review_errors": superseded_v2_review_errors,
        },
        "operational_errors": operational_errors,
        "operational_passed": not operational_errors,
        "proper_scoring": metrics,
        "event_scoring": metrics,
        "market_mapping_scoring": mapping_metrics,
        "unresolved_matured_v2_market_mapping_ids": unresolved_matured_v2_mappings,
    }


def core_story_blocks(text: str) -> list[dict[str, str]]:
    matches = list(re.finditer(r"^###\s+核心主线[：:]\s*(.+?)\s*$", text, re.MULTILINE))
    blocks: list[dict[str, str]] = []
    for match in matches:
        next_heading = re.search(r"^#{1,3}\s+", text[match.end():], re.MULTILINE)
        end = match.end() + next_heading.start() if next_heading else len(text)
        blocks.append({"title": match.group(1).strip(), "body": text[match.end():end]})
    return blocks


def normalized_domain(link: str) -> str:
    return auditable_url_hostname(link)


MULTI_LABEL_PUBLIC_SUFFIXES = {
    "ac.uk",
    "co.jp",
    "co.uk",
    "com.au",
    "com.br",
    "com.cn",
    "com.hk",
    "com.sg",
    "edu.au",
    "gov.au",
    "gov.cn",
    "gov.uk",
    "net.au",
    "org.au",
    "org.cn",
    "org.uk",
}


def registrable_domain(domain: str) -> str:
    clean = normalize_hostname(domain)
    labels = [label for label in clean.split(".") if label]
    if len(labels) <= 2:
        return clean
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in MULTI_LABEL_PUBLIC_SUFFIXES else suffix


def source_family(domain: str, aliases: dict[str, Any]) -> str:
    clean = normalize_hostname(domain)
    for alias, family in aliases.items():
        normalized_alias = normalize_hostname(str(alias))
        if clean == normalized_alias or clean.endswith("." + normalized_alias):
            return str(family)
    return registrable_domain(clean)


def evidence_role_links(role_text: str, label: str, story_links: set[str]) -> set[str]:
    match = re.search(
        rf"{re.escape(label)}\s*=\s*(.*?)(?=\s*[；;]\s*(?:一手来源|事件地区来源|外部核验)\s*=|$)",
        role_text,
    )
    if not match:
        return set()
    return {
        link
        for link in LINK_RE.findall(match.group(1))
        if link in story_links and direct_auditable_url(link)
    }


def audit_core_story(block: dict[str, str], policy: dict[str, Any], *, enforce_roles: bool) -> dict[str, Any]:
    body = block["body"]
    links = {link for link in LINK_RE.findall(body) if direct_auditable_url(link)}
    domains = sorted({domain for link in links if (domain := normalized_domain(link))})
    aliases = policy.get("source_family_aliases", {})
    aliases = aliases if isinstance(aliases, dict) else {}
    source_families = sorted({source_family(domain, aliases) for domain in domains})
    required_layers = {
        "conclusion": "结论",
        "hard_evidence": "硬证据",
        "causal_mechanism": "机制",
        "counterevidence": "反证",
        "falsification_signal": "证伪",
    }
    missing_layers = [name for name, label in required_layers.items() if not re.search(rf"(?:\*\*)?{label}[：:]", body)]
    primary_suffixes = {
        normalize_hostname(str(domain).lstrip("."))
        for domain in policy.get("quality_gate", {}).get("primary_domain_suffixes", [])
    }
    primary_suffixes.discard("")
    primary_allowlist = {
        normalize_hostname(str(domain))
        for domain in policy.get("quality_gate", {}).get("primary_domain_allowlist", [])
    }
    primary_allowlist.discard("")
    primary_links = [
        link for link in links
        if (
            normalized_domain(link) in primary_allowlist
            or any(
                normalized_domain(link) == suffix
                or normalized_domain(link).endswith("." + suffix)
                for suffix in primary_suffixes
            )
        )
    ]
    role_match = re.search(r"(?:\*\*)?证据角色[：:]([^\n]+)", body)
    role_text = role_match.group(1) if role_match else ""
    assigned_role_links = {
        "primary": evidence_role_links(role_text, "一手来源", links),
        "event_region": evidence_role_links(role_text, "事件地区来源", links),
        "external_verification": evidence_role_links(role_text, "外部核验", links),
    }
    unique_role_links = {
        name: assigned - set().union(*(other for key, other in assigned_role_links.items() if key != name))
        for name, assigned in assigned_role_links.items()
    }
    role_source_families = {
        name: {
            source_family(normalized_domain(link), aliases)
            for link in assigned
            if normalized_domain(link)
        }
        for name, assigned in assigned_role_links.items()
    }
    unique_role_families = {
        name: assigned
        - set().union(*(other for key, other in role_source_families.items() if key != name))
        for name, assigned in role_source_families.items()
    }
    primary_families = {
        source_family(normalized_domain(link), aliases) for link in primary_links
    }
    role_coverage = {
        "primary": bool(
            unique_role_links["primary"] & set(primary_links)
            and unique_role_families["primary"] & primary_families
        ),
        "event_region": bool(
            unique_role_links["event_region"] and unique_role_families["event_region"]
        ),
        "external_verification": bool(
            unique_role_links["external_verification"]
            and unique_role_families["external_verification"]
        ),
    }
    errors: list[str] = []
    minimum_sources = int(policy.get("minimum_sources_per_core_story", 0))
    minimum_domains = int(policy.get("minimum_independent_domains_per_core_story", 0))
    if len(links) < minimum_sources:
        errors.append(f"needs {minimum_sources} direct sources; found {len(links)}")
    if len(source_families) < minimum_domains:
        errors.append(
            f"needs {minimum_domains} independent source families; found {len(source_families)}"
        )
    if policy.get("require_primary_source_for_high_impact_story") is True and not primary_links:
        errors.append("needs a primary/institutional source")
    if missing_layers:
        errors.append("missing thesis layers: " + ", ".join(missing_layers))
    if enforce_roles:
        missing_roles = [name for name, present in role_coverage.items() if not present]
        if missing_roles:
            errors.append("missing linked evidence roles: " + ", ".join(missing_roles))
    return {
        "title": block["title"],
        "direct_source_count": len(links),
        "independent_domain_count": len(domains),
        "independent_source_family_count": len(source_families),
        "source_families": source_families,
        "primary_source_count": len(primary_links),
        "missing_layers": missing_layers,
        "role_coverage": role_coverage,
        "role_links": {name: sorted(values) for name, values in assigned_role_links.items()},
        "role_source_families": {
            name: sorted(values) for name, values in role_source_families.items()
        },
        "roles_enforced": enforce_roles,
        "errors": errors,
        "passed": not errors,
    }


def audit_report(report_path: Path, policy: dict[str, Any], *, enforce: bool) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not report_path.exists():
        return {
            "path": str(report_path),
            "exists": False,
            "enforced": enforce,
            "errors": ["dated report is missing"],
            "warnings": [],
            "passed": not enforce,
            "would_pass_if_enforced": False,
        }
    text = report_path.read_text(encoding="utf-8")
    research_policy = policy if isinstance(policy.get("quality_gate"), dict) else {}
    quality_policy = research_policy.get("quality_gate", policy)
    links = LINK_RE.findall(text)
    auditable_links = {link for link in links if direct_auditable_url(link)}
    domains = sorted({domain for link in auditable_links if (domain := normalized_domain(link))})
    aliases = research_policy.get("source_family_aliases", {})
    aliases = aliases if isinstance(aliases, dict) else {}
    source_families = sorted({source_family(domain, aliases) for domain in domains})
    requirements = {
        "minimum_report_characters": int(quality_policy.get("minimum_report_characters", 0)),
        "maximum_report_characters": int(quality_policy.get("maximum_report_characters", 10**9)),
        "minimum_distinct_links": int(quality_policy.get("minimum_distinct_links", 0)),
        "minimum_distinct_domains": int(quality_policy.get("minimum_distinct_domains", 0)),
    }
    if len(text) < requirements["minimum_report_characters"]:
        errors.append("report is shorter than the configured minimum")
    if len(text) > requirements["maximum_report_characters"]:
        errors.append("report exceeds the configured maximum")
    if len(auditable_links) < requirements["minimum_distinct_links"]:
        errors.append("report has too few distinct source links")
    if len(source_families) < requirements["minimum_distinct_domains"]:
        errors.append("report has too few independent source families")

    required_sections = {
        "核心摘要": ["核心摘要"],
        "预测复盘": ["预测复盘", "昨日预测复盘"],
        "预测与市场映射": ["预测与市场映射", "今日预测与市场映射"],
        "风险信号": ["风险信号"],
        "来源与质量": ["来源与质量", "来源"],
    }
    missing_sections = [name for name, aliases in required_sections.items() if not any(alias in text for alias in aliases)]
    if missing_sections:
        errors.append(f"missing required sections: {', '.join(missing_sections)}")
    layer_counts = {
        "conclusion": len(re.findall(r"(?:\*\*)?结论[：:]", text)),
        "hard_evidence": len(re.findall(r"(?:\*\*)?硬证据[：:]", text)),
        "causal_mechanism": len(re.findall(r"(?:\*\*)?机制[：:]", text)),
        "counterevidence": len(re.findall(r"(?:\*\*)?反证[：:]", text)),
        "falsification_signal": len(re.findall(r"(?:\*\*)?证伪[：:]", text)),
    }
    thin_layers = [name for name, count in layer_counts.items() if count < 3]
    if thin_layers:
        warnings.append(f"fewer than three explicit thesis layers: {', '.join(thin_layers)}")
    report_date_match = re.search(r"20\d{2}-\d{2}-\d{2}", text[:500])
    report_date = report_date_match.group(0) if report_date_match else "9999-12-31"
    role_enforce_from = str(research_policy.get("story_evidence_enforce_from_date") or "9999-12-31")
    enforce_story_evidence = bool(research_policy) and date_on_or_after(report_date, parse_date(role_enforce_from))
    story_blocks = core_story_blocks(text)
    story_audits = [
        audit_core_story(block, research_policy, enforce_roles=enforce_story_evidence)
        for block in story_blocks
    ]
    primary_min = int(research_policy.get("primary_thesis_min_items", 0))
    primary_max = int(research_policy.get("primary_thesis_max_items", 5))
    if enforce_story_evidence and len(story_blocks) < primary_min:
        errors.append(f"report needs at least {primary_min} explicitly marked core thesis block(s)")
    if len(story_blocks) > primary_max:
        errors.append(f"report exceeds the {primary_max}-thesis maximum")
    if enforce_story_evidence:
        errors.extend(
            f"core story '{item['title']}': {error}"
            for item in story_audits
            for error in item["errors"]
        )
    role_slots = len(story_audits) * 3
    covered_role_slots = sum(sum(bool(value) for value in item["role_coverage"].values()) for item in story_audits)
    return {
        "path": str(report_path),
        "exists": True,
        "character_count": len(text),
        "distinct_link_count": len(auditable_links),
        "all_http_link_count": len(set(links)),
        "distinct_domain_count": len(domains),
        "domains": domains,
        "distinct_source_family_count": len(source_families),
        "source_families": source_families,
        "required_sections_missing": missing_sections,
        "thesis_layer_counts": layer_counts,
        "primary_thesis_count": len(story_blocks),
        "primary_thesis_maximum": primary_max,
        "core_story_audits": story_audits,
        "all_core_stories_passed": bool(story_audits) and all(item["passed"] for item in story_audits),
        "story_evidence_enforced": enforce_story_evidence,
        "source_role_coverage_pct": round(covered_role_slots / role_slots * 100, 2) if role_slots else 0.0,
        "requirements": requirements,
        "enforced": enforce,
        "errors": errors,
        "warnings": warnings,
        "passed": not errors if enforce else True,
        "would_pass_if_enforced": not errors,
    }


def build_quality_report(
    *,
    date: str,
    records: list[dict[str, Any]],
    report_path: Path,
    settings: dict[str, Any],
    evaluation_cutoff: str | None = None,
) -> dict[str, Any]:
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest() if report_path.is_file() else None
    cutoff = evaluation_cutoff or date
    if parse_date(cutoff) < parse_date(date):
        raise ValueError("evaluation_cutoff cannot precede the report date")
    contract = settings.get("prediction_contract", {})
    enforce_from = str(contract.get("enforce_from_date") or "9999-12-31")
    research_config = settings.get("research_evaluation", {})
    registry_file = str(contract.get("family_registry_file") or "").strip()
    family_registry = read_json(ROOT / registry_file) if registry_file and (ROOT / registry_file).exists() else {}
    research_config = dict(research_config)
    research_config["minimum_sample"] = int(
        research_config.get("minimum_independent_event_families", research_config.get("minimum_sample", 30))
    )
    prediction_audit = audit_prediction_records(
        records,
        cutoff=cutoff,
        enforce_from_date=enforce_from,
        evaluation_config=research_config,
        review_policy=settings.get("review_queue", {}),
        prediction_contract=contract,
        family_registry=family_registry,
    )
    prediction_audit["calibration_recovery"] = calibration_recovery_guidance(
        prediction_audit["proper_scoring"],
        settings.get("calibration_recovery", {}),
    )
    news_policy = dict(settings.get("news_research_policy", {}))
    source_aliases = settings.get("drift_diagnostics", {}).get("source_family_aliases", {})
    if isinstance(source_aliases, dict):
        news_policy["source_family_aliases"] = source_aliases
    content_enforce_from = str(news_policy.get("enforce_from_date") or "9999-12-31")
    report_audit = audit_report(report_path, news_policy, enforce=parse_date(date) >= parse_date(content_enforce_from))
    operational_passed = prediction_audit["operational_passed"] and report_audit["passed"]
    research_ready = operational_passed and prediction_audit["proper_scoring"]["is_research_ready"]
    blockers = list(prediction_audit["operational_errors"])
    blockers.extend(report_audit["errors"] if report_audit.get("enforced") else [])
    if not prediction_audit["proper_scoring"]["is_research_ready"]:
        blockers.append("proper-scoring calibration gate has not passed")
    return {
        "schema_version": 1,
        "date": date,
        "report_sha256": report_sha256,
        "evaluation_cutoff": cutoff,
        "revised": cutoff != date,
        "generated_at": datetime.now().astimezone().isoformat(),
        "operational_passed": operational_passed,
        "research_ready": research_ready,
        "promotion_allowed": research_ready,
        "blocking_reasons": list(dict.fromkeys(blockers)),
        "prediction_audit": prediction_audit,
        "report_audit": report_audit,
        "boundary": {
            "legacy_scores_are_not_proper_scores": True,
            "historical_records_rewritten": False,
            "real_broker_orders_allowed": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit ATLAS report quality and forecast research validity.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Fail unless the proper-scoring research gate passes.")
    args = parser.parse_args(argv)
    try:
        settings = read_json(args.settings)
        records = read_jsonl(args.predictions)
        report = args.report or DEFAULT_OUTPUTS / f"每日全球晨间简报-{args.date}.md"
        report_text = report.read_text(encoding="utf-8-sig") if report.is_file() else ""
        revision_match = re.search(r"修订日期[：:]\s*(20\d{2}-\d{2}-\d{2})", report_text[:1200])
        evaluation_cutoff = revision_match.group(1) if revision_match else args.date
        payload = build_quality_report(
            date=args.date,
            records=records,
            report_path=report,
            settings=settings,
            evaluation_cutoff=evaluation_cutoff,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    output = args.output or DEFAULT_DATA_DIR / f"research-quality-{args.date}.json"
    status = "dry_run" if args.dry_run else "written" if atomic_write_json(output, payload) else "unchanged"
    summary = {
        "status": status,
        "output": str(output),
        "date": args.date,
        "report_sha256": payload["report_sha256"],
        "operational_passed": payload["operational_passed"],
        "research_ready": payload["research_ready"],
        "blocking_reasons": payload["blocking_reasons"],
        "proper_scoring": payload["prediction_audit"]["proper_scoring"],
        "report_audit": payload["report_audit"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if not payload["operational_passed"]:
        return 1
    if args.strict and not payload["research_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
