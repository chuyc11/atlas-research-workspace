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
import threading
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
            VIRTUAL_LEDGER_HEAD_PATH=self.ledger_root / "current.json",
            VIRTUAL_LEDGER_GENERATIONS_ROOT=self.ledger_root / "generations",
            RUN_AUDIT_ROOT=self.runtime / "run_audits",
            CYCLE_STATE_PATH=self.runtime / "cycle_state.json",
            PAPER_TRADING_CONFIG_PATH=self.briefing / "config" / "paper_trading.json",
            PREDICTION_LEDGER_PATH=self.briefing / "data" / "predictions.jsonl",
        )
        self.patcher.start()
        self._seed_sources()
        self.paper_snapshot_patcher = patch.object(
            atlas,
            "load_locked_paper_execution_snapshot",
            side_effect=self._paper_snapshot,
        )
        self.paper_snapshot_patcher.start()

    def tearDown(self) -> None:
        self.paper_snapshot_patcher.stop()
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

    def _paper_snapshot(self) -> dict:
        accounts = []
        for account in ("US", "CHINA"):
            suffix = account.lower()
            portfolio = self.briefing / "data" / f"paper_portfolio_{suffix}.json"
            trades = self.briefing / "data" / f"paper_trades_{suffix}.jsonl"
            valuations = self.briefing / "data" / f"paper_valuations_{suffix}.jsonl"
            accounts.append(
                {
                    "account": account,
                    "account_id": json.loads(portfolio.read_text(encoding="utf-8"))["account_id"],
                    "market_scope": account,
                    "portfolio_path": str(portfolio.resolve()),
                    "trades_path": str(trades.resolve()),
                    "valuations_path": str(valuations.resolve()),
                    "state": json.loads(portfolio.read_text(encoding="utf-8")),
                    "trades": [
                        json.loads(line)
                        for line in trades.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ],
                    "valuations": [],
                }
            )
        return {
            "schema_version": 1,
            "captured_at_utc": "2026-07-10T00:00:00+00:00",
            "generation_sha256": atlas.stable_hash({"accounts": accounts}),
            "accounts": accounts,
        }

    def _seed_polluted_global_history_anchor(self) -> tuple[Path, dict]:
        anchor_path = atlas.global_cycle_history_anchor_path()
        source_root = self.root / "polluted-cycle-root"
        history_root = str(Path("work") / "shared" / "atlas" / "run_audits" / "history")
        with patch.object(atlas, "ROOT", source_root):
            source = {
                "schema_version": atlas.HISTORY_ANCHOR_SCHEMA_VERSION,
                "history_root": history_root,
                "entries": [
                    {
                        "path": (
                            "2026-07-18/ATLAS-CYCLE-20260718-"
                            "RUN-20260801T053058095957Z.json"
                        ),
                        "sha256": "f" * 64,
                    }
                ],
                "entry_count": 1,
                **atlas.next_monotonic_anchor_fields(
                    anchor_path,
                    head_hash_field="anchor_sha256",
                ),
            }
            source["anchor_sha256"] = atlas.history_anchor_hash(source)
            source = atlas.sign_trust_anchor(source)
            atlas.write_monotonic_anchor(
                anchor_path,
                source,
                head_hash_field="anchor_sha256",
            )
        return anchor_path, source

    def _seed_recovery_target_history(self) -> list[dict[str, str]]:
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        payload = {
            "run_id": "REAL-RUN-1",
            "audit_chain": {
                "schema_version": 1,
                "sequence": 1,
                "previous_audit_sha256": None,
            },
        }
        payload["audit_chain"]["entry_sha256"] = atlas.audit_record_hash(payload)
        write_json(history / "REAL-RUN-1.json", payload)
        integrity = atlas.audit_cycle_history(history, allow_unanchored_genesis=True)
        self.assertTrue(integrity["passed"], integrity["errors"])
        atlas.write_cycle_history_anchor(history, integrity)
        return atlas.global_cycle_history_manifest()

    def _recovery_backup_evidence(self, entries: list[dict[str, str]]) -> dict:
        return {
            "latest_path": str(self.runtime / "backups" / "latest.json"),
            "latest_file_sha256": "1" * 64,
            "manifest_path": str(self.root.parent / "atlas-backup.manifest.json"),
            "manifest_file_sha256": "2" * 64,
            "manifest_sha256": "3" * 64,
            "archive_sha256": "4" * 64,
            "created_at": "2026-07-31T19:17:06.193365Z",
            "verified": True,
            "archive_integrity_verified": True,
            "restore_verified": True,
            "encrypted_container_authenticated": True,
            "snapshot_profile": "daily",
            "latest_metadata_authentication_key_id": "latest-key-id",
            "manifest_metadata_authentication_key_id": "manifest-key-id",
            "entries": entries,
            "entries_sha256": atlas.stable_hash(entries),
        }

    def _bootstrap_empty_global_history_anchor(self) -> None:
        integrity = atlas.audit_global_cycle_history()
        self.assertTrue(integrity["passed"], integrity["errors"])
        self.assertFalse(integrity["anchor_present"])
        atlas.write_global_cycle_history_anchor(integrity)
        verified = atlas.audit_global_cycle_history()
        self.assertTrue(verified["passed"], verified["errors"])
        self.assertTrue(verified["anchor_present"])

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

    def test_cycle_fingerprint_changes_when_prediction_semantics_change(self) -> None:
        predictions = self.briefing / "data" / "predictions.jsonl"
        write_jsonl(
            predictions,
            [{"date": "2026-07-10", "prediction_id": "2026-07-10-P01", "probability": 0.6}],
        )
        first = atlas.build_cycle_fingerprint("2026-07-10")

        write_jsonl(
            predictions,
            [{"date": "2026-07-10", "prediction_id": "2026-07-10-P01", "probability": 0.7}],
        )
        second = atlas.build_cycle_fingerprint("2026-07-10")

        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertIn(
            "work/global-briefing/data/predictions.jsonl",
            {item["path"].replace("\\", "/") for item in second["files"]},
        )

    def test_canonical_orders_fail_closed_when_prediction_original_is_missing(self) -> None:
        write_json(
            self.briefing / "config" / "paper_trading.json",
            {"order_contract": {"prediction_reference_required_from_date": "2026-07-10"}},
        )
        write_jsonl(
            self.briefing / "data" / "predictions.jsonl",
            [{"date": "2026-07-10", "prediction_id": "UNRELATED"}],
        )

        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["write_performed"])
        self.assertFalse(failed["audit"]["overall_passed"])
        self.assertTrue(
            any(
                "has no original prediction record" in reason
                or "prediction_id is required" in reason
                for reason in failed["audit"]["blocking_reasons"]
            )
        )

    def test_canonical_state_binds_locked_paper_generation_and_predictions(self) -> None:
        write_jsonl(
            self.briefing / "data" / "predictions.jsonl",
            [{"date": "2026-07-10", "prediction_id": "2026-07-10-P01"}],
        )

        result = atlas.build_virtual_execution_ledger(write_files=True)

        expected_snapshot = self._paper_snapshot()
        self.assertEqual(
            result["state"]["paper_generation_sha256"],
            expected_snapshot["generation_sha256"],
        )
        self.assertEqual(
            result["state"]["prediction_semantic_hash"],
            atlas.stable_hash(
                [{"date": "2026-07-10", "prediction_id": "2026-07-10-P01"}]
            ),
        )

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

    def test_notional_sized_temp_order_is_a_valid_order_intent(self) -> None:
        orders_path = self.briefing / "data" / "temp-orders-2026-07-10.json"
        payload = json.loads(orders_path.read_text(encoding="utf-8"))
        payload["orders"][0].pop("quantity")
        payload["orders"][0]["notional"] = 400
        write_json(orders_path, payload)

        result = atlas.build_virtual_execution_ledger(write_files=True)

        intents = [
            event
            for event in result["events"]
            if event["event_type"] == "virtual_order_intent"
        ]
        self.assertTrue(result["audit"]["overall_passed"])
        self.assertTrue(result["write_performed"])
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["filled_quantity"], 0)
        self.assertEqual(intents[0]["notional"], 400)
        self.assertEqual(intents[0]["status"], "INTENT_RECORDED")

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

    def test_canonical_semantic_mutation_with_unchanged_provenance_fails_closed(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        events = atlas.read_jsonl_file(Path(first["ledger_path"]))
        state = atlas.read_json_file(Path(first["state_path"]))
        mutated = [dict(event) for event in events]
        mutated[0]["filled_price"] = float(mutated[0]["filled_price"]) + 1.0

        continuity = atlas.audit_ledger_continuity(mutated, state["accounts"])

        self.assertFalse(continuity["baseline_verified"])
        self.assertTrue(continuity["mutated_locators"])
        self.assertTrue(
            any(
                "previous canonical event mutated" in reason
                for reason in continuity["blocking_reasons"]
            )
        )

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

    def test_monotonic_anchor_rejects_an_old_signed_pointer_and_workspace_generation(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        anchor_path = Path(first["anchor_path"])
        old_anchor = anchor_path.read_bytes()
        old_ledger = atlas.VIRTUAL_LEDGER_PATH.read_bytes()
        old_state = atlas.VIRTUAL_LEDGER_STATE_PATH.read_bytes()

        trades_path = self.briefing / "data" / "paper_trades_us.jsonl"
        trades = [
            json.loads(line)
            for line in trades_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        trades.append(
            {
                "account": "US",
                "account_id": "global-briefing-us-paper-trading",
                "action": "BUY",
                "date": "2026-07-11",
                "exchange": "NASDAQ",
                "paper_trading_only": True,
                "price": 11,
                "quantity": 1,
                "gross_value": 11,
                "symbol": "TEST2",
                "timestamp": "2026-07-11T09:30:00",
            }
        )
        write_jsonl(trades_path, trades)
        portfolio_path = self.briefing / "data" / "paper_portfolio_us.json"
        portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
        portfolio["positions"]["NASDAQ:TEST2"] = {
            "exchange": "NASDAQ",
            "symbol": "TEST2",
            "quantity": 1,
        }
        write_json(portfolio_path, portfolio)
        second = atlas.build_virtual_execution_ledger(write_files=True)
        self.assertTrue(second["audit"]["overall_passed"])
        self.assertEqual(json.loads(anchor_path.read_text(encoding="utf-8"))["revision"], 2)

        anchor_path.write_bytes(old_anchor)
        atlas.VIRTUAL_LEDGER_PATH.write_bytes(old_ledger)
        atlas.VIRTUAL_LEDGER_STATE_PATH.write_bytes(old_state)
        failed = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertFalse(failed["write_performed"])
        self.assertTrue(
            any(
                "pointer was rolled back" in reason
                for reason in failed["audit"]["blocking_reasons"]
            )
        )

    def test_content_addressed_generation_recovers_after_projection_write_interruption(self) -> None:
        original_write = atlas.atomic_write_text
        interrupted = False

        def interrupt_projection(path: Path, text: str) -> None:
            nonlocal interrupted
            if path == atlas.VIRTUAL_LEDGER_PATH and not interrupted:
                interrupted = True
                raise OSError("simulated legacy projection interruption")
            original_write(path, text)

        with patch.object(atlas, "atomic_write_text", side_effect=interrupt_projection):
            with self.assertRaisesRegex(OSError, "projection interruption"):
                atlas.build_virtual_execution_ledger(write_files=True)

        head = json.loads(atlas.VIRTUAL_LEDGER_HEAD_PATH.read_text(encoding="utf-8"))
        generation_ledger = self.root / head["ledger_path"]
        self.assertTrue(generation_ledger.is_file())
        self.assertFalse(atlas.VIRTUAL_LEDGER_PATH.exists())

        recovered = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertTrue(recovered["write_performed"])
        self.assertEqual(atlas.VIRTUAL_LEDGER_PATH.read_bytes(), generation_ledger.read_bytes())
        anchor = json.loads(Path(recovered["anchor_path"]).read_text(encoding="utf-8"))
        self.assertEqual(anchor["revision"], 1)

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
        repaired = atlas.build_virtual_execution_ledger(write_files=True)

        self.assertTrue(repaired["audit"]["overall_passed"])
        self.assertEqual(ledger_path.read_bytes(), (self.root / json.loads(anchor_path.read_text(encoding="utf-8"))["generation_ledger_path"]).read_bytes())
        self.assertTrue(anchor_path.exists())

        generation_ledger = self.root / json.loads(anchor_path.read_text(encoding="utf-8"))["generation_ledger_path"]
        generation_ledger.unlink()
        missing_generation = atlas.build_virtual_execution_ledger(write_files=True)
        self.assertFalse(missing_generation["audit"]["overall_passed"])
        self.assertFalse(missing_generation["write_performed"])
        self.assertTrue(anchor_path.exists())

    def test_canonical_ledger_emptying_fails_closed_against_anchor_hash(self) -> None:
        first = atlas.build_virtual_execution_ledger(write_files=True)
        ledger_path = Path(first["ledger_path"])
        ledger_path.write_text("", encoding="utf-8")

        repaired = atlas.build_virtual_execution_ledger(write_files=True)
        self.assertTrue(repaired["audit"]["overall_passed"])
        anchor = json.loads(Path(first["anchor_path"]).read_text(encoding="utf-8"))
        generation_ledger = self.root / anchor["generation_ledger_path"]
        generation_ledger.write_text("", encoding="utf-8")
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
        self._bootstrap_empty_global_history_anchor()
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
        test_stage = next(
            stage
            for stage in payload["stages"]
            if stage["name"] == "targeted_integration_tests"
        )
        briefing_evidence = test_stage["detail"]["briefing_test_evidence"]
        self.assertEqual(briefing_evidence["returncode"], 0)
        self.assertTrue(briefing_evidence["passed"])
        self.assertEqual(briefing_evidence["suite"], "python_unittest_discovery")
        self.assertEqual(briefing_evidence["pattern"], "test_*.py")
        self.assertRegex(
            briefing_evidence["input"]["fingerprint_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertEqual(payload["audit_chain"]["entry_sha256"], atlas.audit_record_hash(payload))
        self.assertTrue(atlas.audit_cycle_history(Path(cycle_state["last_history_json"]).parent)["passed"])
        workspace_lock = json.loads((self.runtime / "workspace-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(len(workspace_lock["repositories"]), 3)
        self.assertFalse(workspace_lock["release_reproducible"])

    def test_briefing_test_input_fingerprint_tracks_code_but_not_mutable_ledgers(self) -> None:
        (self.root / "atlas.py").write_text("# atlas\n", encoding="utf-8")
        (self.briefing / "scripts").mkdir(parents=True, exist_ok=True)
        (self.briefing / "tests").mkdir(parents=True, exist_ok=True)
        (self.briefing / "config").mkdir(parents=True, exist_ok=True)
        (self.briefing / "scripts" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.briefing / "tests" / "test_contract.py").write_text("# test\n", encoding="utf-8")
        (self.briefing / "requirements.txt").write_text("requests==1\n", encoding="utf-8")
        generated = self.site / "app" / "briefing.generated.json"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("{}\n", encoding="utf-8")
        report = self.outputs / "每日全球晨间简报-2026-07-10.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# report\n", encoding="utf-8")

        before = atlas.build_briefing_test_input_fingerprint(
            self.root, "2026-07-10"
        )
        write_jsonl(self.briefing / "data" / "predictions.jsonl", [{"mutable": True}])
        after_ledger = atlas.build_briefing_test_input_fingerprint(
            self.root, "2026-07-10"
        )
        (self.briefing / "scripts" / "worker.py").write_text("VALUE = 2\n", encoding="utf-8")
        after_source = atlas.build_briefing_test_input_fingerprint(
            self.root, "2026-07-10"
        )

        self.assertEqual(before, after_ledger)
        self.assertNotEqual(
            before["fingerprint_sha256"], after_source["fingerprint_sha256"]
        )

    def test_release_evidence_requires_commit_bound_github_attestation(self) -> None:
        evidence_path = self.root / "atlas-release-evidence.json"
        commits = {
            "root": "1" * 40,
            "site": "2" * 40,
            "trading-core": "3" * 40,
        }
        write_json(
            evidence_path,
            {
                "schema_version": 1,
                "provider": "github-actions",
                "repository": "example/atlas",
                "workflow": ".github/workflows/quality.yml",
                "source_ref": "refs/heads/main",
                "run_id": "123",
                "run_attempt": "1",
                "test_profile": "full",
                "required_jobs": ["briefing-control-plane", "composed-workspace"],
                "repository_commits": commits,
            },
        )
        workspace_lock = {
            "repositories": [
                {"name": name, "commit": commit}
                for name, commit in commits.items()
            ]
        }
        verified_output = json.dumps(
            [{"attestation": {}, "verificationResult": {"statement": {"subject": []}}}]
        )
        completed = subprocess.CompletedProcess(
            ["gh", "attestation", "verify"],
            0,
            stdout=verified_output,
            stderr="",
        )

        with (
            patch.object(shutil, "which", return_value="gh"),
            patch.object(atlas, "capture_command", return_value=completed) as verifier,
        ):
            result = atlas.verify_release_evidence(
                evidence_path,
                workspace_lock=workspace_lock,
                expected_repository="example/atlas",
                expected_source_ref="refs/heads/main",
            )

        self.assertTrue(result["verified"])
        self.assertEqual(result["verification_count"], 1)
        command = verifier.call_args.args[0]
        self.assertIn("--deny-self-hosted-runners", command)
        self.assertEqual(command[command.index("--source-digest") + 1], commits["root"])
        self.assertEqual(
            command[command.index("--signer-workflow") + 1],
            "example/atlas/.github/workflows/quality.yml",
        )
        self.assertEqual(command[command.index("--signer-digest") + 1], commits["root"])

    def test_release_evidence_commit_mismatch_fails_before_network_verification(self) -> None:
        evidence_path = self.root / "atlas-release-evidence.json"
        write_json(
            evidence_path,
            {
                "schema_version": 1,
                "provider": "github-actions",
                "repository": "example/atlas",
                "workflow": ".github/workflows/quality.yml",
                "source_ref": "refs/heads/main",
                "run_id": "123",
                "run_attempt": "1",
                "test_profile": "full",
                "required_jobs": ["briefing-control-plane", "composed-workspace"],
                "repository_commits": {
                    "root": "f" * 40,
                    "site": "2" * 40,
                    "trading-core": "3" * 40,
                },
            },
        )
        workspace_lock = {
            "repositories": [
                {"name": "root", "commit": "1" * 40},
                {"name": "site", "commit": "2" * 40},
                {"name": "trading-core", "commit": "3" * 40},
            ]
        }

        with patch.object(atlas, "capture_command") as verifier:
            result = atlas.verify_release_evidence(
                evidence_path,
                workspace_lock=workspace_lock,
                expected_repository="example/atlas",
                expected_source_ref="refs/heads/main",
            )

        self.assertFalse(result["verified"])
        self.assertIn("repository commits do not match workspace lock", result["errors"])
        verifier.assert_not_called()

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

    def test_global_history_head_detects_cross_day_directory_rollback(self) -> None:
        first_history = self.runtime / "run_audits" / "history" / "2026-07-09"
        first = {
            "run_id": "RUN-1",
            "audit_chain": {"schema_version": 1, "sequence": 1, "previous_audit_sha256": None},
        }
        first["audit_chain"]["entry_sha256"] = atlas.audit_record_hash(first)
        write_json(first_history / "RUN-1.json", first)
        atlas.write_cycle_history_anchor(
            first_history,
            atlas.audit_cycle_history(first_history, allow_unanchored_genesis=True),
        )
        atlas.write_global_cycle_history_anchor(atlas.audit_global_cycle_history())

        second_history = self.runtime / "run_audits" / "history" / "2026-07-10"
        second = {
            "run_id": "RUN-2",
            "audit_chain": {"schema_version": 1, "sequence": 1, "previous_audit_sha256": None},
        }
        second["audit_chain"]["entry_sha256"] = atlas.audit_record_hash(second)
        write_json(second_history / "RUN-2.json", second)
        atlas.write_cycle_history_anchor(
            second_history,
            atlas.audit_cycle_history(second_history, allow_unanchored_genesis=True),
        )
        pending = atlas.audit_global_cycle_history(allow_append=True)
        self.assertTrue(pending["passed"])
        atlas.write_global_cycle_history_anchor(pending)

        shutil.rmtree(second_history)
        rolled_back = atlas.audit_global_cycle_history()

        self.assertFalse(rolled_back["passed"])
        self.assertIn("deleted, reordered, or mutated", "\n".join(rolled_back["errors"]))

    def test_global_history_recovery_is_dry_run_then_append_only_and_idempotent(self) -> None:
        anchor_path, source = self._seed_polluted_global_history_anchor()
        target_entries = self._seed_recovery_target_history()
        backup = self._recovery_backup_evidence(target_entries)
        old_pointer = anchor_path.read_bytes()
        old_version_path = (
            atlas.monotonic_anchor_versions_path(anchor_path)
            / f"{source['revision']:020d}-{source['anchor_sha256']}.json"
        )
        old_version = old_version_path.read_bytes()

        with patch.object(
            atlas,
            "verify_global_history_recovery_backup",
            return_value=backup,
        ):
            plan = atlas.build_global_history_recovery_plan(
                expected_head_sha256=source["anchor_sha256"],
                backup_latest_path=self.runtime / "backups" / "latest.json",
            )

        self.assertEqual(anchor_path.read_bytes(), old_pointer)
        evidence_path = atlas.global_history_recovery_evidence_path(
            plan["recovery_evidence_sha256"]
        )
        self.assertFalse(evidence_path.exists())
        self.assertEqual(plan["target_entry_count"], len(target_entries))

        with patch.object(
            atlas,
            "verify_global_history_recovery_backup",
            return_value=backup,
        ):
            applied = atlas.apply_global_history_recovery(
                expected_head_sha256=source["anchor_sha256"],
                expected_plan_sha256=plan["recovery_plan_sha256"],
                backup_latest_path=self.runtime / "backups" / "latest.json",
            )

        self.assertTrue(applied["applied"])
        self.assertEqual(applied["revision"], 2)
        self.assertTrue(evidence_path.is_file())
        self.assertEqual(old_version_path.read_bytes(), old_version)
        self.assertTrue(atlas.audit_global_cycle_history()["passed"])
        versions_before_retry = sorted(
            atlas.monotonic_anchor_versions_path(anchor_path).glob("*.json")
        )
        pointer_before_retry = anchor_path.read_bytes()

        retried = atlas.apply_global_history_recovery(
            expected_head_sha256=source["anchor_sha256"],
            expected_plan_sha256=plan["recovery_plan_sha256"],
            backup_latest_path=self.runtime / "backups" / "latest.json",
        )

        self.assertEqual(retried["status"], "already_applied")
        self.assertFalse(retried["applied"])
        self.assertEqual(anchor_path.read_bytes(), pointer_before_retry)
        self.assertEqual(
            sorted(atlas.monotonic_anchor_versions_path(anchor_path).glob("*.json")),
            versions_before_retry,
        )

    def test_global_history_recovery_wrong_plan_is_zero_write(self) -> None:
        anchor_path, source = self._seed_polluted_global_history_anchor()
        target_entries = self._seed_recovery_target_history()
        backup = self._recovery_backup_evidence(target_entries)
        pointer_before = anchor_path.read_bytes()
        versions_before = {
            path.name: path.read_bytes()
            for path in atlas.monotonic_anchor_versions_path(anchor_path).glob("*.json")
        }

        with (
            patch.object(
                atlas,
                "verify_global_history_recovery_backup",
                return_value=backup,
            ),
            self.assertRaisesRegex(ValueError, "plan changed"),
        ):
            atlas.apply_global_history_recovery(
                expected_head_sha256=source["anchor_sha256"],
                expected_plan_sha256="0" * 64,
                backup_latest_path=self.runtime / "backups" / "latest.json",
            )

        self.assertEqual(anchor_path.read_bytes(), pointer_before)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in atlas.monotonic_anchor_versions_path(anchor_path).glob("*.json")
            },
            versions_before,
        )
        recovery_root = self.trust_root / atlas.GLOBAL_HISTORY_RECOVERY_EVIDENCE_DIRECTORY
        self.assertFalse(recovery_root.exists())

    def test_global_history_recovery_rejects_mismatched_bindings(self) -> None:
        anchor_path, source = self._seed_polluted_global_history_anchor()
        target_entries = self._seed_recovery_target_history()
        backup = self._recovery_backup_evidence(target_entries)
        with patch.object(
            atlas,
            "verify_global_history_recovery_backup",
            return_value=backup,
        ):
            plan = atlas.build_global_history_recovery_plan(
                expected_head_sha256=source["anchor_sha256"],
                backup_latest_path=self.runtime / "backups" / "latest.json",
            )
        self.assertTrue(anchor_path.exists())
        atlas.write_immutable_json(
            atlas.global_history_recovery_evidence_path(
                plan["recovery_evidence_sha256"]
            ),
            plan["_evidence"],
        )
        cases = {
            "source_anchor_sha256": "source_anchor_sha256 mismatch",
            "source_workspace_uuid": "source_workspace_uuid mismatch",
            "target_entries_sha256": "target_entries_sha256 mismatch",
            "daily_anchor_evidence_sha256": "daily-anchor evidence hash mismatch",
            "backup_manifest_sha256": "backup_manifest_sha256 mismatch",
        }
        for field, expected_error in cases.items():
            with self.subTest(field=field):
                target = json.loads(json.dumps(plan["_target_payload"]))
                target["workspace_migration"][field] = "0" * 64
                errors = atlas.global_history_workspace_transition_errors(source, target)
                self.assertIn(expected_error, "\n".join(errors))

    def test_global_history_rejects_unbound_workspace_transition(self) -> None:
        anchor_path, source = self._seed_polluted_global_history_anchor()
        target_entries = self._seed_recovery_target_history()
        target = {
            "schema_version": atlas.GLOBAL_HISTORY_ANCHOR_SCHEMA_VERSION,
            "history_root": atlas.relative_path(self.runtime / "run_audits" / "history"),
            "entries": target_entries,
            "entry_count": len(target_entries),
            "workspace_uuid": atlas.workspace_project_uuid(),
            "revision": 2,
            "previous_head_sha256": source["anchor_sha256"],
            **atlas.trust_scope_payload(),
        }
        target["anchor_sha256"] = atlas.history_anchor_hash(target)
        target = atlas.sign_trust_anchor(target)
        atlas.write_monotonic_anchor(
            anchor_path,
            target,
            head_hash_field="anchor_sha256",
        )

        failed = atlas.audit_global_cycle_history()

        self.assertFalse(failed["passed"])
        self.assertIn(
            "workspace migration metadata is missing or malformed",
            "\n".join(failed["errors"]),
        )
        generic_errors = atlas.monotonic_anchor_errors(
            anchor_path,
            target,
            label="generic anchor",
            head_hash_field="anchor_sha256",
        )
        self.assertIn("generic anchor retained workspace UUID changed", generic_errors)

    def test_global_history_recovery_requires_exact_backup_manifest(self) -> None:
        _anchor_path, source = self._seed_polluted_global_history_anchor()
        target_entries = self._seed_recovery_target_history()
        backup = self._recovery_backup_evidence(target_entries[:-1])

        with (
            patch.object(
                atlas,
                "verify_global_history_recovery_backup",
                return_value=backup,
            ),
            self.assertRaisesRegex(ValueError, "does not exactly match"),
        ):
            atlas.build_global_history_recovery_plan(
                expected_head_sha256=source["anchor_sha256"],
                backup_latest_path=self.runtime / "backups" / "latest.json",
            )

    def test_cycle_refuses_to_auto_bootstrap_global_history_anchor(self) -> None:
        args = atlas.build_parser().parse_args(
            [
                "cycle",
                "--date",
                "2026-07-10",
                "--skip-site",
                "--skip-trading-core",
                "--skip-publication",
            ]
        )
        replay = {
            "overall_passed": True,
            "safety_gate_passed": True,
            "strategy_evidence_passed": False,
            "replay": {"passed": True, "strategy_evidence_passed": False},
            "shadow_promotion_gate": {
                "passed": True,
                "evidence_passed": False,
                "payload": {"auto_applied": False, "recommended_state": "shadow"},
            },
        }
        with (
            patch.object(
                atlas,
                "doctor_checks",
                return_value=[atlas.Check("Python", "ok", "3.12")],
            ),
            patch.object(atlas, "command_sync", return_value=0),
            patch.object(atlas, "run_replay_shadow_validation", return_value=replay),
            patch.object(atlas, "command_test", return_value=0),
            patch.object(
                atlas,
                "build_workspace_lock",
                return_value={
                    "content_sha256": "w" * 64,
                    "release_reproducible": False,
                    "repositories": [],
                },
            ),
            patch.object(atlas, "write_global_cycle_history_anchor") as writer,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(atlas.command_cycle(args), 1)

        writer.assert_not_called()
        self.assertFalse(atlas.global_cycle_history_anchor_path().exists())
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        self.assertEqual(list(history.glob("*.json")) if history.exists() else [], [])

    def test_history_nonfinite_json_fails_closed_without_raising(self) -> None:
        history = self.runtime / "run_audits" / "history" / "2026-07-10"
        history.mkdir(parents=True, exist_ok=True)
        (history / "BROKEN.json").write_text('{"value": NaN}\n', encoding="utf-8")

        result = atlas.audit_cycle_history(history)

        self.assertFalse(result["passed"])
        self.assertTrue(any("unreadable audit JSON" in error for error in result["errors"]))

    def test_history_anchor_write_failure_rolls_back_new_record_and_retry_succeeds(self) -> None:
        self._bootstrap_empty_global_history_anchor()
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

    def test_daily_sync_allows_an_explicitly_empty_macro_export(self) -> None:
        commands: list[list[str]] = []

        def record_command(command: list[str], **_: object) -> int:
            commands.append(command)
            return 0

        args = argparse.Namespace(date="2026-07-10", dry_run=False, force_site=False)
        with (
            patch.object(atlas, "command_quality", return_value=0),
            patch.object(atlas, "run_command", side_effect=record_command),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(atlas.command_sync(args), 0)

        exporter = next(
            command
            for command in commands
            if any(str(part).endswith("export_trading_core_signals.py") for part in command)
        )
        self.assertIn("--allow-empty", exporter)
        loader = next(command for command in commands if "load-macro" in command)
        self.assertIn("--allow-empty", loader)

    def test_sync_failure_writes_audit_but_never_writes_canonical_ledger(self) -> None:
        self._bootstrap_empty_global_history_anchor()
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
        self._bootstrap_empty_global_history_anchor()
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
        def provenance(name: str, path: Path, **_kwargs) -> dict:
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

    def test_explicit_workspace_root_is_absolute_and_structurally_validated(self) -> None:
        with patch.dict(os.environ, {atlas.ATLAS_WORKSPACE_ROOT_ENV: "relative/path"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "absolute path"):
                atlas.resolve_workspace_root()

        checkout = self.root / "checkout"
        (checkout / "work" / "global-briefing").mkdir(parents=True)
        (checkout / "work" / "trading-core").mkdir(parents=True)
        (checkout / "src").mkdir()
        (checkout / "atlas.py").write_text("# marker\n", encoding="utf-8")
        with patch.dict(
            os.environ,
            {atlas.ATLAS_WORKSPACE_ROOT_ENV: str(checkout.resolve())},
            clear=False,
        ):
            self.assertEqual(atlas.resolve_workspace_root(), checkout.resolve())

    def test_daily_repository_provenance_skips_remote_network_probe(self) -> None:
        repository = self.root / "daily-repository"
        repository.mkdir()
        git = atlas.shutil.which("git")
        self.assertIsNotNone(git)
        subprocess.run([str(git), "init"], cwd=repository, check=True, capture_output=True)
        subprocess.run(
            [str(git), "config", "user.name", "ATLAS Test"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            [str(git), "config", "user.email", "atlas@example.invalid"],
            cwd=repository,
            check=True,
        )
        (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run([str(git), "add", "tracked.txt"], cwd=repository, check=True)
        subprocess.run(
            [str(git), "commit", "-m", "initial"],
            cwd=repository,
            check=True,
            capture_output=True,
        )

        with patch.object(
            atlas,
            "probe_git_remotes",
            side_effect=AssertionError("daily operational provenance must not access remotes"),
        ):
            provenance = atlas.git_repository_provenance(
                "daily",
                repository,
                probe_remotes=False,
            )

        self.assertTrue(provenance["available"])
        self.assertEqual(provenance["remote_probe_status"], "not_requested_daily")
        self.assertFalse(provenance["remote_fetchable"])
        self.assertFalse(provenance["release_ready"])

    def test_repository_provenance_collection_is_concurrent_and_ordered(self) -> None:
        rendezvous = threading.Barrier(3)

        def inspect(name: str, path: Path, *, probe_remotes: bool) -> dict:
            self.assertFalse(probe_remotes)
            rendezvous.wait(timeout=2)
            return {"name": name, "path": str(path), "available": True}

        repositories = [("one", self.root), ("two", self.site), ("three", self.trading)]
        with patch.object(atlas, "git_repository_provenance", side_effect=inspect):
            collected = atlas.collect_repository_provenance(
                repositories,
                probe_remotes=False,
            )

        self.assertEqual([item["name"] for item in collected], ["one", "two", "three"])

    def test_cycle_idempotency_is_bound_to_workspace_commits(self) -> None:
        self._bootstrap_empty_global_history_anchor()
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

    def test_full_test_mode_runs_audited_trading_core_matrix(self) -> None:
        args = argparse.Namespace(skip_site=True, skip_trading_core=False, full=True)
        root_tests = self.root / "tests"
        briefing_tests = self.briefing / "tests"
        root_tests.mkdir(parents=True)
        briefing_tests.mkdir(parents=True)
        (root_tests / "test_contract.py").write_text("# contract\n", encoding="utf-8")
        (briefing_tests / "test_contract.py").write_text("# contract\n", encoding="utf-8")
        with patch.object(atlas, "run_command", return_value=0) as run:
            self.assertEqual(atlas.command_test(args), 0)

        core_call = next(call for call in run.call_args_list if call.kwargs.get("cwd") == self.trading)
        self.assertEqual(
            core_call.args[0],
            [
                atlas.sys.executable,
                "-m",
                "trading_core.testing.full_test_matrix",
            ],
        )
        self.assertEqual(core_call.kwargs["timeout"], 45 * 60)

    def test_full_test_mode_runs_complete_site_quality_and_security_policy(self) -> None:
        args = argparse.Namespace(skip_site=False, skip_trading_core=True, full=True)

        site_gates = [gate for gate in atlas.build_test_plan(args) if gate.cwd == self.site]

        self.assertEqual(
            [gate.command[1:] for gate in site_gates],
            [("run", "quality"), ("run", "audit:policy")],
        )

    def test_missing_required_test_sources_fail_before_subprocess_execution(self) -> None:
        args = argparse.Namespace(skip_site=True, skip_trading_core=True, full=False)
        with (
            patch.object(
                atlas,
                "capture_command",
                side_effect=AssertionError("missing test sources must fail before execution"),
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(atlas.command_test(args), 1)

        self.assertIn("test directory is missing", stderr.getvalue())

    def test_daily_test_gates_run_concurrently_and_collect_all_failures(self) -> None:
        rendezvous = threading.Barrier(2)
        seen: list[tuple[str, ...]] = []
        seen_lock = threading.Lock()

        def capture(command, **_kwargs):
            normalized = tuple(str(part) for part in command)
            with seen_lock:
                seen.append(normalized)
            rendezvous.wait(timeout=2)
            failed = normalized[0] == "briefing-gate"
            return subprocess.CompletedProcess(
                list(command),
                1 if failed else 0,
                stdout="",
                stderr="expected failure\n" if failed else "",
            )

        with (
            patch.object(atlas, "capture_command", side_effect=capture),
            patch.object(
                atlas,
                "run_command",
                side_effect=AssertionError("daily test gates must use the concurrent runner"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                atlas.run_daily_test_plan(
                    [
                        atlas.TestGate("root", ("root-gate",)),
                        atlas.TestGate("briefing", ("briefing-gate",)),
                    ]
                ),
                1,
            )

        self.assertEqual(len(seen), 2)
        self.assertIn(("root-gate",), seen)
        self.assertIn(("briefing-gate",), seen)

    def test_daily_test_logs_tolerate_console_encoding_limits(self) -> None:
        stdout_bytes = io.BytesIO()
        stderr_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="ascii", errors="strict")
        stderr = io.TextIOWrapper(stderr_bytes, encoding="ascii", errors="strict")
        result = subprocess.CompletedProcess(
            ["unicode-gate"],
            0,
            stdout="quality \u2713\n",
            stderr="",
        )

        with (
            patch.object(atlas, "capture_command", return_value=result),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(
                atlas.run_daily_test_plan([atlas.TestGate("unicode", ("unicode-gate",))]),
                0,
            )
        stdout.flush()

        self.assertIn(b"quality ?", stdout_bytes.getvalue())


if __name__ == "__main__":
    unittest.main()
