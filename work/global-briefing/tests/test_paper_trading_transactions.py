from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock


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

    def enable_prediction_reference_contract(self) -> Path:
        ledger_path = self.root / "data" / "predictions.jsonl"
        self.config["prediction_ledger_file"] = "data/predictions.jsonl"
        self.config["order_contract"] = {
            "prediction_reference_required_from_date": "2026-07-17"
        }
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        return ledger_path

    def write_predictions(self, rows: list[dict]) -> Path:
        path = self.root / "data" / "predictions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
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

    def enable_china_account(self) -> None:
        self.config["accounts"]["CHINA"] = {
            "account_id": "test-china-paper",
            "market_scope": "CHINA",
            "base_currency": "CNY",
            "fx_rates_to_base": {"CNY": 1.0, "HKD": 0.8},
            "initial_cash": 100000.0,
            "base_unit": "points",
            "allowed_market_types": ["A_SHARE", "HK"],
            "portfolio_file": "data/china-portfolio.json",
            "trades_file": "data/china-trades.jsonl",
            "valuations_file": "data/china-valuations.jsonl",
        }
        self.config["market_rules"]["HK"] = {
            "exchanges": ["HK", "HKEX"],
            "currency": "HKD",
            "integer_quantity": True,
            "allow_same_day_sell": True,
        }
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        MODULE.ensure_files(reset=True, account="CHINA")

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

    def test_prediction_reference_contract_allows_pre_enforcement_order(self) -> None:
        self.enable_prediction_reference_contract()

        applied = MODULE.apply_orders(
            self.write_orders([self.order("ORDER-BEFORE-PREDICTION-GATE")]),
            "2026-07-16",
            account="US",
        )

        self.assertEqual(len(applied), 1)
        self.assertFalse(applied[0]["idempotent_replay"])

    def test_prediction_reference_contract_rejects_missing_and_review_only_references(self) -> None:
        self.enable_prediction_reference_contract()
        missing = self.order("ORDER-MISSING-PREDICTION")

        with self.assertRaisesRegex(ValueError, "requires prediction_id"):
            MODULE.apply_orders(self.write_orders([missing]), "2026-07-17", account="US")

        review_only = self.order("ORDER-REVIEW-ONLY")
        review_only["prediction_id"] = "2026-07-17-P01"
        self.write_predictions(
            [
                {
                    "date": "2026-07-17",
                    "prediction_id": "2026-07-17-P01",
                    "review": {"review_date": "2026-07-17"},
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "no original prediction record exists"):
            MODULE.apply_orders(self.write_orders([review_only]), "2026-07-17", account="US")

        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])

    def test_prediction_reference_contract_rejects_future_original(self) -> None:
        self.enable_prediction_reference_contract()
        order = self.order("ORDER-FUTURE-PREDICTION")
        order["prediction_id"] = "2026-07-18-P01"
        self.write_predictions(
            [{"date": "2026-07-18", "prediction_id": "2026-07-18-P01"}]
        )

        with self.assertRaisesRegex(ValueError, "follows order date 2026-07-17"):
            MODULE.apply_orders(self.write_orders([order]), "2026-07-17", account="US")

        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])

    def test_prediction_reference_contract_rejects_duplicate_original_ids(self) -> None:
        self.enable_prediction_reference_contract()
        order = self.order("ORDER-DUPLICATE-PREDICTION")
        order["prediction_id"] = "2026-07-17-P01"

        for conflicting in (False, True):
            with self.subTest(conflicting=conflicting):
                original = {"date": "2026-07-17", "prediction_id": "2026-07-17-P01"}
                duplicate = dict(original)
                if conflicting:
                    duplicate["scenario"] = "same identity, conflicting scenario"
                self.write_predictions([original, duplicate])

                with self.assertRaisesRegex(ValueError, "duplicate original prediction_id"):
                    MODULE.apply_orders(self.write_orders([order]), "2026-07-17", account="US")

        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])

    def test_prediction_reference_contract_rejects_noncanonical_whitespace(self) -> None:
        self.enable_prediction_reference_contract()
        self.write_predictions(
            [{"date": "2026-07-17", "prediction_id": "2026-07-17-P01"}]
        )
        order = self.order("ORDER-WHITESPACE-PREDICTION")
        order["prediction_id"] = " 2026-07-17-P01 "

        with self.assertRaisesRegex(ValueError, "leading or trailing whitespace"):
            MODULE.apply_orders(self.write_orders([order]), "2026-07-17", account="US")

        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])

    def test_prediction_reference_contract_accepts_original_and_replay_survives_missing_ledger(self) -> None:
        ledger_path = self.enable_prediction_reference_contract()
        self.write_predictions(
            [{"date": "2026-07-17", "prediction_id": "2026-07-17-P01"}]
        )
        order = self.order("ORDER-LINKED-PREDICTION")
        order["prediction_id"] = "2026-07-17-P01"
        path = self.write_orders([order])

        first = MODULE.apply_orders(path, "2026-07-17", account="US")
        ledger_path.unlink()
        second = MODULE.apply_orders(path, "2026-07-17", account="US")

        self.assertFalse(first[0]["idempotent_replay"])
        self.assertTrue(second[0]["idempotent_replay"])
        self.assertEqual(len(MODULE.read_jsonl(self.root / "data" / "trades.jsonl")), 1)

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

    def test_old_lock_owned_by_live_process_is_never_stolen(self) -> None:
        config = MODULE.account_config(self.config, "US")
        path = MODULE.lock_path(config)
        path.write_text(json.dumps({"pid": os.getpid(), "hostname": socket.gethostname(), "created_at": "2000-01-01T00:00:00", "token": "live"}), encoding="utf-8")
        os.utime(path, (1, 1))
        with self.assertRaisesRegex(RuntimeError, "is locked"):
            with MODULE.account_lock(config):
                self.fail("live lock was stolen")
        self.assertTrue(path.exists())

    def test_dead_local_process_lock_is_reclaimed(self) -> None:
        config = MODULE.account_config(self.config, "US")
        path = MODULE.lock_path(config)
        path.write_text(json.dumps({"pid": 2147483647, "hostname": socket.gethostname(), "created_at": MODULE.now_iso(), "token": "dead"}), encoding="utf-8")
        with MODULE.account_lock(config):
            current = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(current["pid"], os.getpid())
        self.assertFalse(path.exists())

    def test_fresh_incomplete_lock_is_not_deleted_during_owner_write_window(self) -> None:
        config = MODULE.account_config(self.config, "US")
        path = MODULE.lock_path(config)
        path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "is locked"):
            with MODULE.account_lock(config):
                self.fail("incomplete lock was stolen")
        self.assertTrue(path.exists())

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
        self.assertEqual(MODULE.read_jsonl(valuation_path)[0]["price_snapshot"][0]["price_date"], "2026-07-10")
        self.assertFalse((self.root / "data" / "valuations.transaction.json").exists())

    def test_price_date_contract_separates_quote_date_from_decision_date(self) -> None:
        self.config["order_contract"] = {"price_date_required_from_date": "2026-07-14"}
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        missing = self.order("ORDER-NO-PRICE-DATE")

        with self.assertRaisesRegex(ValueError, "requires price_date"):
            MODULE.apply_orders(self.write_orders([missing]), "2026-07-14", account="US")

        dated = self.order("ORDER-WITH-PRICE-DATE")
        dated["price_date"] = "2026-07-13"
        dated["theme"] = "ai_semiconductors"
        applied = MODULE.apply_orders(self.write_orders([dated]), "2026-07-14", account="US")
        state = MODULE.load_state("US")

        self.assertEqual(applied[0]["date"], "2026-07-14")
        self.assertEqual(applied[0]["price_date"], "2026-07-13")
        self.assertEqual(state["last_prices"]["NASDAQ:AAA"]["date"], "2026-07-13")
        self.assertEqual(state["positions"]["NASDAQ:AAA"]["theme"], "ai_semiconductors")

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

    def test_verified_registry_theme_is_recorded_and_hard_theme_cap_fails_closed(self) -> None:
        self.config["max_position_pct"] = 1.0
        self.config["max_daily_turnover_pct"] = 1.0
        self.config["theme_registry_file"] = "config/paper_theme_registry.json"
        self.config["theme_registry_history_file"] = "audit/paper_theme_registry/history.jsonl"
        self.config["theme_registry_snapshot_dir"] = "audit/paper_theme_registry/snapshots"
        self.config["order_contract"] = {
            "price_date_required_from_date": "2026-07-14",
            "theme_required_from_date": "2026-07-14",
            "maximum_theme_exposure_enforce_from_date": "2026-07-14",
            "theme_registry_history_required_from_date": "2026-07-14",
        }
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
            "decision_policy": {"maximum_actions_per_account_per_day": 4, "hold_requires_explicit_blocker": False},
            "risk_overlays": {"maximum_theme_exposure_pct": 0.5},
        }
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        registry = {
            "schema_version": 1,
            "allowed_themes": ["ai_semiconductors"],
            "entries": [
                {"account": "US", "symbol": symbol, "exchange": "NASDAQ", "primary_theme": "ai_semiconductors", "secondary_themes": [], "status": "verified", "effective_from": "2026-07-01", "evidence": ["test"]}
                for symbol in ("AAA", "BBB")
            ],
        }
        write_path = self.root / "config" / "paper_theme_registry.json"
        write_path.write_text(json.dumps(registry), encoding="utf-8")
        first = self.order("ORDER-THEME-A", symbol="AAA", notional=40000)
        first["price_date"] = "2026-07-14"

        with self.assertRaisesRegex(ValueError, "registry history gate"):
            MODULE.apply_orders(self.write_orders([first]), "2026-07-14", account="US")

        registry_module = sys.modules["paper_theme_registry"]
        revision = registry_module.record_revision(
            self.config,
            self.root,
            "2026-07-14",
            "Initial audited test registry baseline",
        )

        applied = MODULE.apply_orders(self.write_orders([first]), "2026-07-14", account="US")

        self.assertEqual(applied[0]["theme"], "ai_semiconductors")
        self.assertEqual(applied[0]["theme_source"], "verified_registry")
        self.assertEqual(applied[0]["theme_registry_revision_id"], revision["current_revision_id"])
        self.assertEqual(applied[0]["theme_registry_sha256"], revision["current_registry_sha256"])
        self.assertAlmostEqual(applied[0]["theme_exposure_pct_after"], 0.4)

        second = self.order("ORDER-THEME-B", symbol="BBB", notional=15000)
        second["price_date"] = "2026-07-15"
        with self.assertRaisesRegex(ValueError, "maximum theme exposure"):
            MODULE.apply_orders(self.write_orders([second]), "2026-07-15", account="US")

    def test_missing_registry_history_blocks_buy_but_not_hold(self) -> None:
        self.config["theme_registry_file"] = "config/paper_theme_registry.json"
        self.config["theme_registry_history_file"] = "audit/history.jsonl"
        self.config["theme_registry_snapshot_dir"] = "audit/snapshots"
        self.config["order_contract"] = {"theme_registry_history_required_from_date": "2026-07-14"}
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        registry_path = self.root / "config" / "paper_theme_registry.json"
        registry_path.write_text(json.dumps({
            "schema_version": 1,
            "allowed_themes": ["theme_a"],
            "entries": [{
                "account": "US",
                "symbol": "AAA",
                "exchange": "NASDAQ",
                "primary_theme": "theme_a",
                "secondary_themes": [],
                "status": "verified",
                "effective_from": "2026-07-01",
                "evidence": ["test mandate"],
            }],
        }), encoding="utf-8")
        hold = self.order("ORDER-HISTORY-HOLD")
        hold["action"] = "HOLD"

        applied = MODULE.apply_orders(self.write_orders([hold]), "2026-07-14", account="US")

        self.assertEqual(applied[0]["action"], "HOLD")
        buy = self.order("ORDER-HISTORY-BUY")
        with self.assertRaisesRegex(ValueError, "registry history gate"):
            MODULE.apply_orders(self.write_orders([buy]), "2026-07-14", account="US")

    def test_non_finite_numbers_negative_fees_and_non_standard_json_are_rejected_without_writes(self) -> None:
        portfolio_path = self.root / "data" / "portfolio.json"
        initial_portfolio = portfolio_path.read_bytes()
        invalid_orders = []

        invalid_price = self.order("BAD-PRICE")
        invalid_price["price"] = "NaN"
        invalid_orders.append((invalid_price, "price.*finite"))

        invalid_quantity = self.order("BAD-QUANTITY")
        invalid_quantity["quantity"] = "Infinity"
        invalid_quantity["notional"] = None
        invalid_orders.append((invalid_quantity, "quantity.*finite"))

        invalid_notional = self.order("BAD-NOTIONAL")
        invalid_notional["notional"] = "-Infinity"
        invalid_orders.append((invalid_notional, "notional.*finite"))

        invalid_fx = self.order("BAD-FX")
        invalid_fx["fx_to_base"] = "NaN"
        invalid_orders.append((invalid_fx, "fx_to_base.*finite"))

        invalid_fee = self.order("BAD-FEE")
        invalid_fee["fee"] = -1
        invalid_orders.append((invalid_fee, "fee.*non-negative"))

        invalid_tax = self.order("BAD-TAX")
        invalid_tax["tax"] = -0.01
        invalid_orders.append((invalid_tax, "tax.*non-negative"))

        for order, pattern in invalid_orders:
            with self.subTest(order_id=order["order_id"]):
                with self.assertRaisesRegex(ValueError, pattern):
                    MODULE.apply_orders(self.write_orders([order]), "2026-07-10", account="US")
                self.assertEqual(portfolio_path.read_bytes(), initial_portfolio)
                self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])

        raw_path = self.root / "raw-nan-orders.json"
        raw_path.write_text(
            '{"orders":[{"account":"US","action":"BUY","symbol":"AAA","exchange":"NASDAQ",'
            '"notional":1000,"price":NaN,"reason":"bad json","risk":"bad"}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "non-standard numeric constant NaN"):
            MODULE.apply_orders(raw_path, "2026-07-10", account="US")

        output_path = self.root / "nan-output.json"
        with self.assertRaises(ValueError):
            MODULE.atomic_write_json(output_path, {"value": float("nan")})
        self.assertFalse(output_path.exists())

    def test_order_date_must_be_strict_iso_and_equal_the_run_date(self) -> None:
        backdated = self.order("BACKDATED")
        backdated["date"] = "2026-07-09"
        with self.assertRaisesRegex(ValueError, "must equal run date"):
            MODULE.apply_orders(self.write_orders([backdated]), "2026-07-10", account="US")

        malformed = self.order("MALFORMED-DATE")
        malformed["date"] = "2026-7-10"
        with self.assertRaisesRegex(ValueError, "strict ISO"):
            MODULE.apply_orders(self.write_orders([malformed]), "2026-07-10", account="US")

        with self.assertRaisesRegex(ValueError, "strict ISO"):
            MODULE.apply_orders(self.write_orders([self.order("BAD-RUN-DATE")]), "2026-7-10", account="US")

        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "trades.jsonl"), [])

    def test_report_timezone_controls_business_date_and_local_record_timestamp_at_utc_boundary(self) -> None:
        settings_path = self.root / "work" / "global-briefing" / "config" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps({"timezone": "Asia/Shanghai"}),
            encoding="utf-8",
        )
        instant = datetime(2026, 7, 10, 16, 30, tzinfo=UTC)

        self.assertEqual(MODULE.current_business_date(instant), "2026-07-11")
        self.assertEqual(MODULE.now_iso(instant), "2026-07-11T00:30:00+08:00")
        self.assertEqual(MODULE.utc_now_iso(instant), "2026-07-10T16:30:00+00:00")

        orders_path = self.write_orders([self.order("DEFAULT-DATE")])
        with (
            mock.patch.object(MODULE, "current_business_date", return_value="2026-07-11"),
            mock.patch.object(MODULE, "apply_orders", return_value=[]) as apply_orders,
        ):
            result = MODULE.main(
                [
                    "apply-orders",
                    "--input",
                    str(orders_path),
                    "--account",
                    "US",
                ]
            )

        self.assertEqual(result, 0)
        apply_orders.assert_called_once_with(orders_path, "2026-07-11", account="US")

    def test_mark_uses_price_date_and_fails_closed_on_conflict_missing_date_or_empty_price(self) -> None:
        MODULE.apply_orders(self.write_orders([self.order("ORDER-MARK-DATE")]), "2026-07-10", account="US")
        prices = self.root / "prices.json"
        prices.write_text(
            json.dumps(
                {
                    "prices": [
                        {
                            "account": "US",
                            "symbol": "AAA",
                            "exchange": "NASDAQ",
                            "price": 110,
                            "price_date": "2026-07-10",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        valuation = MODULE.mark_to_market(prices, "2026-07-10", account="US")[0]

        self.assertEqual(valuation["price_snapshot"][0]["price_date"], "2026-07-10")
        self.assertEqual(
            MODULE.load_state("US")["last_prices"]["NASDAQ:AAA"]["date"],
            "2026-07-10",
        )
        state_before = (self.root / "data" / "portfolio.json").read_bytes()
        ledger_before = (self.root / "data" / "valuations.jsonl").read_bytes()

        conflicting = self.root / "conflicting-prices.json"
        conflicting.write_text(
            json.dumps(
                {
                    "prices": [
                        {
                            "account": "US",
                            "symbol": "AAA",
                            "exchange": "NASDAQ",
                            "price": 111,
                            "price_date": "2026-07-11",
                            "date": "2026-07-10",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "conflicts with date"):
            MODULE.mark_to_market(conflicting, "2026-07-11", account="US")

        missing_date = self.root / "missing-date-prices.json"
        missing_date.write_text(
            json.dumps(
                {
                    "prices": [
                        {
                            "account": "US",
                            "symbol": "AAA",
                            "exchange": "NASDAQ",
                            "price": 111,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "requires an explicit price_date"):
            MODULE.mark_to_market(missing_date, "2026-07-11", account="US")

        empty_price = self.root / "empty-prices.json"
        empty_price.write_text(
            json.dumps(
                {
                    "prices": [
                        {
                            "account": "US",
                            "symbol": "AAA",
                            "exchange": "NASDAQ",
                            "price": None,
                            "price_date": "2026-07-11",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            MODULE.mark_to_market(empty_price, "2026-07-11", account="US")

        self.assertEqual((self.root / "data" / "portfolio.json").read_bytes(), state_before)
        self.assertEqual((self.root / "data" / "valuations.jsonl").read_bytes(), ledger_before)

    def test_stale_open_position_blocks_valuation_by_default(self) -> None:
        MODULE.apply_orders(self.write_orders([self.order("ORDER-STALE")]), "2026-07-10", account="US")
        prices = self.root / "stale-prices.json"
        prices.write_text(
            json.dumps(
                {
                    "prices": [
                        {
                            "account": "US",
                            "symbol": "AAA",
                            "exchange": "NASDAQ",
                            "price": 110,
                            "price_date": "2026-07-10",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "Valuation blocked.*stale_price"):
            MODULE.mark_to_market(prices, "2026-07-14", account="US")

        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "valuations.jsonl"), [])
        self.assertEqual(MODULE.load_state("US")["as_of_date"], "2026-07-10")

    def test_partial_stale_policy_never_labels_stale_valuation_healthy(self) -> None:
        self.config["valuation_policy"] = {
            "stale_price_policy": "partial",
            "max_price_age_business_days": 1,
        }
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        MODULE.apply_orders(self.write_orders([self.order("ORDER-PARTIAL")]), "2026-07-10", account="US")
        prices = self.root / "partial-prices.json"
        prices.write_text(
            json.dumps(
                {
                    "prices": [
                        {
                            "account": "US",
                            "symbol": "AAA",
                            "exchange": "NASDAQ",
                            "price": 110,
                            "price_date": "2026-07-10",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        valuation = MODULE.mark_to_market(prices, "2026-07-14", account="US")[0]

        self.assertEqual(valuation["valuation_status"], "partial")
        self.assertFalse(valuation["valuation_healthy"])
        self.assertEqual(valuation["valuation_issues"][0]["issue"], "stale_price")

    def test_fx_change_is_reconciled_between_realized_and_unrealized_base_pnl(self) -> None:
        self.enable_china_account()
        buy = {
            "order_id": "HK-BUY",
            "account": "CHINA",
            "action": "BUY",
            "symbol": "0700.HK",
            "exchange": "HK",
            "quantity": 100,
            "price": 10,
            "fx_to_base": 0.8,
            "reason": "fx basis test",
            "risk": "test risk",
        }
        sell = {
            **buy,
            "order_id": "HK-SELL",
            "action": "SELL",
            "quantity": 50,
            "fx_to_base": 0.9,
        }

        MODULE.apply_orders(self.write_orders([buy]), "2026-07-10", account="CHINA")
        sold = MODULE.apply_orders(self.write_orders([sell]), "2026-07-13", account="CHINA")[0]
        state = MODULE.load_state("CHINA")
        summary = MODULE.summarize(state)
        position = summary["positions"][0]

        self.assertAlmostEqual(sold["cost_basis_base_released"], 400.0)
        self.assertAlmostEqual(sold["realized_pnl_base"], 50.0)
        self.assertAlmostEqual(sold["realized_price_pnl_base"], 0.0)
        self.assertAlmostEqual(sold["realized_fx_pnl_base"], 50.0)
        self.assertAlmostEqual(state["cash"], 99650.0)
        self.assertAlmostEqual(state["positions"]["HK:0700.HK"]["cost_basis_base"], 400.0)
        self.assertAlmostEqual(position["unrealized_pnl_base"], 50.0)
        self.assertAlmostEqual(position["unrealized_price_pnl_base"], 0.0)
        self.assertAlmostEqual(position["unrealized_fx_pnl_base"], 50.0)
        self.assertAlmostEqual(summary["equity"], 100100.0)

    def test_multi_account_batch_prewrites_every_journal_before_first_commit(self) -> None:
        self.enable_china_account()
        us_order = self.order("US-BATCH", notional=1000)
        china_order = {
            "order_id": "CHINA-BATCH",
            "account": "CHINA",
            "action": "BUY",
            "symbol": "0700.HK",
            "exchange": "HK",
            "quantity": 100,
            "price": 10,
            "fx_to_base": 0.8,
            "reason": "batch transaction test",
            "risk": "test risk",
        }
        us_config = MODULE.account_config(self.config, "US")
        china_config = MODULE.account_config(self.config, "CHINA")
        trade_paths = {
            MODULE.resolve_path(us_config, "trades_file"),
            MODULE.resolve_path(china_config, "trades_file"),
        }
        journal_paths = {
            MODULE.transaction_path(us_config),
            MODULE.transaction_path(china_config),
        }
        original_atomic_write_text = MODULE.atomic_write_text
        observations: list[bool] = []

        def observe_commit(path: Path, text: str) -> None:
            if path in trade_paths:
                observations.append(all(journal.exists() for journal in journal_paths))
            original_atomic_write_text(path, text)

        with mock.patch.object(MODULE, "atomic_write_text", side_effect=observe_commit):
            MODULE.apply_orders(self.write_orders([us_order, china_order]), "2026-07-10")

        self.assertTrue(observations)
        self.assertTrue(observations[0])
        self.assertTrue(all(not journal.exists() for journal in journal_paths))

    def test_failed_second_account_commit_is_recoverable_on_next_run(self) -> None:
        self.enable_china_account()
        us_order = self.order("US-RECOVER", notional=1000)
        china_order = {
            "order_id": "CHINA-RECOVER",
            "account": "CHINA",
            "action": "BUY",
            "symbol": "0700.HK",
            "exchange": "HK",
            "quantity": 100,
            "price": 10,
            "fx_to_base": 0.8,
            "reason": "batch recovery test",
            "risk": "test risk",
        }
        us_config = MODULE.account_config(self.config, "US")
        china_config = MODULE.account_config(self.config, "CHINA")
        us_trades = MODULE.resolve_path(us_config, "trades_file")
        original_atomic_write_text = MODULE.atomic_write_text
        failed = False

        def fail_us_commit_once(path: Path, text: str) -> None:
            nonlocal failed
            if path == us_trades and not failed:
                failed = True
                raise OSError("simulated account commit interruption")
            original_atomic_write_text(path, text)

        orders_path = self.write_orders([us_order, china_order])
        with mock.patch.object(MODULE, "atomic_write_text", side_effect=fail_us_commit_once):
            with self.assertRaisesRegex(OSError, "simulated account commit interruption"):
                MODULE.apply_orders(orders_path, "2026-07-10")

        self.assertTrue(MODULE.transaction_path(us_config).exists())
        self.assertTrue(MODULE.transaction_path(china_config).exists())
        self.assertEqual(len(MODULE.read_jsonl(MODULE.resolve_path(china_config, "trades_file"))), 1)
        self.assertEqual(len(MODULE.read_jsonl(us_trades)), 0)

        replay = MODULE.apply_orders(orders_path, "2026-07-10")

        self.assertEqual(len(MODULE.read_jsonl(us_trades)), 1)
        self.assertEqual(len(MODULE.read_jsonl(MODULE.resolve_path(china_config, "trades_file"))), 1)
        self.assertTrue(all(record["idempotent_replay"] for record in replay))
        self.assertFalse(MODULE.transaction_path(us_config).exists())
        self.assertFalse(MODULE.transaction_path(china_config).exists())

    def test_partial_journal_staging_aborts_without_publishing_any_account(self) -> None:
        self.enable_china_account()
        us_order = self.order("US-STAGE", notional=1000)
        china_order = {
            "order_id": "CHINA-STAGE",
            "account": "CHINA",
            "action": "BUY",
            "symbol": "0700.HK",
            "exchange": "HK",
            "quantity": 100,
            "price": 10,
            "fx_to_base": 0.8,
            "reason": "staging interruption test",
            "risk": "test risk",
        }
        configs = [
            MODULE.account_config(self.config, name)
            for name in ("CHINA", "US")
        ]
        journal_paths = [MODULE.transaction_path(config) for config in configs]
        initial_states = {
            config["account"]: MODULE.resolve_path(config, "portfolio_file").read_bytes()
            for config in configs
        }
        original_atomic_write_json = MODULE.atomic_write_json

        def interrupt_second_journal(path: Path, payload: object) -> None:
            if path == journal_paths[1]:
                raise OSError("simulated staging interruption")
            original_atomic_write_json(path, payload)

        with mock.patch.object(MODULE, "atomic_write_json", side_effect=interrupt_second_journal):
            with self.assertRaisesRegex(OSError, "simulated staging interruption"):
                MODULE.apply_orders(self.write_orders([us_order, china_order]), "2026-07-10")

        self.assertFalse(MODULE.batch_coordinator_path(valuation=False).exists())
        self.assertTrue(all(not path.exists() for path in journal_paths))
        for config in configs:
            self.assertEqual(
                MODULE.resolve_path(config, "portfolio_file").read_bytes(),
                initial_states[config["account"]],
            )
            self.assertEqual(MODULE.read_jsonl(MODULE.resolve_path(config, "trades_file")), [])

    def test_hard_crash_during_partial_journal_staging_is_aborted_on_recovery(self) -> None:
        self.enable_china_account()
        orders = [
            self.order("US-HARD-STAGE", notional=1000),
            {
                "order_id": "CHINA-HARD-STAGE",
                "account": "CHINA",
                "action": "BUY",
                "symbol": "0700.HK",
                "exchange": "HK",
                "quantity": 100,
                "price": 10,
                "fx_to_base": 0.8,
                "reason": "hard staging crash test",
                "risk": "test risk",
            },
        ]
        us_config = MODULE.account_config(self.config, "US")
        us_journal = MODULE.transaction_path(us_config)
        original_atomic_write_json = MODULE.atomic_write_json

        def hard_crash_on_us_journal(path: Path, payload: object) -> None:
            if path == us_journal:
                raise SystemExit("simulated process death")
            original_atomic_write_json(path, payload)

        with mock.patch.object(MODULE, "atomic_write_json", side_effect=hard_crash_on_us_journal):
            with self.assertRaisesRegex(SystemExit, "simulated process death"):
                MODULE.apply_orders(self.write_orders(orders), "2026-07-10")

        coordinator_path = MODULE.batch_coordinator_path(valuation=False)
        self.assertEqual(json.loads(coordinator_path.read_text(encoding="utf-8"))["phase"], "preparing")
        _state, rows, _valuations = MODULE.load_account_snapshot("US")

        self.assertEqual(rows, [])
        self.assertFalse(coordinator_path.exists())
        self.assertFalse(us_journal.exists())

    def test_reader_recovers_half_published_batch_before_returning_snapshot(self) -> None:
        self.enable_china_account()
        orders = [
            self.order("US-READER", notional=1000),
            {
                "order_id": "CHINA-READER",
                "account": "CHINA",
                "action": "BUY",
                "symbol": "0700.HK",
                "exchange": "HK",
                "quantity": 100,
                "price": 10,
                "fx_to_base": 0.8,
                "reason": "reader recovery test",
                "risk": "test risk",
            },
        ]
        us_config = MODULE.account_config(self.config, "US")
        china_config = MODULE.account_config(self.config, "CHINA")
        us_trades = MODULE.resolve_path(us_config, "trades_file")
        original_atomic_write_text = MODULE.atomic_write_text
        interrupted = False

        def interrupt_us_publish(path: Path, text: str) -> None:
            nonlocal interrupted
            if path == us_trades and not interrupted:
                interrupted = True
                raise OSError("simulated half publication")
            original_atomic_write_text(path, text)

        with mock.patch.object(MODULE, "atomic_write_text", side_effect=interrupt_us_publish):
            with self.assertRaisesRegex(OSError, "simulated half publication"):
                MODULE.apply_orders(self.write_orders(orders), "2026-07-10")

        _state, us_rows, _valuations = MODULE.load_account_snapshot("US")
        _china_state, china_rows, _china_valuations = MODULE.load_account_snapshot("CHINA")

        self.assertEqual([row["order_id"] for row in us_rows], ["US-READER"])
        self.assertEqual([row["order_id"] for row in china_rows], ["CHINA-READER"])
        self.assertFalse(MODULE.batch_coordinator_path(valuation=False).exists())
        self.assertFalse(MODULE.transaction_path(us_config).exists())
        self.assertFalse(MODULE.transaction_path(china_config).exists())

    def test_committed_batch_with_partial_cleanup_is_recoverable_without_journals_for_every_account(self) -> None:
        self.enable_china_account()
        orders = [
            self.order("US-CLEANUP", notional=1000),
            {
                "order_id": "CHINA-CLEANUP",
                "account": "CHINA",
                "action": "BUY",
                "symbol": "0700.HK",
                "exchange": "HK",
                "quantity": 100,
                "price": 10,
                "fx_to_base": 0.8,
                "reason": "cleanup recovery test",
                "risk": "test risk",
            },
        ]
        us_config = MODULE.account_config(self.config, "US")
        china_config = MODULE.account_config(self.config, "CHINA")
        us_journal = MODULE.transaction_path(us_config)
        china_journal = MODULE.transaction_path(china_config)
        original_unlink = Path.unlink
        interrupted = False

        def interrupt_one_cleanup(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal interrupted
            if path == us_journal and not interrupted:
                interrupted = True
                raise OSError("simulated cleanup interruption")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=interrupt_one_cleanup):
            with self.assertRaisesRegex(OSError, "simulated cleanup interruption"):
                MODULE.apply_orders(self.write_orders(orders), "2026-07-10")

        coordinator_path = MODULE.batch_coordinator_path(valuation=False)
        marker_path = MODULE.batch_commit_marker_path(valuation=False)
        self.assertEqual(json.loads(coordinator_path.read_text(encoding="utf-8"))["phase"], "committed")
        self.assertTrue(marker_path.exists())
        self.assertFalse(china_journal.exists())
        self.assertTrue(us_journal.exists())

        _state, rows, _valuations = MODULE.load_account_snapshot("US")

        self.assertEqual([row["order_id"] for row in rows], ["US-CLEANUP"])
        self.assertFalse(coordinator_path.exists())
        self.assertFalse(us_journal.exists())

    def test_all_account_lock_acquisition_uses_one_canonical_order(self) -> None:
        self.enable_china_account()
        observed: list[str] = []

        @contextmanager
        def observe_lock(config: dict):
            observed.append(str(config["account_id"]))
            yield

        expected = [str(config["account_id"]) for config in MODULE.all_account_configs(self.config)]
        with mock.patch.object(MODULE, "account_lock", side_effect=observe_lock):
            MODULE.ensure_files(reset=True, account="ALL")

        self.assertEqual(observed, expected)

    def test_currency_must_match_instrument_position_and_mark(self) -> None:
        wrong_order = self.order("WRONG-CURRENCY")
        wrong_order["currency"] = "EUR"
        with self.assertRaisesRegex(ValueError, "conflicts with US instrument currency USD"):
            MODULE.apply_orders(self.write_orders([wrong_order]), "2026-07-10", account="US")

        MODULE.apply_orders(self.write_orders([self.order("RIGHT-CURRENCY")]), "2026-07-10", account="US")
        wrong_mark = self.root / "wrong-currency-mark.json"
        wrong_mark.write_text(
            json.dumps({
                "prices": [{
                    "account": "US",
                    "symbol": "AAA",
                    "exchange": "NASDAQ",
                    "currency": "EUR",
                    "price": 101,
                    "price_date": "2026-07-10",
                }]
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "conflicts with US instrument currency USD"):
            MODULE.mark_to_market(wrong_mark, "2026-07-10", account="US")

        state = MODULE.load_state("US")
        state["positions"]["NASDAQ:AAA"]["currency"] = "EUR"
        MODULE.atomic_write_json(self.root / "data" / "portfolio.json", state)
        sell = self.order("POSITION-CURRENCY-CONFLICT")
        sell.update({"action": "SELL", "quantity": "ALL", "notional": None})
        with self.assertRaisesRegex(ValueError, "position.*currency|Position.*currency"):
            MODULE.apply_orders(self.write_orders([sell]), "2026-07-10", account="US")

    def test_as_of_date_cannot_move_backward_for_orders_or_valuations(self) -> None:
        MODULE.apply_orders(self.write_orders([self.order("MONOTONIC-BASE")]), "2026-07-10", account="US")
        portfolio_path = self.root / "data" / "portfolio.json"
        portfolio_before = portfolio_path.read_bytes()
        backdated = self.order("BACKWARD-ORDER", symbol="BBB", notional=1000)
        with self.assertRaisesRegex(ValueError, "cannot move portfolio as_of_date backward"):
            MODULE.apply_orders(self.write_orders([backdated]), "2026-07-09", account="US")
        self.assertEqual(portfolio_path.read_bytes(), portfolio_before)

        prices = self.root / "backward-mark.json"
        prices.write_text(
            json.dumps({
                "prices": [{
                    "account": "US",
                    "symbol": "AAA",
                    "exchange": "NASDAQ",
                    "price": 101,
                    "price_date": "2026-07-09",
                }]
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "cannot move portfolio as_of_date backward"):
            MODULE.mark_to_market(prices, "2026-07-09", account="US")
        self.assertEqual(MODULE.read_jsonl(self.root / "data" / "valuations.jsonl"), [])

    def test_historical_turnover_requires_strict_finite_numeric_values(self) -> None:
        trades_path = self.root / "data" / "trades.jsonl"
        MODULE.atomic_write_text(
            trades_path,
            json.dumps({
                "date": "2026-07-10",
                "action": "BUY",
                "order_id": "LEGACY-NAN",
                "gross_value_base": "NaN",
            }) + "\n",
        )
        with self.assertRaisesRegex(ValueError, "historical turnover.*finite"):
            MODULE.turnover_for_date("2026-07-10", trades_path)

        MODULE.atomic_write_text(
            trades_path,
            '{"date":"2026-07-10","action":"BUY","gross_value_base":NaN}\n',
        )
        with self.assertRaisesRegex(ValueError, "non-standard numeric constant NaN"):
            MODULE.turnover_for_date("2026-07-10", trades_path)

    def test_duplicate_and_legacy_order_identity_fail_closed(self) -> None:
        duplicate = self.order("DUPLICATE-INPUT")
        with self.assertRaisesRegex(ValueError, "Duplicate order_id"):
            MODULE.apply_orders(self.write_orders([duplicate, dict(duplicate)]), "2026-07-10", account="US")

        trades_path = self.root / "data" / "trades.jsonl"
        legacy = {"order_id": "LEGACY-ID", "date": "2026-07-10", "action": "HOLD"}
        MODULE.atomic_write_text(trades_path, json.dumps(legacy) + "\n")
        legacy_replay = self.order("LEGACY-ID")
        with self.assertRaisesRegex(ValueError, "lacks order_fingerprint"):
            MODULE.apply_orders(self.write_orders([legacy_replay]), "2026-07-10", account="US")

        duplicate_rows = [
            {"order_id": "LEDGER-DUP", "date": "2026-07-10", "action": "HOLD"},
            {"order_id": "LEDGER-DUP", "date": "2026-07-10", "action": "HOLD"},
        ]
        MODULE.atomic_write_text(
            trades_path,
            "".join(json.dumps(row) + "\n" for row in duplicate_rows),
        )
        with self.assertRaisesRegex(ValueError, "Duplicate order_id"):
            MODULE.apply_orders(self.write_orders([self.order("NEW-ID")]), "2026-07-10", account="US")

    def test_order_quote_staleness_uses_configured_business_day_limit(self) -> None:
        self.config["order_contract"] = {"max_price_age_business_days": 0}
        MODULE.CONFIG_PATH.write_text(json.dumps(self.config), encoding="utf-8")
        stale = self.order("STALE-ORDER")
        stale["price_date"] = "2026-07-10"

        with self.assertRaisesRegex(ValueError, "Order price.*stale.*business_day_age=1"):
            MODULE.apply_orders(self.write_orders([stale]), "2026-07-13", account="US")


if __name__ == "__main__":
    unittest.main()
