from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report_clock.py"
SPEC = importlib.util.spec_from_file_location("report_clock_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ReportClockTests(unittest.TestCase):
    def test_report_date_uses_configured_timezone_across_utc_midnight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(
                json.dumps({"timezone": "Asia/Shanghai"}),
                encoding="utf-8",
            )
            now = datetime(2026, 7, 16, 16, 30, tzinfo=UTC)

            self.assertEqual(
                MODULE.report_date(now, settings_path=settings),
                "2026-07-17",
            )

    def test_report_clock_rejects_naive_datetime(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            MODULE.report_now(datetime(2026, 7, 17, 0, 0))

    def test_report_clock_fails_closed_for_invalid_configured_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(
                json.dumps({"timezone": "Mars/Olympus"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid report timezone"):
                MODULE.report_date(settings_path=settings)


if __name__ == "__main__":
    unittest.main()
