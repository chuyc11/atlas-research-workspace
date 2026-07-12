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

HORIZON_DAYS = {"1d": 1, "1w": 7, "1m": 30}
CLOSED_STATUSES = {"validated", "partial", "wrong", "expired"}
LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
PREDICTION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-P\d{2,}$")


def parse_date(value: Any) -> date_type:
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def date_on_or_after(value: Any, threshold: date_type) -> bool:
    try:
        return parse_date(value) >= threshold
    except (TypeError, ValueError):
        return False


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: row is not an object")
        rows.append(value)
    return rows


def atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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


def maturity_date(record: dict[str, Any]) -> date_type:
    if record.get("deadline"):
        return parse_date(record["deadline"])
    horizon = str(record.get("horizon") or "1d").lower()
    return parse_date(record.get("date")) + timedelta(days=HORIZON_DAYS.get(horizon, 1))


def original_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in records if not isinstance(row.get("review"), dict)]


def latest_reviews(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in records:
        prediction_id = str(row.get("prediction_id") or "")
        status = str(row.get("status") or "").lower()
        if not prediction_id or not isinstance(row.get("review"), dict) or status not in CLOSED_STATUSES:
            continue
        current = latest.get(prediction_id)
        current_day = str((current or {}).get("review", {}).get("review_date") or (current or {}).get("date") or "")
        candidate_day = str(row.get("review", {}).get("review_date") or row.get("date") or "")
        if current is None or candidate_day >= current_day:
            latest[prediction_id] = row
    return latest


def _nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def validate_v2_prediction(record: dict[str, Any]) -> list[str]:
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
    mappings = record.get("market_mapping")
    tickers = record.get("tickers")
    ticker_symbols = {
        str(item.get("symbol") if isinstance(item, dict) else item).strip().upper()
        for item in tickers if str(item.get("symbol") if isinstance(item, dict) else item).strip()
    } if isinstance(tickers, list) else set()
    if ticker_symbols and (not isinstance(mappings, list) or not mappings):
        errors.append(f"{prediction_id}: non-empty market_mapping is required when tickers are named")
    elif isinstance(mappings, list):
        mapped_symbols: set[str] = set()
        for index, item in enumerate(mappings):
            if not isinstance(item, dict):
                errors.append(f"{prediction_id}: market_mapping[{index}] must be an object")
                continue
            for field in ("symbol", "direction", "benchmark", "verification_rule", "evaluation_deadline"):
                if not str(item.get(field) or "").strip():
                    errors.append(f"{prediction_id}: market_mapping[{index}].{field} is required")
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol:
                mapped_symbols.add(symbol)
            if str(item.get("direction") or "") not in {"up", "down", "outperform", "underperform", "neutral"}:
                errors.append(f"{prediction_id}: market_mapping[{index}].direction is invalid")
            try:
                mapping_deadline = parse_date(item.get("evaluation_deadline"))
                if prediction_day and mapping_deadline <= prediction_day:
                    errors.append(f"{prediction_id}: market_mapping[{index}].evaluation_deadline must be after date")
            except (TypeError, ValueError):
                errors.append(f"{prediction_id}: market_mapping[{index}].evaluation_deadline must be YYYY-MM-DD")
        for missing_symbol in sorted(ticker_symbols - mapped_symbols):
            errors.append(f"{prediction_id}: ticker {missing_symbol} has no market_mapping")
    return errors


def validate_v2_review(record: dict[str, Any], original: dict[str, Any] | None) -> list[str]:
    """Validate a resolution record against its immutable original forecast."""
    prediction_id = str(record.get("prediction_id") or "<missing-id>")
    errors: list[str] = []
    if original is None:
        return [f"{prediction_id}: review has no original prediction"]
    status = str(record.get("status") or "").lower()
    if status not in CLOSED_STATUSES:
        errors.append(f"{prediction_id}: review status must be one of {sorted(CLOSED_STATUSES)}")
    review = record.get("review")
    if not isinstance(review, dict):
        return [f"{prediction_id}: review object is required"]
    try:
        resolved_day = parse_date(review.get("review_date") or record.get("date"))
    except (TypeError, ValueError):
        errors.append(f"{prediction_id}: review.review_date must be YYYY-MM-DD")
        resolved_day = None
    if observed_outcome(review) is None:
        errors.append(f"{prediction_id}: review.observed_outcome must be binary 0 or 1")
    evidence = review.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{prediction_id}: review.evidence must be a non-empty list")
    if resolved_day and resolved_day < maturity_date(original) and review.get("terminal_evidence") is not True:
        errors.append(f"{prediction_id}: premature review requires terminal_evidence=true")
    return errors


def proper_scoring_metrics(
    records: list[dict[str, Any]],
    *,
    cutoff: str,
    minimum_sample: int = 30,
    maximum_brier: float = 0.25,
    maximum_ece: float = 0.15,
) -> dict[str, Any]:
    cutoff_day = parse_date(cutoff)
    originals = original_records(records)
    original_by_id: dict[str, dict[str, Any]] = {}
    for row in originals:
        prediction_id = str(row.get("prediction_id") or "")
        if prediction_id and prediction_id not in original_by_id:
            original_by_id[prediction_id] = row
    reviews = latest_reviews(records)
    all_matured = {key: row for key, row in original_by_id.items() if maturity_date(row) <= cutoff_day}
    matured = {
        key: row
        for key, row in all_matured.items()
        if row.get("schema_version") == 2
    }
    legacy_matured_count = len(all_matured) - len(matured)
    samples: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()

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
        if resolved_day < maturity_date(original) and not terminal:
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
                "probability": probability,
                "observed_outcome": outcome,
                "brier": round(brier, 6),
                "log_loss": round(log_loss, 6),
            }
        )

    calibration_bins = []
    ece = None
    if samples:
        weighted_gap = 0.0
        for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
            upper = lower + 0.2
            members = [
                item for item in samples
                if lower <= item["probability"] < upper or (upper == 1.0 and item["probability"] == 1.0)
            ]
            if not members:
                continue
            mean_probability = sum(item["probability"] for item in members) / len(members)
            outcome_rate = sum(item["observed_outcome"] for item in members) / len(members)
            gap = abs(mean_probability - outcome_rate)
            weighted_gap += gap * len(members) / len(samples)
            calibration_bins.append(
                {
                    "range": [round(lower, 1), round(upper, 1)],
                    "count": len(members),
                    "mean_probability": round(mean_probability, 4),
                    "observed_rate": round(outcome_rate, 4),
                    "absolute_gap": round(gap, 4),
                }
            )
        ece = round(weighted_gap, 6)

    mean_brier = round(sum(item["brier"] for item in samples) / len(samples), 6) if samples else None
    mean_log_loss = round(sum(item["log_loss"] for item in samples) / len(samples), 6) if samples else None
    resolved_coverage = len(samples) / len(matured) if matured else 0.0
    gates = {
        "minimum_sample": len(samples) >= minimum_sample,
        "resolved_coverage_at_least_80pct": resolved_coverage >= 0.8,
        "brier_at_or_below_threshold": mean_brier is not None and mean_brier <= maximum_brier,
        "ece_at_or_below_threshold": ece is not None and ece <= maximum_ece,
    }
    return {
        "metric_standard": "binary proper scoring; lower is better",
        "cutoff": cutoff,
        "matured_prediction_count": len(matured),
        "matured_v2_prediction_count": len(matured),
        "legacy_matured_prediction_count_excluded": legacy_matured_count,
        "total_matured_prediction_count": len(all_matured),
        "eligible_sample_count": len(samples),
        "resolved_coverage_pct": round(resolved_coverage * 100, 2),
        "brier_score": mean_brier,
        "log_loss": mean_log_loss,
        "expected_calibration_error": ece,
        "calibration_bins": calibration_bins,
        "exclusion_counts": dict(sorted(exclusion_counts.items())),
        "thresholds": {
            "minimum_sample": minimum_sample,
            "maximum_brier": maximum_brier,
            "maximum_expected_calibration_error": maximum_ece,
            "minimum_resolved_coverage_pct": 80.0,
        },
        "gates": gates,
        "is_research_ready": all(gates.values()),
        "samples": samples,
    }


def audit_prediction_records(
    records: list[dict[str, Any]],
    *,
    cutoff: str,
    enforce_from_date: str,
    evaluation_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    originals = original_records(records)
    reviews = latest_reviews(records)
    counts = Counter(str(row.get("prediction_id") or "") for row in originals)
    duplicate_original_ids = sorted(key for key, count in counts.items() if key and count > 1)
    original_by_id: dict[str, dict[str, Any]] = {}
    for row in originals:
        prediction_id = str(row.get("prediction_id") or "")
        if prediction_id and prediction_id not in original_by_id:
            original_by_id[prediction_id] = row
    orphan_reviews = sorted(key for key in reviews if key not in original_by_id)
    current_rows = [row for row in originals if str(row.get("date") or "") == cutoff]
    enforce_day = parse_date(enforce_from_date)
    v2_errors: list[str] = []
    v2_review_errors: list[str] = []
    v2_applicable = []
    for row in originals:
        try:
            row_day = parse_date(row.get("date"))
        except (TypeError, ValueError):
            continue
        if row_day >= enforce_day:
            v2_applicable.append(row)
            v2_errors.extend(validate_v2_prediction(row))
    for prediction_id, review_row in reviews.items():
        original = original_by_id.get(prediction_id)
        if not original:
            continue
        try:
            original_day = parse_date(original.get("date"))
        except (TypeError, ValueError):
            continue
        if original_day >= enforce_day:
            v2_review_errors.extend(validate_v2_review(review_row, original))
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
        if date_on_or_after(
            reviews[prediction_id].get("review", {}).get("review_date") or reviews[prediction_id].get("date"),
            enforce_day,
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
        if review_day < maturity_date(original) and review.get("terminal_evidence") is not True:
            early_closed.append(prediction_id)

    eval_config = evaluation_config or {}
    metrics = proper_scoring_metrics(
        records,
        cutoff=cutoff,
        minimum_sample=int(eval_config.get("minimum_sample", 30)),
        maximum_brier=float(eval_config.get("maximum_brier", 0.25)),
        maximum_ece=float(eval_config.get("maximum_expected_calibration_error", 0.15)),
    )
    operational_errors = []
    if v2_duplicate_ids:
        operational_errors.append("duplicate v2 original prediction IDs")
    if v2_orphan_reviews:
        operational_errors.append("orphan v2 review records")
    operational_errors.extend(v2_errors)
    operational_errors.extend(v2_review_errors)
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
        "numeric_probability_coverage_pct": round(numeric_count / len(originals) * 100, 2) if originals else None,
        "v2_contract": {
            "enforce_from_date": enforce_from_date,
            "applicable_prediction_count": len(v2_applicable),
            "valid_prediction_count": len(v2_applicable) - len({error.split(":", 1)[0] for error in v2_errors}),
            "errors": v2_errors,
            "review_errors": v2_review_errors,
        },
        "operational_errors": operational_errors,
        "operational_passed": not operational_errors,
        "proper_scoring": metrics,
    }


def audit_report(report_path: Path, policy: dict[str, Any], *, enforce: bool) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not report_path.exists():
        return {
            "path": str(report_path),
            "exists": False,
            "errors": ["dated report is missing"],
            "warnings": [],
            "passed": False,
        }
    text = report_path.read_text(encoding="utf-8")
    links = LINK_RE.findall(text)
    domains = sorted({urlparse(link).netloc.lower().removeprefix("www.") for link in links})
    requirements = {
        "minimum_report_characters": int(policy.get("minimum_report_characters", 0)),
        "maximum_report_characters": int(policy.get("maximum_report_characters", 10**9)),
        "minimum_distinct_links": int(policy.get("minimum_distinct_links", 0)),
        "minimum_distinct_domains": int(policy.get("minimum_distinct_domains", 0)),
    }
    if len(text) < requirements["minimum_report_characters"]:
        errors.append("report is shorter than the configured minimum")
    if len(text) > requirements["maximum_report_characters"]:
        errors.append("report exceeds the configured maximum")
    if len(set(links)) < requirements["minimum_distinct_links"]:
        errors.append("report has too few distinct source links")
    if len(domains) < requirements["minimum_distinct_domains"]:
        errors.append("report has too few independent source domains")

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
    return {
        "path": str(report_path),
        "exists": True,
        "character_count": len(text),
        "distinct_link_count": len(set(links)),
        "distinct_domain_count": len(domains),
        "domains": domains,
        "required_sections_missing": missing_sections,
        "thesis_layer_counts": layer_counts,
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
) -> dict[str, Any]:
    contract = settings.get("prediction_contract", {})
    enforce_from = str(contract.get("enforce_from_date") or "9999-12-31")
    research_config = settings.get("research_evaluation", {})
    prediction_audit = audit_prediction_records(
        records,
        cutoff=date,
        enforce_from_date=enforce_from,
        evaluation_config=research_config,
    )
    news_policy = settings.get("news_research_policy", {})
    quality_policy = news_policy.get("quality_gate", {})
    content_enforce_from = str(news_policy.get("enforce_from_date") or "9999-12-31")
    report_audit = audit_report(report_path, quality_policy, enforce=parse_date(date) >= parse_date(content_enforce_from))
    operational_passed = prediction_audit["operational_passed"] and report_audit["passed"]
    research_ready = operational_passed and prediction_audit["proper_scoring"]["is_research_ready"]
    blockers = list(prediction_audit["operational_errors"])
    blockers.extend(report_audit["errors"] if report_audit.get("enforced") else [])
    if not prediction_audit["proper_scoring"]["is_research_ready"]:
        blockers.append("proper-scoring calibration gate has not passed")
    return {
        "schema_version": 1,
        "date": date,
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
        payload = build_quality_report(date=args.date, records=records, report_path=report, settings=settings)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    output = args.output or DEFAULT_DATA_DIR / f"research-quality-{args.date}.json"
    status = "dry_run" if args.dry_run else "written" if atomic_write_json(output, payload) else "unchanged"
    summary = {
        "status": status,
        "output": str(output),
        "date": args.date,
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
