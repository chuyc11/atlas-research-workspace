#!/usr/bin/env python3
"""File-backed storage helpers for the daily global briefing automation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from research_quality import parse_date as parse_contract_date
from research_quality import validate_v2_prediction, validate_v2_review

ROOT = SCRIPT_PATH.parents[3]
REPORT_DIR = ROOT / "outputs"
DAILY_REPORT_STEM = "每日全球晨间简报"
WEEKLY_SUMMARY_STEM = "每周全球简报总结"
MONTHLY_SUMMARY_STEM = "每月全球简报总结"
DEFAULT_REPORT = REPORT_DIR / f"{DAILY_REPORT_STEM}.md"
DEFAULT_PREDICTIONS = ROOT / "work" / "global-briefing" / "data" / "predictions.jsonl"
DEFAULT_EVOLUTION_STATE = ROOT / "work" / "global-briefing" / "data" / "evolution_state.json"
DEFAULT_SETTINGS = ROOT / "work" / "global-briefing" / "config" / "settings.json"
TITLE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b.*$", re.MULTILINE)
DAILY_FILE_RE = re.compile(rf"^{re.escape(DAILY_REPORT_STEM)}-(\d{{4}}-\d{{2}}-\d{{2}})\.md$")


def ensure_files(report: Path = DEFAULT_REPORT, predictions: Path = DEFAULT_PREDICTIONS) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    predictions.parent.mkdir(parents=True, exist_ok=True)
    if not predictions.exists():
        predictions.write_text("", encoding="utf-8")
    migrate_legacy_report(report)


def parse_date(value: str) -> date_type:
    return datetime.strptime(value, "%Y-%m-%d").date()


def today_string() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def report_path_for_date(date: str) -> Path:
    return REPORT_DIR / f"{DAILY_REPORT_STEM}-{date}.md"


def summary_path_for_period(period: str, date: str) -> Path:
    current = parse_date(date)
    if period == "week":
        iso = current.isocalendar()
        return REPORT_DIR / f"{WEEKLY_SUMMARY_STEM}-{iso.year}-W{iso.week:02d}.md"
    if period == "month":
        return REPORT_DIR / f"{MONTHLY_SUMMARY_STEM}-{current:%Y-%m}.md"
    raise ValueError("period must be 'week' or 'month'.")


def period_bounds(period: str, date: str) -> tuple[date_type, date_type]:
    current = parse_date(date)
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
    raise ValueError("period must be 'week' or 'month'.")


def extract_sections(text: str) -> dict[str, str]:
    matches = list(TITLE_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[start:end].strip() + "\n"
    return sections


def migrate_legacy_report(report: Path = DEFAULT_REPORT) -> list[Path]:
    """Split the old combined report into dated files without deleting the archive."""
    if not report.exists():
        return []
    sections = extract_sections(report.read_text(encoding="utf-8"))
    written: list[Path] = []
    for section_date, section in sections.items():
        daily_path = report_path_for_date(section_date)
        if daily_path.exists():
            continue
        daily_path.write_text(normalize_report(section_date, section), encoding="utf-8")
        written.append(daily_path)
    return written


def normalize_report(date: str, content: str) -> str:
    content = content.strip()
    if not content:
        raise ValueError("Report content is empty.")
    first_line = content.splitlines()[0].strip()
    if not first_line.startswith("## "):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"## {date} 每日全球晨间简报\n\n资料检索时间：{stamp}\n\n{content}"
    return content.rstrip() + "\n"


def write_daily_report(date: str, content: str) -> tuple[Path, bool]:
    ensure_files()
    daily_path = report_path_for_date(date)
    new_section = normalize_report(date, content)
    existed = daily_path.exists()
    daily_path.write_text(new_section, encoding="utf-8")
    return daily_path, existed


def write_summary(period: str, date: str, content: str) -> tuple[Path, bool]:
    ensure_files()
    summary_path = summary_path_for_period(period, date)
    normalized = content.strip()
    if not normalized:
        raise ValueError("Summary content is empty.")
    if not normalized.startswith("# "):
        start, end = period_bounds(period, date)
        title = "每周全球简报总结" if period == "week" else "每月全球简报总结"
        normalized = f"# {title} {start:%Y-%m-%d} 至 {end:%Y-%m-%d}\n\n{normalized}"
    existed = summary_path.exists()
    summary_path.write_text(normalized.rstrip() + "\n", encoding="utf-8")
    return summary_path, existed


def load_json_records(path: Path) -> list[dict[str, Any]]:
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


def latest_section(report: Path = DEFAULT_REPORT) -> str:
    if not report.exists():
        return ""
    text = report.read_text(encoding="utf-8")
    matches = list(TITLE_RE.finditer(text))
    if not matches:
        return ""
    start = matches[-1].start()
    return text[start:].strip()


def read_report_for_date(date: str, report: Path = DEFAULT_REPORT) -> str:
    daily_path = report_path_for_date(date)
    if daily_path.exists():
        return daily_path.read_text(encoding="utf-8").strip()
    if report.exists():
        return extract_sections(report.read_text(encoding="utf-8")).get(date, "").strip()
    return ""


def dated_report_files() -> list[tuple[date_type, Path]]:
    files: list[tuple[date_type, Path]] = []
    if not REPORT_DIR.exists():
        return files
    for path in REPORT_DIR.glob(f"{DAILY_REPORT_STEM}-*.md"):
        match = DAILY_FILE_RE.match(path.name)
        if not match:
            continue
        files.append((parse_date(match.group(1)), path))
    return sorted(files)


def latest_report_before(date: str) -> tuple[str, str] | None:
    current = parse_date(date)
    candidates = [(day, path) for day, path in dated_report_files() if day < current]
    if not candidates:
        return None
    day, path = candidates[-1]
    return day.isoformat(), path.read_text(encoding="utf-8").strip()


def reports_in_period(period: str, date: str) -> list[tuple[str, Path, str]]:
    start, end = period_bounds(period, date)
    reports: list[tuple[str, Path, str]] = []
    for day, path in dated_report_files():
        if start <= day <= end:
            reports.append((day.isoformat(), path, path.read_text(encoding="utf-8").strip()))
    return reports


def append_prediction_records(input_path: Path, date: str, predictions: Path = DEFAULT_PREDICTIONS) -> int:
    ensure_files(predictions=predictions)
    raw = input_path.read_text(encoding="utf-8")
    value = json.loads(raw)
    if isinstance(value, dict):
        records = value.get("predictions", [value])
    elif isinstance(value, list):
        records = value
    else:
        raise ValueError("Prediction input must be a JSON object, a list, or an object with a predictions list.")

    existing = load_json_records(predictions)
    settings = json.loads(DEFAULT_SETTINGS.read_text(encoding="utf-8"))
    contract = settings.get("prediction_contract", {})
    enforce_from = parse_contract_date(contract.get("enforce_from_date") or "9999-12-31")
    originals = {
        str(item.get("prediction_id")): item
        for item in existing
        if item.get("prediction_id") and not isinstance(item.get("review"), dict)
    }
    prepared: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise ValueError("Each prediction record must be a JSON object.")
        record = dict(raw_record)
        record.setdefault("date", date)
        record.setdefault("status", "open")
        prediction_id = str(record.get("prediction_id") or "")
        is_review = isinstance(record.get("review"), dict)
        try:
            record_day = parse_contract_date(record.get("date"))
        except (TypeError, ValueError):
            errors.append(f"{prediction_id or '<missing-id>'}: invalid date")
            prepared.append(record)
            continue
        if not is_review and prediction_id in originals:
            errors.append(f"{prediction_id}: original prediction already exists")
        if record_day >= enforce_from:
            if is_review:
                original = originals.get(prediction_id)
                original_is_v2 = bool(
                    original
                    and original.get("schema_version") == 2
                    and parse_contract_date(original.get("date")) >= enforce_from
                )
                if original is None or original_is_v2:
                    errors.extend(validate_v2_review(record, original))
            else:
                errors.extend(validate_v2_prediction(record))
        if not is_review and prediction_id:
            originals[prediction_id] = record
        prepared.append(record)
    if errors:
        raise ValueError("Prediction contract rejected the batch: " + "; ".join(errors))

    count = 0
    with predictions.open("a", encoding="utf-8") as handle:
        for record in prepared:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def print_previous(
    date: str | None = None,
    report: Path = DEFAULT_REPORT,
    predictions: Path = DEFAULT_PREDICTIONS,
    limit: int = 20,
) -> None:
    ensure_files(report=report, predictions=predictions)
    run_date = date or today_string()
    previous_day = (parse_date(run_date) - timedelta(days=1)).isoformat()
    section = read_report_for_date(previous_day, report=report)
    used_date = previous_day
    if not section:
        fallback = latest_report_before(run_date)
        if fallback:
            used_date, section = fallback
    if not section:
        section = latest_section(report)
        used_date = "legacy-latest" if section else previous_day
    records = load_json_records(predictions)
    print(f"=== PREVIOUS_REPORT_SECTION target={previous_day} used={used_date} ===")
    print(section if section else "(none)")
    print("\n=== ACTIVE_EVOLUTION_POLICY ===")
    if DEFAULT_EVOLUTION_STATE.exists():
        print(DEFAULT_EVOLUTION_STATE.read_text(encoding="utf-8").strip())
    else:
        print("(none; run evolution.py update-policy)")
    print("\n=== RECENT_PREDICTION_RECORDS ===")
    recent = records[-limit:]
    if not recent:
        print("(none)")
        return
    for record in recent:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))


def print_summary_context(period: str, date: str) -> None:
    ensure_files()
    start, end = period_bounds(period, date)
    print(f"=== SUMMARY_CONTEXT period={period} start={start:%Y-%m-%d} end={end:%Y-%m-%d} ===")
    reports = reports_in_period(period, date)
    if not reports:
        print("(none)")
        return
    for report_date, path, content in reports:
        print(f"\n=== REPORT {report_date} path={path} ===")
        print(content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage daily global briefing report and prediction records.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create storage files and migrate legacy combined reports into dated files.")

    previous_parser = subparsers.add_parser("previous", help="Print previous-day report section and recent predictions.")
    previous_parser.add_argument("--date", default=None, help="Run date in YYYY-MM-DD. Defaults to today.")
    previous_parser.add_argument("--limit", type=int, default=20)

    write_parser = subparsers.add_parser("write", help="Write one dated Markdown report file.")
    write_parser.add_argument("--date", required=True)
    write_parser.add_argument("--input", required=True, type=Path)

    record_parser = subparsers.add_parser("record", help="Append structured prediction records from JSON.")
    record_parser.add_argument("--date", required=True)
    record_parser.add_argument("--input", required=True, type=Path)

    context_parser = subparsers.add_parser("summary-context", help="Print daily reports for a week or month.")
    context_parser.add_argument("--period", choices=["week", "month"], required=True)
    context_parser.add_argument("--date", required=True)

    summary_parser = subparsers.add_parser("write-summary", help="Write a weekly or monthly summary Markdown file.")
    summary_parser.add_argument("--period", choices=["week", "month"], required=True)
    summary_parser.add_argument("--date", required=True)
    summary_parser.add_argument("--input", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "init":
        ensure_files()
        print(f"initialized report_dir={REPORT_DIR}")
        print(f"daily_report_pattern={REPORT_DIR / (DAILY_REPORT_STEM + '-YYYY-MM-DD.md')}")
        print(f"initialized predictions={DEFAULT_PREDICTIONS}")
        return 0
    if args.command == "previous":
        print_previous(date=args.date, limit=args.limit)
        return 0
    if args.command == "write":
        path, existed = write_daily_report(args.date, args.input.read_text(encoding="utf-8"))
        print("updated" if existed else "created")
        print(path)
        return 0
    if args.command == "record":
        count = append_prediction_records(args.input, args.date)
        print(f"recorded {count} prediction(s)")
        print(DEFAULT_PREDICTIONS)
        return 0
    if args.command == "summary-context":
        print_summary_context(args.period, args.date)
        return 0
    if args.command == "write-summary":
        path, existed = write_summary(args.period, args.date, args.input.read_text(encoding="utf-8"))
        print("updated" if existed else "created")
        print(path)
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
