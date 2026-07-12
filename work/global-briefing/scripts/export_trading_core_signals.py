#!/usr/bin/env python3
"""Export daily briefing predictions to trading-core macro signals.

The exporter is deliberately narrow: it only maps open predictions for one
date and only keeps instruments present in trading-core's configured China ETF
universe.  It does not create orders, execute a portfolio workflow, or mutate a
broker/account state.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT = ROOT / "work" / "global-briefing" / "data" / "predictions.jsonl"
DEFAULT_UNIVERSE = ROOT / "work" / "trading-core" / "config" / "universe_china_etf.yaml"
DEFAULT_DATA_DIR = ROOT / "work" / "global-briefing" / "data"


@dataclass(frozen=True)
class ExportResult:
    date: str
    predictions_seen: int
    predictions_exported: int
    signals: list[dict[str, Any]]
    skipped: list[dict[str, str]]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: row is not an object")
        rows.append(row)
    return rows


def load_universe_symbols(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Trading universe must be JSON-compatible YAML: {path}") from exc
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    result = {
        str(item.get("symbol", "")).strip().upper()
        for item in symbols
        if isinstance(item, dict) and str(item.get("symbol", "")).strip()
    }
    if not result:
        raise ValueError(f"Trading universe contains no symbols: {path}")
    return result


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol.endswith(".SS"):
        return f"{symbol[:-3]}.SH"
    return symbol


def ticker_symbols(prediction: dict[str, Any]) -> Iterable[str]:
    raw = prediction.get("tickers", [])
    if not isinstance(raw, list):
        return []
    symbols: list[str] = []
    for item in raw:
        value = item.get("symbol") if isinstance(item, dict) else item
        symbol = normalize_symbol(value)
        if symbol:
            symbols.append(symbol)
    return symbols


def normalize_confidence(value: Any) -> str:
    text = str(value or "low").strip().lower()
    aliases = {
        "高": "high",
        "中": "medium",
        "低": "low",
        "med": "medium",
        "moderate": "medium",
    }
    normalized = aliases.get(text, text)
    return normalized if normalized in {"high", "medium", "low"} else "low"


def build_theme(prediction: dict[str, Any]) -> str:
    beneficiaries = prediction.get("beneficiaries", [])
    if isinstance(beneficiaries, list):
        values = [str(item).strip() for item in beneficiaries if str(item).strip()]
        if values:
            return " / ".join(values[:3])
    return "global_briefing"


def build_risk_flags(prediction: dict[str, Any], assets: list[str]) -> list[str]:
    flags: list[str] = []
    probability = normalize_confidence(prediction.get("probability"))
    if probability == "low":
        flags.append("low_confidence")
    if any(symbol.endswith(".HK") for symbol in assets):
        flags.append("cross_market_hk")
    return flags


def export_predictions(
    predictions: list[dict[str, Any]],
    *,
    date: str,
    universe: set[str],
) -> ExportResult:
    selected = [row for row in predictions if str(row.get("date", "")) == date]
    signals: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for index, prediction in enumerate(selected, 1):
        prediction_id = str(prediction.get("prediction_id") or f"{date}-P{index:02d}")
        status = str(prediction.get("status") or "open").strip().lower()
        if status not in {"open", "active"}:
            skipped.append({"prediction_id": prediction_id, "reason": f"status={status}"})
            continue

        assets = list(dict.fromkeys(symbol for symbol in ticker_symbols(prediction) if symbol in universe))
        if not assets:
            skipped.append({"prediction_id": prediction_id, "reason": "no supported China ETF assets"})
            continue

        scenario = str(prediction.get("scenario") or "").strip()
        if not scenario:
            skipped.append({"prediction_id": prediction_id, "reason": "missing scenario"})
            continue

        compact_id = prediction_id.replace("-", "")
        signals.append(
            {
                "macro_signal_id": f"GB-{compact_id}",
                "source": "global_briefing_predictions",
                "source_prediction_id": prediction_id,
                "date": date,
                "region": "CHINA",
                "theme": build_theme(prediction),
                "scenario": scenario,
                "horizon": str(prediction.get("horizon") or "").strip(),
                "confidence": normalize_confidence(prediction.get("probability")),
                "affected_assets": assets,
                "risk_flags": build_risk_flags(prediction, assets),
                "status": "open",
            }
        )

    signals.sort(key=lambda row: str(row["macro_signal_id"]))
    return ExportResult(
        date=date,
        predictions_seen=len(selected),
        predictions_exported=len(signals),
        signals=signals,
        skipped=skipped,
    )


def latest_prediction_date(predictions: list[dict[str, Any]]) -> str:
    dates = sorted({str(row.get("date", "")) for row in predictions if str(row.get("date", ""))})
    if not dates:
        raise ValueError("Prediction store contains no dated rows")
    return dates[-1]


def serialize_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def write_atomic(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return "unchanged"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return "written"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export global briefing predictions to trading-core macro signal JSONL."
    )
    parser.add_argument("--date", help="Prediction date (defaults to latest date in the store).")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Prediction JSONL path.")
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE, help="Trading-core universe path.")
    parser.add_argument("--output", type=Path, help="Output JSONL path.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the export without writing.")
    parser.add_argument("--allow-empty", action="store_true", help="Allow an empty signal export.")
    args = parser.parse_args(argv)

    try:
        predictions = read_jsonl(args.input)
        date = args.date or latest_prediction_date(predictions)
        universe = load_universe_symbols(args.universe)
        result = export_predictions(predictions, date=date, universe=universe)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2

    output = args.output or DEFAULT_DATA_DIR / f"macro_signals-{date}.jsonl"
    if not result.signals and not args.allow_empty:
        print(
            json.dumps(
                {
                    "status": "blocked_empty_export",
                    "date": date,
                    "predictions_seen": result.predictions_seen,
                    "skipped": result.skipped,
                    "output": str(output),
                },
                ensure_ascii=False,
            )
        )
        return 1

    write_status = "dry_run" if args.dry_run else write_atomic(output, serialize_jsonl(result.signals))
    print(
        json.dumps(
            {
                "status": write_status,
                "date": date,
                "predictions_seen": result.predictions_seen,
                "signals_exported": len(result.signals),
                "skipped": result.skipped,
                "output": str(output),
                "paper_research_only": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
