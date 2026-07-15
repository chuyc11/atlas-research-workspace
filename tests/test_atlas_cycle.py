from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
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
        self.tmp.cleanup()

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
            self.briefing / "data" / "temp_orders_2026-07-10.json",
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
            {"account_id": "global-briefing-us-paper-trading", "mode": "paper_trading", "cash": 99980, "initial_cash": 100000, "positions": {}},
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
            {"replay_id": "GB-HIST-REPLAY-20260710-20260710", "cash": 999599.8, "equity": 1000000, "positions": [], "isolated": True},
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
        self.assertTrue(payload["boundary"]["single_virtual_execution_ledger"])
        self.assertTrue(payload["boundary"]["targeted_integration_test_gate"])
        self.assertFalse(payload["boundary"]["full_test_suite_executed"])
        self.assertEqual(payload["ledger"]["event_count"], 3)
        cycle_state = json.loads((self.runtime / "cycle_state.json").read_text(encoding="utf-8"))
        self.assertTrue(Path(cycle_state["last_history_json"]).exists())

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

    def test_full_test_mode_runs_unfiltered_trading_core_suite(self) -> None:
        args = argparse.Namespace(skip_site=True, skip_trading_core=False, full=True)
        with patch.object(atlas, "run_command", return_value=0) as run:
            self.assertEqual(atlas.command_test(args), 0)

        core_call = next(call for call in run.call_args_list if call.kwargs.get("cwd") == self.trading)
        self.assertEqual(core_call.args[0], [atlas.sys.executable, "-m", "pytest"])


if __name__ == "__main__":
    unittest.main()
