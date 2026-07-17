#!/usr/bin/env python3
"""Authoritative report-timezone helpers for the global briefing workflow."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
DEFAULT_SETTINGS_PATH = ROOT / "work" / "global-briefing" / "config" / "settings.json"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def configured_timezone(settings_path: Path = DEFAULT_SETTINGS_PATH) -> ZoneInfo:
    timezone_name = DEFAULT_TIMEZONE
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict) and isinstance(payload.get("timezone"), str):
        timezone_name = payload["timezone"].strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid report timezone in {settings_path}: {timezone_name}") from exc


def report_now(
    now: datetime | None = None,
    *,
    settings_path: Path = DEFAULT_SETTINGS_PATH,
) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("report clock requires a timezone-aware datetime")
    return current.astimezone(configured_timezone(settings_path))


def report_date(
    now: datetime | None = None,
    *,
    settings_path: Path = DEFAULT_SETTINGS_PATH,
) -> str:
    return report_now(now, settings_path=settings_path).date().isoformat()
