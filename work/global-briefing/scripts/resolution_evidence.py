#!/usr/bin/env python3
"""Prepare an auditable daily forecast-resolution evidence workbench.

This script is deliberately non-authoritative: it may collect objective price
series and construct review candidates, but it never appends reviews or decides
event outcomes. Event resolution still requires cited, analyst-verified evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date as Date, datetime, time, timedelta
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from briefing_store import due_review_queue, load_json_records, review_queue_input_fingerprint
from research_quality import market_mapping_key


ROOT = SCRIPT_PATH.parents[3]
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
DATA_DIR = BRIEFING_ROOT / "data"
SETTINGS_PATH = BRIEFING_ROOT / "config" / "settings.json"
PREDICTIONS_PATH = DATA_DIR / "predictions.jsonl"
SCHEMA_VERSION = 1
USER_AGENT = "Mozilla/5.0 ATLASResolutionEvidence/1.0"


def open_yahoo_url(request: urllib.request.Request, timeout: int):
    parsed = urllib.parse.urlsplit(request.full_url)
    if parsed.scheme != "https" or parsed.hostname != "query2.finance.yahoo.com":
        raise ValueError(f"resolution URL is not allowlisted: {request.full_url}")
    # The scheme and exact provider hostname are validated immediately above.
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return True


def artifact_path(run_date: str) -> Path:
    return DATA_DIR / f"resolution-evidence-{run_date}.json"


def resolution_input_material(run_date: str, *, settings_path: Path = SETTINGS_PATH) -> dict[str, Any]:
    settings = read_json(settings_path, {})
    policy = settings.get("resolution_evidence", {}) if isinstance(settings, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "run_date": run_date,
        "review_queue_input_fingerprint": review_queue_input_fingerprint(PREDICTIONS_PATH),
        "resolution_evidence_policy": policy if isinstance(policy, dict) else {},
    }


def resolution_input_fingerprint(run_date: str, *, settings_path: Path = SETTINGS_PATH) -> str:
    return stable_hash(resolution_input_material(run_date, settings_path=settings_path))


def yahoo_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value.endswith(".SH"):
        return value[:-3] + ".SS"
    return value


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def fetch_yahoo_history(symbol: str, start: Date, end: Date, timeout: int) -> dict[str, Any]:
    ticker = yahoo_symbol(symbol)
    period1 = int(datetime.combine(start, time.min, tzinfo=UTC).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC).timestamp())
    encoded = urllib.parse.quote(ticker, safe="")
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}"
        f"?period1={period1}&period2={period2}&interval=1d&events=div%2Csplits"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with open_yahoo_url(request, timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"symbol": symbol, "provider_symbol": ticker, "source_url": url, "error": str(exc), "points": []}
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return {"symbol": symbol, "provider_symbol": ticker, "source_url": url, "error": "no chart result", "points": []}
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adjclose = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    closes = quote.get("close") or []
    points = []
    for index, stamp in enumerate(timestamps):
        try:
            price_date = datetime.fromtimestamp(int(stamp), UTC).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        close = _as_float(closes[index]) if index < len(closes) else None
        adjusted = _as_float(adjclose[index]) if index < len(adjclose) else None
        if close is None and adjusted is None:
            continue
        points.append({"date": price_date, "close": close, "adjusted_close": adjusted or close})
    return {
        "symbol": symbol,
        "provider_symbol": ticker,
        "source": "Yahoo Finance chart",
        "source_url": url,
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "points": points,
    }


def _point(points: list[dict[str, Any]], predicate: Any) -> dict[str, Any] | None:
    eligible = [item for item in points if predicate(Date.fromisoformat(str(item.get("date"))))]
    return max(eligible, key=lambda item: str(item.get("date"))) if eligible else None


def evaluate_market_mapping(
    prediction_id: str,
    mapping: dict[str, Any],
    series: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    mapping_id = market_mapping_key(prediction_id, mapping)
    base = {
        "mapping_id": mapping_id,
        "symbol": mapping.get("symbol"),
        "benchmark": mapping.get("benchmark"),
        "direction": mapping.get("direction"),
        "evaluation_deadline": mapping.get("evaluation_deadline"),
        "verification_rule": mapping.get("verification_rule"),
        "ledger_mutation_allowed": False,
    }
    evaluation = mapping.get("evaluation")
    if not isinstance(evaluation, dict):
        return {
            **base,
            "status": "blocked",
            "blockers": ["missing_machine_evaluation_contract"],
            "next_action": "人工把预注册文字规则翻译为 window_start/metric/price_field/comparison；不得事后改方向。",
        }
    try:
        window_start = Date.fromisoformat(str(evaluation.get("window_start")))
        deadline = Date.fromisoformat(str(mapping.get("evaluation_deadline")))
    except ValueError:
        return {**base, "status": "blocked", "blockers": ["invalid_evaluation_dates"]}
    price_field = str(evaluation.get("price_field") or "")
    comparison = str(evaluation.get("comparison") or "")
    threshold = _as_float(evaluation.get("threshold_pct")) or 0.0
    symbol = str(mapping.get("symbol") or "")
    benchmark = str(mapping.get("benchmark") or "")
    symbol_series = series.get(symbol, {})
    benchmark_series = series.get(benchmark, {})
    blockers = []
    if symbol_series.get("error") or not symbol_series.get("points"):
        blockers.append(f"symbol_price_unavailable:{symbol_series.get('error') or 'empty'}")
    if benchmark_series.get("error") or not benchmark_series.get("points"):
        blockers.append(f"benchmark_price_unavailable:{benchmark_series.get('error') or 'empty'}")
    if blockers:
        return {**base, "status": "blocked", "blockers": blockers}

    symbol_start = _point(symbol_series["points"], lambda day: day < window_start)
    benchmark_start = _point(benchmark_series["points"], lambda day: day < window_start)
    symbol_end = _point(symbol_series["points"], lambda day: day <= deadline)
    benchmark_end = _point(benchmark_series["points"], lambda day: day <= deadline)
    if not all((symbol_start, benchmark_start, symbol_end, benchmark_end)):
        return {**base, "status": "blocked", "blockers": ["insufficient_price_window"]}
    dates = {
        "symbol_start": symbol_start["date"],
        "benchmark_start": benchmark_start["date"],
        "symbol_end": symbol_end["date"],
        "benchmark_end": benchmark_end["date"],
    }
    if symbol_end["date"] != deadline.isoformat() or benchmark_end["date"] != deadline.isoformat():
        return {**base, "status": "blocked", "blockers": ["deadline_close_missing_or_stale"], "price_dates": dates}
    if symbol_start["date"] != benchmark_start["date"] or symbol_end["date"] != benchmark_end["date"]:
        return {**base, "status": "blocked", "blockers": ["non_comparable_market_sessions"], "price_dates": dates}
    values = [
        _as_float(symbol_start.get(price_field)),
        _as_float(symbol_end.get(price_field)),
        _as_float(benchmark_start.get(price_field)),
        _as_float(benchmark_end.get(price_field)),
    ]
    if any(value is None or value <= 0 for value in values):
        return {**base, "status": "blocked", "blockers": ["invalid_price_values"], "price_dates": dates}
    symbol_return = (values[1] / values[0] - 1.0) * 100
    benchmark_return = (values[3] / values[2] - 1.0) * 100
    excess_return = symbol_return - benchmark_return
    outcomes = {
        "symbol_gt_benchmark": excess_return > threshold,
        "symbol_lt_benchmark": excess_return < -threshold,
        "symbol_return_gt": symbol_return > threshold,
        "symbol_return_lt": symbol_return < -threshold,
        "abs_symbol_return_lte": abs(symbol_return) <= threshold,
    }
    if comparison not in outcomes:
        return {**base, "status": "blocked", "blockers": ["unsupported_comparison"]}
    evidence = [
        {"source": symbol_series.get("source"), "url": symbol_series.get("source_url"), "symbol": symbol},
        {"source": benchmark_series.get("source"), "url": benchmark_series.get("source_url"), "symbol": benchmark},
    ]
    return {
        **base,
        "status": "resolved_candidate",
        "requires_source_verification": True,
        "observed_outcome": int(outcomes[comparison]),
        "symbol_return_pct": round(symbol_return, 6),
        "benchmark_return_pct": round(benchmark_return, 6),
        "excess_return_pct": round(excess_return, 6),
        "start_price_date": symbol_start["date"],
        "end_price_date": symbol_end["date"],
        "price_field": price_field,
        "comparison": comparison,
        "threshold_pct": threshold,
        "evidence": evidence,
        "ledger_fragment_after_verification": {
            "symbol": symbol,
            "benchmark": benchmark,
            "evaluation_deadline": deadline.isoformat(),
            "status": "resolved",
            "observed_outcome": int(outcomes[comparison]),
            "price_field": price_field,
            "symbol_start_price": values[0],
            "symbol_end_price": values[1],
            "benchmark_start_price": values[2],
            "benchmark_end_price": values[3],
            "symbol_return_pct": round(symbol_return, 6),
            "benchmark_return_pct": round(benchmark_return, 6),
            "excess_return_pct": round(excess_return, 6),
            "start_price_date": symbol_start["date"],
            "end_price_date": symbol_end["date"],
            "evidence": evidence,
        },
    }


def build_artifact(
    run_date: str,
    *,
    network: bool,
    timeout: int,
    workers: int,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records = records if records is not None else load_json_records(PREDICTIONS_PATH)
    queue = due_review_queue(run_date, records)
    items = queue.get("review_now", [])
    due_mappings = [
        mapping
        for item in items
        for mapping in item.get("market_mappings_due", [])
        if isinstance(mapping, dict)
    ]
    symbols = sorted({
        str(mapping.get(field) or "")
        for mapping in due_mappings
        for field in ("symbol", "benchmark")
        if str(mapping.get(field) or "")
    })
    series: dict[str, dict[str, Any]] = {}
    if network and symbols:
        starts = [
            Date.fromisoformat(str(mapping.get("evaluation", {}).get("window_start")))
            for mapping in due_mappings
            if isinstance(mapping.get("evaluation"), dict) and mapping.get("evaluation", {}).get("window_start")
        ]
        deadlines = [Date.fromisoformat(str(mapping.get("evaluation_deadline"))) for mapping in due_mappings]
        fetch_start = (min(starts) if starts else Date.fromisoformat(run_date)) - timedelta(days=14)
        fetch_end = max(deadlines) + timedelta(days=2)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_map = {
                executor.submit(fetch_yahoo_history, symbol, fetch_start, fetch_end, timeout): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    series[symbol] = future.result()
                except Exception as exc:
                    series[symbol] = {"symbol": symbol, "error": str(exc), "points": []}
    elif symbols:
        series = {symbol: {"symbol": symbol, "error": "network collection disabled", "points": []} for symbol in symbols}

    prepared = []
    market_candidate_count = 0
    market_blocked_count = 0
    for item in items:
        prediction = item.get("prediction", {}) if isinstance(item.get("prediction"), dict) else {}
        prediction_id = str(item.get("prediction_id") or "")
        event_status = str(item.get("event_resolution_status") or "unresolved")
        resolution = prediction.get("resolution") if isinstance(prediction.get("resolution"), dict) else {}
        is_preregistered = bool(
            prediction.get("schema_version") == 2
            and resolution.get("question")
            and resolution.get("success_criteria")
            and resolution.get("failure_criteria")
        )
        scenario = str(prediction.get("scenario") or "该预测的核心情景")
        event = {
            "status": "already_resolved" if event_status == "resolved" else "needs_verified_resolution_evidence",
            "deadline": item.get("event_deadline"),
            "contract_quality": "pre_registered_v2" if is_preregistered else "legacy_reconstruction_not_proper_scoring",
            "question": resolution.get("question") or f"截至原预测期限，核心情景“{scenario}”是否发生？",
            "success_criteria": resolution.get("success_criteria") or "存在截止日前发布且可核验的独立证据支持核心情景。",
            "failure_criteria": resolution.get("failure_criteria") or "存在相反证据，或截止后仍无足够证据支持核心情景。",
            "verification_signals": prediction.get("verification_signals", []),
            "falsification_signals": prediction.get("falsification_signals", []),
            "prior_source_snapshots": prediction.get("evidence", []),
            "prior_review_candidate": item.get("latest_review"),
            "required_output": "事件结果必须由分析员以独立可点击来源确认；本工件不自动给出 observed_outcome。",
        }
        market_results = []
        for mapping in item.get("market_mappings_due", []):
            if not isinstance(mapping, dict):
                continue
            result = evaluate_market_mapping(prediction_id, mapping, series)
            market_results.append(result)
            if result.get("status") == "resolved_candidate":
                market_candidate_count += 1
            else:
                market_blocked_count += 1
        fragments = [
            result["ledger_fragment_after_verification"]
            for result in market_results
            if result.get("status") == "resolved_candidate"
            and isinstance(result.get("ledger_fragment_after_verification"), dict)
        ]
        draft_market_review = None
        if fragments:
            draft_market_review = {
                "prediction_id": prediction_id,
                "date": run_date,
                "status": "active",
                "review": {
                    "resolution_scope": "market",
                    "review_date": run_date,
                    "market_resolution": fragments,
                },
                "append_only_after_all_sources_are_verified": True,
                "bundle_same_day_mappings_in_one_record": True,
            }
        prepared.append({
            "prediction_id": prediction_id,
            "schema_version": item.get("schema_version"),
            "scenario": prediction.get("scenario"),
            "event_resolution": event,
            "market_resolution_candidates": market_results,
            "draft_market_review_after_verification": draft_market_review,
        })

    material = resolution_input_material(run_date)
    return {
        "schema_version": SCHEMA_VERSION,
        "date": run_date,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "input_fingerprint": stable_hash(material),
        "input_material": material,
        "queue_counts": queue.get("counts", {}),
        "scope": "review_now only; backlog remains in the deterministic review queue",
        "counts": {
            "prediction_work_items": len(prepared),
            "event_items_needing_verified_evidence": sum(
                item["event_resolution"]["status"] == "needs_verified_resolution_evidence" for item in prepared
            ),
            "market_resolved_candidates": market_candidate_count,
            "market_blocked": market_blocked_count,
        },
        "items": prepared,
        "guardrails": {
            "append_to_prediction_ledger": False,
            "automatic_event_outcomes": False,
            "market_candidates_require_source_verification": True,
            "event_and_market_scoring_are_independent": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare daily event and market resolution evidence.")
    parser.add_argument("prepare", nargs="?", default="prepare")
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-network", action="store_true")
    args = parser.parse_args(argv)
    Date.fromisoformat(args.date)
    payload = build_artifact(
        args.date,
        network=not args.no_network,
        timeout=max(1, args.timeout),
        workers=max(1, args.workers),
    )
    path = args.output or artifact_path(args.date)
    existing = read_json(path, {})
    if isinstance(existing, dict):
        comparable_existing = {key: value for key, value in existing.items() if key != "generated_at"}
        comparable_new = {key: value for key, value in payload.items() if key != "generated_at"}
        if comparable_existing == comparable_new and existing.get("generated_at"):
            payload["generated_at"] = existing["generated_at"]
    changed = atomic_write_json(path, payload)
    print(json.dumps({
        "status": "updated" if changed else "unchanged",
        "path": str(path),
        "date": args.date,
        "input_fingerprint": payload["input_fingerprint"],
        "counts": payload["counts"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
