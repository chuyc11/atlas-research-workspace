from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "market_snapshot.py"
SPEC = importlib.util.spec_from_file_location("market_snapshot_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class FakeSeries:
    def __init__(self, values: list[float], dates: list[date]) -> None:
        self.values = values
        self.index = dates
        self.iloc = self

    @property
    def empty(self) -> bool:
        return not self.values

    def dropna(self):
        return self

    def __len__(self) -> int:
        return len(self.values)

    def __getitem__(self, index: int) -> float:
        return self.values[index]


class FakeFrame:
    empty = False

    def __init__(self, closes: FakeSeries) -> None:
        self.closes = closes
        self.columns = SimpleNamespace(nlevels=1)

    def __getitem__(self, key: str) -> FakeSeries:
        if key != "Close":
            raise KeyError(key)
        return self.closes


class FakeQueue:
    def __init__(self, value: dict | None = None) -> None:
        self.value = value

    def empty(self) -> bool:
        return self.value is None

    def get(self) -> dict:
        assert self.value is not None
        return self.value

    def put(self, value: dict) -> None:
        self.value = value


class FakeProcess:
    def __init__(self, *, alive: bool = False, exitcode: int = 0) -> None:
        self.alive = alive
        self.exitcode = exitcode
        self.started = False
        self.terminated = False

    def start(self) -> None:
        self.started = True

    def join(self, _timeout: int) -> None:
        return None

    def is_alive(self) -> bool:
        return self.alive and not self.terminated

    def terminate(self) -> None:
        self.terminated = True


class FakeContext:
    def __init__(self, queue: FakeQueue, process: FakeProcess) -> None:
        self.queue = queue
        self.process = process

    def Queue(self) -> FakeQueue:
        return self.queue

    def Process(self, **_kwargs) -> FakeProcess:
        return self.process


class MarketSnapshotTests(unittest.TestCase):
    def test_numeric_parsing_and_exact_yahoo_allowlist(self) -> None:
        self.assertEqual(MODULE.as_float("1.25"), 1.25)
        self.assertIsNone(MODULE.as_float(None))
        self.assertIsNone(MODULE.as_float("bad"))
        rejected = MODULE.urllib.request.Request("https://query2.finance.yahoo.com.evil.test/chart")
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            MODULE.open_yahoo_url(rejected, 1)
        allowed = MODULE.urllib.request.Request("https://query2.finance.yahoo.com/chart")
        with patch.object(MODULE.urllib.request, "urlopen", return_value="response") as urlopen:
            self.assertEqual(MODULE.open_yahoo_url(allowed, 3), "response")
        urlopen.assert_called_once_with(allowed, timeout=3)

    def test_close_series_reports_prices_and_empty_inputs(self) -> None:
        frame = FakeFrame(FakeSeries([100.0, 105.0], [date(2026, 7, 14), date(2026, 7, 15)]))
        with patch.object(MODULE, "now_utc", return_value="now"):
            result = MODULE.parse_close_series(frame, "SPY")
        self.assertEqual(result["last_close"], 105.0)
        self.assertEqual(result["previous_close"], 100.0)
        self.assertAlmostEqual(result["change_pct"], 5.0)
        self.assertEqual(result["price_date"], "2026-07-15")
        self.assertEqual(result["fetched_at"], "now")
        self.assertEqual(MODULE.parse_close_series(SimpleNamespace(empty=True), "SPY")["error"], "no price data")
        empty = FakeFrame(FakeSeries([], []))
        self.assertEqual(MODULE.parse_close_series(empty, "SPY")["error"], "no close data")

    def test_batch_worker_timeout_result_and_empty_worker_paths(self) -> None:
        timeout_process = FakeProcess(alive=True)
        timeout_context = FakeContext(FakeQueue(), timeout_process)
        with patch.object(MODULE.mp, "get_context", return_value=timeout_context):
            timed_out = MODULE.fetch_batch_with_timeout(["SPY"], 1, 2)
        self.assertTrue(timeout_process.started)
        self.assertTrue(timeout_process.terminated)
        self.assertIn("timed out", timed_out[0]["error"])

        result_context = FakeContext(FakeQueue({"items": [{"ticker": "SPY", "last_close": 1.0}]}), FakeProcess())
        with patch.object(MODULE.mp, "get_context", return_value=result_context):
            result = MODULE.fetch_batch_with_timeout(["SPY"], 1, 2)
        self.assertEqual(result[0]["last_close"], 1.0)

        empty_context = FakeContext(FakeQueue(), FakeProcess(exitcode=7))
        with patch.object(MODULE.mp, "get_context", return_value=empty_context):
            empty = MODULE.fetch_batch_with_timeout(["SPY"], 1, 2)
        self.assertIn("code 7", empty[0]["error"])

    def test_yfinance_worker_reports_success_and_provider_failure(self) -> None:
        frame = FakeFrame(FakeSeries([100.0, 101.0], [date(2026, 7, 14), date(2026, 7, 15)]))
        queue = FakeQueue()
        provider = SimpleNamespace(download=lambda *_args, **_kwargs: frame)
        with patch.dict(sys.modules, {"yfinance": provider}), patch.object(MODULE, "now_utc", return_value="now"):
            MODULE.fetch_yfinance_batch(["SPY"], 2, queue)
        assert queue.value is not None
        self.assertEqual(queue.value["items"][0]["last_close"], 101.0)

        def failed_download(*_args, **_kwargs):
            raise OSError("provider offline")

        queue = FakeQueue()
        provider = SimpleNamespace(download=failed_download)
        with patch.dict(sys.modules, {"yfinance": provider}):
            MODULE.fetch_yfinance_batch(["SPY", "QQQ"], 2, queue)
        assert queue.value is not None
        self.assertEqual(len(queue.value["items"]), 2)
        self.assertTrue(all("provider offline" in item["error"] for item in queue.value["items"]))

    def test_yahoo_chart_success_and_failure_contracts(self) -> None:
        payload = {
            "chart": {
                "result": [{
                    "timestamp": [1721001600, 1721088000, 1721174400],
                    "indicators": {"quote": [{"close": [100, None, 110]}]},
                }]
            }
        }
        with (
            patch.object(MODULE, "open_yahoo_url", return_value=FakeResponse(payload)),
            patch.object(MODULE, "now_utc", return_value="now"),
        ):
            result = MODULE.fetch_yahoo_chart("SPY", 2)
        self.assertEqual(result["last_close"], 110.0)
        self.assertEqual(result["previous_close"], 100.0)
        self.assertAlmostEqual(result["change_pct"], 10.0)
        self.assertEqual(result["price_date"], "2024-07-17")

        with patch.object(MODULE, "open_yahoo_url", side_effect=OSError("offline")):
            self.assertIn("offline", MODULE.fetch_yahoo_chart("SPY", 2)["error"])
        with patch.object(MODULE, "open_yahoo_url", return_value=FakeResponse({"chart": {"result": []}})):
            self.assertIn("not_found", MODULE.fetch_yahoo_chart("SPY", 2)["error"])
        no_close = {"chart": {"result": [{"indicators": {"quote": [{"close": [None]}]}}]}}
        with patch.object(MODULE, "open_yahoo_url", return_value=FakeResponse(no_close)):
            self.assertIn("no close", MODULE.fetch_yahoo_chart("SPY", 2)["error"])

    def test_snapshot_falls_back_per_ticker_without_losing_batch_error(self) -> None:
        batch = [
            {"ticker": "SPY", "last_close": 100.0},
            {"ticker": "QQQ", "error": "batch failed"},
            {"ticker": "DIA", "error": "batch failed"},
        ]

        def fallback(ticker: str, timeout: int) -> dict:
            self.assertEqual(timeout, 15)
            if ticker == "QQQ":
                return {"ticker": ticker, "last_close": 200.0}
            return {"ticker": ticker, "error": "fallback failed"}

        with (
            patch.object(MODULE, "fetch_batch_with_timeout", return_value=batch) as fetch_batch,
            patch.object(MODULE, "fetch_yahoo_chart", side_effect=fallback),
            patch.object(MODULE.time, "sleep"),
            patch.object(MODULE, "now_utc", return_value="now"),
        ):
            result = MODULE.snapshot(["SPY", "QQQ", "DIA"], 3)
        fetch_batch.assert_called_once_with(["SPY", "QQQ", "DIA"], request_timeout=15, total_timeout=45)
        self.assertEqual(result["generated_at"], "now")
        self.assertEqual(result["items"][1]["last_close"], 200.0)
        self.assertEqual(result["items"][2]["fallback_error"], "fallback failed")
        self.assertEqual(result["items"][2]["error"], "batch failed")

    def test_main_writes_machine_readable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            payload = {"generated_at": "now", "items": [{"ticker": "SPY", "last_close": 1.0}]}
            stream = StringIO()
            with patch.object(MODULE, "snapshot", return_value=payload), redirect_stdout(stream):
                code = MODULE.main(["SPY", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            self.assertIn("items=1 errors=0", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
