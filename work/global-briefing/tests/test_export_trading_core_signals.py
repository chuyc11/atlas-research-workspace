from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_trading_core_signals.py"
SPEC = importlib.util.spec_from_file_location("export_trading_core_signals", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ExportTradingCoreSignalsTests(unittest.TestCase):
    def test_maps_only_open_predictions_with_supported_assets(self) -> None:
        predictions = [
            {
                "date": "2026-07-10",
                "prediction_id": "2026-07-10-P01",
                "status": "open",
                "probability": "medium",
                "scenario": "China growth remains selective",
                "beneficiaries": ["宽基ETF", "国产科技"],
                "tickers": [
                    {"symbol": "510300.SH"},
                    {"symbol": "SPY"},
                    {"symbol": "588000.SS"},
                ],
            },
            {
                "date": "2026-07-10",
                "prediction_id": "2026-07-10-P02",
                "status": "validated",
                "scenario": "Closed prediction",
                "tickers": [{"symbol": "510300.SH"}],
            },
        ]

        result = MODULE.export_predictions(
            predictions,
            date="2026-07-10",
            universe={"510300.SH", "588000.SH"},
        )

        self.assertEqual(result.predictions_seen, 2)
        self.assertEqual(result.predictions_exported, 1)
        self.assertEqual(result.signals[0]["affected_assets"], ["510300.SH", "588000.SH"])
        self.assertEqual(result.signals[0]["confidence"], "medium")
        self.assertEqual(result.signals[0]["region"], "CHINA")
        self.assertEqual(result.signals[0]["source_prediction_id"], "2026-07-10-P01")
        self.assertEqual(result.skipped[0]["reason"], "status=validated")

    def test_atomic_writer_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.jsonl"
            content = MODULE.serialize_jsonl([{"macro_signal_id": "GB-1"}])
            self.assertEqual(MODULE.write_atomic(path, content), "written")
            self.assertEqual(MODULE.write_atomic(path, content), "unchanged")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"macro_signal_id": "GB-1"})

    def test_empty_supported_assets_fail_closed_in_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            universe = root / "universe.json"
            output = root / "signals.jsonl"
            predictions.write_text(
                json.dumps(
                    {
                        "date": "2026-07-10",
                        "prediction_id": "P1",
                        "status": "open",
                        "scenario": "US only",
                        "tickers": [{"symbol": "SPY"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            universe.write_text(
                json.dumps({"symbols": [{"symbol": "510300.SH"}]}),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                code = MODULE.main(
                    [
                        "--date",
                        "2026-07-10",
                        "--input",
                        str(predictions),
                        "--universe",
                        str(universe),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(code, 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
