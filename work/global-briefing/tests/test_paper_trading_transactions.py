from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_trading.py"
SPEC = importlib.util.spec_from_file_location("paper_trading_transaction_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PaperTradingTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_root = MODULE.ROOT
        self.original_config_path = MODULE.CONFIG_PATH
        MODULE.ROOT = self.root
        MODULE.CONFIG_PATH = self.root / "config" / "paper_trading.json"
        MODULE.CONFIG_PATH.parent.mkdir(parents=True)
        self.config = {
            "initial_cash": 100000.0,
            "base_unit": "points",
            "allow_fractional_shares": True,
            "allow_short": False,
            "default_fee_rate": 0.0,
            "max_position_pct": 0.25,
            "max_daily_turnover_pct": 0.15,
            "min_cash_pct": 0.05,
            "max_new_positions_per_day": 5,
            "default_account": "US",
            "accounts": {
                "US": {
                    "account_id": "test-us-paper",
                    "market_scope": "US",
                    "initial_cash": 100000.0,
                    "base_unit": "points",
                    "allowed_market_types": ["US"],
                    "portfolio_file": "data/portfolio.json",
                    "trades_file": "data/trades.jsonl",
                    "valuations_file": "data/valuations.jsonl",
                }
            },
            "market_rules": {
                "US": {
                    "exchanges": ["NASDAQ", "NYSE", "US"],
                    "currency": "USD",
                    "integer_quantity": False,
                    "allow_same_day_sell": True,
                }
            },
        }
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        MODULE.ensure_files(reset=True, account="US")

    def tearDown(self) -> None:
        MODULE.ROOT = self.original_root
        MODULE.CONFIG_PATH = self.original_config_path
        self.temporary.cleanup()

    def write_orders(self, orders: list[dict]) -> Path:
        path = self.root / "orders.json"
        path.write_text(json.dumps({"orders": orders}), encoding="utf-8")
        return path

    def order(self, order_id: str, symbol: str = "AAA", notional: float = 10000.0) -> dict:
        return {
            "order_id": order_id,
            "account": "US",
            "action": "BUY",
            "symbol": symbol,
            "exchange": "NASDAQ",
            "notional": notional,
            "price": 100.0,
            "source": "test quote",
            "reason": "transaction test",
            "risk": "test risk",
        }

    def test_repeating_same_order_is_a_no_write_idempotent_replay(self) -> None:
        path = self.write_orders([self.order("ORDER-001")])
        first = MODULE.apply_orders(path, "2026-07-10", account="US")
        portfolio_path = self.root / "data" / "portfolio.json"
        trades_path = self.root / "data" / "trades.jsonl"
        first_portfolio = portfolio_path.read_bytes()
        first_ledger = trades_path.read_bytes()

        second = MODULE.apply_orders(path, "2026-07-10", account="US")

        self.assertFalse(first[0]["idempotent_replay"])
        self.assertTrue(second[0]["idempotent_replay"])
        self.assertEqual(portfolio_path.read_bytes(), first_portfolio)
        self.assertEqual(trades_path.read_bytes(), first_ledger)
        self.assertEqual(len(MODULE.read_jsonl(trades_path)), 1)

    def test_later_invalid_order_rolls_back_the_whole_account_batch(self) -> None:
        invalid = self.order("ORDER-INVALID", symbol="BBB")
        invalid["price"] = 0
        path = self.write_orders([self.order("ORDER-FIRST"), invalid])
        portfolio_path = self.root / "data" / "portfolio.json"
        initial_portfolio = portfolio_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "requires positive price"):
            MODULE.apply_orders(path, "2026-07-10", account="US")

        self.assertEqual(portfolio_path.read_bytes(), initial_portfolio)
        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])
        self.assertFalse((self.root / "data" / "portfolio.lock").exists())
        self.assertFalse((self.root / "data" / "trades.transaction.json").exists())

    def test_pending_orders_count_toward_batch_turnover_limit(self) -> None:
        path = self.write_orders([
            self.order("ORDER-A", symbol="AAA", notional=10000.0),
            self.order("ORDER-B", symbol="BBB", notional=10000.0),
        ])

        with self.assertRaisesRegex(ValueError, "daily turnover limit"):
            MODULE.apply_orders(path, "2026-07-10", account="US")

        state = MODULE.load_state("US")
        self.assertEqual(state["cash"], 100000.0)
        self.assertEqual(state["positions"], {})
        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])

    def test_reusing_explicit_order_id_with_changed_payload_fails_closed(self) -> None:
        path = self.write_orders([self.order("ORDER-CONFLICT")])
        MODULE.apply_orders(path, "2026-07-10", account="US")
        portfolio_path = self.root / "data" / "portfolio.json"
        trades_path = self.root / "data" / "trades.jsonl"
        portfolio_before = portfolio_path.read_bytes()
        ledger_before = trades_path.read_bytes()
        changed = self.order("ORDER-CONFLICT")
        changed["price"] = 101.0
        changed["notional"] = 10100.0
        path = self.write_orders([changed])

        with self.assertRaisesRegex(ValueError, "Conflicting paper order"):
            MODULE.apply_orders(path, "2026-07-10", account="US")

        self.assertEqual(portfolio_path.read_bytes(), portfolio_before)
        self.assertEqual(trades_path.read_bytes(), ledger_before)

    def test_mark_to_market_is_single_row_per_account_date_and_idempotent(self) -> None:
        MODULE.apply_orders(self.write_orders([self.order("ORDER-MARK")]), "2026-07-10", account="US")
        prices = self.root / "prices.json"
        prices.write_text(json.dumps({"prices": [{"account": "US", "symbol": "AAA", "exchange": "NASDAQ", "price": 110, "date": "2026-07-10"}]}), encoding="utf-8")

        first = MODULE.mark_to_market(prices, "2026-07-10", account="US")
        valuation_path = self.root / "data" / "valuations.jsonl"
        first_bytes = valuation_path.read_bytes()
        second = MODULE.mark_to_market(prices, "2026-07-10", account="US")

        self.assertFalse(first[0]["idempotent_replay"])
        self.assertTrue(second[0]["idempotent_replay"])
        self.assertEqual(first_bytes, valuation_path.read_bytes())
        self.assertEqual(len(MODULE.read_jsonl(valuation_path)), 1)
        self.assertFalse((self.root / "data" / "valuations.transaction.json").exists())

    def test_mixed_currency_positions_are_converted_to_account_base_currency(self) -> None:
        state = {
            "base_currency": "CNY",
            "fx_rates_to_base": {"CNY": 1.0, "HKD": 0.92},
            "initial_cash": 100000.0,
            "cash": 90000.0,
            "realized_pnl": 0.0,
            "positions": {
                "HK:3033.HK": {"symbol": "3033.HK", "exchange": "HK", "currency": "HKD", "quantity": 1000, "avg_cost": 5.0, "cost_basis": 5000.0}
            },
            "last_prices": {
                "HK:3033.HK": {"currency": "HKD", "price": 6.0, "fx_to_base": 0.92}
            },
        }

        summary = MODULE.summarize(state)

        self.assertEqual(summary["base_currency"], "CNY")
        self.assertAlmostEqual(summary["positions_value"], 5520.0)
        self.assertAlmostEqual(summary["equity"], 95520.0)
        self.assertAlmostEqual(summary["positions"][0]["market_value_native"], 6000.0)

    def test_future_dated_mark_is_rejected(self) -> None:
        prices = self.root / "future-prices.json"
        prices.write_text(json.dumps({"prices": [{"account": "US", "symbol": "AAA", "exchange": "NASDAQ", "price": 110, "date": "2026-07-11"}]}), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "after valuation date"):
            MODULE.mark_to_market(prices, "2026-07-10", account="US")

    def test_aggressive_mode_hold_requires_an_allowed_blocker(self) -> None:
        self.config["strategy_profile"] = {
            "enabled": True,
            "target_invested_pct": {"minimum": 0.7, "preferred": 0.82, "maximum": 0.98},
            "target_cash_pct": {"hard_minimum": 0.05, "preferred_minimum": 0.08, "preferred_maximum": 0.2},
            "signal_score": {
                "exit_threshold": 38,
                "reduce_threshold": 48,
                "buy_threshold": 70,
                "add_threshold": 82,
                "components": {"evidence": 25, "trend": 25, "breadth": 20, "catalyst": 15, "liquidity": 15},
            },
            "decision_policy": {
                "maximum_actions_per_account_per_day": 4,
                "hold_requires_explicit_blocker": True,
                "allowed_hold_blockers": ["stale_price", "risk_limit_breach"],
            },
        }
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        hold = self.order("ORDER-HOLD")
        hold.update({"action": "HOLD", "notional": None})

        with self.assertRaisesRegex(ValueError, "requires an allowed blocker"):
            MODULE.apply_orders(self.write_orders([hold]), "2026-07-10", account="US")

        hold["blocker"] = "stale_price"
        applied = MODULE.apply_orders(self.write_orders([hold]), "2026-07-10", account="US")
        self.assertEqual(applied[0]["blocker"], "stale_price")

    def test_aggressive_mode_caps_daily_action_count(self) -> None:
        self.config["max_daily_turnover_pct"] = 1.0
        self.config["max_position_pct"] = 1.0
        self.config["strategy_profile"] = {
            "enabled": True,
            "target_invested_pct": {"minimum": 0.7, "preferred": 0.82, "maximum": 0.98},
            "target_cash_pct": {"hard_minimum": 0.05, "preferred_minimum": 0.08, "preferred_maximum": 0.2},
            "signal_score": {
                "exit_threshold": 38,
                "reduce_threshold": 48,
                "buy_threshold": 70,
                "add_threshold": 82,
                "components": {"evidence": 25, "trend": 25, "breadth": 20, "catalyst": 15, "liquidity": 15},
            },
            "decision_policy": {
                "maximum_actions_per_account_per_day": 2,
                "hold_requires_explicit_blocker": False,
            },
        }
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        orders = [
            self.order("ORDER-ONE", symbol="AAA", notional=1000),
            self.order("ORDER-TWO", symbol="BBB", notional=1000),
            self.order("ORDER-THREE", symbol="CCC", notional=1000),
        ]

        with self.assertRaisesRegex(ValueError, "action count"):
            MODULE.apply_orders(self.write_orders(orders), "2026-07-10", account="US")


if __name__ == "__main__":
    unittest.main()
