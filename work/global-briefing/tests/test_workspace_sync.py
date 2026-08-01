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


def write_staged_candidate_fixture(
    *,
    output_root: Path,
    candidate_root: Path,
    report_date: str,
    raw_report: bytes = b"staged report",
) -> tuple[Path, Path, dict, dict, str]:
    """Create a material-valid staged record without invoking Phase A."""
    report_path = output_root / f"每日全球晨间简报-{report_date}.md"
    report_path.write_bytes(raw_report)
    content_hash = SITE_SYNC.site_input_hash(raw_report, report_date)
    payload = {
        "schemaVersion": SITE_SYNC.SITE_SCHEMA_VERSION,
        "reportDate": report_date,
        "contentHash": content_hash,
        "fixture": "stored-candidate-payload",
    }
    fingerprint = candidate_fingerprint(report_date, raw_report, content_hash, payload)
    candidate_path = candidate_root / f"atlas-publication-candidate-{report_date}.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": SITE_SYNC.PUBLICATION_CANDIDATE_SCHEMA_VERSION,
                "staged_at": datetime.now(timezone.utc).isoformat(),
                "status": "staged",
                "date": report_date,
                "report": SITE_SYNC._evidence_path(report_path),
                "candidate_fingerprint": fingerprint,
                "readiness": {"ready": False, "reasons": ["awaiting gates"], "evidence": {}},
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )
    return report_path, candidate_path, payload, fingerprint, content_hash


def staged_candidate_state_fixture(
    *,
    report_date: str,
    report_path: Path,
    candidate_path: Path,
    payload: dict,
    fingerprint: dict,
    content_hash: str,
    status: str = "staged",
) -> dict:
    return {
        "staged_sha": content_hash,
        "staged_payload_sha": SITE_SYNC.payload_sha256(payload),
        "staged_candidate_fingerprint": fingerprint["fingerprint_sha256"],
        "staged_report": str(report_path),
        "staged_date": report_date,
        "staged_path": str(candidate_path),
        "staged_status": status,
    }


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

    def test_validate_payload_rejects_page_crashing_market_risk_and_evolution_shapes(self) -> None:
        payload = json.loads(
            (ROOT / "src" / "app" / "briefing.generated.json").read_text(encoding="utf-8")
        )
        self.assertEqual(SITE_SYNC.validate_payload(payload), [])

        payload["markets"]["us"]["items"] = {"not": "an array"}
        payload["risks"] = ["", 7]
        payload["evolution"]["integrity"] = {
            "early_closed_without_terminal_evidence": "not an array",
            "overdue_unreviewed_prediction_ids": [],
        }

        errors = SITE_SYNC.validate_payload(payload)

        self.assertIn("markets.us.items must be a list", errors)
        self.assertIn("risks must be a list of at least 1 non-empty string(s)", errors)
        self.assertIn(
            "evolution.integrity.early_closed_without_terminal_evidence must be a list of at least 0 non-empty string(s)",
            errors,
        )
        self.assertNotIn("evolution.integrity.overdue_unreviewed_prediction_ids must be a list of at least 0 non-empty string(s)", errors)

    def test_validate_payload_requires_explicit_stale_or_undated_valuation_provenance(self) -> None:
        payload = json.loads(
            (ROOT / "src" / "app" / "briefing.generated.json").read_text(encoding="utf-8")
        )
        payload["portfolios"]["us"]["valuationAsOf"] = "2026-07-15"
        payload["portfolios"]["us"]["valuationIsStale"] = False
        payload["portfolios"]["china"]["valuationAsOf"] = None
        payload["portfolios"]["china"]["valuationIsStale"] = False

        errors = SITE_SYNC.validate_payload(payload)

        self.assertIn(
            "portfolios.us.valuationIsStale must flag a non-report-date valuation",
            errors,
        )
        self.assertIn("portfolios.china.valuationIsStale must flag an undated valuation", errors)

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

    def test_short_transmission_label_does_not_replace_event_implication(self) -> None:
        sections = {
            "经济、宏观与能源": [
                "1. **关税与油运共同推高成本。** 确认事实：企业成本上升；传导：腾讯科创50。"
            ]
        }

        events = SITE_SYNC.parse_events(sections, [], [])

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "宏观")
        self.assertEqual(
            events[0]["implication"],
            SITE_SYNC.default_event_implication("宏观"),
        )
        self.assertGreaterEqual(len(events[0]["implication"]), 12)

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

    def test_market_as_of_uses_selected_assets_actual_price_dates(self) -> None:
        us = {
            "items": [
                {"ticker": "SPY", "price_date": "2026-07-31", "change_pct": 1.0},
                {"ticker": "QQQ", "price_date": "2026-07-30", "change_pct": 2.0},
                {"ticker": "OTHER", "price_date": "2026-08-01", "change_pct": 3.0},
            ]
        }
        china = {
            "generated_at": "2026-08-01T08:00:00+08:00",
            "items": [
                {"symbol": "000300.SH", "name": "沪深300指数", "price_date": "2026-07-31", "change_pct": 0.5},
                {"symbol": "399006.SZ", "name": "创业板指数", "price_date": "2026-07-30", "change_pct": -0.5},
            ],
        }

        with patch.object(SITE_SYNC, "latest_snapshot", side_effect=[us, china]):
            markets = SITE_SYNC.parse_markets("2026-08-01")

        self.assertEqual(markets["us"]["asOf"], "2026-07-30")
        self.assertEqual(markets["us"]["priceDateRange"], ["2026-07-30", "2026-07-31"])
        self.assertEqual(markets["china"]["asOf"], "2026-07-30")
        self.assertEqual(markets["china"]["priceDateRange"], ["2026-07-30", "2026-07-31"])
        self.assertTrue(markets["china"]["isStale"])
        self.assertNotEqual(markets["china"]["asOf"], "2026-08-01")

    def test_market_snapshot_rejects_future_invalid_and_nonfinite_rows(self) -> None:
        us = {
            "items": [
                {"ticker": "SPY", "price_date": "2026-08-02", "change_pct": 1.0},
                {"ticker": "QQQ", "price_date": "not-a-date", "change_pct": 2.0},
                {"ticker": "SMH", "price_date": "2026-07-31", "change_pct": float("nan")},
                {"ticker": "GLD", "price_date": "2026-07-30", "change_pct": -0.25},
            ]
        }

        with patch.object(SITE_SYNC, "latest_snapshot", side_effect=[us, {"items": []}]):
            markets = SITE_SYNC.parse_markets("2026-08-01")

        self.assertEqual(markets["us"]["items"], [
            {"label": "GLD", "change": "-0.25%", "direction": "down"}
        ])
        self.assertEqual(markets["us"]["asOf"], "2026-07-30")
        self.assertEqual(markets["us"]["priceDateRange"], ["2026-07-30", "2026-07-30"])
        self.assertNotIn("nan", json.dumps(markets, ensure_ascii=False).lower())

    def test_market_snapshot_with_no_valid_prices_is_unknown_and_stale(self) -> None:
        us = {
            "items": [
                {"ticker": "SPY", "price_date": "", "change_pct": 1.0},
                {"ticker": "QQQ", "price_date": "2026-08-02", "change_pct": float("inf")},
            ]
        }

        with patch.object(SITE_SYNC, "latest_snapshot", side_effect=[us, {"items": []}]):
            markets = SITE_SYNC.parse_markets("2026-08-01")

        self.assertEqual(markets["us"]["asOf"], "未知")
        self.assertEqual(markets["us"]["priceDateRange"], [])
        self.assertEqual(markets["us"]["items"], [])
        self.assertTrue(markets["us"]["isStale"])
        self.assertEqual(markets["us"]["freshness"], "行情日期未知")

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
            receipt_path = root / "deployment-receipt.json"
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
                        "deployment_url": SITE_SYNC.SITES_PRODUCTION_URL,
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
                            "tls_server_name": "atlas-global-brief-2026.poetic-kiwi-4295.chatgpt.site",
                            "host_header": "atlas-global-brief-2026.poetic-kiwi-4295.chatgpt.site",
                        },
                        "passed": True,
                    }
                ),
                encoding="utf-8",
            )
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": SITE_SYNC.DEPLOYMENT_RECEIPT_SCHEMA_VERSION,
                        "status": "succeeded",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "project_id": SITE_SYNC.SITES_PROJECT_ID,
                        "deployment_url": SITE_SYNC.SITES_PRODUCTION_URL,
                        "source_base_commit": "1" * 40,
                        "published_commit": "2" * 40,
                        "artifact_sha256": "3" * 64,
                        "artifact_size_bytes": 1234,
                        "sites_version_id": "version-27",
                        "sites_deployment_id": "deployment-27",
                        "publication_manifest_sha256": manifest_sha,
                        "content_hash": content_hash,
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "DEPLOYMENT_RECEIPT_ARCHIVE_ROOT", root / "receipt-archive"),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "report_for_date", return_value=(report_path, "2026-07-12")),
                patch.object(SITE_SYNC, "site_input_hash", return_value=content_hash),
                patch.object(SITE_SYNC, "load_publication_snapshot", return_value=snapshot),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(SITE_SYNC, "refresh_production_verification", return_value=0) as refresh,
                patch.object(
                    sys,
                    "argv",
                    [
                        "sync_briefing_site.py",
                        "--mark-deployed",
                        content_hash,
                        "--deployment-url",
                        SITE_SYNC.SITES_PRODUCTION_URL,
                        "--verification-artifact",
                        str(verification_path),
                        "--deployment-receipt",
                        str(receipt_path),
                    ],
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(SITE_SYNC.main(), 0)

            refresh.assert_called_once_with(SITE_SYNC.SITES_PRODUCTION_URL, verification_path)

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
        self.assertEqual(state["last_deployment_published_commit"], "2" * 40)
        self.assertEqual(state["last_deployment_artifact_sha256"], "3" * 64)
        self.assertEqual(state["last_sites_version_id"], "version-27")
        self.assertEqual(state["last_sites_deployment_id"], "deployment-27")

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
                patch.object(SITE_SYNC, "refresh_production_verification", return_value=1),
                patch.object(
                    sys,
                    "argv",
                    [
                        "sync_briefing_site.py",
                        "--mark-deployed",
                        content_hash,
                        "--deployment-url",
                        SITE_SYNC.SITES_PRODUCTION_URL,
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

    def test_retry_staged_candidate_blocks_without_rebuilding_phase_a(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "outputs"
            output_root.mkdir()
            candidate_root = root / "publication_candidates"
            report_date = "2026-07-18"
            report_path, candidate_path, payload, fingerprint, content_hash = write_staged_candidate_fixture(
                output_root=output_root,
                candidate_root=candidate_root,
                report_date=report_date,
            )
            state_path = root / "site-sync-state.json"
            legacy_state = staged_candidate_state_fixture(
                report_date=report_date,
                report_path=report_path,
                candidate_path=candidate_path,
                payload=payload,
                fingerprint=fingerprint,
                content_hash=content_hash,
            )
            legacy_state.pop("staged_status")
            state_path.write_text(
                json.dumps(legacy_state),
                encoding="utf-8",
            )
            site_data = root / "app" / "briefing.generated.json"
            site_manifest = root / "app" / "publication.generated.json"
            site_data.parent.mkdir()
            site_data.write_text('{"sentinel":"payload"}\n', encoding="utf-8")
            site_manifest.write_text('{"sentinel":"manifest"}\n', encoding="utf-8")
            readiness = {
                "ready": False,
                "reasons": ["closed-loop gates remain blocked"],
                "evidence": {"candidateFingerprint": fingerprint},
            }
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "OUTPUTS", output_root),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "PUBLICATION_RETRY_LOCK_ROOT", root / "retry-locks"),
                patch.object(SITE_SYNC, "PUBLICATION_SNAPSHOT_ROOT", root / "snapshots"),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(SITE_SYNC, "current_repository_commits", return_value=TEST_REPOSITORY_COMMITS),
                patch.object(SITE_SYNC, "publication_snapshot_readiness", return_value=readiness),
                patch.object(
                    SITE_SYNC,
                    "build_payload",
                    side_effect=AssertionError("retry must not rebuild Phase A payload"),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--retry-staged-candidate", "--date", report_date],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), SITE_SYNC.RETRY_BLOCKED_EXIT_CODE)

            result = json.loads(output.getvalue())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            staged = json.loads(candidate_path.read_text(encoding="utf-8"))
            site_data_after = site_data.read_text(encoding="utf-8")
            site_manifest_after = site_manifest.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["candidate_fingerprint"], fingerprint["fingerprint_sha256"])
        self.assertEqual(site_data_after, '{"sentinel":"payload"}\n')
        self.assertEqual(site_manifest_after, '{"sentinel":"manifest"}\n')
        self.assertEqual(state["staged_status"], "blocked")
        self.assertEqual(state["staged_sha"], content_hash)
        self.assertEqual(state["staged_report"], str(report_path))
        self.assertEqual(staged["payload"], payload)
        self.assertEqual(staged["candidate_fingerprint"], fingerprint)
        self.assertEqual(staged["retry"]["status"], "blocked")
        self.assertEqual(staged["retry"]["attempt"], 1)

    def test_retry_staged_candidate_freezes_stored_payload_once_and_clears_its_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "outputs"
            output_root.mkdir()
            candidate_root = root / "publication_candidates"
            report_date = "2026-07-18"
            report_path, candidate_path, payload, fingerprint, content_hash = write_staged_candidate_fixture(
                output_root=output_root,
                candidate_root=candidate_root,
                report_date=report_date,
            )
            state_path = root / "site-sync-state.json"
            state_path.write_text(
                json.dumps(
                    staged_candidate_state_fixture(
                        report_date=report_date,
                        report_path=report_path,
                        candidate_path=candidate_path,
                        payload=payload,
                        fingerprint=fingerprint,
                        content_hash=content_hash,
                    )
                ),
                encoding="utf-8",
            )
            site_data = root / "app" / "briefing.generated.json"
            site_manifest = root / "app" / "publication.generated.json"
            site_data.parent.mkdir()
            readiness = {
                "ready": True,
                "reasons": [],
                "evidence": {
                    "candidateFingerprint": fingerprint,
                    "candidateFingerprintSha256": fingerprint["fingerprint_sha256"],
                    "candidateFormula": fingerprint["formula"],
                    "gateArtifacts": {
                        "cycle": {
                            "candidateFingerprint": fingerprint["fingerprint_sha256"],
                            "sha256": "9" * 64,
                        }
                    },
                },
            }
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "OUTPUTS", output_root),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "PUBLICATION_RETRY_LOCK_ROOT", root / "retry-locks"),
                patch.object(SITE_SYNC, "PUBLICATION_SNAPSHOT_ROOT", root / "snapshots"),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(SITE_SYNC, "current_repository_commits", return_value=TEST_REPOSITORY_COMMITS),
                patch.object(SITE_SYNC, "publication_snapshot_readiness", return_value=readiness),
                patch.object(
                    SITE_SYNC,
                    "build_payload",
                    side_effect=AssertionError("retry must not rebuild Phase A payload"),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--retry-staged-candidate", "--date", report_date],
                ),
                redirect_stdout(StringIO()) as first_output,
            ):
                self.assertEqual(SITE_SYNC.main(), 0)

            first = json.loads(first_output.getvalue())
            frozen_path = root / "snapshots" / f"atlas-publication-{report_date}.json"
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
            state_after_first = json.loads(state_path.read_text(encoding="utf-8"))
            written_payload = json.loads(site_data.read_text(encoding="utf-8"))
            staged_after_first = json.loads(candidate_path.read_text(encoding="utf-8"))

            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "OUTPUTS", output_root),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "PUBLICATION_RETRY_LOCK_ROOT", root / "retry-locks"),
                patch.object(SITE_SYNC, "PUBLICATION_SNAPSHOT_ROOT", root / "snapshots"),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(SITE_SYNC, "current_repository_commits", return_value=TEST_REPOSITORY_COMMITS),
                patch.object(
                    SITE_SYNC,
                    "freeze_publication_snapshot",
                    side_effect=AssertionError("an existing frozen snapshot must not be recreated"),
                ),
                patch.object(
                    SITE_SYNC,
                    "build_payload",
                    side_effect=AssertionError("retry must not rebuild Phase A payload"),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--retry-staged-candidate", "--date", report_date],
                ),
                redirect_stdout(StringIO()) as second_output,
            ):
                self.assertEqual(SITE_SYNC.main(), SITE_SYNC.RETRY_NO_CANDIDATE_EXIT_CODE)

            second = json.loads(second_output.getvalue())
            frozen_after_second = json.loads(frozen_path.read_text(encoding="utf-8"))
            state_after_second = json.loads(state_path.read_text(encoding="utf-8"))
            staged_after_second = json.loads(candidate_path.read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "queued")
        self.assertEqual(written_payload, payload)
        self.assertEqual(SITE_SYNC.payload_sha256(written_payload), fingerprint["payload_sha256"])
        self.assertEqual(frozen["payload"], payload)
        self.assertEqual(frozen["candidate_fingerprint"], fingerprint)
        self.assertEqual(frozen["revision"], 1)
        self.assertEqual(state_after_first["pending_sha"], content_hash)
        self.assertNotIn("staged_date", state_after_first)
        self.assertEqual(staged_after_first["retry"]["status"], "pending")
        self.assertEqual(second["status"], "no_candidate")
        self.assertEqual(frozen_after_second["revision"], 1)
        self.assertEqual(state_after_second["pending_sha"], content_hash)
        self.assertNotIn("staged_date", state_after_second)
        self.assertEqual(staged_after_second["retry"]["attempt"], 1)

    def test_retry_staged_candidate_refuses_current_report_or_repository_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "outputs"
            output_root.mkdir()
            candidate_root = root / "publication_candidates"
            report_date = "2026-07-18"
            report_path, candidate_path, payload, fingerprint, content_hash = write_staged_candidate_fixture(
                output_root=output_root,
                candidate_root=candidate_root,
                report_date=report_date,
                raw_report=b"original report",
            )
            state_path = root / "site-sync-state.json"
            state_path.write_text(
                json.dumps(
                    staged_candidate_state_fixture(
                        report_date=report_date,
                        report_path=report_path,
                        candidate_path=candidate_path,
                        payload=payload,
                        fingerprint=fingerprint,
                        content_hash=content_hash,
                    )
                ),
                encoding="utf-8",
            )
            site_data = root / "app" / "briefing.generated.json"
            site_manifest = root / "app" / "publication.generated.json"
            site_data.parent.mkdir()
            site_data.write_text('{"sentinel":"payload"}\n', encoding="utf-8")
            site_manifest.write_text('{"sentinel":"manifest"}\n', encoding="utf-8")
            report_path.write_bytes(b"mutated report after candidate staging")
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "OUTPUTS", output_root),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "PUBLICATION_RETRY_LOCK_ROOT", root / "retry-locks"),
                patch.object(SITE_SYNC, "PUBLICATION_SNAPSHOT_ROOT", root / "snapshots"),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(SITE_SYNC, "current_repository_commits", return_value=TEST_REPOSITORY_COMMITS),
                patch.object(
                    SITE_SYNC,
                    "build_payload",
                    side_effect=AssertionError("retry must not rebuild Phase A payload"),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--retry-staged-candidate", "--date", report_date],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), 2)

            result = json.loads(output.getvalue())
            staged = json.loads(candidate_path.read_text(encoding="utf-8"))
            site_data_after = site_data.read_text(encoding="utf-8")
            site_manifest_after = site_manifest.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "error")
        self.assertTrue(any("current report" in reason for reason in result["reasons"]))
        self.assertEqual(staged["status"], "staged")
        self.assertNotIn("retry", staged)
        self.assertEqual(site_data_after, '{"sentinel":"payload"}\n')
        self.assertEqual(site_manifest_after, '{"sentinel":"manifest"}\n')

    def test_retry_staged_candidate_requires_matching_active_state_even_with_explicit_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "outputs"
            output_root.mkdir()
            candidate_root = root / "publication_candidates"
            report_date = "2026-07-18"
            report_path, candidate_path, payload, fingerprint, content_hash = write_staged_candidate_fixture(
                output_root=output_root,
                candidate_root=candidate_root,
                report_date=report_date,
            )
            state = staged_candidate_state_fixture(
                report_date=report_date,
                report_path=report_path,
                candidate_path=candidate_path,
                payload=payload,
                fingerprint=fingerprint,
                content_hash=content_hash,
            )
            state["staged_path"] = str(root / "publication_candidates" / "other-candidate.json")
            state_path = root / "site-sync-state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            site_data = root / "app" / "briefing.generated.json"
            site_manifest = root / "app" / "publication.generated.json"
            site_data.parent.mkdir()
            site_data.write_text('{"sentinel":"payload"}\n', encoding="utf-8")
            site_manifest.write_text('{"sentinel":"manifest"}\n', encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "OUTPUTS", output_root),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "PUBLICATION_RETRY_LOCK_ROOT", root / "retry-locks"),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "publication_snapshot_readiness") as readiness,
                patch.object(
                    SITE_SYNC,
                    "build_payload",
                    side_effect=AssertionError("retry must not rebuild Phase A payload"),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--retry-staged-candidate", "--date", report_date],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), SITE_SYNC.RETRY_BLOCKED_EXIT_CODE)

            result = json.loads(output.getvalue())
            staged = json.loads(candidate_path.read_text(encoding="utf-8"))
            site_data_after = site_data.read_text(encoding="utf-8")
            site_manifest_after = site_manifest.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("candidate path" in reason for reason in result["reasons"]))
        readiness.assert_not_called()
        self.assertEqual(staged["status"], "staged")
        self.assertNotIn("retry", staged)
        self.assertEqual(site_data_after, '{"sentinel":"payload"}\n')
        self.assertEqual(site_manifest_after, '{"sentinel":"manifest"}\n')

    def test_retry_staged_candidate_fails_closed_on_an_existing_or_empty_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "outputs"
            output_root.mkdir()
            candidate_root = root / "publication_candidates"
            lock_root = root / "retry-locks"
            report_date = "2026-07-18"
            report_path, candidate_path, payload, fingerprint, content_hash = write_staged_candidate_fixture(
                output_root=output_root,
                candidate_root=candidate_root,
                report_date=report_date,
            )
            state_path = root / "site-sync-state.json"
            state_path.write_text(
                json.dumps(
                    staged_candidate_state_fixture(
                        report_date=report_date,
                        report_path=report_path,
                        candidate_path=candidate_path,
                        payload=payload,
                        fingerprint=fingerprint,
                        content_hash=content_hash,
                    )
                ),
                encoding="utf-8",
            )
            lock_path = lock_root / f"atlas-publication-retry-{report_date}.lock"
            lock_path.parent.mkdir()
            lock_path.write_text("", encoding="utf-8")
            site_data = root / "app" / "briefing.generated.json"
            site_manifest = root / "app" / "publication.generated.json"
            site_data.parent.mkdir()
            site_data.write_text('{"sentinel":"payload"}\n', encoding="utf-8")
            site_manifest.write_text('{"sentinel":"manifest"}\n', encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "OUTPUTS", output_root),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "PUBLICATION_RETRY_LOCK_ROOT", lock_root),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "publication_snapshot_readiness") as readiness,
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--retry-staged-candidate", "--date", report_date],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), SITE_SYNC.RETRY_BLOCKED_EXIT_CODE)

            result = json.loads(output.getvalue())
            staged = json.loads(candidate_path.read_text(encoding="utf-8"))
            lock_contents = lock_path.read_text(encoding="utf-8")
            site_data_after = site_data.read_text(encoding="utf-8")
            site_manifest_after = site_manifest.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("retry lock already exists" in reason for reason in result["reasons"]))
        readiness.assert_not_called()
        self.assertEqual(staged["status"], "staged")
        self.assertNotIn("retry", staged)
        self.assertEqual(lock_contents, "")
        self.assertEqual(site_data_after, '{"sentinel":"payload"}\n')
        self.assertEqual(site_manifest_after, '{"sentinel":"manifest"}\n')

    def test_retry_staged_candidate_refuses_repository_commit_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "outputs"
            output_root.mkdir()
            candidate_root = root / "publication_candidates"
            report_date = "2026-07-18"
            report_path, candidate_path, payload, fingerprint, content_hash = write_staged_candidate_fixture(
                output_root=output_root,
                candidate_root=candidate_root,
                report_date=report_date,
            )
            state_path = root / "site-sync-state.json"
            state_path.write_text(
                json.dumps(
                    staged_candidate_state_fixture(
                        report_date=report_date,
                        report_path=report_path,
                        candidate_path=candidate_path,
                        payload=payload,
                        fingerprint=fingerprint,
                        content_hash=content_hash,
                    )
                ),
                encoding="utf-8",
            )
            site_data = root / "app" / "briefing.generated.json"
            site_manifest = root / "app" / "publication.generated.json"
            site_data.parent.mkdir()
            site_data.write_text('{"sentinel":"payload"}\n', encoding="utf-8")
            site_manifest.write_text('{"sentinel":"manifest"}\n', encoding="utf-8")
            changed_commits = {**TEST_REPOSITORY_COMMITS, "root": "4" * 40}
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "OUTPUTS", output_root),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "PUBLICATION_CANDIDATE_ROOT", candidate_root),
                patch.object(SITE_SYNC, "PUBLICATION_RETRY_LOCK_ROOT", root / "retry-locks"),
                patch.object(SITE_SYNC, "PUBLICATION_SNAPSHOT_ROOT", root / "snapshots"),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(SITE_SYNC, "current_repository_commits", return_value=changed_commits),
                patch.object(
                    SITE_SYNC,
                    "build_payload",
                    side_effect=AssertionError("retry must not rebuild Phase A payload"),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--retry-staged-candidate", "--date", report_date],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), 2)

            result = json.loads(output.getvalue())
            staged = json.loads(candidate_path.read_text(encoding="utf-8"))
            site_data_after = site_data.read_text(encoding="utf-8")
            site_manifest_after = site_manifest.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "error")
        self.assertTrue(any("repository commits" in reason for reason in result["reasons"]))
        self.assertEqual(staged["status"], "staged")
        self.assertNotIn("retry", staged)
        self.assertEqual(site_data_after, '{"sentinel":"payload"}\n')
        self.assertEqual(site_manifest_after, '{"sentinel":"manifest"}\n')

    def test_pending_sync_returns_the_exact_two_file_deployment_allowlist_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "site-sync-state.json"
            site_data = root / "app" / "briefing.generated.json"
            site_manifest = root / "app" / "publication.generated.json"
            site_data.parent.mkdir()
            report_path = root / "每日全球晨间简报-2026-07-13.md"
            raw_report = b"report"
            report_path.write_bytes(raw_report)
            content_hash = "6" * 64
            payload = {
                "schemaVersion": SITE_SYNC.SITE_SCHEMA_VERSION,
                "reportDate": "2026-07-13",
                "contentHash": content_hash,
            }
            candidate = candidate_fingerprint(
                "2026-07-13", raw_report, content_hash, payload
            )
            snapshot = {
                "schema_version": 2,
                "date": "2026-07-13",
                "revision": 1,
                "report_sha256": SITE_SYNC.hashlib.sha256(raw_report).hexdigest(),
                "content_hash": content_hash,
                "payload_sha256": SITE_SYNC.payload_sha256(payload),
                "candidate_fingerprint": candidate,
                "prerequisites": {},
                "payload": payload,
            }
            manifest = SITE_SYNC.build_publication_manifest(snapshot)
            site_data.write_text(SITE_SYNC.serialized_payload(payload), encoding="utf-8")
            site_manifest.write_text(
                SITE_SYNC.serialized_payload(manifest), encoding="utf-8"
            )
            state_path.write_text(
                json.dumps(
                    {
                        "pending_sha": content_hash,
                        "pending_payload_sha": SITE_SYNC.payload_sha256(payload),
                        "pending_manifest_sha": SITE_SYNC.payload_sha256(manifest),
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(SITE_SYNC, "STATE_FILE", state_path),
                patch.object(SITE_SYNC, "SITE_DATA", site_data),
                patch.object(SITE_SYNC, "SITE_PUBLICATION_MANIFEST", site_manifest),
                patch.object(SITE_SYNC, "site_dependency_source_errors", return_value=[]),
                patch.object(
                    SITE_SYNC,
                    "report_for_date",
                    return_value=(report_path, "2026-07-13"),
                ),
                patch.object(SITE_SYNC, "site_input_hash", return_value=content_hash),
                patch.object(SITE_SYNC, "build_payload", return_value=payload),
                patch.object(SITE_SYNC, "validate_payload", return_value=[]),
                patch.object(
                    SITE_SYNC,
                    "current_repository_commits",
                    return_value=TEST_REPOSITORY_COMMITS,
                ),
                patch.object(
                    SITE_SYNC, "load_publication_snapshot", return_value=snapshot
                ),
                patch.object(
                    sys,
                    "argv",
                    ["sync_briefing_site.py", "--date", "2026-07-13"],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(SITE_SYNC.main(), 0)

            result = json.loads(output.getvalue())
            expected_hashes = [
                SITE_SYNC.file_sha256(site_data),
                SITE_SYNC.file_sha256(site_manifest),
            ]
            expected_workspace_paths = [
                str(site_data.resolve()),
                str(site_manifest.resolve()),
            ]

        self.assertEqual(result["status"], "pending")
        self.assertEqual(
            result["allowed_deployment_diff_paths"],
            ["app/briefing.generated.json", "app/publication.generated.json"],
        )
        self.assertEqual(
            [item["relative_path"] for item in result["generated_artifacts"]],
            result["allowed_deployment_diff_paths"],
        )
        self.assertEqual(
            [item["sha256"] for item in result["generated_artifacts"]],
            expected_hashes,
        )
        self.assertEqual(
            [item["workspace_path"] for item in result["generated_artifacts"]],
            expected_workspace_paths,
        )

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
            report_date = "2026-07-13"
            cycle = root / "cycle.json"
            healing = root / "healing.json"
            improvements = root / "improvements.json"
            alerts = root / "alerts.json"
            backup = root / "backup.json"
            quality = root / f"research-quality-{report_date}.json"
            report = root / f"每日全球晨间简报-{report_date}.md"
            raw_report = b"current report"
            report.write_bytes(raw_report)
            content_hash = "8" * 64
            candidate = candidate_fingerprint(
                report_date,
                raw_report,
                content_hash,
                {"reportDate": report_date, "contentHash": content_hash},
            )
            report_member = SITE_SYNC._evidence_path(report).replace("\\", "/")
            cycle_payload = {
                "date": report_date,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "overall_passed": True,
                "operational_gate_passed": True,
                # These software-release-only gates intentionally remain false.
                "release_candidate_passed": False,
                "idempotent_replay": False,
                "execution_profile": {"full_tests": False, "skip_site": True},
                "stages": [
                    {"name": name, "status": "passed"}
                    for name in SITE_SYNC.DAILY_PUBLICATION_REQUIRED_CYCLE_STAGES
                ],
                "ledger": {"write_performed": True, "content_hash": "4" * 64},
                "boundary": {
                    "canonical_write_performed": True,
                    "idempotency_verified_by_repeated_fingerprint": False,
                    "full_test_suite_executed": False,
                },
                "fingerprint_after": {
                    "fingerprint": "5" * 64,
                    "files": [
                        {
                            "path": report_member,
                            "sha256": SITE_SYNC.hashlib.sha256(raw_report).hexdigest(),
                            "bytes": len(raw_report),
                        }
                    ],
                },
                "workspace_lock": {
                    "release_reproducible": False,
                    "repositories": [
                        {"name": name, "commit": commit, "clean": False}
                        for name, commit in TEST_REPOSITORY_COMMITS.items()
                    ],
                },
            }
            healing_payload = {
                "date": report_date,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "overall_status": "healthy",
                "deep": True,
                "strict": True,
                "counts": {"blocking": 0, "failed": 0, "unresolved": 0},
                "checks": [
                    {"check_id": "briefing_tests", "executed": True, "passed": True},
                    {"check_id": "site_quality", "executed": True, "passed": True},
                ],
            }
            improvement_payload = {
                "date": report_date,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "degraded",
                "counts": {"blocking": 0},
            }
            alerts_payload = {
                "date": report_date,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "healthy",
                "finding_count": 0,
            }
            quality_payload = {
                "date": report_date,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "report_sha256": SITE_SYNC.hashlib.sha256(raw_report).hexdigest(),
                "operational_passed": True,
                "report_audit": {"passed": True},
            }
            for path, payload in (
                (cycle, cycle_payload),
                (healing, healing_payload),
                (improvements, improvement_payload),
                (alerts, alerts_payload),
                (quality, quality_payload),
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")

            authentication = {
                "algorithm": "HMAC-SHA256",
                "key_id": "1" * 16,
                "value": "2" * 64,
            }
            archive = root / "external-backup.atlasdr"
            archive.write_bytes(b"encrypted backup fixture")
            manifest_path = archive.with_suffix(".manifest.json")
            manifest_members = [report, cycle, healing, improvements, alerts, quality]
            manifest_payload = {
                "schema_version": 4,
                "date": report_date,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "workspace": str(ROOT.resolve()),
                "encrypted": True,
                "encryption_algorithm": "AES-256-GCM",
                "restore_scope": "configured_workspace_files_and_git_bundles",
                "full_runtime_restore_expected": False,
                "excluded_sensitive_file_patterns": [],
                "previous_manifest_sha256": None,
                "files": [
                    {
                        "path": SITE_SYNC._evidence_path(path).replace("\\", "/"),
                        "size": path.stat().st_size,
                        "sha256": SITE_SYNC.file_sha256(path),
                        "kind": "workspace_file",
                    }
                    for path in manifest_members
                ],
                "metadata_authentication": authentication,
            }
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
            backup_payload = {
                "schema_version": 4,
                "date": report_date,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "archive": str(archive.resolve()),
                "archive_sha256": SITE_SYNC.file_sha256(archive),
                "manifest_sidecar": str(manifest_path.resolve()),
                "manifest_sha256": SITE_SYNC.canonical_json_sha256(manifest_payload),
                "verified": True,
                "encrypted": True,
                "encryption_algorithm": "AES-256-GCM",
                "encrypted_container_authenticated": True,
                "archive_integrity_verified": True,
                "restore_verified": True,
                "restore_scope": "configured_workspace_files_and_git_bundles",
                "full_runtime_restore_verified": False,
                "target_outside_workspace": True,
                "metadata_authentication": authentication,
            }
            backup.write_text(json.dumps(backup_payload), encoding="utf-8")
            with (
                patch.object(SITE_SYNC, "cycle_audit_path", return_value=cycle),
                patch.object(SITE_SYNC, "DATA_DIR", root),
                patch.object(SITE_SYNC, "OUTPUTS", root),
                patch.object(SITE_SYNC, "ATLAS_SELF_HEALING_LATEST", healing),
                patch.object(SITE_SYNC, "ATLAS_IMPROVEMENTS_LATEST", improvements),
                patch.object(SITE_SYNC, "ATLAS_ALERTS_LATEST", alerts),
                patch.object(SITE_SYNC, "ATLAS_BACKUPS_LATEST", backup),
            ):
                ready = SITE_SYNC.publication_snapshot_readiness(
                    report_date, candidate_fingerprint=candidate
                )
                quality_payload["report_sha256"] = "0" * 64
                quality.write_text(json.dumps(quality_payload), encoding="utf-8")
                blocked_quality = SITE_SYNC.publication_snapshot_readiness(
                    report_date, candidate_fingerprint=candidate
                )
                quality_payload["report_sha256"] = SITE_SYNC.hashlib.sha256(raw_report).hexdigest()
                quality.write_text(json.dumps(quality_payload), encoding="utf-8")
                backup_payload["encrypted"] = False
                backup.write_text(json.dumps(backup_payload), encoding="utf-8")
                blocked_backup = SITE_SYNC.publication_snapshot_readiness(
                    report_date, candidate_fingerprint=candidate
                )
                backup_payload["encrypted"] = True
                backup_payload["schema_version"] = 3
                backup.write_text(json.dumps(backup_payload), encoding="utf-8")
                blocked_backup_schema = SITE_SYNC.publication_snapshot_readiness(
                    report_date, candidate_fingerprint=candidate
                )
                backup_payload["schema_version"] = 4
                backup.write_text(json.dumps(backup_payload), encoding="utf-8")
                healing_payload["deep"] = False
                healing.write_text(json.dumps(healing_payload), encoding="utf-8")
                blocked = SITE_SYNC.publication_snapshot_readiness(
                    report_date, candidate_fingerprint=candidate
                )
                healing_payload["deep"] = True
                healing.write_text(json.dumps(healing_payload), encoding="utf-8")
                cycle_payload["operational_gate_passed"] = False
                cycle.write_text(json.dumps(cycle_payload), encoding="utf-8")
                blocked_operational = SITE_SYNC.publication_snapshot_readiness(
                    report_date, candidate_fingerprint=candidate
                )

        self.assertTrue(ready["ready"])
        self.assertFalse(ready["evidence"]["cycle"]["releaseCandidatePassed"])
        self.assertFalse(ready["evidence"]["cycle"]["idempotentReplay"])
        self.assertFalse(ready["evidence"]["cycle"]["fullTestsExecuted"])
        self.assertFalse(ready["evidence"]["cycle"]["workspaceReproducible"])
        self.assertTrue(ready["evidence"]["dailyContentPublicationPassed"])
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
        self.assertFalse(blocked_backup_schema["ready"])
        self.assertTrue(any("schema version 4" in reason for reason in blocked_backup_schema["reasons"]))
        self.assertFalse(blocked["ready"])
        self.assertTrue(any("deep self-healing" in reason for reason in blocked["reasons"]))
        self.assertFalse(blocked_operational["ready"])
        self.assertTrue(any("cycle has not passed" in reason for reason in blocked_operational["reasons"]))

    def test_publication_status_accepts_verified_repairs_and_nonblocking_alerts(self) -> None:
        report_date = "2026-07-22"
        artifacts = {
            "cycle": {
                "date": report_date,
                "overall_passed": True,
                "operational_gate_passed": True,
                "stages": [
                    {"name": name, "status": "passed"}
                    for name in SITE_SYNC.DAILY_PUBLICATION_REQUIRED_CYCLE_STAGES
                ],
                "ledger": {"write_performed": True},
                "boundary": {"canonical_write_performed": True},
            },
            "selfHealing": {
                "date": report_date,
                "overall_status": "healthy",
                "deep": True,
                "strict": True,
                "counts": {"blocking": 0, "failed": 0, "unresolved": 0},
                "checks": [
                    {"check_id": "briefing_tests", "executed": True, "passed": True},
                    {"check_id": "site_quality", "executed": True, "passed": True},
                    {
                        "check_id": "drift_diagnostics_freshness",
                        "executed": True,
                        "passed": False,
                    },
                ],
            },
            "improvements": {"date": report_date, "counts": {"blocking": 0}},
            "alerts": {
                "date": report_date,
                "status": "attention_required",
                "finding_count": 1,
                "findings": [
                    {
                        "id": "ATLAS-IMP-NONBLOCKING",
                        "severity": "medium",
                        "status": "regressed",
                        "summary": "External delivery receipt remains unavailable",
                    }
                ],
                "requires_acknowledgement": False,
            },
            "backup": {
                "schema_version": SITE_SYNC.BACKUP_SCHEMA_VERSION,
                "date": report_date,
                "verified": True,
                "encrypted": True,
                "encryption_algorithm": "AES-256-GCM",
                "encrypted_container_authenticated": True,
                "archive_integrity_verified": True,
                "restore_verified": True,
                "restore_scope": "configured_workspace_files_and_git_bundles",
                "target_outside_workspace": True,
            },
        }

        result = SITE_SYNC._publication_snapshot_status_checks(
            report_date,
            artifacts=artifacts,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["reasons"], [])

        artifacts["alerts"]["findings"][0]["severity"] = "critical"
        blocked = SITE_SYNC._publication_snapshot_status_checks(
            report_date,
            artifacts=artifacts,
        )
        self.assertFalse(blocked["ready"])
        self.assertIn(
            "date-aligned alerts are missing or still require attention",
            blocked["reasons"],
        )

        artifacts["alerts"]["findings"][0]["severity"] = "medium"
        artifacts["alerts"]["finding_count"] = 2
        malformed = SITE_SYNC._publication_snapshot_status_checks(
            report_date,
            artifacts=artifacts,
        )
        self.assertFalse(malformed["ready"])

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

    def test_historical_portfolio_replay_never_overwrites_later_trade_cash_with_old_valuation(self) -> None:
        """A prior valuation may supply prices, but cannot erase replayed cash flows."""
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
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "paper_portfolio_us.json").write_text(
                json.dumps({"mode": "paper_trading", "as_of_date": "2026-06-30"}),
                encoding="utf-8",
            )
            (data_dir / "us.jsonl").write_text(
                json.dumps(
                    {
                        "date": "2026-06-18",
                        "action": "BUY",
                        "symbol": "AAPL",
                        "exchange": "NASDAQ",
                        "currency": "USD",
                        "quantity": 1,
                        "price": 5000,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            # This snapshot is older than the replayed purchase and therefore
            # must not restore cash to 100,000 while retaining the AAPL position.
            (data_dir / "paper_valuations_us.jsonl").write_text(
                json.dumps({"date": "2026-06-14", "cash": 100000, "equity": 100000, "price_snapshot": []})
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(SITE_SYNC, "ROOT", root),
                patch.object(SITE_SYNC, "DATA_DIR", data_dir),
                patch.object(SITE_SYNC, "PAPER_CONFIG_PATH", config_path),
            ):
                reconstructed = SITE_SYNC.portfolio("US", "2026-06-20")

        self.assertEqual(reconstructed["asOf"], "2026-06-20")
        self.assertEqual(reconstructed["valuationAsOf"], "2026-06-18")
        self.assertTrue(reconstructed["valuationIsStale"])
        self.assertEqual(reconstructed["cash"], 95000)
        self.assertEqual(reconstructed["equity"], 100000)
        self.assertEqual(reconstructed["cashPct"], 95)
        self.assertEqual(
            {item["label"]: item["pct"] for item in reconstructed["allocations"]},
            {"现金": 95.0, "AAPL": 5.0, "其他": 0.0},
        )
        self.assertEqual(sum(item["pct"] for item in reconstructed["allocations"]), 100.0)

    def test_public_portfolio_exposes_account_totals_but_not_ledger_details(self) -> None:
        public = SITE_SYNC.public_portfolio(
            {
                "name": "US 虚拟组合",
                "value": "99,008.90",
                "return": "-0.99%",
                "returnPct": -0.99,
                "asOf": "2026-07-16",
                "valuationAsOf": "2026-07-15",
                "valuationIsStale": True,
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
        self.assertEqual(public["valuationAsOf"], "2026-07-15")
        self.assertTrue(public["valuationIsStale"])
        self.assertEqual(public["allocations"][1]["label"], "匿名资产 1")
        for field in ("accountId", "positions", "realizedPnl", "initialCash", "previousEquity"):
            self.assertNotIn(field, public)
