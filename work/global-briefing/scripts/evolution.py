#!/usr/bin/env python3
"""Forecast scorecards and paper-trading attribution for the briefing loop."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from research_quality import audit_prediction_records

ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "evolution.json"
SETTINGS_PATH = ROOT / "work" / "global-briefing" / "config" / "settings.json"
PREDICTIONS_PATH = ROOT / "work" / "global-briefing" / "data" / "predictions.jsonl"
PAPER_CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "paper_trading.json"
DATA_DIR = ROOT / "work" / "global-briefing" / "data"
OUTPUT_DIR = ROOT / "outputs"
EVOLUTION_STATE_PATH = DATA_DIR / "evolution_state.json"

HORIZON_DAYS = {"1d": 1, "1w": 7, "1m": 30}
FAILURE_GUARDRAILS = {
    "already_priced_in": "区分事件方向与已计价程度；没有相对表现或估值证据，不把新闻方向直接映射为资产结论。",
    "time_window_too_short": "预测未到期不得提前关闭；除非记录 terminal_evidence，否则保持 open 并只更新置信度。",
    "time_window_too_long": "把长周期判断拆成可在 1 日或 1 周验证的中间节点。",
    "source_lag": "来源发布时间或数据时点落后时，降置信度并明确下一次刷新时间。",
    "source_quality_issue": "关键结论至少需要一手来源或两个独立来源；回退行情不得支撑高置信资产映射。",
    "asset_mapping_error": "事实成立不等于标的成立；资产映射必须补相对表现、成交/流量或盈利传导证据。",
    "policy_reversal": "政策主题必须同时记录执行主体、正式文件和撤回/延期条件。",
    "event_reversal": "地缘事件拆分声明、执行、实体流量、价格反应，避免用单一标题外推。",
    "liquidity_or_valuation_override": "方向判断之外必须检查估值、拥挤度和流动性是否覆盖基本面传导。",
    "correlation_mistake": "相关性不能替代因果；保留基准和反向资产作为证伪对照。",
    "data_stale": "陈旧或假期价格只能标记状态，不能验证当日市场传导。",
    "insufficient_evidence": "证据不足时保持 open/HOLD，不用模糊措辞包装成 partial 或 validated。",
}


def parse_date(value: str) -> date_type:
    return datetime.strptime(value, "%Y-%m-%d").date()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def period_bounds(period: str, date: str) -> tuple[date_type, date_type]:
    current = parse_date(date)
    if period == "day":
        return current, current
    if period == "week":
        start = current - timedelta(days=current.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        start = current.replace(day=1)
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1, day=1)
        else:
            next_month = current.replace(month=current.month + 1, day=1)
        return start, next_month - timedelta(days=1)
    raise ValueError("period must be day, week, or month.")


def in_period(record: dict[str, Any], start: date_type, end: date_type) -> bool:
    raw_date = record.get("date") or record.get("review", {}).get("review_date")
    if not raw_date:
        return False
    day = parse_date(str(raw_date)[:10])
    return start <= day <= end


def score_from_parts(parts: dict[str, Any], weights: dict[str, float]) -> int:
    total = 0.0
    for key, weight in weights.items():
        total += float(parts.get(key, 0.0)) * float(weight)
    return round(total * 100)


def normalized_score(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    review = record.get("review") if isinstance(record.get("review"), dict) else {}
    explicit = review.get("score") or record.get("score")
    weights = config["scoring_weights"]
    if isinstance(explicit, dict):
        result = {
            "direction": float(explicit.get("direction", 0.0)),
            "timing": float(explicit.get("timing", 0.0)),
            "transmission": float(explicit.get("transmission", 0.0)),
            "calibration": float(explicit.get("calibration", 0.0)),
        }
        result["total"] = int(explicit.get("total", score_from_parts(result, weights)))
        return result
    status = str(record.get("status", "")).lower()
    if status in {"open", ""}:
        return None
    default = config.get("default_score_by_status", {}).get(status)
    if not default:
        return None
    return dict(default)


def component_score_source(record: dict[str, Any]) -> str:
    """Label subjective component scores so they cannot masquerade as calibration."""
    review = record.get("review") if isinstance(record.get("review"), dict) else {}
    if isinstance(review.get("score") or record.get("score"), dict):
        return "explicit_subjective_rubric"
    return "legacy_status_default"


def review_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        prediction_id = record.get("prediction_id")
        if not prediction_id or not isinstance(record.get("review"), dict) or record.get("status") == "open":
            continue
        latest[str(prediction_id)] = record
    return list(latest.values())


def original_prediction_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    originals: dict[str, dict[str, Any]] = {}
    for record in records:
        prediction_id = record.get("prediction_id")
        if not prediction_id or isinstance(record.get("review"), dict):
            continue
        originals[str(prediction_id)] = record
    return originals


def review_day(record: dict[str, Any]) -> date_type:
    review = record.get("review") if isinstance(record.get("review"), dict) else {}
    raw = review.get("review_date") or record.get("date")
    return parse_date(str(raw)[:10])


def maturity_day(record: dict[str, Any]) -> date_type:
    horizon = str(record.get("horizon") or "1d").lower()
    return parse_date(str(record.get("date"))[:10]) + timedelta(days=HORIZON_DAYS.get(horizon, 1))


def record_integrity(date: str) -> dict[str, Any]:
    """Audit whether review records can support genuine forecast iteration."""
    cutoff = parse_date(date)
    records = read_jsonl(PREDICTIONS_PATH)
    originals = original_prediction_index(records)
    reviews = {str(item.get("prediction_id")): item for item in review_records(records)}
    matured = {
        prediction_id: original
        for prediction_id, original in originals.items()
        if maturity_day(original) <= cutoff
    }
    overdue = sorted(prediction_id for prediction_id in matured if prediction_id not in reviews)
    early_closed: list[str] = []
    nonvalidated = []
    explicit_scores = 0
    explicit_failures = 0
    status_counts: Counter[str] = Counter()
    for prediction_id, review_record in reviews.items():
        original = originals.get(prediction_id)
        review = review_record.get("review") if isinstance(review_record.get("review"), dict) else {}
        status = str(review_record.get("status") or "unknown").lower()
        status_counts[status] += 1
        if original and review_day(review_record) < maturity_day(original) and review.get("terminal_evidence") is not True:
            early_closed.append(prediction_id)
        if isinstance(review.get("score") or review_record.get("score"), dict):
            explicit_scores += 1
        if status in {"partial", "wrong", "expired"}:
            nonvalidated.append(prediction_id)
            if review.get("failure_reasons") or review_record.get("failure_reasons"):
                explicit_failures += 1

    review_count = len(reviews)
    warnings = []
    if review_count >= 10 and not (status_counts.get("wrong", 0) or status_counts.get("expired", 0)):
        warnings.append("optimism_bias: ten or more reviews contain no wrong/expired outcome")
    if early_closed:
        warnings.append("early_closure_bias: forecasts were closed before maturity without terminal evidence")
    if overdue:
        warnings.append("review_coverage_gap: matured forecasts remain unreviewed")
    if review_count and explicit_scores / review_count < 0.8:
        warnings.append("score_coverage_low: fewer than 80% of reviews have explicit component scores")
    if nonvalidated and explicit_failures / len(nonvalidated) < 0.8:
        warnings.append("failure_reason_coverage_low: fewer than 80% of non-validated reviews have explicit failure reasons")

    return {
        "as_of": cutoff.isoformat(),
        "original_prediction_count": len(originals),
        "review_count": review_count,
        "matured_prediction_count": len(matured),
        "overdue_unreviewed_prediction_ids": overdue,
        "early_closed_without_terminal_evidence": sorted(early_closed),
        "explicit_score_coverage_pct": round(explicit_scores / review_count * 100, 2) if review_count else None,
        "explicit_failure_reason_coverage_pct": round(explicit_failures / len(nonvalidated) * 100, 2) if nonvalidated else None,
        "status_counts": dict(sorted(status_counts.items())),
        "warnings": warnings,
    }


def infer_failure_reasons(record: dict[str, Any]) -> list[str]:
    status = str(record.get("status", "")).lower()
    if status == "validated":
        return []
    review = record.get("review") if isinstance(record.get("review"), dict) else {}
    text = " ".join(
        str(value)
        for value in [review.get("why"), review.get("correction"), record.get("scenario")]
        if value
    ).lower()
    reasons: list[str] = []
    if any(token in text for token in ["transmission", "asset", "etf", "标的", "传导"]):
        reasons.append("asset_mapping_error")
    if any(token in text for token in ["time", "window", "not enough", "窗口", "时间"]):
        reasons.append("time_window_too_short")
    if any(token in text for token in ["de-escalation", "deal", "reversal", "协议", "反转", "降温"]):
        reasons.append("event_reversal")
    if any(token in text for token in ["priced", "提前", "price action"]):
        reasons.append("already_priced_in")
    if any(token in text for token in ["stale", "fallback", "source", "data", "数据", "源"]):
        reasons.append("source_quality_issue")
    if status in {"wrong", "expired"} and not reasons:
        reasons.append("insufficient_evidence")
    return sorted(set(reasons))


FAILURE_REASON_ALIASES = {
    "asset_transmission_lag": "time_window_too_short",
    "data_quality_or_stale_price": "source_quality_issue",
    "execution_risk_not_closed": "insufficient_evidence",
    "headline_reversal": "event_reversal",
    "market_closed_or_stale": "data_stale",
    "price_confirmation_missing": "insufficient_evidence",
    "relative_trade_not_confirmed": "asset_mapping_error",
    "source_quality_limit": "source_quality_issue",
}


def allowed_failure_reasons(config: dict[str, Any]) -> set[str]:
    fields = config.get("prediction_review_fields", {})
    values = fields.get("failure_reasons", [])
    return {str(value) for value in values}


def normalize_failure_reasons(reasons: Any, config: dict[str, Any]) -> list[str]:
    if not isinstance(reasons, list):
        return []
    allowed = allowed_failure_reasons(config)
    normalized: list[str] = []
    for reason in reasons:
        value = FAILURE_REASON_ALIASES.get(str(reason), str(reason))
        if value in allowed:
            normalized.append(value)
        else:
            normalized.append("insufficient_evidence")
    return sorted(set(normalized))


def prediction_scorecard(period: str, date: str) -> dict[str, Any]:
    config = read_json(CONFIG_PATH)
    start, end = period_bounds(period, date)
    end = min(end, parse_date(date))
    records = read_jsonl(PREDICTIONS_PATH)
    originals = original_prediction_index(records)
    reviews = [record for record in review_records(records) if in_period(record, start, end)]
    status_counts: Counter[str] = Counter()
    horizon_counts: Counter[str] = Counter()
    probability_counts: Counter[str] = Counter()
    failure_counts: Counter[str] = Counter()
    scored: list[dict[str, Any]] = []
    for record in reviews:
        original = originals.get(str(record.get("prediction_id")), {})
        status = str(record.get("status", "unknown"))
        status_counts[status] += 1
        horizon = str(record.get("horizon") or original.get("horizon") or "unknown")
        horizon_counts[horizon] += 1
        probability = str(record.get("probability") or original.get("probability") or "unknown")
        probability_counts[probability] += 1
        review = record.get("review", {})
        raw_failure_reasons = review.get("failure_reasons") or record.get("failure_reasons") or infer_failure_reasons(record)
        failure_reasons = normalize_failure_reasons(raw_failure_reasons, config)
        for reason in failure_reasons:
            failure_counts[str(reason)] += 1
        score = normalized_score(record, config)
        if score:
            scored.append(
                {
                    "prediction_id": record.get("prediction_id"),
                    "date": record.get("date"),
                    "scenario": record.get("scenario") or original.get("scenario"),
                    "horizon": horizon,
                    "probability": probability,
                    "status": status,
                    "score": score,
                    "score_source": component_score_source(record),
                    "failure_reasons": failure_reasons,
                    "why": review.get("why"),
                    "correction": review.get("correction"),
                }
            )
    totals = [item["score"]["total"] for item in scored]
    explicit_totals = [
        item["score"]["total"]
        for item in scored
        if item["score_source"] == "explicit_subjective_rubric"
    ]
    closed = sum(status_counts.values())
    validated = status_counts.get("validated", 0)
    partial = status_counts.get("partial", 0)
    wrong = status_counts.get("wrong", 0) + status_counts.get("expired", 0)
    settings = read_json(SETTINGS_PATH) if SETTINGS_PATH.exists() else {}
    contract = settings.get("prediction_contract", {})
    research_audit = audit_prediction_records(
        records,
        cutoff=date,
        enforce_from_date=str(contract.get("enforce_from_date") or "9999-12-31"),
        evaluation_config=settings.get("research_evaluation", {}),
    )
    proper_metrics = research_audit["proper_scoring"]
    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "review_count": closed,
        "status_counts": dict(sorted(status_counts.items())),
        "horizon_counts": dict(sorted(horizon_counts.items())),
        "probability_counts": dict(sorted(probability_counts.items())),
        "hit_rate_pct": round((validated / closed) * 100, 2) if closed else None,
        "useful_rate_pct": round(((validated + partial) / closed) * 100, 2) if closed else None,
        "wrong_or_expired_rate_pct": round((wrong / closed) * 100, 2) if closed else None,
        "average_score": round(sum(totals) / len(totals), 2) if totals else None,
        "average_explicit_component_score": round(sum(explicit_totals) / len(explicit_totals), 2) if explicit_totals else None,
        "legacy_status_default_score_count": sum(item["score_source"] == "legacy_status_default" for item in scored),
        "failure_reasons": dict(failure_counts.most_common()),
        "scored_reviews": scored,
        "integrity": record_integrity(date),
        "proper_scoring": proper_metrics,
        "research_audit": research_audit,
        "schema_note": "Component scores are a subjective diagnostic rubric. Legacy status defaults are compatibility-only; Brier/log loss/ECE are the research metrics.",
    }


def validate_records(period: str, date: str) -> dict[str, Any]:
    start, end = period_bounds(period, date)
    end = min(end, parse_date(date))
    records = read_jsonl(PREDICTIONS_PATH)
    config = read_json(CONFIG_PATH)
    reviewed_ids = {
        str(record.get("prediction_id"))
        for record in records
        if record.get("prediction_id") and isinstance(record.get("review"), dict) and str(record.get("status", "")).lower() != "open"
    }
    open_missing_falsification = []
    for record in records:
        if not in_period(record, start, end):
            continue
        prediction_id = record.get("prediction_id")
        status = str(record.get("status", "open")).lower()
        has_review = isinstance(record.get("review"), dict)
        if not has_review and status == "open" and str(prediction_id) not in reviewed_ids:
            if not (record.get("falsification_signals") or record.get("verification_signals")):
                open_missing_falsification.append(prediction_id)
    reviews_missing_detail = []
    reviews_missing_explicit_score = []
    reviews_missing_explicit_failure_reasons = []
    reviews_with_nonstandard_failure_reasons = []
    period_reviews = [record for record in review_records(records) if in_period(record, start, end)]
    allowed_reasons = allowed_failure_reasons(config)
    for record in period_reviews:
        status = str(record.get("status", "open")).lower()
        if status in {"partial", "wrong", "expired"}:
            review = record.get("review", {})
            raw_failure_reasons = review.get("failure_reasons") or record.get("failure_reasons") or infer_failure_reasons(record)
            has_failure_reasons = bool(normalize_failure_reasons(raw_failure_reasons, config))
            has_score = isinstance(review.get("score") or record.get("score"), dict)
            if not has_failure_reasons or not has_score:
                reviews_missing_detail.append(record.get("prediction_id"))
            if not has_score:
                reviews_missing_explicit_score.append(record.get("prediction_id"))
            if not (review.get("failure_reasons") or record.get("failure_reasons")):
                reviews_missing_explicit_failure_reasons.append(record.get("prediction_id"))
            for reason in raw_failure_reasons if isinstance(raw_failure_reasons, list) else []:
                normalized = FAILURE_REASON_ALIASES.get(str(reason), str(reason))
                if str(reason) not in allowed_reasons or normalized != str(reason):
                    reviews_with_nonstandard_failure_reasons.append(
                        {
                            "prediction_id": record.get("prediction_id"),
                            "raw": str(reason),
                            "normalized": normalized if normalized in allowed_reasons else "insufficient_evidence",
                        }
                    )
    integrity = record_integrity(date)
    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "open_predictions_missing_falsification_signals": sorted(set(open_missing_falsification)),
        "closed_reviews_missing_score_or_failure_reasons": sorted(set(reviews_missing_detail)),
        "nonvalidated_reviews_missing_explicit_score": sorted(set(reviews_missing_explicit_score)),
        "nonvalidated_reviews_missing_explicit_failure_reasons": sorted(set(reviews_missing_explicit_failure_reasons)),
        "closed_reviews_with_normalized_failure_reasons": reviews_with_nonstandard_failure_reasons,
        "integrity": integrity,
        "recommendation": "Keep immature predictions open; require explicit score and failure_reasons for partial/wrong/expired reviews; treat zero misses as a calibration warning, not proof of quality.",
    }


def account_configs() -> dict[str, dict[str, Any]]:
    base = read_json(PAPER_CONFIG_PATH)
    accounts = {}
    for name, account_config in base.get("accounts", {}).items():
        merged = dict(base)
        merged.pop("accounts", None)
        merged.update(account_config)
        merged["account"] = name
        accounts[name] = merged
    return accounts


def position_key(symbol: str, exchange: str | None = None) -> str:
    symbol_clean = symbol.strip().upper()
    exchange_clean = (exchange or "").strip().upper()
    return f"{exchange_clean}:{symbol_clean}" if exchange_clean else symbol_clean


def latest_valuation(valuations: list[dict[str, Any]], end: date_type) -> dict[str, Any] | None:
    candidates = []
    for record in valuations:
        raw_date = record.get("date")
        if not raw_date:
            continue
        if parse_date(str(raw_date)[:10]) <= end:
            candidates.append(record)
    if not candidates:
        return None
    return candidates[-1]


def attribution_fx_rate(config: dict[str, Any], currency: Any, explicit: Any = None) -> float:
    if explicit not in (None, ""):
        rate = float(explicit)
    else:
        clean = str(currency or config.get("base_currency") or "").upper()
        rates = config.get("fx_rates_to_base", {})
        if not config.get("base_currency") and not rates:
            return 1.0
        rate = float(rates.get(clean, 1.0 if clean == str(config.get("base_currency") or "").upper() else 0.0))
    if rate <= 0:
        raise ValueError(f"Missing positive fx_to_base for {currency!r} in {config.get('account')} attribution.")
    return rate


def reconstruct_account_at_date(
    config: dict[str, Any], trades: list[dict[str, Any]], end: date_type
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, float], float, float]:
    """Replay only records available by the requested date; never read the latest portfolio snapshot."""
    positions: dict[str, dict[str, Any]] = {}
    price_book: dict[str, dict[str, Any]] = {}
    realized_by_key: defaultdict[str, float] = defaultdict(float)
    cash = float(config.get("initial_cash", 0.0))
    realized_total = 0.0
    for trade in trades:
        raw_date = str(trade.get("date") or "")[:10]
        if not raw_date or parse_date(raw_date) > end:
            continue
        action = str(trade.get("action") or "").upper()
        symbol = str(trade.get("symbol") or "").upper()
        exchange = str(trade.get("exchange") or "").upper()
        key = position_key(symbol, exchange)
        currency = str(trade.get("currency") or config.get("base_currency") or "").upper()
        fx_to_base = attribution_fx_rate(config, currency, trade.get("fx_to_base"))
        price_raw = trade.get("price")
        if price_raw not in (None, "") and float(price_raw) > 0:
            price_book[key] = {
                "price": float(price_raw),
                "date": raw_date,
                "source": trade.get("source"),
                "currency": currency,
                "fx_to_base": fx_to_base,
            }
        if action not in {"BUY", "SELL"}:
            continue
        quantity = float(trade.get("quantity", 0.0))
        gross_native = float(trade.get("gross_value", quantity * float(price_raw or 0.0)))
        fee_native = float(trade.get("fee", 0.0))
        gross_base = float(trade.get("gross_value_base", gross_native * fx_to_base))
        fee_base = float(trade.get("fee_base", fee_native * fx_to_base))
        if action == "BUY":
            position = positions.setdefault(
                key,
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "currency": currency,
                    "fx_to_base": fx_to_base,
                    "quantity": 0.0,
                    "avg_cost": 0.0,
                    "prediction_id": str(trade.get("prediction_id") or "unlinked"),
                    "scenario": str(trade.get("scenario") or ""),
                },
            )
            old_qty = float(position["quantity"])
            old_cost_native = old_qty * float(position["avg_cost"])
            new_qty = old_qty + quantity
            position["quantity"] = new_qty
            position["avg_cost"] = (old_cost_native + gross_native + fee_native) / new_qty if new_qty else 0.0
            position["fx_to_base"] = fx_to_base
            cash -= gross_base + fee_base
        else:
            position = positions.get(key)
            if not position or quantity <= 0:
                continue
            sell_qty = min(quantity, float(position["quantity"]))
            realized_native = (float(price_raw or 0.0) - float(position["avg_cost"])) * sell_qty - fee_native
            realized_base = float(trade.get("realized_pnl_base", realized_native * fx_to_base))
            realized_by_key[key] += realized_base
            realized_total += realized_base
            cash += gross_base - fee_base
            position["quantity"] = float(position["quantity"]) - sell_qty
            if float(position["quantity"]) <= 1e-9:
                positions.pop(key, None)
    return positions, price_book, dict(realized_by_key), cash, realized_total


def paper_attribution(period: str, date: str) -> dict[str, Any]:
    start, end = period_bounds(period, date)
    end = min(end, parse_date(date))
    accounts = account_configs()
    account_results = []
    by_prediction: dict[str, dict[str, Any]] = {}
    for account, config in accounts.items():
        trades_path = ROOT / config["trades_file"]
        valuations_path = ROOT / config["valuations_file"]
        trades = read_jsonl(trades_path)
        valuations = read_jsonl(valuations_path)
        recorded_valuation = latest_valuation(valuations, end)
        positions, last_prices, realized_by_key, cash, realized_total = reconstruct_account_at_date(config, trades, end)
        period_actions: list[dict[str, Any]] = []
        for trade in trades:
            trade_date = trade.get("date")
            if trade_date and start <= parse_date(str(trade_date)[:10]) <= end:
                period_actions.append(trade)

        positions_out = []
        for key, position in sorted(positions.items()):
            price = float(last_prices.get(key, {}).get("price", position.get("avg_cost", 0.0)))
            quantity = float(position.get("quantity", 0.0))
            fx_to_base = float(last_prices.get(key, {}).get("fx_to_base", position.get("fx_to_base", 1.0)))
            market_value_native = quantity * price
            market_value = market_value_native * fx_to_base
            cost_basis_native = quantity * float(position.get("avg_cost", 0.0))
            cost_basis = cost_basis_native * fx_to_base
            unrealized = market_value - cost_basis
            prediction_id = str(position.get("prediction_id") or "unlinked")
            row = {
                "account": account,
                "key": key,
                "symbol": position.get("symbol"),
                "exchange": position.get("exchange"),
                "prediction_id": prediction_id,
                "scenario": position.get("scenario"),
                "currency": position.get("currency"),
                "base_currency": config.get("base_currency"),
                "fx_to_base": fx_to_base,
                "quantity": quantity,
                "avg_cost": float(position.get("avg_cost", 0.0)),
                "last_price": price,
                "market_value": market_value,
                "market_value_native": market_value_native,
                "cost_basis": cost_basis,
                "cost_basis_native": cost_basis_native,
                "unrealized_pnl": unrealized,
                "realized_pnl": realized_by_key.get(key, 0.0),
                "return_pct": round((unrealized / cost_basis) * 100, 4) if cost_basis else None,
            }
            positions_out.append(row)
            attribution_key = f"{account}:{prediction_id}"
            pred = by_prediction.setdefault(
                attribution_key,
                {
                    "prediction_id": prediction_id,
                    "account": account,
                    "base_currency": config.get("base_currency"),
                    "market_value": 0.0,
                    "cost_basis": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "positions": [],
                },
            )
            pred["market_value"] += market_value
            pred["cost_basis"] += cost_basis
            pred["unrealized_pnl"] += unrealized
            pred["realized_pnl"] += realized_by_key.get(key, 0.0)
            pred["positions"].append({"account": account, "symbol": row["symbol"], "exchange": row["exchange"]})

        positions_value = sum(float(item["market_value"]) for item in positions_out)
        initial_cash = float(config.get("initial_cash", 0.0))
        reconstructed_valuation = {
            "account": account,
            "account_id": config.get("account_id"),
            "date": end.isoformat(),
            "cash": cash,
            "positions_value": positions_value,
            "equity": cash + positions_value,
            "realized_pnl": realized_total,
            "total_return_pct": ((cash + positions_value) / initial_cash - 1.0) * 100 if initial_cash else None,
            "base_currency": config.get("base_currency"),
            "valuation_source": "reconstructed_point_in_time_from_trades",
            "paper_trading_only": True,
        }

        account_results.append(
            {
                "account": account,
                "latest_valuation": reconstructed_valuation,
                "recorded_valuation": recorded_valuation,
                "positions": positions_out,
                "period_actions": period_actions,
            }
        )

    prediction_rows = []
    for value in by_prediction.values():
        cost_basis = float(value["cost_basis"])
        value["return_pct"] = round((float(value["unrealized_pnl"]) / cost_basis) * 100, 4) if cost_basis else None
        prediction_rows.append(value)
    prediction_rows.sort(key=lambda item: float(item.get("unrealized_pnl", 0.0)))
    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "accounts": account_results,
        "by_prediction": prediction_rows,
        "paper_trading_only": True,
    }


def write_json_output(kind: str, date: str, payload: dict[str, Any]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{kind}-{date}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return True


def build_evolution_state(period: str, date: str) -> dict[str, Any]:
    scorecard = prediction_scorecard(period, date)
    integrity = scorecard["integrity"]
    failure_reasons = scorecard.get("failure_reasons", {})
    learned_rules = [
        {
            "rule_id": f"failure-{reason}",
            "trigger_count": count,
            "instruction": FAILURE_GUARDRAILS[reason],
        }
        for reason, count in failure_reasons.items()
        if reason in FAILURE_GUARDRAILS and int(count) > 0
    ][:6]
    learned_rules.extend(
        [
            {
                "rule_id": "review-explicit-scoring",
                "trigger_count": len(integrity.get("warnings", [])),
                "instruction": "partial/wrong/expired 必须填写 direction、timing、transmission、calibration 四项显式分数和失败原因。",
            },
            {
                "rule_id": "report-decision-density",
                "trigger_count": 1,
                "instruction": "正文只保留结论、硬证据、因果机制、反方证据和证伪信号；背景只出现一次，删除同义改写。",
            },
        ]
    )
    score_coverage = integrity.get("explicit_score_coverage_pct")
    failure_coverage = integrity.get("explicit_failure_reason_coverage_pct")
    legacy_diagnostic_warnings = list(integrity.get("warnings", []))
    if score_coverage is None or score_coverage < 80:
        legacy_diagnostic_warnings.append("legacy_explicit_score_coverage_below_80pct")
    if failure_coverage is not None and failure_coverage < 80:
        legacy_diagnostic_warnings.append("legacy_explicit_failure_reason_coverage_below_80pct")
    gate_reasons: list[str] = []
    research_audit = scorecard.get("research_audit", {})
    for error in research_audit.get("operational_errors", []):
        gate_reasons.append(f"v2_contract:{error}")
    proper_scoring = scorecard.get("proper_scoring", {})
    if not proper_scoring.get("is_research_ready", False):
        for gate, passed in proper_scoring.get("gates", {}).items():
            if not passed:
                gate_reasons.append(f"proper_scoring:{gate}")
    promotion_allowed = not gate_reasons
    return {
        "schema_version": 1,
        "as_of": date,
        "lookback_period": period,
        "mode": "gated_self_evolution",
        "state": "validated" if promotion_allowed else "shadow",
        "promotion_allowed": promotion_allowed,
        "gate_reasons": list(dict.fromkeys(gate_reasons)),
        "legacy_diagnostic_warnings": list(dict.fromkeys(legacy_diagnostic_warnings)),
        "scorecard": {
            "start": scorecard.get("start"),
            "end": scorecard.get("end"),
            "review_count": scorecard.get("review_count"),
            "hit_rate_pct": scorecard.get("hit_rate_pct"),
            "useful_rate_pct": scorecard.get("useful_rate_pct"),
            "wrong_or_expired_rate_pct": scorecard.get("wrong_or_expired_rate_pct"),
            "average_score": scorecard.get("average_score"),
            "status_counts": scorecard.get("status_counts", {}),
            "failure_reasons": failure_reasons,
        },
        "proper_scoring": proper_scoring,
        "integrity": integrity,
        "active_rules": learned_rules,
        "report_contract": {
            "executive_summary_max_items": 5,
            "primary_thesis_max_items": 5,
            "observation_list_max_items": 7,
            "required_layers": ["conclusion", "hard_evidence", "causal_mechanism", "counterevidence", "falsification_signal"],
            "repeat_background_once": True,
            "ban_generic_fillers": True,
            "separate_fact_analysis_and_market_mapping": True,
        },
        "boundary": {
            "auto_rewrite_historical_records": False,
            "auto_promote_strategy": False,
            "real_broker_orders_allowed": False,
        },
    }


def update_evolution_state(period: str, date: str, *, write: bool = True) -> tuple[dict[str, Any], bool]:
    payload = build_evolution_state(period, date)
    changed = False
    if write:
        changed = atomic_write_json(EVOLUTION_STATE_PATH, payload)
        changed = atomic_write_json(DATA_DIR / f"evolution-state-{date}.json", payload) or changed
    return payload, changed


def markdown_scorecard(scorecard: dict[str, Any], attribution: dict[str, Any] | None = None) -> str:
    lines = [
        f"# 预测复盘 {scorecard['start']} 至 {scorecard['end']}",
        "",
        "## 总览",
        "",
        f"- 复盘预测数：{scorecard['review_count']}",
        f"- 命中率：{scorecard['hit_rate_pct'] if scorecard['hit_rate_pct'] is not None else 'N/A'}%",
        f"- 有用率（validated+partial）：{scorecard['useful_rate_pct'] if scorecard['useful_rate_pct'] is not None else 'N/A'}%",
        f"- 平均分：{scorecard['average_score'] if scorecard['average_score'] is not None else 'N/A'}",
        f"- 状态分布：{json.dumps(scorecard['status_counts'], ensure_ascii=False)}",
        "",
        "## 主要失败原因",
        "",
    ]
    if scorecard["failure_reasons"]:
        for reason, count in scorecard["failure_reasons"].items():
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- 暂无结构化失败原因；后续复盘应补充 failure_reasons。")
    lines.extend(["", "## 逐条复盘", ""])
    for item in scorecard["scored_reviews"]:
        score = item["score"]
        lines.append(
            f"- {item['prediction_id']} [{item['status']}, {item.get('horizon')}, {item.get('probability')}]: total={score['total']}, "
            f"direction={score['direction']}, timing={score['timing']}, "
            f"transmission={score['transmission']}, calibration={score['calibration']}"
        )
        if item.get("failure_reasons"):
            lines.append(f"  失败/降权原因：{', '.join(item['failure_reasons'])}")
        if item.get("correction"):
            lines.append(f"  修正：{item['correction']}")
    if attribution:
        lines.extend(["", "## 纸面交易归因", ""])
        rows = attribution.get("by_prediction", [])
        if rows:
            for row in rows:
                lines.append(
                    f"- {row['prediction_id']}: market_value={row['market_value']:.2f}, "
                    f"unrealized_pnl={row['unrealized_pnl']:.2f}, return_pct={row['return_pct']}"
                )
        else:
            lines.append("- 暂无可归因持仓。")
    lines.append("")
    return "\n".join(lines)


def write_markdown_summary(period: str, date: str, scorecard: dict[str, Any], attribution: dict[str, Any]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    current = parse_date(date)
    if period == "week":
        iso = current.isocalendar()
        path = OUTPUT_DIR / f"每周预测复盘-{iso.year}-W{iso.week:02d}.md"
    elif period == "month":
        path = OUTPUT_DIR / f"每月预测复盘-{current:%Y-%m}.md"
    else:
        path = OUTPUT_DIR / f"每日预测复盘-{date}.md"
    path.write_text(markdown_scorecard(scorecard, attribution), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Improve the briefing loop with scorecards and attribution.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    score_parser = subparsers.add_parser("scorecard", help="Generate a prediction scorecard JSON.")
    score_parser.add_argument("--period", choices=["day", "week", "month"], required=True)
    score_parser.add_argument("--date", required=True)
    score_parser.add_argument("--write", action="store_true")

    attribution_parser = subparsers.add_parser("paper-attribution", help="Generate paper-trading attribution JSON.")
    attribution_parser.add_argument("--period", choices=["day", "week", "month"], required=True)
    attribution_parser.add_argument("--date", required=True)
    attribution_parser.add_argument("--write", action="store_true")

    summary_parser = subparsers.add_parser("write-review", help="Write a Markdown review with scorecard and attribution.")
    summary_parser.add_argument("--period", choices=["day", "week", "month"], required=True)
    summary_parser.add_argument("--date", required=True)

    validate_parser = subparsers.add_parser("validate-records", help="Find missing evolution fields in prediction records.")
    validate_parser.add_argument("--period", choices=["day", "week", "month"], required=True)
    validate_parser.add_argument("--date", required=True)

    policy_parser = subparsers.add_parser("update-policy", help="Build the machine-readable gated evolution state used by the next briefing.")
    policy_parser.add_argument("--period", choices=["week", "month"], default="month")
    policy_parser.add_argument("--date", required=True)
    policy_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "scorecard":
        payload = prediction_scorecard(args.period, args.date)
        if args.write:
            path = write_json_output(f"scorecard-{args.period}", args.date, payload)
            print(path)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "paper-attribution":
        payload = paper_attribution(args.period, args.date)
        if args.write:
            path = write_json_output(f"paper-attribution-{args.period}", args.date, payload)
            print(path)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "write-review":
        scorecard = prediction_scorecard(args.period, args.date)
        attribution = paper_attribution(args.period, args.date)
        score_path = write_json_output(f"scorecard-{args.period}", args.date, scorecard)
        attribution_path = write_json_output(f"paper-attribution-{args.period}", args.date, attribution)
        review_path = write_markdown_summary(args.period, args.date, scorecard, attribution)
        print(json.dumps({"scorecard": str(score_path), "paper_attribution": str(attribution_path), "review": str(review_path)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-records":
        print(json.dumps(validate_records(args.period, args.date), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "update-policy":
        payload, changed = update_evolution_state(args.period, args.date, write=not args.dry_run)
        print(json.dumps({"status": "dry_run" if args.dry_run else "changed" if changed else "unchanged", "path": str(EVOLUTION_STATE_PATH), "evolution": payload}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
