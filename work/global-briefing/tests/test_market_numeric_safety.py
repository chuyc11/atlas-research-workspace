from __future__ import annotations

import importlib.util
import math
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(f"numeric_safety_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHINA = load_module("china_market")
MARKET = load_module("market_snapshot")


class MarketNumericSafetyTests(unittest.TestCase):
    def test_all_numeric_parsers_reject_non_finite_values(self) -> None:
        for value in (math.nan, math.inf, -math.inf, "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.assertIsNone(CHINA.as_float(value))
                self.assertIsNone(MARKET.as_float(value))

        quote = CHINA.quote_from_record(
            {"symbol": "600000.SH", "market": "A股", "group": "a_shares"},
            {"代码": "600000", "最新价": math.nan, "涨跌幅": math.inf, "成交额": -math.inf},
            "test",
            "test",
        )
        self.assertIsNone(quote["price"])
        self.assertIsNone(quote["change_pct"])
        self.assertIsNone(quote["amount"])
        self.assertEqual(quote["data_status"], "missing_price")

        class NonFiniteSeries:
            empty = False
            index = [date(2026, 8, 1)]

            @property
            def iloc(self):
                return self

            def dropna(self):
                return self

            def __len__(self) -> int:
                return 1

            def __getitem__(self, _index: int) -> float:
                return math.nan

        class NonFiniteFrame:
            empty = False
            columns = SimpleNamespace(nlevels=1)

            def __getitem__(self, _key: str) -> NonFiniteSeries:
                return NonFiniteSeries()

        frame = NonFiniteFrame()
        self.assertIn("not finite", MARKET.parse_close_series(frame, "SPY")["error"])

    def test_snapshot_writers_refuse_non_standard_json_numbers(self) -> None:
        payload = {"generated_at": "now", "items": [{"price": math.nan}], "errors": []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_output = root / "market.json"
            with patch.object(MARKET, "snapshot", return_value=payload), self.assertRaisesRegex(ValueError, "Out of range"):
                MARKET.main(["SPY", "--output", str(market_output)])
            self.assertFalse(market_output.exists())

            china_output = root / "china.json"
            with patch.object(CHINA, "snapshot", return_value=payload), self.assertRaisesRegex(ValueError, "Out of range"):
                CHINA.main(["snapshot", "--dry-run", "--output", str(china_output)])
            self.assertFalse(china_output.exists())


if __name__ == "__main__":
    unittest.main()
