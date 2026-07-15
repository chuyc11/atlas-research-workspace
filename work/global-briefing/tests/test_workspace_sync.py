from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ATLAS = load_module("atlas_workspace_sync_test_module", ROOT / "atlas.py")
SITE_SYNC = load_module(
    "site_sync_test_module",
    ROOT / "work" / "global-briefing" / "scripts" / "sync_briefing_site.py",
)


def requires_runtime_report(report_date: str):
    report = ROOT / "outputs" / f"每日全球晨间简报-{report_date}.md"
    return unittest.skipUnless(
        report.is_file(),
        f"requires local runtime briefing artifact for {report_date}",
    )


class WorkspaceSyncTests(unittest.TestCase):
    def test_date_validation_rejects_nonexistent_calendar_date(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            ATLAS.valid_iso_date("2026-99-99")

    def test_version_parser_requires_a_version_only_response(self) -> None:
        self.assertEqual(ATLAS.parse_version("v22.13.0"), (22, 13, 0))
        self.assertEqual(ATLAS.parse_version("error while starting v22.13.0"), ())

    def test_v2_numeric_probability_and_trigger_are_site_compatible(self) -> None:
        self.assertEqual(SITE_SYNC.probability_label(0.72), "高")
        self.assertEqual(SITE_SYNC.probability_label(0.62), "中")
        self.assertEqual(SITE_SYNC.probability_label(0.35), "低")
        self.assertEqual(
            SITE_SYNC.prediction_drivers({"trigger": "official release"}),
            ["official release"],
        )

    def test_v2_matching_requires_shared_evidence_url(self) -> None:
        predictions = [
            {
                "schema_version": 2,
                "prediction_id": "2026-07-12-P03",
                "scenario": "古巴电网再次停电",
                "evidence": [{"source": "AP", "url": "https://example.com/cuba"}],
            },
            {
                "schema_version": 2,
                "prediction_id": "2026-07-12-P04",
                "scenario": "俄乌导弹与无人机打击",
                "evidence": [{"source": "Official", "url": "https://example.com/ukraine"}],
            },
        ]

        matched = SITE_SYNC.matching_predictions(
            {"category": "安全", "sources": [{"label": "Official", "href": "https://example.com/ukraine"}]},
            predictions,
        )

        self.assertEqual([item["prediction_id"] for item in matched], ["2026-07-12-P04"])

    def test_v2_matching_does_not_guess_from_category_keywords(self) -> None:
        predictions = [
            {
                "schema_version": 2,
                "prediction_id": "2026-07-12-P05",
                "scenario": "中国半导体与AI资产映射",
                "evidence": [{"source": "NBS", "url": "https://example.com/china"}],
            }
        ]

        matched = SITE_SYNC.matching_predictions(
            {"category": "科技", "sources": [{"label": "AP", "href": "https://example.com/apple"}]},
            predictions,
        )

        self.assertEqual(matched, [])

    def test_dry_run_checks_every_sync_stage_for_the_same_date(self) -> None:
        args = SimpleNamespace(date="2026-07-10", dry_run=True, force_site=False)
        with patch.object(ATLAS, "run_command", return_value=0) as run:
            self.assertEqual(ATLAS.command_sync(args), 0)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 6)
        self.assertTrue(all("2026-07-10" in command for command in commands))
        self.assertIn("--dry-run", commands[0])
        self.assertIn("--dry-run", commands[1])
        self.assertIn("--dry-run", commands[3])
        self.assertIn("--dry-run", commands[4])
        self.assertIn("--dry-run", commands[5])

    @requires_runtime_report("2026-07-10")
    def test_current_report_builds_quality_checked_payload(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-10")
        raw = report_path.read_bytes()
        payload = SITE_SYNC.build_payload(raw.decode("utf-8-sig"), report_date, 1, "test-sha")

        self.assertEqual(SITE_SYNC.validate_payload(payload), [])
        self.assertTrue(all(len(event["body"]) >= 12 for event in payload["events"]))
        self.assertEqual(len(payload["events"]), 7)
        self.assertEqual(len(payload["scenarios"]), 6)
        self.assertTrue(all(event["facts"] for event in payload["events"]))
        self.assertTrue(all(event["drivers"] for event in payload["events"]))
        self.assertTrue(all(event["verificationSignals"] for event in payload["events"]))
        self.assertTrue(all(event["sources"] for event in payload["events"]))
        self.assertTrue(all(scenario["sourceRefs"] for scenario in payload["scenarios"]))
        self.assertEqual(payload["evolution"]["mode"], "gated_self_evolution")
        self.assertFalse(payload["evolution"]["promotion_allowed"])
        self.assertTrue(payload["reportQuality"]["passed"])
        self.assertEqual(payload["reportQuality"]["fillerCount"], 0)
        self.assertTrue(payload["reportQuality"]["deepResearchMode"])
        self.assertGreaterEqual(payload["reportQuality"]["characterCount"], payload["reportQuality"]["minimumCharacters"])
        self.assertLessEqual(payload["reportQuality"]["characterCount"], payload["reportQuality"]["maximumCharacters"])
        self.assertEqual(payload["reportQuality"]["primaryThesisCount"], 5)
        self.assertEqual(payload["reportQuality"]["topicSectionCount"], 6)
        self.assertGreaterEqual(payload["reportQuality"]["sourceDomainCount"], payload["reportQuality"]["minimumDistinctDomains"])
        self.assertEqual(payload["metrics"]["sourceHealth"]["method"], "artifact_backed_source_health_v2")
        self.assertFalse(payload["metrics"]["riskModel"]["calibrated"])
        self.assertEqual(len({event["id"] for event in payload["events"]}), len(payload["events"]))
        self.assertGreater(len({event["implication"] for event in payload["events"]}), 1)

    def test_source_health_penalizes_stale_and_unknown_rss_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "rss-items-2026-07-14.json").write_text(
                json.dumps({
                    "generated_at": "2026-07-14T23:59:59+08:00",
                    "items": [
                        {"source": "A", "published": "Tue, 14 Jul 2026 04:00:00 GMT"},
                        {"source": "A", "published": "Fri, 10 Jul 2026 04:00:00 GMT"},
                        {"source": "A", "published": "unknown"},
                    ],
                    "errors": [],
                    "fallbacks": [],
                }),
                encoding="utf-8",
            )
            (data_dir / "china-market-snapshot-2026-07-14.json").write_text("{}", encoding="utf-8")
            (data_dir / "market-snapshot-2026-07-14.json").write_text("{}", encoding="utf-8")
            sources = data_dir / "sources.json"
            sources.write_text(json.dumps({"sources": [{"name": "A", "rss": "https://a.example/rss"}]}), encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "DATA_DIR", data_dir),
                patch.object(SITE_SYNC, "SOURCES_CONFIG_PATH", sources),
            ):
                health = SITE_SYNC.source_health("2026-07-14")

        self.assertLess(health["score"], 85)
        self.assertEqual(health["rssStaleOrUnknownPct"], 66.67)
        self.assertTrue(any("时间戳" in item for item in health["limitations"]))

    @requires_runtime_report("2026-07-14")
    def test_current_report_exposes_nonempty_observation_table(self) -> None:
        report_path, _report_date = SITE_SYNC.report_for_date("2026-07-14")
        audit = SITE_SYNC.report_quality_audit(report_path.read_text(encoding="utf-8"))

        self.assertGreaterEqual(audit["observationCount"], 1)

    @requires_runtime_report("2026-07-12")
    def test_v2_report_builds_with_numeric_probabilities_and_complete_events(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-12")
        payload = SITE_SYNC.build_payload(report_path.read_text(encoding="utf-8"), report_date, 1, "v2-sha")

        self.assertEqual(SITE_SYNC.validate_payload(payload), [])
        self.assertTrue(all(event["drivers"] for event in payload["events"]))
        self.assertTrue(all(event["sources"] for event in payload["events"]))
        self.assertEqual(len(payload["scenarios"]), 5)
        self.assertIn("高", {scenario["chance"] for scenario in payload["scenarios"]})
        self.assertLessEqual(sum(len(line) for line in payload["hero"]["headline"]), 34)
        events = {event["category"]: event for event in payload["events"]}
        self.assertEqual(events["地缘"]["predictionId"], "2026-07-12-P02")
        self.assertEqual(events["安全"]["predictionId"], "2026-07-12-P04")
        self.assertEqual(events["气候"]["predictionId"], "2026-07-12-P01")
        self.assertEqual(events["宏观"]["predictionId"], "2026-07-12-P03")
        self.assertEqual(events["中国"]["predictionId"], "2026-07-12-P05")
        self.assertEqual(events["科技"]["predictionId"], "")
        self.assertNotIn("510300.SH", events["科技"]["analysis"])
        scenarios = {scenario["id"]: scenario for scenario in payload["scenarios"]}
        self.assertEqual(
            {source["href"] for source in scenarios["2026-07-12-P01"]["sourceRefs"]},
            {
                "https://www.weather.gov.hk/textonly/v2/tc/tcp.htm",
                "https://apnews.com/article/bfdfdbb239f38b6c22a54c8349ce8d28",
            },
        )

    def test_system_status_uses_the_date_aligned_cycle_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit_path = Path(temporary) / "atlas-cycle-2026-07-01.json"
            audit_path.write_text(
                json.dumps(
                    {
                        "date": "2026-07-01",
                        "overall_passed": True,
                        "ledger": {"event_count": 77, "account_count": 3, "content_hash": "dated"},
                        "ledger_audit": {"overall_passed": True, "account_count": 3, "source_counts": {"dated": 77}},
                        "stages": [],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(SITE_SYNC, "cycle_audit_path", return_value=audit_path):
                status = SITE_SYNC.build_system_status("2026-07-01")

        self.assertEqual(status["asOf"], "2026-07-01")
        self.assertEqual(status["ledger"]["eventCount"], 77)
        self.assertEqual(status["ledger"]["accountCount"], 3)
        self.assertEqual(status["ledger"]["contentHash"], "dated")
        self.assertNotIn("runId", status)
        self.assertNotIn("finishedAt", status)
        self.assertNotIn("idempotentReplay", status)
        self.assertEqual(status["selfHealing"]["status"], "not_run")
        self.assertEqual(status["improvements"]["status"], "not_run")

    def test_system_status_never_leaks_future_self_healing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            latest = Path(temporary) / "latest.json"
            latest.write_text(
                json.dumps(
                    {
                        "date": "2026-07-12",
                        "overall_status": "healthy",
                        "counts": {"checks": 10, "passed": 10},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(SITE_SYNC, "ATLAS_SELF_HEALING_LATEST", latest):
                historical = SITE_SYNC.build_system_status("2026-07-10")
                current = SITE_SYNC.build_system_status("2026-07-12")

        self.assertEqual(historical["selfHealing"]["status"], "not_run")
        self.assertEqual(current["selfHealing"]["status"], "healthy")
        self.assertEqual(current["selfHealing"]["passed"], 10)

    @requires_runtime_report("2026-07-12")
    def test_self_healing_telemetry_does_not_change_site_content_identity(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-12")
        baseline = SITE_SYNC.site_input_hash(report_path.read_bytes(), report_date)
        with tempfile.TemporaryDirectory() as temporary:
            latest = Path(temporary) / "latest.json"
            latest.write_text(
                json.dumps({"date": report_date, "overall_status": "blocked", "counts": {"blocking": 3}}),
                encoding="utf-8",
            )
            with patch.object(SITE_SYNC, "ATLAS_SELF_HEALING_LATEST", latest):
                changed_telemetry = SITE_SYNC.site_input_hash(report_path.read_bytes(), report_date)

        self.assertEqual(changed_telemetry, baseline)

    @requires_runtime_report("2026-07-12")
    def test_cycle_telemetry_is_not_part_of_editorial_content_identity(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-12")
        with patch.object(SITE_SYNC, "build_system_status", side_effect=AssertionError("must not be called")):
            content_hash = SITE_SYNC.site_input_hash(report_path.read_bytes(), report_date)

        self.assertRegex(content_hash, r"^[0-9a-f]{64}$")

    @requires_runtime_report("2026-07-12")
    def test_improvement_telemetry_is_date_aligned_and_not_content_identity(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-12")
        baseline = SITE_SYNC.site_input_hash(report_path.read_bytes(), report_date)
        with tempfile.TemporaryDirectory() as temporary:
            latest = Path(temporary) / "latest.json"
            latest.write_text(
                json.dumps({"date": report_date, "status": "degraded", "counts": {"total": 13, "verified": 1, "regressed": 1}}),
                encoding="utf-8",
            )
            with patch.object(SITE_SYNC, "ATLAS_IMPROVEMENTS_LATEST", latest):
                status = SITE_SYNC.build_system_status(report_date)
                changed_telemetry = SITE_SYNC.site_input_hash(report_path.read_bytes(), report_date)
                historical = SITE_SYNC.build_system_status("2026-07-10")

        self.assertEqual(changed_telemetry, baseline)
        self.assertEqual(status["improvements"]["total"], 13)
        self.assertEqual(status["improvements"]["regressed"], 1)
        self.assertEqual(historical["improvements"]["status"], "not_run")

    def test_mark_deployed_clears_matching_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "site-sync-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "pending_sha": "abc",
                        "pending_report": "report.md",
                        "pending_date": "2026-07-12",
                        "last_deployed_sha": "older",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(sys, "argv", ["sync_briefing_site.py", "--mark-deployed", "abc", "--deployment-url", "https://example.com"]),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(SITE_SYNC.main(), 0)

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(state["last_deployed_sha"], "abc")
        self.assertTrue(state["last_deployed_payload_sha"])
        self.assertNotIn("pending_sha", state)
        self.assertNotIn("pending_report", state)
        self.assertNotIn("pending_date", state)

    @requires_runtime_report("2026-07-12")
    def test_payload_identity_detects_telemetry_changes_without_changing_content_identity(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-12")
        content_hash = SITE_SYNC.site_input_hash(report_path.read_bytes(), report_date)
        baseline = SITE_SYNC.build_payload(report_path.read_text(encoding="utf-8"), report_date, 1, content_hash)
        with tempfile.TemporaryDirectory() as temporary:
            latest = Path(temporary) / "latest.json"
            latest.write_text(
                json.dumps({"date": report_date, "status": "blocked", "counts": {"total": 999, "verified": 0, "blocking": 9}}),
                encoding="utf-8",
            )
            with patch.object(SITE_SYNC, "ATLAS_IMPROVEMENTS_LATEST", latest):
                changed = SITE_SYNC.build_payload(report_path.read_text(encoding="utf-8"), report_date, 1, content_hash)

        self.assertEqual(baseline["contentHash"], changed["contentHash"])
        self.assertNotEqual(SITE_SYNC.payload_sha256(baseline), SITE_SYNC.payload_sha256(changed))

    def test_publication_snapshot_requires_all_date_aligned_closed_loop_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cycle = root / "cycle.json"
            healing = root / "healing.json"
            improvements = root / "improvements.json"
            alerts = root / "alerts.json"
            backup = root / "backup.json"
            cycle.write_text(json.dumps({"date": "2026-07-13", "overall_passed": True}), encoding="utf-8")
            healing.write_text(json.dumps({"date": "2026-07-13", "overall_status": "healthy", "counts": {"blocking": 0}}), encoding="utf-8")
            improvements.write_text(json.dumps({"date": "2026-07-13", "status": "degraded", "counts": {"blocking": 0}}), encoding="utf-8")
            alerts.write_text(json.dumps({"date": "2026-07-13", "status": "healthy", "finding_count": 0}), encoding="utf-8")
            backup.write_text(json.dumps({"date": "2026-07-13", "verified": True, "restore_verified": True, "target_outside_workspace": True}), encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "cycle_audit_path", return_value=cycle),
                patch.object(SITE_SYNC, "ATLAS_SELF_HEALING_LATEST", healing),
                patch.object(SITE_SYNC, "ATLAS_IMPROVEMENTS_LATEST", improvements),
                patch.object(SITE_SYNC, "ATLAS_ALERTS_LATEST", alerts),
                patch.object(SITE_SYNC, "ATLAS_BACKUPS_LATEST", backup),
            ):
                ready = SITE_SYNC.publication_snapshot_readiness("2026-07-13")
                backup.write_text(json.dumps({"date": "2026-07-12", "verified": True, "restore_verified": True, "target_outside_workspace": True}), encoding="utf-8")
                blocked = SITE_SYNC.publication_snapshot_readiness("2026-07-13")

        self.assertTrue(ready["ready"])
        self.assertFalse(blocked["ready"])
        self.assertTrue(any("backup" in reason for reason in blocked["reasons"]))

    @requires_runtime_report("2026-07-12")
    def test_frozen_publication_snapshot_is_retry_stable_and_rejects_silent_report_drift(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-12")
        raw = report_path.read_bytes()
        content_hash = SITE_SYNC.site_input_hash(raw, report_date)
        payload = SITE_SYNC.build_payload(raw.decode("utf-8"), report_date, 1, content_hash)
        readiness = {"ready": True, "reasons": [], "evidence": {"cycle": {"overallPassed": True}}}
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_root = Path(temporary) / "snapshots"
            with (
                patch.object(SITE_SYNC, "PUBLICATION_SNAPSHOT_ROOT", snapshot_root),
                patch.object(SITE_SYNC, "publication_snapshot_readiness", return_value=readiness),
            ):
                first = SITE_SYNC.freeze_publication_snapshot(
                    report_date=report_date,
                    raw_report=raw,
                    content_hash=content_hash,
                    payload=payload,
                )
                loaded = SITE_SYNC.load_publication_snapshot(report_date, raw, content_hash)
                with self.assertRaisesRegex(ValueError, "report changed"):
                    SITE_SYNC.load_publication_snapshot(report_date, raw + b"\nchanged", content_hash)
                second = SITE_SYNC.freeze_publication_snapshot(
                    report_date=report_date,
                    raw_report=raw,
                    content_hash=content_hash,
                    payload=payload,
                    refresh=True,
                )

            history_exists = (snapshot_root / "history" / report_date / "revision-1.json").exists()

        self.assertEqual(first["revision"], 1)
        self.assertEqual(loaded["payload_sha256"], first["payload_sha256"])
        self.assertEqual(second["revision"], 2)
        self.assertTrue(history_exists)

    def test_snapshot_selection_never_uses_a_future_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "market-snapshot-2026-07-09.json").write_text("{}", encoding="utf-8")
            (data_dir / "market-snapshot-2026-07-11.json").write_text("{}", encoding="utf-8")
            with patch.object(SITE_SYNC, "DATA_DIR", data_dir):
                selected = SITE_SYNC.snapshot_path("market-snapshot", "2026-07-10")

        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, "market-snapshot-2026-07-09.json")

    def test_evolution_selection_never_uses_future_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "evolution-state-2026-07-09.json").write_text(json.dumps({"as_of": "2026-07-09"}), encoding="utf-8")
            (data_dir / "evolution-state-2026-07-11.json").write_text(json.dumps({"as_of": "2026-07-11"}), encoding="utf-8")
            with patch.object(SITE_SYNC, "DATA_DIR", data_dir):
                selected = SITE_SYNC.evolution_for_date("2026-07-10")

        self.assertEqual(selected["as_of"], "2026-07-09")

    def test_historical_site_portfolio_is_reconstructed_without_future_positions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            config_path = root / "paper_trading.json"
            config_path.write_text(
                json.dumps(
                    {
                        "accounts": {
                            "US": {
                                "account_id": "us-test",
                                "initial_cash": 100000,
                                "base_currency": "USD",
                                "trades_file": "data/us.jsonl",
                            },
                            "CHINA": {
                                "account_id": "china-test",
                                "initial_cash": 100000,
                                "base_currency": "CNY",
                                "trades_file": "data/china.jsonl",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            future_state = {
                "mode": "paper_trading",
                "as_of_date": "2026-07-15",
                "cash": 90000,
                "positions": {"future": {"symbol": "CIBR", "quantity": 1}},
            }
            (data_dir / "paper_portfolio_us.json").write_text(json.dumps(future_state), encoding="utf-8")
            (data_dir / "paper_portfolio_china.json").write_text(json.dumps(future_state), encoding="utf-8")
            (data_dir / "us.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "date": "2026-06-10",
                                "action": "BUY",
                                "symbol": "AAPL",
                                "exchange": "NASDAQ",
                                "currency": "USD",
                                "quantity": 1,
                                "price": 100,
                            }
                        ),
                        json.dumps(
                            {
                                "date": "2026-07-01",
                                "action": "BUY",
                                "symbol": "CIBR",
                                "exchange": "NYSE",
                                "currency": "USD",
                                "quantity": 1,
                                "price": 50,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (data_dir / "china.jsonl").write_text(
                json.dumps(
                    {
                        "date": "2026-06-10",
                        "action": "BUY",
                        "symbol": "510300.SH",
                        "exchange": "SSE",
                        "currency": "CNY",
                        "quantity": 100,
                        "price": 4,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(SITE_SYNC, "ROOT", root),
                patch.object(SITE_SYNC, "DATA_DIR", data_dir),
                patch.object(SITE_SYNC, "PAPER_CONFIG_PATH", config_path),
            ):
                us = SITE_SYNC.portfolio("US", "2026-06-14")
                china = SITE_SYNC.portfolio("CHINA", "2026-06-14")

        self.assertNotIn("CIBR", {item["symbol"] for item in us["positions"]})
        self.assertEqual(china["baseCurrency"], "CNY")
        self.assertTrue(any("逐笔重建" in item for item in us["limitations"]))
