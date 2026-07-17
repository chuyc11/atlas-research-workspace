from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
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

TEST_REPOSITORY_COMMITS = {
    "root": "1" * 40,
    "site": "2" * 40,
    "trading-core": "3" * 40,
}


def candidate_fingerprint(
    report_date: str,
    raw_report: bytes,
    content_hash: str,
    payload: dict,
) -> dict:
    return SITE_SYNC.build_candidate_fingerprint(
        report_date=report_date,
        raw_report=raw_report,
        content_hash=content_hash,
        payload=payload,
        repository_commits=TEST_REPOSITORY_COMMITS,
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

    def test_site_dependency_preflight_rejects_nonofficial_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lockfile = Path(temporary) / "package-lock.json"
            manifest = Path(temporary) / "package.json"
            lockfile.write_text(
                json.dumps({
                    "lockfileVersion": 3,
                    "packages": {
                        "": {},
                        "node_modules/example": {
                            "resolved": "https://registry.npmmirror.com/example/-/example-1.0.0.tgz",
                            "integrity": "sha512-test",
                        },
                    },
                }),
                encoding="utf-8",
            )
            manifest.write_text(json.dumps({"packageManager": "npm@10.9.2"}), encoding="utf-8")

            errors = SITE_SYNC.site_dependency_source_errors(lockfile)

        self.assertTrue(any("unsupported dependency host" in error for error in errors))

    def test_site_dependency_preflight_accepts_official_integrity_pinned_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lockfile = Path(temporary) / "package-lock.json"
            manifest = Path(temporary) / "package.json"
            lockfile.write_text(
                json.dumps({
                    "lockfileVersion": 3,
                    "packages": {
                        "": {},
                        "node_modules/example": {
                            "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
                            "integrity": "sha512-test",
                        },
                    },
                }),
                encoding="utf-8",
            )
            manifest.write_text(json.dumps({"packageManager": "npm@10.9.2"}), encoding="utf-8")

            errors = SITE_SYNC.site_dependency_source_errors(lockfile)

        self.assertEqual(errors, [])

    def test_site_dependency_preflight_only_allows_documented_resolved_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lockfile = Path(temporary) / "package-lock.json"
            manifest = Path(temporary) / "package.json"
            manifest.write_text(json.dumps({"packageManager": "npm@10.9.2"}), encoding="utf-8")
            lockfile.write_text(
                json.dumps({
                    "lockfileVersion": 3,
                    "packages": {
                        "": {},
                        "node_modules/missing": {"version": "1.0.0"},
                        "node_modules/link": {"link": True},
                        "node_modules/bundled": {"inBundle": True},
                    },
                }),
                encoding="utf-8",
            )

            errors = SITE_SYNC.site_dependency_source_errors(lockfile, manifest)

        self.assertEqual(errors, ["node_modules/missing is missing a resolved dependency source"])

    def test_site_dependency_preflight_rejects_unpinned_npm_major(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lockfile = Path(temporary) / "package-lock.json"
            manifest = Path(temporary) / "package.json"
            lockfile.write_text(
                json.dumps({"lockfileVersion": 3, "packages": {"": {}}}),
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps({"packageManager": "npm@11.6.2"}),
                encoding="utf-8",
            )

            errors = SITE_SYNC.site_dependency_source_errors(lockfile, manifest)

        self.assertTrue(any("npm@10.9.2" in error for error in errors))

    def test_report_parser_accepts_canonical_long_form_topic_headings(self) -> None:
        sections = SITE_SYNC.split_sections(
            "## 科技与人工智能\n内容\n"
            "## 经济、宏观与能源\n内容\n"
        )

        self.assertEqual(
            SITE_SYNC.find_section(sections, "科技与AI", "科技和AI", "科技与人工智能"),
            ["内容"],
        )
        self.assertEqual(
            SITE_SYNC.find_section(
                sections,
                "经济与能源",
                "经济和能源",
                "经济、宏观与能源",
                "经济宏观与能源",
            ),
            ["内容"],
        )

    def test_editorial_scaffolding_is_removed_from_public_copy(self) -> None:
        cleaned = SITE_SYNC.sanitize_editorial_text(
            "结论：确认事实不等于分析判断｜｜后续验证",
            120,
        )

        self.assertEqual(cleaned, "确认事实不等于分析判断；后续验证")
        self.assertNotIn("｜｜", cleaned)

    def test_core_summary_table_produces_distinct_public_hero_copy(self) -> None:
        sections = {
            "一、核心摘要": [
                "|决策主线|确认事实|分析判断|下一验证|",
                "|---|---|---|---|",
                "|霍尔木兹风险重新压过外交窗口|美国恢复港口封锁|航运风险上升|协议文本、船流、战争险|",
            ]
        }
        events = [
            {
                "title": "备用标题",
                "body": "备用正文",
                "implication": "备用验证",
            }
        ]

        headline, dek, editor_note = SITE_SYNC.public_hero_copy(sections, events)

        self.assertEqual(headline, "霍尔木兹风险重新压过外交窗口")
        self.assertEqual(dek, "美国恢复港口封锁；航运风险上升")
        self.assertEqual(editor_note, "接下来验证协议文本、船流、战争险。")
        self.assertEqual(len({headline, dek, editor_note}), 3)

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
        self.assertEqual(payload["metrics"]["sourceHealth"]["method"], "artifact_backed_source_health_v3")
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

    def test_source_health_treats_24_to_72_hour_items_as_declared_background(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "rss-items-2026-07-14.json").write_text(
                json.dumps({
                    "generated_at": "2026-07-14T23:59:59+08:00",
                    "items": [
                        {"source": "A", "published": "Tue, 14 Jul 2026 04:00:00 GMT"},
                        {"source": "A", "published": "Sun, 12 Jul 2026 04:00:00 GMT"},
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
            settings = data_dir / "settings.json"
            sources.write_text(json.dumps({"sources": [{"name": "A", "rss": "https://a.example/rss"}]}), encoding="utf-8")
            settings.write_text(
                json.dumps({"news_research_policy": {"collection_lookback_hours": 72}}),
                encoding="utf-8",
            )
            with (
                patch.object(SITE_SYNC, "DATA_DIR", data_dir),
                patch.object(SITE_SYNC, "SOURCES_CONFIG_PATH", sources),
                patch.object(SITE_SYNC, "SETTINGS_CONFIG_PATH", settings),
            ):
                health = SITE_SYNC.source_health("2026-07-14")

        self.assertEqual(health["rssBackgroundCount"], 1)
        self.assertEqual(health["rssStaleCount"], 0)
        self.assertEqual(health["rssStaleOrUnknownPct"], 33.33)

    def test_source_health_infers_legacy_china_price_date_only_after_local_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "rss-items-2026-07-14.json").write_text(
                json.dumps({"generated_at": "2026-07-14T23:59:59+08:00", "items": [], "errors": [], "fallbacks": []}),
                encoding="utf-8",
            )
            (data_dir / "china-market-snapshot-2026-07-14.json").write_text(
                json.dumps({
                    "generated_at": "2026-07-14T08:05:00+00:00",
                    "items": [
                        {"symbol": "510300.SH", "price": 4.8, "fetched_at": "2026-07-14T08:05:00+00:00"},
                        {"symbol": "159915.SZ", "price": 3.8, "fetched_at": "2026-07-14T06:00:00+00:00"},
                    ],
                    "errors": [],
                }),
                encoding="utf-8",
            )
            (data_dir / "market-snapshot-2026-07-14.json").write_text("{}", encoding="utf-8")
            sources = data_dir / "sources.json"
            settings = data_dir / "settings.json"
            sources.write_text(json.dumps({"sources": []}), encoding="utf-8")
            settings.write_text(json.dumps({"news_research_policy": {"collection_lookback_hours": 72}}), encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "DATA_DIR", data_dir),
                patch.object(SITE_SYNC, "SOURCES_CONFIG_PATH", sources),
                patch.object(SITE_SYNC, "SETTINGS_CONFIG_PATH", settings),
            ):
                health = SITE_SYNC.source_health("2026-07-14")

        self.assertEqual(health["chinaInferredPriceDateItemCount"], 1)
        self.assertEqual(health["chinaMissingPriceDateItemCount"], 1)

    @requires_runtime_report("2026-07-14")
    def test_current_report_exposes_nonempty_observation_table(self) -> None:
        report_path, _report_date = SITE_SYNC.report_for_date("2026-07-14")
        audit = SITE_SYNC.report_quality_audit(report_path.read_text(encoding="utf-8"))

        self.assertGreaterEqual(audit["observationCount"], 1)

    @requires_runtime_report("2026-07-15")
    def test_revised_report_builds_distinct_clean_public_copy(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-15")
        payload = SITE_SYNC.build_payload(report_path.read_text(encoding="utf-8"), report_date, 1, "revised-sha")
        serialized = json.dumps(payload, ensure_ascii=False)
        headline = "".join(payload["hero"]["headline"])

        self.assertEqual(headline, "霍尔木兹风险重新压过外交窗口")
        self.assertNotEqual(headline, payload["hero"]["dek"])
        self.assertNotEqual(payload["hero"]["dek"], payload["hero"]["editorNote"])
        self.assertNotIn("｜｜", serialized)
        self.assertTrue(
            all(
                not event["cardTitle"].startswith(("结论：", "确认事实：", "分析判断：", "判断："))
                for event in payload["events"]
            )
        )

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
            root = Path(temporary)
            state_path = root / "site-sync-state.json"
            site_data = root / "briefing.generated.json"
            site_manifest = root / "publication.generated.json"
            report_path = root / "report.md"
            verification_path = root / "production-verification.json"
            content_hash = "a" * 64
            report_date = "2026-07-12"
            raw_report = b"report"
            current_payload = {"reportDate": "2026-07-12", "contentHash": content_hash}
            current_payload_sha = SITE_SYNC.payload_sha256(current_payload)
            candidate = candidate_fingerprint(report_date, raw_report, content_hash, current_payload)
            snapshot = {
                "schema_version": 2,
                "date": report_date,
                "revision": 1,
                "report_sha256": SITE_SYNC.hashlib.sha256(raw_report).hexdigest(),
                "content_hash": content_hash,
                "payload_sha256": current_payload_sha,
                "candidate_fingerprint": candidate,
                "prerequisites": {},
                "payload": current_payload,
            }
            snapshot_sha = SITE_SYNC.publication_snapshot_sha256(snapshot)
            manifest = SITE_SYNC.build_publication_manifest(snapshot)
            manifest_sha = SITE_SYNC.payload_sha256(manifest)
            site_data.write_text(SITE_SYNC.serialized_payload(current_payload), encoding="utf-8")
            site_manifest.write_text(SITE_SYNC.serialized_payload(manifest), encoding="utf-8")
            report_path.write_bytes(raw_report)
            state_path.write_text(
                json.dumps(
                    {
                        "pending_sha": content_hash,
                        "pending_payload_sha": current_payload_sha,
                        "pending_manifest_sha": manifest_sha,
                        "pending_report": "report.md",
                        "pending_date": "2026-07-12",
                        "pending_snapshot_revision": 1,
                        "pending_snapshot_sha256": snapshot_sha,
                        "pending_build_id": manifest["buildId"],
                        "pending_deployment_id": manifest["deploymentId"],
                        "last_deployed_sha": "older",
                    }
                ),
                encoding="utf-8",
            )
            verified_at = datetime.now(timezone.utc).isoformat()
            verification_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "checked_at": verified_at,
                        "deployment_url": "https://example.com",
                        "expected": {
                            "content_hash": manifest["contentHash"],
                            "report_date": manifest["reportDate"],
                            "payload_sha256": manifest["payloadSha256"],
                            "snapshot_revision": manifest["snapshotRevision"],
                            "snapshot_sha256": manifest["snapshotSha256"],
                            "candidate_fingerprint": manifest["candidateFingerprint"],
                            "build_id": manifest["buildId"],
                            "deployment_id": manifest["deploymentId"],
                        },
                        "source_isolation": {"passed": True},
                        "live": {
                            "passed": True,
                            "observed_content_hash": manifest["contentHash"],
                            "observed_report_date": manifest["reportDate"],
                            "observed_payload_sha256": manifest["payloadSha256"],
                            "observed_snapshot_revision": manifest["snapshotRevision"],
                            "observed_snapshot_sha256": manifest["snapshotSha256"],
                            "observed_candidate_fingerprint": manifest["candidateFingerprint"],
                            "observed_build_id": manifest["buildId"],
                            "observed_deployment_id": manifest["deploymentId"],
                        },
                        "network": {
                            "connected_ip": "93.184.216.34",
                            "validated_addresses": ["93.184.216.34"],
                            "dns_pinned": True,
                            "tls_server_name": "example.com",
                            "host_header": "example.com",
                        },
                        "passed": True,
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "report_for_date", return_value=(report_path, "2026-07-12")),
                patch.object(SITE_SYNC, "site_input_hash", return_value=content_hash),
                patch.object(SITE_SYNC, "load_publication_snapshot", return_value=snapshot),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(
                    sys,
                    "argv",
                    [
                        "sync_briefing_site.py",
                        "--mark-deployed",
                        content_hash,
                        "--deployment-url",
                        "https://example.com",
                        "--verification-artifact",
                        str(verification_path),
                    ],
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(SITE_SYNC.main(), 0)

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(state["last_deployed_sha"], content_hash)
        self.assertEqual(state["last_deployed_payload_sha"], current_payload_sha)
        self.assertEqual(state["last_deployed_manifest_sha"], manifest_sha)
        self.assertEqual(state["last_deployed_snapshot_revision"], 1)
        self.assertEqual(state["last_deployed_snapshot_sha256"], snapshot_sha)
        self.assertNotIn("pending_sha", state)
        self.assertNotIn("pending_report", state)
        self.assertNotIn("pending_date", state)
        self.assertEqual(state["last_deployment_verified_at"], verified_at)

    def test_mark_deployed_rejects_missing_production_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "site-sync-state.json"
            site_data = root / "briefing.generated.json"
            report_path = root / "report.md"
            content_hash = "b" * 64
            current_payload = {"reportDate": "2026-07-12", "contentHash": content_hash}
            current_payload_sha = SITE_SYNC.payload_sha256(current_payload)
            snapshot = {
                "revision": 1,
                "payload_sha256": current_payload_sha,
                "payload": current_payload,
            }
            snapshot_sha = SITE_SYNC.publication_snapshot_sha256(snapshot)
            state_path.write_text(
                json.dumps({
                    "pending_sha": content_hash,
                    "pending_payload_sha": current_payload_sha,
                    "pending_date": "2026-07-12",
                    "pending_snapshot_revision": 1,
                    "pending_snapshot_sha256": snapshot_sha,
                }),
                encoding="utf-8",
            )
            site_data.write_text(SITE_SYNC.serialized_payload(current_payload), encoding="utf-8")
            report_path.write_text("report", encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "report_for_date", return_value=(report_path, "2026-07-12")),
                patch.object(SITE_SYNC, "site_input_hash", return_value=content_hash),
                patch.object(SITE_SYNC, "load_publication_snapshot", return_value=snapshot),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(
                    sys,
                    "argv",
                    [
                        "sync_briefing_site.py",
                        "--mark-deployed",
                        content_hash,
                        "--deployment-url",
                        "https://example.com",
                        "--verification-artifact",
                        str(Path(temporary) / "missing.json"),
                    ],
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(SITE_SYNC.main(), 2)

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertNotIn("last_deployed_sha", state)

    def test_mark_deployed_rejects_unfrozen_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "site-sync-state.json"
            site_data = root / "briefing.generated.json"
            content_hash = "c" * 64
            current_payload = {"reportDate": "2026-07-12", "contentHash": content_hash}
            current_payload_sha = SITE_SYNC.payload_sha256(current_payload)
            state_path.write_text(
                json.dumps({
                    "pending_sha": content_hash,
                    "pending_payload_sha": current_payload_sha,
                    "pending_date": "2026-07-12",
                }),
                encoding="utf-8",
            )
            site_data.write_text(SITE_SYNC.serialized_payload(current_payload), encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--mark-deployed", content_hash],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), 2)

            result = json.loads(output.getvalue())
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertTrue(any("not bound to a frozen" in reason for reason in result["reasons"]))
        self.assertNotIn("last_deployed_sha", state)

    def test_unready_candidate_is_staged_without_touching_deployable_site_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "site-sync-state.json"
            site_data = root / "briefing.generated.json"
            site_manifest = root / "publication.generated.json"
            candidate_root = root / "publication_candidates"
            report_path = root / "每日全球晨间简报-2026-07-12.md"
            report_path.write_text("report", encoding="utf-8")
            state_path.write_text("{}", encoding="utf-8")
            site_data.write_text('{"sentinel":"payload"}\n', encoding="utf-8")
            site_manifest.write_text('{"sentinel":"manifest"}\n', encoding="utf-8")
            content_hash = "7" * 64
            payload = {"reportDate": "2026-07-12", "contentHash": content_hash}
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "OUTPUTS", root),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "report_for_date", return_value=(report_path, "2026-07-12")),
                patch.object(SITE_SYNC, "site_input_hash", return_value=content_hash),
                patch.object(SITE_SYNC, "build_payload", return_value=payload),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(SITE_SYNC, "current_repository_commits", return_value=TEST_REPOSITORY_COMMITS),
                patch.object(
                    SITE_SYNC,
                    "load_publication_snapshot",
                    side_effect=ValueError("legacy snapshot schema is invalid"),
                ),
                patch.object(
                    SITE_SYNC,
                    "publication_snapshot_readiness",
                    return_value={"ready": False, "reasons": ["gates not ready"], "evidence": {}},
                ),
                patch.object(sys, "argv", ["sync_briefing_site.py", "--date", "2026-07-12"]),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), 0)

            result = json.loads(output.getvalue())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            staged = json.loads(Path(result["staged"]).read_text(encoding="utf-8"))
            site_data_after = site_data.read_text(encoding="utf-8")
            site_manifest_after = site_manifest.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "staged")
        self.assertTrue(any("existing publication snapshot conflicts" in reason for reason in result["reasons"]))
        self.assertEqual(staged["payload"], payload)
        self.assertEqual(staged["candidate_fingerprint"]["report_sha256"], SITE_SYNC.hashlib.sha256(b"report").hexdigest())
        self.assertNotIn("pending_sha", state)
        self.assertEqual(site_data_after, '{"sentinel":"payload"}\n')
        self.assertEqual(site_manifest_after, '{"sentinel":"manifest"}\n')

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
            quality = root / "research-quality-2026-07-13.json"
            raw_report = b"current report"
            content_hash = "8" * 64
            candidate = candidate_fingerprint(
                "2026-07-13",
                raw_report,
                content_hash,
                {"reportDate": "2026-07-13", "contentHash": content_hash},
            )
            cycle.write_text(
                json.dumps({
                    "date": "2026-07-13",
                    "overall_passed": True,
                    "operational_gate_passed": True,
                    "release_candidate_passed": True,
                    "idempotent_replay": True,
                    "execution_profile": {"full_tests": True},
                    "stages": [
                        {"name": "sync", "status": "passed"},
                        {"name": "canonical_virtual_ledger_commit", "status": "passed"},
                    ],
                    "ledger": {"write_performed": True},
                    "boundary": {
                        "canonical_write_performed": True,
                        "idempotency_verified_by_repeated_fingerprint": True,
                        "full_test_suite_executed": True,
                    },
                    "workspace_lock": {
                        "release_reproducible": True,
                        "repositories": [
                            {"name": name, "commit": commit}
                            for name, commit in TEST_REPOSITORY_COMMITS.items()
                        ],
                    },
                }),
                encoding="utf-8",
            )
            healing.write_text(
                json.dumps({
                    "date": "2026-07-13",
                    "overall_status": "healthy",
                    "deep": True,
                    "strict": True,
                    "counts": {"blocking": 0, "failed": 0, "unresolved": 0},
                    "checks": [
                        {"check_id": "briefing_tests", "executed": True, "passed": True},
                        {"check_id": "site_quality", "executed": True, "passed": True},
                    ],
                }),
                encoding="utf-8",
            )
            improvements.write_text(json.dumps({"date": "2026-07-13", "status": "degraded", "counts": {"blocking": 0}}), encoding="utf-8")
            alerts.write_text(json.dumps({"date": "2026-07-13", "status": "healthy", "finding_count": 0}), encoding="utf-8")
            quality_payload = {
                "date": "2026-07-13",
                "report_sha256": SITE_SYNC.hashlib.sha256(raw_report).hexdigest(),
                "operational_passed": True,
                "report_audit": {"passed": True},
            }
            quality.write_text(json.dumps(quality_payload), encoding="utf-8")
            backup_payload = {
                "date": "2026-07-13",
                "encrypted": True,
                "encryption_algorithm": "AES-256-GCM",
                "encrypted_container_authenticated": True,
                "archive_integrity_verified": True,
                "restore_verified": True,
                "restore_scope": "configured_workspace_files_and_git_bundles",
                "full_runtime_restore_verified": False,
                "target_outside_workspace": True,
            }
            backup.write_text(json.dumps(backup_payload), encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "cycle_audit_path", return_value=cycle),
                patch.object(SITE_SYNC, "DATA_DIR", root),
                patch.object(SITE_SYNC, "ATLAS_SELF_HEALING_LATEST", healing),
                patch.object(SITE_SYNC, "ATLAS_IMPROVEMENTS_LATEST", improvements),
                patch.object(SITE_SYNC, "ATLAS_ALERTS_LATEST", alerts),
                patch.object(SITE_SYNC, "ATLAS_BACKUPS_LATEST", backup),
            ):
                ready = SITE_SYNC.publication_snapshot_readiness(
                    "2026-07-13", candidate_fingerprint=candidate
                )
                quality_payload["report_sha256"] = "0" * 64
                quality.write_text(json.dumps(quality_payload), encoding="utf-8")
                blocked_quality = SITE_SYNC.publication_snapshot_readiness(
                    "2026-07-13", candidate_fingerprint=candidate
                )
                quality_payload["report_sha256"] = SITE_SYNC.hashlib.sha256(raw_report).hexdigest()
                quality.write_text(json.dumps(quality_payload), encoding="utf-8")
                backup_payload["encrypted"] = False
                backup.write_text(json.dumps(backup_payload), encoding="utf-8")
                blocked_backup = SITE_SYNC.publication_snapshot_readiness(
                    "2026-07-13", candidate_fingerprint=candidate
                )
                backup_payload["encrypted"] = True
                backup.write_text(json.dumps(backup_payload), encoding="utf-8")
                healing_payload = json.loads(healing.read_text(encoding="utf-8"))
                healing_payload["deep"] = False
                healing.write_text(json.dumps(healing_payload), encoding="utf-8")
                blocked = SITE_SYNC.publication_snapshot_readiness(
                    "2026-07-13", candidate_fingerprint=candidate
                )
                healing_payload["deep"] = True
                healing.write_text(json.dumps(healing_payload), encoding="utf-8")
                cycle_payload = json.loads(cycle.read_text(encoding="utf-8"))
                cycle_payload["release_candidate_passed"] = False
                cycle.write_text(json.dumps(cycle_payload), encoding="utf-8")
                blocked_release = SITE_SYNC.publication_snapshot_readiness(
                    "2026-07-13", candidate_fingerprint=candidate
                )

        self.assertTrue(ready["ready"])
        self.assertEqual(ready["evidence"]["candidateFingerprint"], candidate)
        self.assertEqual(
            ready["evidence"]["candidateFormula"],
            "sha256(utf8(json(candidate_inputs,sort_keys=true,separators=(',',':'))))",
        )
        self.assertTrue(
            all(
                evidence["candidateFingerprint"] == candidate["fingerprint_sha256"]
                and len(evidence["sha256"]) == 64
                for evidence in ready["evidence"]["gateArtifacts"].values()
            )
        )
        self.assertFalse(blocked_quality["ready"])
        self.assertTrue(any("current report SHA" in reason for reason in blocked_quality["reasons"]))
        self.assertFalse(blocked_backup["ready"])
        self.assertTrue(any("encrypted external backup" in reason for reason in blocked_backup["reasons"]))
        self.assertFalse(blocked["ready"])
        self.assertTrue(any("deep self-healing" in reason for reason in blocked["reasons"]))
        self.assertFalse(blocked_release["ready"])
        self.assertTrue(any("release candidate" in reason for reason in blocked_release["reasons"]))

    @requires_runtime_report("2026-07-12")
    def test_frozen_publication_snapshot_is_retry_stable_and_rejects_silent_report_drift(self) -> None:
        report_path, report_date = SITE_SYNC.report_for_date("2026-07-12")
        raw = report_path.read_bytes()
        content_hash = SITE_SYNC.site_input_hash(raw, report_date)
        payload = SITE_SYNC.build_payload(raw.decode("utf-8"), report_date, 1, content_hash)
        candidate = candidate_fingerprint(report_date, raw, content_hash, payload)
        readiness = {
            "ready": True,
            "reasons": [],
            "evidence": {
                "candidateFingerprint": candidate,
                "candidateFingerprintSha256": candidate["fingerprint_sha256"],
                "candidateFormula": candidate["formula"],
                "gateArtifacts": {
                    "cycle": {
                        "sha256": "9" * 64,
                        "candidateFingerprint": candidate["fingerprint_sha256"],
                    }
                },
            },
        }
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
                    candidate_fingerprint=candidate,
                )
                loaded = SITE_SYNC.load_publication_snapshot(report_date, raw, content_hash)
                with self.assertRaisesRegex(ValueError, "report changed"):
                    SITE_SYNC.load_publication_snapshot(report_date, raw + b"\nchanged", content_hash)
                second = SITE_SYNC.freeze_publication_snapshot(
                    report_date=report_date,
                    raw_report=raw,
                    content_hash=content_hash,
                    payload=payload,
                    candidate_fingerprint=candidate,
                    refresh=True,
                )

            history_exists = (snapshot_root / "history" / report_date / "revision-1.json").exists()

        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["candidate_fingerprint"], candidate)
        self.assertEqual(
            SITE_SYNC.build_publication_manifest(first)["payloadSha256"],
            first["payload_sha256"],
        )
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
                                "valuations_file": "data/paper_valuations_us.jsonl",
                            },
                            "CHINA": {
                                "account_id": "china-test",
                                "initial_cash": 100000,
                                "base_currency": "CNY",
                                "trades_file": "data/china.jsonl",
                                "valuations_file": "data/paper_valuations_china.jsonl",
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
                                "price_date": "2026-06-09",
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
            (data_dir / "paper_valuations_us.jsonl").write_text(
                json.dumps({
                    "date": "2026-06-14",
                    "cash": 99900,
                    "positions_value": 120,
                    "equity": 100020,
                    "price_snapshot": [
                        {
                            "key": "NASDAQ:AAPL",
                            "symbol": "AAPL",
                            "exchange": "NASDAQ",
                            "currency": "USD",
                            "fx_to_base": 1,
                            "price": 120,
                            "price_date": "2026-06-14",
                            "source": "dated valuation",
                        }
                    ],
                })
                + "\n",
                encoding="utf-8",
            )
            (data_dir / "paper_valuations_china.jsonl").write_text("", encoding="utf-8")
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
        self.assertEqual(us["positions"][0]["lastPrice"], 120)
        self.assertEqual(us["positions"][0]["priceDate"], "2026-06-14")
        self.assertEqual(us["equity"], 100020)
        self.assertEqual(china["baseCurrency"], "CNY")
        self.assertTrue(any("逐笔重建" in item for item in us["limitations"]))

    def test_public_portfolio_exposes_account_totals_but_not_ledger_details(self) -> None:
        public = SITE_SYNC.public_portfolio(
            {
                "name": "US 虚拟组合",
                "value": "99,008.90",
                "return": "-0.99%",
                "returnPct": -0.99,
                "asOf": "2026-07-16",
                "baseCurrency": "USD",
                "cash": 30000.0,
                "cashPct": 30.3,
                "equity": 99008.9,
                "periodPnl": -340.16,
                "periodReturnPct": -0.34,
                "allocations": [
                    {"label": "现金", "pct": 30.3},
                    {"label": "SMH", "pct": 22.5},
                    {"label": "其他", "pct": 47.2},
                ],
                "accountId": "private-account",
                "positions": [{"symbol": "SMH"}],
                "realizedPnl": 0.0,
                "initialCash": 100000.0,
                "paperTradingOnly": True,
            }
        )

        self.assertEqual(public["equity"], 99008.9)
        self.assertEqual(public["cash"], 30000.0)
        self.assertEqual(public["periodPnl"], -340.16)
        self.assertEqual(public["baseCurrency"], "USD")
        self.assertEqual(public["allocations"][1]["label"], "匿名资产 1")
        for field in ("accountId", "positions", "realizedPnl", "initialCash", "previousEquity"):
            self.assertNotIn(field, public)
