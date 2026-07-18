from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import atlas


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class AtlasCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.briefing = self.root / "work" / "global-briefing"
        self.trading = self.root / "work" / "trading-core"
        self.site = self.root / "src"
        self.outputs = self.root / "outputs"
        self.runtime = self.root / "work" / "shared" / "atlas"
        self.ledger_root = self.runtime / "virtual_execution"
        self.trust_root = self.root.parent / f"{self.root.name}-external-trust"
        self.environment_patcher = patch.dict(
            os.environ,
            {
                atlas.TRUST_ANCHOR_ROOT_ENV: str(self.trust_root),
                atlas.TRUST_ANCHOR_HMAC_KEY_ENV: "test-only-external-anchor-key",
                atlas.TRUST_ANCHOR_NAMESPACE_ENV: "atlas-cycle-test",
            },
            clear=False,
        )
        self.environment_patcher.start()
        self.patcher = patch.multiple(
            atlas,
            ROOT=self.root,
            BRIEFING_ROOT=self.briefing,
            TRADING_ROOT=self.trading,
            SITE_ROOT=self.site,
            OUTPUTS_ROOT=self.outputs,
            ATLAS_RUNTIME_ROOT=self.runtime,
            VIRTUAL_LEDGER_ROOT=self.ledger_root,
            VIRTUAL_LEDGER_PATH=self.ledger_root / "atlas_virtual_execution_ledger.jsonl",
            VIRTUAL_LEDGER_STATE_PATH=self.ledger_root / "atlas_virtual_execution_state.json",
            VIRTUAL_LEDGER_AUDIT_PATH=self.ledger_root / "atlas_virtual_execution_audit.json",
            RUN_AUDIT_ROOT=self.runtime / "run_audits",
            CYCLE_STATE_PATH=self.runtime / "cycle_state.json",
        )
        self.patcher.start()
        self._seed_sources()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.environment_patcher.stop()
        self.tmp.cleanup()
        shutil.rmtree(self.trust_root, ignore_errors=True)

    def _seed_sources(self) -> None:
        trade = {
            "account": "US",
            "account_id": "global-briefing-us-paper-trading",
            "action": "BUY",
            "date": "2026-07-10",
            "exchange": "NASDAQ",
            "paper_trading_only": True,
            "price": 10,
            "quantity": 2,
            "gross_value": 20,
            "symbol": "TEST",
            "timestamp": "2026-07-10T09:30:00",
        }
        write_jsonl(self.briefing / "data" / "paper_trades_us.jsonl", [trade])
        write_jsonl(self.briefing / "data" / "paper_trades_china.jsonl", [])
        write_json(
            self.briefing / "data" / "temp-orders-2026-07-10.json",
            {
                "orders": [
                    {
                        "account": "US",
                        "action": "BUY",
                        "date": "2026-07-10",
                        "exchange": "NASDAQ",
                        "paper_trading_only": True,
                        "price": 11,
                        "quantity": 1,
                        "symbol": "TEST2",
                    }
                ]
            },
        )
        write_json(
            self.briefing / "data" / "paper_portfolio_us.json",
            {
                "account_id": "global-briefing-us-paper-trading",
                "mode": "paper_trading",
                "cash": 99980,
                "initial_cash": 100000,
                "positions": {
                    "NASDAQ:TEST": {"exchange": "NASDAQ", "symbol": "TEST", "quantity": 2}
                },
            },
        )
        write_json(
            self.briefing / "data" / "paper_portfolio_china.json",
            {"account_id": "global-briefing-china-paper-trading", "mode": "paper_trading", "cash": 100000, "initial_cash": 100000, "positions": {}},
        )
        write_jsonl(
            self.trading / "data" / "replays" / "global_briefing" / "trades" / "trades-GB-HIST-REPLAY-20260710-20260710.jsonl",
            [
                {
                    "trade_id": "R1",
                    "order_id": "O1",
                    "replay_id": "GB-HIST-REPLAY-20260710-20260710",
                    "date": "2026-07-10",
                    "symbol": "510300.SH",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 4,
                    "notional": 400,
                    "fee": 0.2,
                    "isolated": True,
                }
            ],
        )
        write_json(
            self.trading / "data" / "replays" / "global_briefing" / "accounts" / "account-GB-HIST-REPLAY-20260710-20260710.json",
            {
                "replay_id": "GB-HIST-REPLAY-20260710-20260710",
                "cash": 999599.8,
                "equity": 1000000,
                "positions": [{"symbol": "510300.SH", "quantity": 100}],
                "isolated": True,
            },
        )
        write_json(
            self.trading / "data" / "replays" / "global_briefing" / "global_briefing_replay_evaluation-2026-07-10-2026-07-10.json",
            {
                "overall_status": "research_review_ready",
                "integrity": {
                    "isolated_ledger_complete": True,
                    "isolated_replay": True,
                    "main_ledger_written": False,
                    "run_daily_called": False,
                },
                "execution": {"mode": "isolated", "isolated_outputs_present": True, "no_trade_fallback": False},
            },
        )
        (self.site / "app").mkdir(parents=True, exist_ok=True)
        write_json(self.site / "app" / "briefing.generated.json", {"reportDate": "2026-07-10"})
        write_jsonl(self.briefing / "data" / "macro_signals-2026-07-10.jsonl", [])
        write_jsonl(self.trading / "data" / "macro_signals" / "macro_signals-2026-07-10.jsonl", [])
        self.outputs.mkdir(parents=True, exist_ok=True)
        (self.outputs / "每日全球晨间简报-2026-07-10.md").write_text("briefing", encoding="utf-8")

    def test_canonical_virtual_ledger_is_idempotent_and_audited(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        ledger_path = Path(first["ledger_path"])
        first_bytes = ledger_path.read_bytes()
        state_path = Path(first["state_path"])
        audit_path = Path(first["audit_path"])
        first_state_bytes = state_path.read_bytes()
        first_audit_bytes = audit_path.read_bytes()

        second = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertEqual(first["event_count"], 3)
        self.assertEqual(second["event_count"], 3)
        self.assertEqual(ledger_path.read_bytes(), first_bytes)
        self.assertEqual(state_path.read_bytes(), first_state_bytes)
        self.assertEqual(audit_path.read_bytes(), first_audit_bytes)
        self.assertTrue(second["audit"]["overall_passed"])
        self.assertTrue(second["write_performed"])
        self.assertEqual(len({event["ledger_event_id"] for event in second["events"]}), 3)
        self.assertIn("virtual_order_intent", {event["event_type"] for event in second["events"]})
        self.assertEqual(second["audit"]["source_counts"]["global_briefing_temp_orders"], 1)
        self.assertTrue(second["audit"]["reconciliation"]["passed"])

    def test_symbolic_all_temp_order_reconciles_to_unique_executed_trade(self) -> None:
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        executed = {
            "account": "US",
            "account_id": "global-briefing-us-paper-trading",
            "action": "SELL",
            "date": "2026-07-10",
            "exchange": "NASDAQ",
            "gross_value": 10,
            "market_type": "US",
            "order_id": "2026-07-10-US-SELL-TEST-P01",
            "paper_trading_only": True,
            "prediction_id": "2026-07-10-P01",
            "price": 10,
            "price_date": "2026-07-10",
            "quantity": 1,
            "reason": "risk threshold reached",
            "risk": "the price could rebound",
            "scenario": "reduce test exposure",
            "source": "fixture close",
            "symbol": "TEST",
            "timestamp": "2026-07-10T10:00:00",
        }
        existing = [
            json.loads(line)
            for line in trades_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        orders_path = self.briefing / "data" / "temp-orders-2026-07-10.json"
        payload = json.loads(orders_path.read_text(encoding="utf-8"))
        intent = {
            "account": "US",
            "action": "SELL",
            "date": "2026-07-10",
            "exchange": "NASDAQ",
            "market": "US",
            "order_id": executed["order_id"],
            "paper_trading_only": True,
            "prediction_id": executed["prediction_id"],
            "price": executed["price"],
            "price_date": executed["price_date"],
            "previous_close": 9.5,
            "quantity": "ALL",
            "reason": executed["reason"],
            "risk": executed["risk"],
            "scenario": executed["scenario"],
            "source": executed["source"],
            "symbol": executed["symbol"],
        }
        executed["order_fingerprint"] = atlas.stable_hash(
            {
                "account_id": executed["account_id"],
                "date": intent["date"],
                "order": {
                    key: value
                    for key, value in intent.items()
                    if key
                    not in {
                        "timestamp",
                        "order_id",
                        "idempotency_key",
                        "account",
                        "paper_account",
                    }
                },
            }
        )
        write_jsonl(trades_path, [*existing, executed])
        payload["orders"].append(intent)
        write_json(orders_path, payload)
        portfolio_path = self.briefing / "data" / "paper_portfolio_us.json"
        portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
        portfolio["positions"]["NASDAQ:TEST"]["quantity"] = 1
        write_json(portfolio_path, portfolio)

        result = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertTrue(result["audit"]["overall_passed"])
        resolved = next(
            event
            for event in result["events"]
            if event["event_type"] == "virtual_order_intent"
            and event["order_id"] == executed["order_id"]
        )
        self.assertEqual(resolved["filled_quantity"], 1)
        self.assertEqual(resolved["source_hash"], atlas.stable_hash(intent))
        self.assertEqual(resolved["quantity_resolution"], "executed_order_id")
        self.assertEqual(resolved["resolved_source_hash"], atlas.stable_hash(executed))
        self.assertEqual(
            resolved["resolved_order_fingerprint"], executed["order_fingerprint"]
        )
        self.assertEqual(
            json.loads(orders_path.read_text(encoding="utf-8"))["orders"][1]["quantity"],
            "ALL",
        )
        self.assertEqual(
            json.loads(trades_path.read_text(encoding="utf-8").splitlines()[-1])["quantity"],
            1,
        )

    def test_symbolic_all_temp_order_mismatch_fails_closed(self) -> None:
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        executed = {
            "account": "US",
            "account_id": "global-briefing-us-paper-trading",
            "action": "SELL",
            "date": "2026-07-10",
            "exchange": "NASDAQ",
            "gross_value": 10,
            "market_type": "US",
            "order_id": "2026-07-10-US-SELL-TEST-P01",
            "paper_trading_only": True,
            "prediction_id": "2026-07-10-P01",
            "price": 10,
            "price_date": "2026-07-10",
            "quantity": 1,
            "reason": "risk threshold reached",
            "risk": "the price could rebound",
            "scenario": "reduce test exposure",
            "source": "fixture close",
            "symbol": "TEST",
            "timestamp": "2026-07-10T10:00:00",
        }
        existing = [
            json.loads(line)
            for line in trades_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        orders_path = self.briefing / "data" / "temp-orders-2026-07-10.json"
        payload = json.loads(orders_path.read_text(encoding="utf-8"))
        intent = {
            "account": "US",
            "action": "SELL",
            "date": "2026-07-10",
            "exchange": "NASDAQ",
            "market": "US",
            "order_id": executed["order_id"],
            "paper_trading_only": True,
            "prediction_id": executed["prediction_id"],
            "price": executed["price"],
            "price_date": executed["price_date"],
            "previous_close": 9.5,
            "quantity": "ALL",
            "reason": executed["reason"],
            "risk": executed["risk"],
            "scenario": executed["scenario"],
            "source": executed["source"],
            "symbol": executed["symbol"],
        }
        executed["order_fingerprint"] = atlas.stable_hash(
            {
                "account_id": executed["account_id"],
                "date": intent["date"],
                "order": {
                    key: value
                    for key, value in intent.items()
                    if key
                    not in {
                        "timestamp",
                        "order_id",
                        "idempotency_key",
                        "account",
                        "paper_account",
                    }
                },
            }
        )
        write_jsonl(trades_path, [*existing, executed])
        intent["previous_close"] = 9.25
        payload["orders"].append(intent)
        write_json(orders_path, payload)

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        reasons = "\n".join(failed["audit"]["blocking_reasons"])
        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertIn("order_fingerprint mismatch", reasons)
        self.assertIn(executed["order_id"], reasons)
        self.assertFalse((self.ledger_root / "atlas_virtual_execution_ledger.jsonl").exists())

    def test_raw_virtual_source_rejects_nested_broker_keys_before_canonicalization(self) -> None:
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        trade = json.loads(trades_path.read_text(encoding="utf-8").splitlines()[0])
        trade["metadata"] = {"execution": {"broker_order_id": "REAL-ORDER-1"}}
        write_jsonl(trades_path, [trade])

        with patch.object(
            atlas,
            "canonicalize_global_paper_trade",
            wraps=atlas.canonicalize_global_paper_trade,
        ) as canonicalize:
            failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertEqual(canonicalize.call_count, 0)
        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertTrue(
            any(
                "metadata.execution.broker_order_id" in reason
                for reason in failed["audit"]["blocking_reasons"]
            )
        )
        self.assertFalse((self.ledger_root / "atlas_virtual_execution_ledger.jsonl").exists())

    def test_false_raw_safety_declaration_is_not_overwritten(self) -> None:
        orders_path = self.briefing / "data" / "temp-orders-2026-07-10.json"
        payload = json.loads(orders_path.read_text(encoding="utf-8"))
        payload["orders"][0]["no_real_broker_order"] = False
        write_json(orders_path, payload)

        with patch.object(
            atlas,
            "canonicalize_temp_order_intent",
            wraps=atlas.canonicalize_temp_order_intent,
        ) as canonicalize:
            failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertEqual(canonicalize.call_count, 0)
        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertTrue(
            any(
                "no_real_broker_order=False" in reason
                for reason in failed["audit"]["blocking_reasons"]
            )
        )
        raw_event = atlas.canonicalize_temp_order_intent(payload["orders"][0], orders_path, 1)
        self.assertFalse(raw_event["no_real_broker_order"])

    def test_raw_safety_audit_normalizes_camel_case_and_separator_variants(self) -> None:
        orders_path = self.briefing / "data" / "temp-orders-2026-07-10.json"
        payload = json.loads(orders_path.read_text(encoding="utf-8"))
        payload["orders"][0].update(
            {
                "paperTradingOnly": False,
                "live-Trading": True,
                "metadata": {"brokerOrderId": "REAL-ORDER-1"},
            }
        )
        write_json(orders_path, payload)

        with patch.object(
            atlas,
            "canonicalize_temp_order_intent",
            wraps=atlas.canonicalize_temp_order_intent,
        ) as canonicalize:
            failed = atlas.build_virtual_execution_ledger(write_files=True)

        reasons = "\n".join(failed["audit"]["blocking_reasons"])
        self.assertEqual(canonicalize.call_count, 0)
        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertIn("paperTradingOnly=False", reasons)
        self.assertIn("live-Trading=True", reasons)
        self.assertIn("metadata.brokerOrderId", reasons)

    def test_invalid_or_negative_ledger_numbers_fail_closed(self) -> None:
        replay_path = next(
            (self.trading / "data" / "replays" / "global_briefing" / "trades").glob("*.jsonl")
        )
        replay = json.loads(replay_path.read_text(encoding="utf-8").splitlines()[0])
        replay["fee"] = "oops"
        write_jsonl(replay_path, [replay])

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        reasons = "\n".join(failed["audit"]["blocking_reasons"])
        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertIn("fee='oops'", reasons)
        with self.assertRaisesRegex(ValueError, "invalid ledger number"):
            atlas.maybe_float("oops")

        event = atlas.canonicalize_global_paper_trade(
            json.loads(
                (self.briefing / "data" / "paper_trades_us.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            ),
            self.briefing / "data" / "paper_trades_us.jsonl",
            1,
            "global_briefing_paper_us",
        )
        event.update({"fee": -1, "tax": -2, "notional": -20})
        audit = atlas.audit_virtual_execution_ledger([event], {})
        audit_reasons = "\n".join(audit["blocking_reasons"])
        self.assertIn("negative fee", audit_reasons)
        self.assertIn("negative tax", audit_reasons)
        self.assertIn("negative notional", audit_reasons)

    def test_raw_virtual_source_rejects_nonfinite_numeric_strings(self) -> None:
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        trade = json.loads(trades_path.read_text(encoding="utf-8").splitlines()[0])
        trade["price"] = "NaN"
        write_jsonl(trades_path, [trade])

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertTrue(
            any(
                "non-finite numeric values" in reason and "price='NaN'" in reason
                for reason in failed["audit"]["blocking_reasons"]
            )
        )

    def test_raw_account_snapshot_rejects_nested_broker_fields(self) -> None:
        portfolio_path = self.briefing / "data" / "paper_portfolio_us.json"
        portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
        portfolio["positions"]["NASDAQ:TEST"]["routing"] = {"broker_account": "REAL-ACCOUNT"}
        write_json(portfolio_path, portfolio)

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertTrue(
            any(
                "positions.NASDAQ:TEST.routing.broker_account" in reason
                for reason in failed["audit"]["blocking_reasons"]
            )
        )
        with self.assertRaisesRegex(ValueError, "broker_account"):
            atlas.load_virtual_account_snapshots()

    def test_previous_canonical_source_mutation_fails_closed(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        ledger_path = Path(first["ledger_path"])
        first_bytes = ledger_path.read_bytes()
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        trade = json.loads(trades_path.read_text(encoding="utf-8").splitlines()[0])
        trade["price"] = 12
        write_jsonl(trades_path, [trade])

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertTrue(failed["audit"]["continuity"]["mutated_locators"])
        self.assertEqual(ledger_path.read_bytes(), first_bytes)

    def test_valid_new_source_event_can_append_to_an_authenticated_ledger(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        original = json.loads(trades_path.read_text(encoding="utf-8").splitlines()[0])
        appended = {
            **original,
            "date": "2026-07-11",
            "gross_value": 5,
            "price": 5,
            "quantity": 1,
            "symbol": "APPEND",
            "timestamp": "2026-07-11T09:30:00",
        }
        write_jsonl(trades_path, [original, appended])
        portfolio_path = self.briefing / "data" / "paper_portfolio_us.json"
        portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
        portfolio["cash"] = 99975
        portfolio["positions"]["NASDAQ:APPEND"] = {
            "exchange": "NASDAQ",
            "symbol": "APPEND",
            "quantity": 1,
        }
        write_json(portfolio_path, portfolio)

        preview = atlas.build_virtual_execution_ledger(write_files=False)
        second = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertTrue(first["audit"]["overall_passed"])
        self.assertTrue(preview["audit"]["overall_passed"])
        self.assertEqual(preview["audit"]["continuity"]["added_event_count"], 1)
        self.assertTrue(second["audit"]["overall_passed"])
        self.assertTrue(second["write_performed"])
        self.assertEqual(second["event_count"], first["event_count"] + 1)

    def test_missing_external_anchor_fails_closed_without_recreating_it(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        anchor_path = Path(first["anchor_path"])
        ledger_path = Path(first["ledger_path"])
        ledger_bytes = ledger_path.read_bytes()
        anchor_path.unlink()

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertFalse(anchor_path.exists())
        self.assertEqual(ledger_path.read_bytes(), ledger_bytes)
        self.assertIn(
            "canonical ledger exists without an externally authenticated trust anchor",
            failed["audit"]["blocking_reasons"],
        )

    def test_deleting_mutable_runtime_cannot_reset_external_authenticated_baseline(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        anchor_path = Path(first["anchor_path"])
        self.assertTrue(anchor_path.exists())
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        altered = json.loads(trades_path.read_text(encoding="utf-8").splitlines()[0])
        altered["price"] = 12
        altered["gross_value"] = 24
        write_jsonl(trades_path, [altered])
        shutil.rmtree(self.runtime)

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertTrue(anchor_path.exists())
        self.assertIn(
            "canonical ledger is missing while persistent baseline evidence exists",
            failed["audit"]["blocking_reasons"],
        )

    def test_external_anchor_hmac_rejects_a_rehashed_forgery(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        anchor_path = Path(first["anchor_path"])
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor["event_count"] = 0
        anchor["anchor_sha256"] = atlas.ledger_anchor_hash(anchor)
        write_json(anchor_path, anchor)

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertIn("canonical ledger anchor HMAC signature mismatch", failed["audit"]["blocking_reasons"])

    def test_missing_anchor_key_fails_closed_before_any_ledger_write(self) -> None:
        with patch.dict(os.environ, {atlas.TRUST_ANCHOR_HMAC_KEY_ENV: ""}, clear=False):
            failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertIn(
            f"canonical ledger cannot be verified: {atlas.TRUST_ANCHOR_HMAC_KEY_ENV} is not configured",
            failed["audit"]["blocking_reasons"],
        )
        self.assertFalse(atlas.VIRTUAL_LEDGER_PATH.exists())

    @unittest.skipUnless(os.name == "nt", "Windows user-environment fallback")
    def test_windows_user_environment_fallback_preserves_explicit_empty_values(self) -> None:
        values = {
            atlas.TRUST_ANCHOR_ROOT_ENV: str(self.trust_root),
            atlas.TRUST_ANCHOR_NAMESPACE_ENV: "registry-test-namespace",
            atlas.TRUST_ANCHOR_HMAC_KEY_ENV: "registry-test-key",
        }

        class FakeWinReg:
            HKEY_CURRENT_USER = object()

            @staticmethod
            def OpenKey(_hive: object, _path: str) -> contextlib.AbstractContextManager[object]:
                return contextlib.nullcontext(object())

            @staticmethod
            def QueryValueEx(_key: object, name: str) -> tuple[str, int]:
                return values[name], 1

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.dict(sys.modules, {"winreg": FakeWinReg}, clear=False),
        ):
            self.assertEqual(
                atlas.configured_trust_anchor_value(atlas.TRUST_ANCHOR_ROOT_ENV),
                str(self.trust_root),
            )
            self.assertEqual(atlas.trust_anchor_hmac_key(), b"registry-test-key")
            with patch.dict(os.environ, {atlas.TRUST_ANCHOR_HMAC_KEY_ENV: ""}, clear=False):
                self.assertIsNone(atlas.trust_anchor_hmac_key())

    def test_workspace_local_trust_anchor_root_is_rejected(self) -> None:
        with patch.dict(os.environ, {atlas.TRUST_ANCHOR_ROOT_ENV: str(self.root)}, clear=False):
            failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertIn(
            "external trust-anchor root must not be inside the workspace",
            failed["audit"]["blocking_reasons"],
        )
        self.assertFalse(atlas.VIRTUAL_LEDGER_PATH.exists())

    def test_canonical_ledger_deletion_or_emptying_fails_closed_against_external_anchor(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        ledger_path = Path(first["ledger_path"])
        anchor_path = Path(first["anchor_path"])
        self.assertTrue(anchor_path.exists())

        ledger_path.unlink()
        deleted = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(deleted["audit"]["overall_passed"])
        self.assertFalse(deleted["write_performed"])
        self.assertIn(
            "canonical ledger is missing while persistent baseline evidence exists",
            deleted["audit"]["blocking_reasons"],
        )
        self.assertTrue(anchor_path.exists())

        # Removing the whole mutable ledger directory must still leave the anchor
        # behind and prevent reconstruction from potentially altered sources.
        shutil.rmtree(self.ledger_root)
        missing_directory = atlas.build_virtual_execution_ledger(write_files=True)
        self.assertFalse(missing_directory["audit"]["overall_passed"])
        self.assertFalse(missing_directory["write_performed"])
        self.assertTrue(anchor_path.exists())

    def test_canonical_ledger_emptying_fails_closed_against_anchor_hash(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        ledger_path = Path(first["ledger_path"])
        ledger_path.write_text("", encoding="utf-8")

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        reasons = "\n".join(failed["audit"]["blocking_reasons"])
        self.assertIn("canonical ledger anchor file hash mismatch", reasons)

    def test_account_position_mismatch_fails_closed(self) -> None:
        portfolio_path = self.briefing / "data" / "paper_portfolio_us.json"
        portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
        portfolio["positions"]["NASDAQ:TEST"]["quantity"] = 99
        write_json(portfolio_path, portfolio)

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertTrue(failed["audit"]["reconciliation"]["blocking_reasons"])

    def test_duplicate_source_event_fails_closed_without_overwriting_good_ledger(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        ledger_path = Path(first["ledger_path"])
        first_bytes = ledger_path.read_bytes()
        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        trade = json.loads(trades_path.read_text(encoding="utf-8").splitlines()[0])
        write_jsonl(trades_path, [trade, trade])

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertFalse(failed["write_performed"])
        self.assertIn("duplicate ledger_event_id detected", failed["audit"]["blocking_reasons"])
        self.assertEqual(ledger_path.read_bytes(), first_bytes)

    def test_replay_selection_is_period_based_not_mtime_based(self) -> None:
        replay_dir = self.trading / "data" / "replays" / "global_briefing"
        older = replay_dir / "global_briefing_replay_evaluation-2025-01-01-2025-12-31.json"
        write_json(older, {})
        older.touch()

        selected, start, end = atlas.select_replay_evaluation(replay_dir, "2026-07-10")

        self.assertEqual(selected.name, "global_briefing_replay_evaluation-2026-07-10-2026-07-10.json")
        self.assertEqual((start, end), ("2026-07-10", "2026-07-10"))

    def test_replay_safety_does_not_claim_strategy_evidence(self) -> None:
        with patch.object(atlas, "capture_command", return_value=type("Result", (), {"returncode": 0, "stdout": '{"auto_applied": false, "recommended_state": "shadow"}', "stderr": ""})()):
            result = atlas.run_replay_shadow_validation("2026-07-10")

        self.assertTrue(result["replay"]["execution_safety_passed"])
        self.assertFalse(result["replay"]["strategy_evidence_passed"])
        self.assertEqual(result["replay"]["validation_scope"], "execution_isolation_only")

    def test_cycle_writes_run_audit_with_fail_closed_gates(self) -> None:
        args = argparse.Namespace(
            date="2026-07-10",
            dry_run=False,
            force=False,
            force_site=False,
            skip_sync=False,
            skip_tests=False,
            skip_site=True,
            skip_trading_core=True,
        )
        replay_shadow = {
            "overall_passed": True,
            "replay": {"passed": True},
            "shadow_promotion_gate": {"passed": True, "payload": {"auto_applied": False, "recommended_state": "shadow"}},
        }
        with (
            patch.object(atlas, "doctor_checks", return_value=[atlas.Check("Python", "ok", "3.12")]),
            patch.object(atlas, "command_sync", return_value=0),
            patch.object(atlas, "run_replay_shadow_validation", return_value=replay_shadow),
            patch.object(atlas, "command_test", return_value=0),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(atlas.command_cycle(args), 0)

        audit_path = self.runtime / "run_audits" / "atlas-cycle-2026-07-10.json"
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["overall_passed"])
        self.assertTrue(payload["operational_gate_passed"])
        self.assertFalse(payload["release_candidate_passed"])
        self.assertFalse(payload["research_promotion_passed"])
        self.assertEqual(payload["gate_scope"], "daily_operational")
        self.assertTrue(payload["boundary"]["single_virtual_execution_ledger"])
        self.assertTrue(payload["boundary"]["targeted_integration_test_gate"])
        self.assertFalse(payload["boundary"]["full_test_suite_executed"])
        self.assertEqual(payload["ledger"]["event_count"], 3)
        cycle_state = json.loads((self.runtime / "cycle_state.json").read_text(encoding="utf-8"))
        self.assertTrue(Path(cycle_state["last_history_json"]).exists())
        self.assertTrue(cycle_state["operational_gate_passed"])
        self.assertFalse(cycle_state["release_candidate_passed"])
        self.assertEqual(cycle_state["blocking_reasons"], [])
        self.assertTrue(payload["release_candidate_evidence"]["sync_passed"])
        self.assertTrue(payload["release_candidate_evidence"]["canonical_ledger_write_performed"])
        self.assertTrue(payload["release_candidate_evidence"]["required_stages_passed"])
        self.assertEqual(payload["audit_chain"]["entry_sha256"], atlas.audit_record_hash(payload))
        self.assertTrue(atlas.audit_cycle_history(Path(cycle_state["last_history_json"]).parent)["passed"])
        workspace_lock = json.loads((self.runtime / "workspace-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(len(workspace_lock["repositories"]), 3)
        self.assertFalse(workspace_lock["release_reproducible"])

    def test_run_audit_hash_chain_detects_history_mutation(self) -> None:
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        payload = {"run_id": "RUN-1", "audit_chain": {"schema_version": 1, "sequence": 1, "previous_audit_sha256": None}}
        payload["audit_chain"]["entry_sha256"] = atlas.audit_record_hash(payload)
        write_json(history / "RUN-1.json", payload)
        genesis = atlas.audit_cycle_history(history, allow_unanchored_genesis=True)
        self.assertTrue(genesis["passed"])
        atlas.write_cycle_history_anchor(history, genesis)
        self.assertTrue(atlas.audit_cycle_history(history)["passed"])
        payload["run_id"] = "TAMPERED"
        write_json(history / "RUN-1.json", payload)
        result = atlas.audit_cycle_history(history)
        self.assertFalse(result["passed"])
        self.assertIn("entry hash mismatch", result["errors"][0])

    def test_run_audit_anchor_detects_tail_truncation_and_pre_genesis_legacy_insert(self) -> None:
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        first = {"run_id": "RUN-1", "audit_chain": {"schema_version": 1, "sequence": 1, "previous_audit_sha256": None}}
        first["audit_chain"]["entry_sha256"] = atlas.audit_record_hash(first)
        write_json(history / "RUN-1.json", first)
        atlas.write_cycle_history_anchor(
            history,
            atlas.audit_cycle_history(history, allow_unanchored_genesis=True),
        )

        (history / "RUN-1.json").unlink()
        truncated = atlas.audit_cycle_history(history)
        self.assertFalse(truncated["passed"])
        self.assertIn("manifest differs from its external anchor", "\n".join(truncated["errors"]))

        write_json(history / "RUN-1.json", first)
        write_json(history / "0000-forged-legacy.json", {"run_id": "FORGED-LEGACY"})
        inserted = atlas.audit_cycle_history(history)
        self.assertFalse(inserted["passed"])
        inserted_errors = "\n".join(inserted["errors"])
        self.assertIn("manifest differs from its external anchor", inserted_errors)
        self.assertIn("legacy run audit records", inserted_errors)

    def test_run_audit_anchor_allows_only_verified_append_then_reanchors(self) -> None:
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        first = {"run_id": "RUN-1", "audit_chain": {"schema_version": 1, "sequence": 1, "previous_audit_sha256": None}}
        first["audit_chain"]["entry_sha256"] = atlas.audit_record_hash(first)
        write_json(history / "RUN-1.json", first)
        atlas.write_cycle_history_anchor(
            history,
            atlas.audit_cycle_history(history, allow_unanchored_genesis=True),
        )

        second = {
            "run_id": "RUN-2",
            "audit_chain": {
                "schema_version": 1,
                "sequence": 2,
                "previous_audit_sha256": first["audit_chain"]["entry_sha256"],
            },
        }
        second["audit_chain"]["entry_sha256"] = atlas.audit_record_hash(second)
        write_json(history / "RUN-2.json", second)
        pending = atlas.audit_cycle_history(history, allow_anchor_append=True)
        self.assertTrue(pending["passed"])
        atlas.write_cycle_history_anchor(history, pending)
        self.assertTrue(atlas.audit_cycle_history(history)["passed"])

    def test_history_nonfinite_json_fails_closed_without_raising(self) -> None:
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        history.mkdir(parents=True, exist_ok=True)
        (history / "BROKEN.json").write_text('{"value": NaN}\n', encoding="utf-8")

        result = atlas.audit_cycle_history(history)

        self.assertFalse(result["passed"])
        self.assertTrue(any("unreadable audit JSON" in error for error in result["errors"]))

    def test_history_anchor_write_failure_rolls_back_new_record_and_retry_succeeds(self) -> None:
        args = argparse.Namespace(
            date="2026-07-10",
            dry_run=False,
            force=False,
            force_site=False,
            skip_sync=False,
            skip_tests=False,
            skip_site=True,
            skip_trading_core=True,
        )
        replay_shadow = {
            "overall_passed": True,
            "replay": {"passed": True},
            "shadow_promotion_gate": {"passed": True, "payload": {"auto_applied": False}},
        }
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(atlas, "doctor_checks", return_value=[atlas.Check("Python", "ok", "3.12")])
            )
            stack.enter_context(patch.object(atlas, "command_sync", return_value=0))
            stack.enter_context(
                patch.object(atlas, "run_replay_shadow_validation", return_value=replay_shadow)
            )
            stack.enter_context(patch.object(atlas, "command_test", return_value=0))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(
                patch.object(atlas, "write_cycle_history_anchor", side_effect=OSError("disk full"))
            )
            self.assertEqual(atlas.command_cycle(args), 2)

        self.assertEqual(list(history.glob("*.json")) if history.exists() else [], [])
        self.assertTrue(atlas.audit_cycle_history(history)["passed"])

        with (
            patch.object(atlas, "doctor_checks", return_value=[atlas.Check("Python", "ok", "3.12")]),
            patch.object(atlas, "command_sync", return_value=0),
            patch.object(atlas, "run_replay_shadow_validation", return_value=replay_shadow),
            patch.object(atlas, "command_test", return_value=0),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(atlas.command_cycle(args), 0)
        self.assertTrue(atlas.audit_cycle_history(history)["passed"])

    def test_cycle_lock_refuses_empty_or_malformed_owner_record(self) -> None:
        lock_path = self.runtime / "cycle.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "unreadable; refusing unsafe takeover"):
            with atlas.cycle_lock("2026-07-10"):
                self.fail("invalid lock must not be taken over")

        self.assertEqual(lock_path.read_text(encoding="utf-8"), "")

    def test_cycle_lock_never_takes_over_a_live_owner_even_when_old(self) -> None:
        lock_path = self.runtime / "cycle.lock"
        write_json(
            lock_path,
            {
                "pid": os.getpid(),
                "date": "2026-07-10",
                "started_at": "2000-01-01T00:00:00Z",
                "token": "live-owner",
            },
        )

        with patch.object(atlas, "process_is_running", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with atlas.cycle_lock("2026-07-10"):
                    self.fail("live lock must not be taken over")

        self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8"))["token"], "live-owner")

    def test_cycle_lock_reclaims_only_a_verified_dead_owner(self) -> None:
        lock_path = self.runtime / "cycle.lock"
        write_json(
            lock_path,
            {
                "pid": 999999,
                "date": "2026-07-10",
                "started_at": "2000-01-01T00:00:00Z",
                "token": "dead-owner",
            },
        )

        with patch.object(atlas, "process_is_running", return_value=False):
            with atlas.cycle_lock("2026-07-10"):
                acquired = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertNotEqual(acquired["token"], "dead-owner")
                self.assertEqual(acquired["pid"], os.getpid())

        self.assertFalse(lock_path.exists())

    def test_cycle_lock_guard_blocks_dead_metadata_takeover_during_contention(self) -> None:
        """A contender must not unlink a stale-looking record while the OS guard is held."""
        lock_path = self.runtime / "cycle.lock"
        write_json(
            lock_path,
            {
                "pid": 999999,
                "date": "2026-07-10",
                "started_at": "2000-01-01T00:00:00Z",
                "token": "dead-looking-owner",
            },
        )

        # A held guard is authoritative even when the diagnostic pid is dead.
        # This models the race where another process has already taken over a
        # stale record but has not yet replaced its JSON metadata.
        with patch.object(atlas, "acquire_cycle_lock_guard", return_value=None), patch.object(
            atlas, "process_is_running", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with atlas.cycle_lock("2026-07-10"):
                    self.fail("contending cycle must not start")

        self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8"))["token"], "dead-looking-owner")

    def test_cycle_lock_releases_advisory_guard_after_owner_exits(self) -> None:
        lock_path = self.runtime / "cycle.lock"
        guard_path = atlas.cycle_lock_guard_path(lock_path)

        with atlas.cycle_lock("2026-07-10"):
            self.assertTrue(lock_path.exists())
            self.assertIsNone(atlas.acquire_cycle_lock_guard(guard_path))

        descriptor = atlas.acquire_cycle_lock_guard(guard_path)
        self.assertIsNotNone(descriptor)
        if descriptor is not None:
            atlas.release_cycle_lock_guard(descriptor)

    def test_subprocess_helpers_enforce_timeout_and_return_failure(self) -> None:
        timeout_error = subprocess.TimeoutExpired(["blocked"], timeout=0.01)
        with patch.object(atlas.subprocess, "run", side_effect=timeout_error) as run:
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(atlas.run_command(["blocked"], quiet=True, timeout=0.01), 124)
            self.assertIn("timed out", stderr.getvalue())
            self.assertEqual(run.call_args.kwargs["timeout"], 0.01)

        with patch.object(atlas.subprocess, "run", side_effect=timeout_error):
            result = atlas.capture_command(["blocked"], timeout=0.01)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)

    def test_sync_failure_writes_audit_but_never_writes_canonical_ledger(self) -> None:
        args = argparse.Namespace(
            date="2026-07-10",
            dry_run=False,
            force=False,
            force_site=False,
            skip_sync=False,
            skip_tests=True,
            skip_site=True,
            skip_trading_core=True,
        )
        replay_shadow = {
            "overall_passed": True,
            "replay": {"passed": True},
            "shadow_promotion_gate": {"passed": True, "payload": {"auto_applied": False, "recommended_state": "shadow"}},
        }
        with (
            patch.object(atlas, "doctor_checks", return_value=[atlas.Check("Python", "ok", "3.12")]),
            patch.object(atlas, "command_sync", return_value=1),
            patch.object(atlas, "run_replay_shadow_validation", return_value=replay_shadow),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(atlas.command_cycle(args), 1)

        audit_path = self.runtime / "run_audits" / "atlas-cycle-2026-07-10.json"
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertFalse(payload["overall_passed"])
        self.assertFalse(payload["ledger"]["write_performed"])
        self.assertTrue(payload["boundary"]["canonical_write_blocked_by_upstream_gate"])
        self.assertFalse((self.ledger_root / "atlas_virtual_execution_ledger.jsonl").exists())

    def test_skip_sync_blocks_operational_and_release_gates_and_persists_state(self) -> None:
        args = argparse.Namespace(
            date="2026-07-10",
            dry_run=False,
            force=False,
            force_site=False,
            skip_sync=True,
            skip_tests=False,
            skip_site=False,
            skip_trading_core=False,
            full_tests=True,
        )
        replay_shadow = {
            "overall_passed": True,
            "safety_gate_passed": True,
            "strategy_evidence_passed": True,
            "replay": {"passed": True, "strategy_evidence_passed": True},
            "shadow_promotion_gate": {
                "passed": True,
                "evidence_passed": True,
                "payload": {"auto_applied": False, "recommended_state": "shadow"},
            },
        }
        with (
            patch.object(atlas, "doctor_checks", return_value=[atlas.Check("Python", "ok", "3.12")]),
            patch.object(atlas, "command_sync", return_value=0) as sync,
            patch.object(atlas, "run_replay_shadow_validation", return_value=replay_shadow),
            patch.object(atlas, "command_test", return_value=0),
            patch.object(
                atlas,
                "build_workspace_lock",
                return_value={"release_reproducible": True, "repositories": []},
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(atlas.command_cycle(args), 1)

        sync.assert_not_called()
        payload = json.loads(
            (self.runtime / "run_audits" / "atlas-cycle-2026-07-10.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(payload["operational_gate_passed"])
        self.assertFalse(payload["release_candidate_passed"])
        self.assertIn("sync stage was skipped", payload["blocking_reasons"])
        self.assertFalse(payload["release_candidate_evidence"]["sync_passed"])
        self.assertFalse(payload["release_candidate_evidence"]["canonical_ledger_write_performed"])
        self.assertFalse(payload["release_candidate_evidence"]["required_stages_passed"])
        cycle_state = json.loads((self.runtime / "cycle_state.json").read_text(encoding="utf-8"))
        self.assertFalse(cycle_state["operational_gate_passed"])
        self.assertFalse(cycle_state["release_candidate_passed"])
        self.assertEqual(cycle_state["blocking_reasons"], payload["blocking_reasons"])

    def test_release_required_stages_must_all_pass(self) -> None:
        stages = [{"name": name, "status": "passed"} for name in atlas.RELEASE_REQUIRED_STAGES]
        self.assertTrue(atlas.required_cycle_stages_passed(stages))
        stages[-1]["status"] = "blocked"
        self.assertFalse(atlas.required_cycle_stages_passed(stages))

    def test_git_release_ready_requires_fetchable_remote(self) -> None:
        git = atlas.shutil.which("git")
        self.assertIsNotNone(git)
        repository = self.root / "repository"
        repository.mkdir()

        def run_git(*arguments: str, cwd: Path = repository) -> None:
            subprocess.run(
                [str(git), *arguments],
                cwd=cwd,
                check=True,
                text=True,
                encoding="utf-8",
                capture_output=True,
            )

        run_git("init")
        run_git("config", "user.name", "ATLAS Test")
        run_git("config", "user.email", "atlas@example.invalid")
        (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        run_git("add", "tracked.txt")
        run_git("commit", "-m", "initial")
        run_git("remote", "add", "origin", str(self.root / "missing-remote.git"))

        dead_remote = atlas.git_repository_provenance("test", repository)

        self.assertTrue(dead_remote["available"])
        self.assertTrue(dead_remote["clean"])
        self.assertEqual(dead_remote["remote_names"], ["origin"])
        self.assertFalse(dead_remote["remote_fetchable"])
        self.assertFalse(dead_remote["release_ready"])
        self.assertFalse(dead_remote["remote_probes"][0]["fetchable"])
        self.assertNotIn("missing-remote.git", json.dumps(dead_remote))

        working_remote = self.root / "working-remote.git"
        run_git("init", "--bare", str(working_remote), cwd=self.root)
        run_git("remote", "set-url", "origin", str(working_remote))

        unpublished_head = atlas.git_repository_provenance("test", repository)

        self.assertFalse(unpublished_head["remote_fetchable"])
        self.assertFalse(unpublished_head["remote_probes"][0]["head_advertised"])
        self.assertFalse(unpublished_head["release_ready"])

        run_git("push", "origin", "HEAD:refs/heads/main")
        fetchable_remote = atlas.git_repository_provenance("test", repository)

        self.assertTrue(fetchable_remote["remote_fetchable"])
        self.assertEqual(fetchable_remote["fetchable_remote_names"], ["origin"])
        self.assertTrue(fetchable_remote["remote_probes"][0]["head_advertised"])
        self.assertTrue(fetchable_remote["release_ready"])

    def test_workspace_lock_hash_excludes_generated_at(self) -> None:
        def provenance(name: str, path: Path) -> dict:
            return {
                "name": name,
                "path": str(path),
                "available": True,
                "clean": True,
                "commit": "a" * 40,
                "remote_fetchable": True,
                "release_ready": True,
            }

        with (
            patch.object(atlas, "git_repository_provenance", side_effect=provenance),
            patch.object(
                atlas,
                "utc_now",
                side_effect=["2026-07-10T00:00:00Z", "2026-07-10T00:00:01Z"],
            ),
        ):
            first = atlas.build_workspace_lock()
            second = atlas.build_workspace_lock()

        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["content_sha256"], second["content_sha256"])

    def test_cycle_idempotency_is_bound_to_workspace_commits(self) -> None:
        args = argparse.Namespace(
            date="2026-07-10",
            dry_run=False,
            force=False,
            force_site=False,
            skip_sync=False,
            skip_tests=False,
            skip_site=True,
            skip_trading_core=True,
            full_tests=False,
        )
        replay_shadow = {
            "overall_passed": True,
            "safety_gate_passed": True,
            "strategy_evidence_passed": False,
            "replay": {"passed": True},
            "shadow_promotion_gate": {"passed": True, "payload": {}},
        }

        with (
            patch.object(atlas, "doctor_checks", return_value=[atlas.Check("Python", "ok", "3.12")]),
            patch.object(atlas, "command_sync", return_value=0),
            patch.object(atlas, "run_replay_shadow_validation", return_value=replay_shadow),
            patch.object(atlas, "command_test", return_value=0),
            patch.object(
                atlas,
                "build_workspace_lock",
                side_effect=[
                    {"content_sha256": "a" * 64, "release_reproducible": False, "repositories": []},
                    {"content_sha256": "b" * 64, "release_reproducible": False, "repositories": []},
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(atlas.command_cycle(args), 0)
            first_state = json.loads((self.runtime / "cycle_state.json").read_text(encoding="utf-8"))
            self.assertEqual(atlas.command_cycle(args), 0)

        second_state = json.loads((self.runtime / "cycle_state.json").read_text(encoding="utf-8"))
        second_audit = json.loads(Path(second_state["last_audit_json"]).read_text(encoding="utf-8"))
        self.assertNotEqual(first_state["idempotency_key"], second_state["idempotency_key"])
        self.assertEqual(second_state["workspace_lock_content_sha256"], "b" * 64)
        self.assertFalse(second_audit["idempotent_replay"])

    def test_configured_report_date_uses_briefing_timezone(self) -> None:
        settings = self.root / "settings.json"
        write_json(settings, {"timezone": "Asia/Shanghai"})

        current = atlas.configured_report_date(
            datetime(2026, 7, 16, 16, 30, tzinfo=UTC),
            settings_path=settings,
        )

        self.assertEqual(current.isoformat(), "2026-07-17")

    def test_doctor_node_version_matches_site_and_includes_root_repository(self) -> None:
        def which(command: str) -> str | None:
            return "node" if command == "node" else None

        def capture_for(node_version: str):
            def capture(
                command: list[str],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                stdout = node_version if command[-1] == "--version" else "ok"
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

            return capture

        for version, expected_status in (("v22.14.0", "error"), ("v22.15.0", "ok")):
            with self.subTest(version=version):
                with (
                    patch.object(atlas.shutil, "which", side_effect=which),
                    patch.object(atlas, "capture_command", side_effect=capture_for(version)),
                    patch.object(atlas.importlib.util, "find_spec", return_value=object()),
                ):
                    checks = atlas.doctor_checks()

                node_check = next(check for check in checks if check.name == "Node.js")
                self.assertEqual(node_check.status, expected_status)
                self.assertIn("required>=22.15.0", node_check.detail)
                self.assertIn("root repository", {check.name for check in checks})

    def test_heal_command_routes_safe_flags_to_control_plane(self) -> None:
        args = argparse.Namespace(
            date="2026-07-10",
            apply_safe=True,
            deep=True,
            strict=True,
            status=False,
            json=True,
        )
        with patch.object(atlas, "run_command", return_value=0) as run:
            self.assertEqual(atlas.command_heal(args), 0)

        command = run.call_args.args[0]
        self.assertIn("self_healing.py", command[1])
        self.assertEqual(command[command.index("--date") + 1], "2026-07-10")
        self.assertIn("--apply-safe", command)
        self.assertIn("--deep", command)
        self.assertIn("--strict", command)
        self.assertIn("--json", command)

    def test_improvement_command_routes_cross_run_verification(self) -> None:
        args = argparse.Namespace(
            date="2026-07-10",
            apply_safe=True,
            strict=True,
            status=False,
            json=True,
        )
        with patch.object(atlas, "run_command", return_value=0) as run:
            self.assertEqual(atlas.command_improvements(args), 0)

        command = run.call_args.args[0]
        self.assertIn("improvement_tracker.py", command[1])
        self.assertIn("--apply-safe", command)
        self.assertIn("--strict", command)
        self.assertIn("--json", command)

    def test_alert_due_processor_routes_without_a_date(self) -> None:
        args = atlas.build_parser().parse_args(["alerts", "--process-due", "--json"])

        with patch.object(atlas, "run_command", return_value=0) as run:
            self.assertEqual(atlas.command_alerts(args), 0)

        command = run.call_args.args[0]
        self.assertIn("alert_dispatch.py", command[1])
        self.assertIn("--process-due", command)
        self.assertNotIn("--date", command)
        self.assertIn("--json", command)

    def test_alert_command_routes_an_immutable_alert_revision_id(self) -> None:
        args = atlas.build_parser().parse_args(
            ["alerts", "--date", "2026-07-10", "--alert-id", "alert-2026-07-10-deadbeef", "--retry"]
        )

        with patch.object(atlas, "run_command", return_value=0) as run:
            self.assertEqual(atlas.command_alerts(args), 0)

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--alert-id") + 1], "alert-2026-07-10-deadbeef")
        self.assertIn("--retry", command)

    def test_full_test_mode_runs_unfiltered_trading_core_suite(self) -> None:
        args = argparse.Namespace(skip_site=True, skip_trading_core=False, full=True)
        with patch.object(atlas, "run_command", return_value=0) as run:
            self.assertEqual(atlas.command_test(args), 0)

        core_call = next(call for call in run.call_args_list if call.kwargs.get("cwd") == self.trading)
        self.assertEqual(core_call.args[0], [atlas.sys.executable, "-m", "pytest"])


if __name__ == "__main__":
    unittest.main()
