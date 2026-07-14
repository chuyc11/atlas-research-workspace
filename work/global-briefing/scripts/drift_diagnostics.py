#!/usr/bin/env python3
"""Point-in-time diagnostics for research and paper-account drift.

The artifact is deliberately diagnostic-only. It never rewrites sources, forecasts,
probabilities, reviews, paper orders, or portfolio state.
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
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


SCRIPT_PATH = Path(__file__).resolve()
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from evolution import paper_attribution
from research_quality import proper_scoring_metrics, review_scope


ROOT = SCRIPT_PATH.parents[3]
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
DATA_DIR = BRIEFING_ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"
SETTINGS_PATH = BRIEFING_ROOT / "config" / "settings.json"
SOURCES_PATH = BRIEFING_ROOT / "config" / "sources.json"
PAPER_CONFIG_PATH = BRIEFING_ROOT / "config" / "paper_trading.json"
PREDICTIONS_PATH = DATA_DIR / "predictions.jsonl"
URL_RE = re.compile(r"https?://[^\s<>\]\)]+", re.IGNORECASE)
MULTIPART_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "com.cn", "gov.cn", "org.cn",
    "com.hk", "com.au", "go.jp", "go.kr", "com.br", "com.mx",
}
STATUS_RANK = {"healthy": 0, "insufficient_sample": 1, "watch": 2, "alert": 3}


def parse_date(value: Any) -> date_type:
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Expected object at {path}:{line_no}")
        rows.append(value)
    return rows


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return True


def root_domain(host: str) -> str:
    clean = host.lower().strip(".")
    if clean.startswith("www."):
        clean = clean[4:]
    labels = clean.split(".")
    if len(labels) <= 2:
        return clean
    suffix = ".".join(labels[-2:])
    if suffix in MULTIPART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def normalize_url(value: str) -> str | None:
    clean = value.rstrip(".,;:!?，。；：！？'")
    try:
        parsed = urlsplit(clean)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


def hhi(counts: dict[str, int] | Counter[str]) -> float | None:
    total = sum(int(value) for value in counts.values())
    if total <= 0:
        return None
    return round(sum((int(value) / total) ** 2 for value in counts.values()), 6)


def top_share(counts: dict[str, int] | Counter[str]) -> tuple[str | None, float | None]:
    total = sum(int(value) for value in counts.values())
    if total <= 0:
        return None, None
    key, value = max(counts.items(), key=lambda item: (int(item[1]), str(item[0])))
    return str(key), round(int(value) / total * 100, 2)


def worst_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    return max(values, key=lambda value: STATUS_RANK.get(value, -1)) if values else "insufficient_sample"


def metric_status(
    value: float | None,
    *,
    watch: float,
    alert: float,
    higher_is_worse: bool = True,
) -> str:
    if value is None:
        return "insufficient_sample"
    if higher_is_worse:
        return "alert" if value >= alert else "watch" if value >= watch else "healthy"
    return "alert" if value <= alert else "watch" if value <= watch else "healthy"


def report_paths(as_of: date_type, lookback_days: int) -> list[tuple[date_type, Path]]:
    start = as_of - timedelta(days=lookback_days - 1)
    found: list[tuple[date_type, Path]] = []
    for path in OUTPUT_DIR.glob("每日全球晨间简报-????-??-??.md"):
        try:
            day = parse_date(path.stem[-10:])
        except ValueError:
            continue
        if start <= day <= as_of:
            found.append((day, path))
    return sorted(found)


def source_catalog(source_config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    entries: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for item in [*source_config.get("verification_sources", []), *source_config.get("sources", [])]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "unknown")
        names[name.lower()] = name
        homepage = str(item.get("homepage") or "")
        host = (urlsplit(homepage).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            entries.append({
                "host": host,
                "root": root_domain(host),
                "family": str(item.get("source_family") or name),
                "tier": item.get("tier"),
                "region": item.get("region") or item.get("country_or_region"),
            })
    entries.sort(key=lambda item: len(str(item["host"])), reverse=True)
    return entries, names


def classify_host(
    host: str,
    catalog: list[dict[str, Any]],
    aliases: dict[str, str],
    primary_suffixes: list[str],
) -> dict[str, Any]:
    clean = host.lower().removeprefix("www.").removeprefix("m.")
    family = None
    for suffix, value in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        suffix_clean = suffix.lower().lstrip(".")
        if clean == suffix_clean or clean.endswith(f".{suffix_clean}"):
            family = str(value)
            break
    match = next(
        (
            item for item in catalog
            if clean == item["host"]
            or clean.endswith(f".{item['host']}")
            or root_domain(clean) == item["root"]
        ),
        None,
    )
    tier = match.get("tier") if match else None
    if tier is None and any(clean == value.lstrip(".") or clean.endswith(value) for value in primary_suffixes):
        tier = 1
    return {
        "domain": clean,
        "family": family or (str(match["family"]) if match else root_domain(clean)),
        "tier": tier,
        "region": match.get("region") if match else None,
    }


def source_diagnostics(
    as_of: date_type,
    policy: dict[str, Any],
    source_config: dict[str, Any],
) -> tuple[dict[str, Any], list[Path]]:
    lookback = int(policy.get("lookback_days", 28))
    reports = report_paths(as_of, lookback)
    catalog, source_names = source_catalog(source_config)
    aliases = {str(key): str(value) for key, value in policy.get("source_family_aliases", {}).items()}
    primary_suffixes = [str(value).lower() for value in policy.get("primary_domain_suffixes", [])]
    family_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    tier_counts: Counter[str] = Counter()
    region_counts: Counter[str] = Counter()
    unique_articles: set[str] = set()
    daily: list[dict[str, Any]] = []

    for day, path in reports:
        urls = {normalized for raw in URL_RE.findall(path.read_text(encoding="utf-8")) if (normalized := normalize_url(raw))}
        day_family: Counter[str] = Counter()
        day_domain: Counter[str] = Counter()
        for url in sorted(urls):
            host = urlsplit(url).hostname or ""
            classified = classify_host(host, catalog, aliases, primary_suffixes)
            family_counts[classified["family"]] += 1
            domain_counts[classified["domain"]] += 1
            day_family[classified["family"]] += 1
            day_domain[classified["domain"]] += 1
            tier_counts[str(classified["tier"] or "unknown")] += 1
            if classified["region"]:
                region_counts[str(classified["region"])] += 1
            unique_articles.add(url)
        day_top_family, day_top_pct = top_share(day_family)
        daily.append({
            "date": day.isoformat(),
            "citation_count_after_within_report_dedup": len(urls),
            "distinct_domains": len(day_domain),
            "distinct_source_families": len(day_family),
            "source_family_hhi": hhi(day_family),
            "top_source_family": day_top_family,
            "top_source_family_share_pct": day_top_pct,
        })

    rss_paths: list[Path] = []
    discovery_counts: Counter[str] = Counter()
    discovery_errors = 0
    discovery_source_days = 0
    start = as_of - timedelta(days=lookback - 1)
    for path in sorted(DATA_DIR.glob("rss-items-????-??-??.json")):
        try:
            day = parse_date(path.stem[-10:])
        except ValueError:
            continue
        if not start <= day <= as_of:
            continue
        rss_paths.append(path)
        payload = read_json(path, {})
        for item in payload.get("source_health", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("source") or "unknown")
            family = source_names.get(name.lower(), name)
            discovery_counts[family] += int(item.get("items") or 0)
            discovery_errors += int(item.get("errors") or 0)
            discovery_source_days += 1

    top_family, top_family_pct = top_share(family_counts)
    source_hhi = hhi(family_counts)
    minimum_history = int(policy.get("minimum_history_days", 7))
    if len(reports) < minimum_history:
        status = "insufficient_sample"
    else:
        status = worst_status([
            metric_status(
                source_hhi,
                watch=float(policy.get("source_family_hhi_watch", 0.18)),
                alert=float(policy.get("source_family_hhi_alert", 0.28)),
            ),
            metric_status(
                top_family_pct,
                watch=float(policy.get("top_source_family_share_watch_pct", 35.0)),
                alert=float(policy.get("top_source_family_share_alert_pct", 50.0)),
            ),
        ])
    total_citations = sum(family_counts.values())
    known_tier = sum(value for key, value in tier_counts.items() if key != "unknown")
    primary_count = tier_counts.get("1", 0)
    signals = []
    if status in {"watch", "alert"}:
        signals.append("rolling evidence citations are concentrated in a small number of source families")
    if total_citations and known_tier / total_citations < 0.7:
        signals.append("tier classification coverage is below 70%; primary-source share is directional only")
    return {
        "status": status,
        "lookback_start": start.isoformat(),
        "lookback_end": as_of.isoformat(),
        "report_days_observed": len(reports),
        "citation_instances_after_daily_dedup": total_citations,
        "unique_articles": len(unique_articles),
        "distinct_domains": len(domain_counts),
        "distinct_source_families": len(family_counts),
        "source_family_hhi": source_hhi,
        "top_source_family": top_family,
        "top_source_family_share_pct": top_family_pct,
        "primary_citation_share_pct_of_classified": round(primary_count / known_tier * 100, 2) if known_tier else None,
        "tier_classification_coverage_pct": round(known_tier / total_citations * 100, 2) if total_citations else None,
        "top_source_families": [
            {"family": key, "citations": value, "share_pct": round(value / total_citations * 100, 2)}
            for key, value in family_counts.most_common(10)
        ] if total_citations else [],
        "region_citation_counts": dict(region_counts.most_common()),
        "daily": daily,
        "discovery_supply": {
            "artifact_days": len(rss_paths),
            "source_day_observations": discovery_source_days,
            "item_count": sum(discovery_counts.values()),
            "source_family_hhi": hhi(discovery_counts),
            "errors": discovery_errors,
            "note": "Discovery supply concentration is not evidence independence and is reported separately.",
        },
        "signals": signals,
        "method": "Each direct URL is counted once per report; repeated use across days remains visible. Source families, not raw domains, are the independence unit.",
        "thresholds": {
            "minimum_history_days": minimum_history,
            "source_family_hhi_watch": float(policy.get("source_family_hhi_watch", 0.18)),
            "source_family_hhi_alert": float(policy.get("source_family_hhi_alert", 0.28)),
            "top_source_family_share_watch_pct": float(policy.get("top_source_family_share_watch_pct", 35.0)),
            "top_source_family_share_alert_pct": float(policy.get("top_source_family_share_alert_pct", 50.0)),
        },
    }, [path for _day, path in reports] + rss_paths


def point_in_time_records(records: list[dict[str, Any]], as_of: date_type) -> list[dict[str, Any]]:
    kept = []
    for row in records:
        review = row.get("review") if isinstance(row.get("review"), dict) else None
        raw_date = review.get("review_date") if review else row.get("date")
        if raw_date:
            try:
                if parse_date(raw_date) <= as_of:
                    kept.append(row)
            except ValueError:
                continue
    return kept


def event_review_dates(records: list[dict[str, Any]]) -> dict[str, date_type]:
    dates: dict[str, date_type] = {}
    for row in records:
        if not isinstance(row.get("review"), dict) or review_scope(row) not in {"event", "combined"}:
            continue
        prediction_id = str(row.get("prediction_id") or "")
        raw = row["review"].get("review_date") or row.get("date")
        if prediction_id and raw:
            dates[prediction_id] = parse_date(raw)
    return dates


def sample_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {"sample_count": 0, "brier_score": None, "expected_calibration_error": None}
    family_counts = Counter(str(item.get("event_family_id") or item.get("prediction_id")) for item in samples)
    weights = {
        id(item): 1.0 / family_counts[str(item.get("event_family_id") or item.get("prediction_id"))]
        for item in samples
    }
    total_weight = sum(weights.values())
    brier = sum(float(item["brier"]) * weights[id(item)] for item in samples) / total_weight
    weighted_gap = 0.0
    bins = []
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        upper = lower + 0.2
        members = [item for item in samples if lower <= float(item["probability"]) < upper or (upper == 1.0 and float(item["probability"]) == 1.0)]
        if not members:
            continue
        bin_weight = sum(weights[id(item)] for item in members)
        mean_probability = sum(float(item["probability"]) * weights[id(item)] for item in members) / bin_weight
        outcome_rate = sum(int(item["observed_outcome"]) * weights[id(item)] for item in members) / bin_weight
        gap = abs(mean_probability - outcome_rate)
        weighted_gap += gap * bin_weight / total_weight
        bins.append({"range": [lower, upper], "count": len(members), "independent_family_weight": round(bin_weight, 6), "mean_probability": round(mean_probability, 4), "observed_rate": round(outcome_rate, 4)})
    return {
        "sample_count": len(family_counts),
        "raw_observation_count": len(samples),
        "brier_score": round(brier, 6),
        "expected_calibration_error": round(weighted_gap, 6),
        "calibration_bins": bins,
    }


def distribution_js(left: Counter[str], right: Counter[str]) -> float | None:
    if not left or not right:
        return None
    keys = sorted(set(left) | set(right))
    left_total = sum(left.values())
    right_total = sum(right.values())
    p = [left[key] / left_total for key in keys]
    q = [right[key] / right_total for key in keys]
    m = [(a + b) / 2 for a, b in zip(p, q)]

    def kl(values: list[float], middle: list[float]) -> float:
        return sum(value * math.log2(value / mid) for value, mid in zip(values, middle) if value > 0 and mid > 0)

    return round((kl(p, m) + kl(q, m)) / 2, 6)


def probability_bucket(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "unknown"
    lower = min(int(numeric * 5) * 20, 80)
    return f"{lower:02d}-{lower + 20:02d}%"


def forecast_diagnostics(
    as_of: date_type,
    policy: dict[str, Any],
    records: list[dict[str, Any]],
    family_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lookback = int(policy.get("lookback_days", 28))
    recent_start = as_of - timedelta(days=lookback - 1)
    baseline_end = recent_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=lookback - 1)
    point_records = point_in_time_records(records, as_of)
    overall = proper_scoring_metrics(
        point_records, cutoff=as_of.isoformat(), minimum_sample=1, family_registry=family_registry
    )
    dates = event_review_dates(point_records)
    recent_samples = [item for item in overall.get("samples", []) if recent_start <= dates.get(str(item.get("prediction_id")), date_type.min) <= as_of]
    baseline_samples = [item for item in overall.get("samples", []) if baseline_start <= dates.get(str(item.get("prediction_id")), date_type.min) <= baseline_end]
    recent_metrics = sample_metrics(recent_samples)
    baseline_metrics = sample_metrics(baseline_samples)
    minimum = int(policy.get("minimum_event_samples", 10))
    minimum_ece = int(policy.get("minimum_ece_samples", 30))
    brier_delta = None
    ece_delta = None
    if recent_metrics["sample_count"] >= minimum and baseline_metrics["sample_count"] >= minimum:
        brier_delta = round(recent_metrics["brier_score"] - baseline_metrics["brier_score"], 6)
    if recent_metrics["sample_count"] >= minimum_ece and baseline_metrics["sample_count"] >= minimum_ece:
        ece_delta = round(recent_metrics["expected_calibration_error"] - baseline_metrics["expected_calibration_error"], 6)
    if brier_delta is None:
        status = "insufficient_sample"
    else:
        status = worst_status([
            metric_status(
                brier_delta,
                watch=float(policy.get("brier_deterioration_watch", 0.05)),
                alert=float(policy.get("brier_deterioration_alert", 0.10)),
            ),
            metric_status(
                ece_delta,
                watch=float(policy.get("ece_deterioration_watch", 0.07)),
                alert=float(policy.get("ece_deterioration_alert", 0.12)),
            ) if ece_delta is not None else "healthy",
        ])

    originals = [row for row in point_records if not isinstance(row.get("review"), dict)]
    recent_originals = [row for row in originals if recent_start <= parse_date(row.get("date")) <= as_of]
    baseline_originals = [row for row in originals if baseline_start <= parse_date(row.get("date")) <= baseline_end]
    recent_horizons = Counter(str(row.get("horizon") or "unknown") for row in recent_originals)
    baseline_horizons = Counter(str(row.get("horizon") or "unknown") for row in baseline_originals)
    recent_probabilities = Counter(probability_bucket(row.get("probability")) for row in recent_originals)
    baseline_probabilities = Counter(probability_bucket(row.get("probability")) for row in baseline_originals)
    composition_min = int(policy.get("minimum_composition_predictions", 10))
    composition_status = "healthy" if len(recent_originals) >= composition_min and len(baseline_originals) >= composition_min else "insufficient_sample"
    signals = []
    if status in {"watch", "alert"}:
        signals.append("recent event proper scores deteriorated relative to the preceding point-in-time window")
    if status == "insufficient_sample":
        signals.append("sample is too small for a drift claim; no quality conclusion is drawn")
    return {
        "status": status,
        "recent_window": {"start": recent_start.isoformat(), "end": as_of.isoformat(), **recent_metrics},
        "baseline_window": {"start": baseline_start.isoformat(), "end": baseline_end.isoformat(), **baseline_metrics},
        "brier_deterioration": brier_delta,
        "ece_deterioration": ece_delta,
        "cumulative_point_in_time": {
            "eligible_sample_count": overall.get("eligible_sample_count"),
            "matured_v2_prediction_count": overall.get("matured_v2_prediction_count"),
            "resolved_coverage_pct": overall.get("resolved_coverage_pct"),
            "brier_score": overall.get("brier_score"),
            "expected_calibration_error": overall.get("expected_calibration_error"),
        },
        "composition": {
            "status": composition_status,
            "recent_prediction_count": len(recent_originals),
            "baseline_prediction_count": len(baseline_originals),
            "recent_horizons": dict(sorted(recent_horizons.items())),
            "baseline_horizons": dict(sorted(baseline_horizons.items())),
            "horizon_js_divergence": distribution_js(recent_horizons, baseline_horizons) if composition_status == "healthy" else None,
            "recent_probability_bins": dict(sorted(recent_probabilities.items())),
            "baseline_probability_bins": dict(sorted(baseline_probabilities.items())),
            "probability_js_divergence": distribution_js(recent_probabilities, baseline_probabilities) if composition_status == "healthy" else None,
        },
        "signals": signals,
        "method": "Only schema-v2 event outcomes eligible for proper scoring and known by the as-of date are compared. Market-mapping outcomes are excluded.",
        "thresholds": {
            "minimum_event_samples_per_window": minimum,
            "minimum_ece_samples_per_window": minimum_ece,
            "brier_deterioration_watch": float(policy.get("brier_deterioration_watch", 0.05)),
            "brier_deterioration_alert": float(policy.get("brier_deterioration_alert", 0.10)),
            "ece_deterioration_watch": float(policy.get("ece_deterioration_watch", 0.07)),
            "ece_deterioration_alert": float(policy.get("ece_deterioration_alert", 0.12)),
        },
    }


def record_text(record: dict[str, Any]) -> str:
    values: list[str] = []
    for field in ("scenario", "trigger", "drivers", "beneficiaries", "pressure"):
        value = record.get(field)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return " ".join(values).lower()


def keyword_matches(text: str, keyword: str) -> bool:
    text = text.lower()
    clean = keyword.lower().strip()
    if not clean:
        return False
    if clean.isascii() and any(character.isalnum() for character in clean):
        return re.search(rf"(?<![a-z0-9]){re.escape(clean)}(?![a-z0-9])", text) is not None
    return clean in text


def classify_themes(record: dict[str, Any], taxonomy: dict[str, list[str]]) -> list[str]:
    text = record_text(record)
    return sorted({theme for theme, keywords in taxonomy.items() if any(keyword_matches(text, str(keyword)) for keyword in keywords)})


def theme_diagnostics(as_of: date_type, policy: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    lookback = int(policy.get("lookback_days", 28))
    start = as_of - timedelta(days=lookback - 1)
    originals = [
        row for row in point_in_time_records(records, as_of)
        if not isinstance(row.get("review"), dict) and start <= parse_date(row.get("date")) <= as_of
    ]
    taxonomy = {str(key): [str(value) for value in values] for key, values in policy.get("theme_taxonomy", {}).items()}
    themes: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    directional: Counter[str] = Counter()
    unmapped = []
    for row in originals:
        assigned = classify_themes(row, taxonomy)
        if not assigned:
            unmapped.append(str(row.get("prediction_id") or "unknown"))
        for theme in assigned:
            themes[theme] += 1
        seen_symbols = set()
        seen_directional = set()
        for mapping in row.get("market_mapping", []) if isinstance(row.get("market_mapping"), list) else []:
            if not isinstance(mapping, dict) or not mapping.get("symbol"):
                continue
            symbol = str(mapping["symbol"]).upper()
            direction = str(mapping.get("direction") or "unknown").lower()
            seen_symbols.add(symbol)
            seen_directional.add(f"{symbol}|{direction}")
        for symbol in seen_symbols:
            symbols[symbol] += 1
        for exposure in seen_directional:
            directional[exposure] += 1
    theme_top, theme_top_pct = top_share(themes)
    symbol_top, symbol_top_pct = top_share(symbols)
    theme_hhi = hhi(themes)
    minimum = int(policy.get("minimum_theme_predictions", 10))
    if len(originals) < minimum or not themes:
        status = "insufficient_sample"
    else:
        status = worst_status([
            metric_status(theme_hhi, watch=float(policy.get("theme_hhi_watch", 0.20)), alert=float(policy.get("theme_hhi_alert", 0.30))),
            metric_status(theme_top_pct, watch=float(policy.get("top_theme_share_watch_pct", 45.0)), alert=float(policy.get("top_theme_share_alert_pct", 60.0))),
            metric_status(symbol_top_pct, watch=float(policy.get("single_symbol_share_watch_pct", 30.0)), alert=float(policy.get("single_symbol_share_alert_pct", 45.0))),
        ])
    signals = []
    if status in {"watch", "alert"}:
        signals.append("forecast themes or mapped instruments are concentrated; repeated narratives need explicit counter-theses")
    if originals and len(unmapped) / len(originals) > 0.25:
        signals.append("more than 25% of recent forecasts are outside the configured deterministic theme taxonomy")
    return {
        "status": status,
        "window_start": start.isoformat(),
        "window_end": as_of.isoformat(),
        "prediction_count": len(originals),
        "theme_assignment_count": sum(themes.values()),
        "theme_hhi": theme_hhi,
        "top_theme": theme_top,
        "top_theme_share_pct": theme_top_pct,
        "theme_counts": dict(themes.most_common()),
        "unmapped_prediction_count": len(unmapped),
        "unmapped_prediction_ids": unmapped,
        "mapped_instrument_count": sum(symbols.values()),
        "single_symbol_top": symbol_top,
        "single_symbol_top_share_pct": symbol_top_pct,
        "symbol_counts": dict(symbols.most_common()),
        "directional_exposure_counts": dict(directional.most_common()),
        "signals": signals,
        "method": "Configured keyword taxonomy; each forecast contributes at most once to a theme and once to a symbol. No semantic model or future data is used.",
        "thresholds": {
            "minimum_theme_predictions": minimum,
            "theme_hhi_watch": float(policy.get("theme_hhi_watch", 0.20)),
            "theme_hhi_alert": float(policy.get("theme_hhi_alert", 0.30)),
            "top_theme_share_watch_pct": float(policy.get("top_theme_share_watch_pct", 45.0)),
            "top_theme_share_alert_pct": float(policy.get("top_theme_share_alert_pct", 60.0)),
            "single_symbol_share_watch_pct": float(policy.get("single_symbol_share_watch_pct", 30.0)),
            "single_symbol_share_alert_pct": float(policy.get("single_symbol_share_alert_pct", 45.0)),
        },
    }


def paper_account_diagnostics(
    attribution: dict[str, Any],
    paper_config: dict[str, Any],
    as_of: date_type,
    policy: dict[str, Any],
) -> dict[str, Any]:
    account_rows = []
    stale_after = int(policy.get("paper_price_stale_after_calendar_days", 4))
    stale_watch = float(policy.get("paper_stale_value_watch_pct", 20.0))
    stale_alert = float(policy.get("paper_stale_value_alert_pct", 50.0))
    concentration_watch_ratio = float(policy.get("paper_position_limit_watch_ratio", 0.90))
    max_position_rule = float(paper_config.get("max_position_pct", 0.35)) * 100
    min_cash_rule = float(paper_config.get("min_cash_pct", 0.02)) * 100
    taxonomy = {str(key): [str(value) for value in values] for key, values in policy.get("theme_taxonomy", {}).items()}
    for account in attribution.get("accounts", []):
        if not isinstance(account, dict):
            continue
        valuation = account.get("latest_valuation", {})
        positions = account.get("positions", []) if isinstance(account.get("positions"), list) else []
        equity = float(valuation.get("equity") or 0.0)
        cash = float(valuation.get("cash") or 0.0)
        weights: dict[str, float] = {}
        stale_value = 0.0
        unknown_price_date_value = 0.0
        unlinked_value = 0.0
        explicit_theme_value = 0.0
        explicit_theme_values: dict[str, float] = {}
        derived_theme_counts: Counter[str] = Counter()
        absolute_pnl: dict[str, float] = {}
        position_rows = []
        for position in positions:
            market_value = float(position.get("market_value") or 0.0)
            symbol = str(position.get("symbol") or "unknown")
            weight = market_value / equity * 100 if equity else 0.0
            weights[symbol] = weights.get(symbol, 0.0) + weight
            price_date_raw = position.get("last_price_date")
            age = None
            if price_date_raw:
                try:
                    age = (as_of - parse_date(price_date_raw)).days
                except ValueError:
                    age = None
            if age is None:
                unknown_price_date_value += market_value
            elif age > stale_after:
                stale_value += market_value
            if str(position.get("prediction_id") or "unlinked") == "unlinked":
                unlinked_value += market_value
            explicit_theme = position.get("theme")
            if explicit_theme:
                explicit_theme_value += market_value
                explicit_theme_values[str(explicit_theme)] = explicit_theme_values.get(str(explicit_theme), 0.0) + market_value
            derived = classify_themes({"scenario": position.get("scenario")}, taxonomy)
            for theme in derived:
                derived_theme_counts[theme] += 1
            pnl = abs(float(position.get("unrealized_pnl") or 0.0) + float(position.get("realized_pnl") or 0.0))
            absolute_pnl[symbol] = absolute_pnl.get(symbol, 0.0) + pnl
            position_rows.append({
                "symbol": symbol,
                "market_value": round(market_value, 6),
                "weight_pct": round(weight, 2),
                "last_price_date": price_date_raw,
                "price_age_calendar_days": age,
                "price_date_provenance": position.get("last_price_date_provenance"),
                "prediction_id": position.get("prediction_id"),
                "explicit_theme": explicit_theme,
                "theme_source": position.get("theme_source"),
                "secondary_themes": position.get("secondary_themes", []),
                "theme_conflict": position.get("theme_conflict", False),
                "derived_themes": derived,
            })
        invested = sum(float(item.get("market_value") or 0.0) for item in positions)
        max_symbol, max_weight = top_share({key: int(round(value * 10000)) for key, value in weights.items()})
        # top_share above normalizes weights again; use the actual maximum portfolio weight.
        max_weight = round(max(weights.values()), 2) if weights else None
        stale_pct = round(stale_value / invested * 100, 2) if invested else 0.0
        unknown_price_pct = round(unknown_price_date_value / invested * 100, 2) if invested else 0.0
        cash_pct = round(cash / equity * 100, 2) if equity else None
        unlinked_pct = round(unlinked_value / invested * 100, 2) if invested else 0.0
        theme_coverage = round(explicit_theme_value / invested * 100, 2) if invested else 100.0
        theme_weights = {key: value / equity * 100 for key, value in explicit_theme_values.items()} if equity else {}
        largest_theme = max(theme_weights, key=theme_weights.get) if theme_weights else None
        largest_theme_pct = round(max(theme_weights.values()), 2) if theme_weights else None
        maximum_theme_rule = float(
            paper_config.get("strategy_profile", {}).get("risk_overlays", {}).get("maximum_theme_exposure_pct", 1.0)
        ) * 100
        pnl_total = sum(absolute_pnl.values())
        pnl_top_symbol, pnl_top_share = top_share({key: int(round(value * 1000000)) for key, value in absolute_pnl.items()})
        account_statuses = [
            metric_status(stale_pct, watch=stale_watch, alert=stale_alert),
            "alert" if max_weight is not None and max_weight > max_position_rule + 0.01 else "watch" if max_weight is not None and max_weight >= max_position_rule * concentration_watch_ratio else "healthy",
            "alert" if cash_pct is not None and cash_pct < min_cash_rule else "healthy",
            "watch" if invested and theme_coverage < 80 else "healthy",
            "watch" if invested and unknown_price_pct > 20 else "healthy",
            "alert" if largest_theme_pct is not None and largest_theme_pct > maximum_theme_rule + 0.01 else "watch" if largest_theme_pct is not None and largest_theme_pct >= maximum_theme_rule * concentration_watch_ratio else "healthy",
        ]
        status = worst_status(account_statuses)
        signals = []
        if max_weight is not None and max_weight >= max_position_rule * concentration_watch_ratio:
            signals.append("single-position exposure is close to or above the configured hard limit")
        if stale_pct >= stale_watch:
            signals.append("a material share of invested value uses prices older than the configured calendar-day tolerance")
        if theme_coverage < 80 and invested:
            signals.append("explicit position-theme coverage is below 80%; the configured theme cap cannot yet be audited reliably")
        if unknown_price_pct > 20 and invested:
            signals.append("price-date provenance is incomplete; freshness conclusions are conservative")
        if largest_theme_pct is not None and largest_theme_pct >= maximum_theme_rule * concentration_watch_ratio:
            signals.append("primary-theme exposure is close to or above the configured hard limit")
        account_rows.append({
            "account": account.get("account"),
            "base_currency": valuation.get("base_currency"),
            "status": status,
            "equity": round(equity, 6),
            "cash": round(cash, 6),
            "cash_pct": cash_pct,
            "invested_pct": round(invested / equity * 100, 2) if equity else None,
            "position_count": len(positions),
            "position_hhi": round(sum((value / 100) ** 2 for value in weights.values()), 6) if weights else None,
            "largest_position_symbol": max(weights, key=weights.get) if weights else None,
            "largest_position_weight_pct": max_weight,
            "stale_position_value_pct": stale_pct,
            "unknown_price_date_value_pct": unknown_price_pct,
            "unlinked_prediction_value_pct": unlinked_pct,
            "explicit_theme_attribution_coverage_pct": theme_coverage,
            "primary_theme_exposure_pct": {key: round(value, 2) for key, value in sorted(theme_weights.items())},
            "largest_primary_theme": largest_theme,
            "largest_primary_theme_exposure_pct": largest_theme_pct,
            "derived_theme_position_counts": dict(derived_theme_counts.most_common()),
            "largest_absolute_pnl_contributor": pnl_top_symbol if pnl_total else None,
            "largest_absolute_pnl_share_pct": pnl_top_share if pnl_total else None,
            "positions": position_rows,
            "signals": signals,
        })
    return {
        "status": worst_status(row["status"] for row in account_rows),
        "accounts": account_rows,
        "accounts_are_never_aggregated": True,
        "paper_trading_only": True,
        "method": "US and CHINA accounts are evaluated independently in their own base currencies from point-in-time reconstructed attribution. No cross-account equity or P/L total is produced.",
        "thresholds": {
            "configured_max_position_pct": max_position_rule,
            "configured_min_cash_pct": min_cash_rule,
            "configured_maximum_theme_exposure_pct": float(paper_config.get("strategy_profile", {}).get("risk_overlays", {}).get("maximum_theme_exposure_pct", 1.0)) * 100,
            "paper_position_limit_watch_ratio": concentration_watch_ratio,
            "paper_price_stale_after_calendar_days": stale_after,
            "paper_stale_value_watch_pct": stale_watch,
            "paper_stale_value_alert_pct": stale_alert,
        },
    }


def input_manifest(as_of: date_type, source_paths: list[Path], paper_config: dict[str, Any]) -> dict[str, Any]:
    paths = [SETTINGS_PATH, SOURCES_PATH, PAPER_CONFIG_PATH, PREDICTIONS_PATH, *source_paths]
    for account in paper_config.get("accounts", {}).values():
        if not isinstance(account, dict):
            continue
        for field in ("trades_file", "valuations_file"):
            if account.get(field):
                paths.append(ROOT / str(account[field]))
    if paper_config.get("theme_registry_file"):
        registry = Path(str(paper_config["theme_registry_file"]))
        paths.append(registry if registry.is_absolute() else ROOT / registry)
    if paper_config.get("theme_registry_history_file"):
        history = Path(str(paper_config["theme_registry_history_file"]))
        paths.append(history if history.is_absolute() else ROOT / history)
    unique = sorted({path.resolve() for path in paths}, key=str)
    return {
        "as_of": as_of.isoformat(),
        "files": [
            {"path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path), "sha256": file_hash(path)}
            for path in unique
        ],
    }


def build_diagnostics(date: str) -> dict[str, Any]:
    as_of = parse_date(date)
    settings = read_json(SETTINGS_PATH, {})
    policy = settings.get("drift_diagnostics", {}) if isinstance(settings, dict) else {}
    if policy.get("enabled") is False:
        raise ValueError("drift diagnostics are disabled in settings.json")
    sources = read_json(SOURCES_PATH, {})
    paper_config = read_json(PAPER_CONFIG_PATH, {})
    records = read_jsonl(PREDICTIONS_PATH)
    source_result, source_paths = source_diagnostics(as_of, policy, sources)
    contract = settings.get("prediction_contract", {}) if isinstance(settings, dict) else {}
    registry_file = str(contract.get("family_registry_file") or "").strip()
    family_registry = read_json(ROOT / registry_file, {}) if registry_file else {}
    forecast_result = forecast_diagnostics(as_of, policy, records, family_registry)
    theme_result = theme_diagnostics(as_of, policy, records)
    attribution = paper_attribution("day", date)
    paper_result = paper_account_diagnostics(attribution, paper_config, as_of, policy)
    manifest = input_manifest(as_of, source_paths, paper_config)
    statuses = {
        "source_concentration": source_result["status"],
        "forecast_calibration": forecast_result["status"],
        "theme_crowding": theme_result["status"],
        "paper_account_attribution": paper_result["status"],
    }
    return {
        "schema_version": 1,
        "date": date,
        "timezone": settings.get("timezone", "Asia/Shanghai"),
        "gate_mode": str(policy.get("gate_mode") or "shadow"),
        "input_fingerprint": stable_hash(manifest),
        "input_manifest": manifest,
        "dimension_statuses": statuses,
        "attention_dimensions": [key for key, value in statuses.items() if value in {"watch", "alert"}],
        "source_concentration": source_result,
        "forecast_calibration": forecast_result,
        "theme_crowding": theme_result,
        "paper_account_attribution": paper_result,
        "no_opaque_composite_score": True,
        "deployment_blocking": False,
        "automatic_mutations": [],
        "limitations": [
            "Shadow mode reports independent dimension states and does not block publication.",
            "Insufficient samples remain insufficient; they are never converted into a healthy or poor score.",
            "Theme classification is deterministic taxonomy matching, not a semantic truth claim.",
            "Legacy paper rows without an explicit price_date retain conservative provenance warnings.",
        ],
    }


def artifact_path(date: str) -> Path:
    return DATA_DIR / f"drift-diagnostics-{date}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate point-in-time ATLAS drift diagnostics.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    payload = build_diagnostics(args.date)
    if args.write:
        path = artifact_path(args.date)
        changed = atomic_write_json(path, payload)
        print(json.dumps({
            "status": "changed" if changed else "unchanged",
            "date": args.date,
            "path": str(path),
            "input_fingerprint": payload["input_fingerprint"],
            "dimension_statuses": payload["dimension_statuses"],
            "deployment_blocking": False,
        }, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
