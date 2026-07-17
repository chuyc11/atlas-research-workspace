from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_quality.py"
SPEC = importlib.util.spec_from_file_location("research_quality_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

STORE_SPEC = importlib.util.spec_from_file_location(
    "briefing_store_contract_test_module",
    SCRIPT.parent / "briefing_store.py",
)
assert STORE_SPEC and STORE_SPEC.loader
STORE = importlib.util.module_from_spec(STORE_SPEC)
sys.modules[STORE_SPEC.name] = STORE
STORE_SPEC.loader.exec_module(STORE)


def v2_prediction(prediction_id: str = "2026-07-12-P01", probability: float = 0.8) -> dict:
    return {
        "schema_version": 2,
        "prediction_id": prediction_id,
        "date": "2026-07-12",
        "deadline": "2026-07-13",
        "horizon": "1d",
        "scenario": "A precisely resolvable event occurs",
        "probability": probability,
        "status": "open",
        "trigger": "An official release",
        "verification_signals": ["official release"],
        "falsification_signals": ["official denial"],
        "resolution": {
            "question": "Did the event occur by the deadline?",
            "success_criteria": "The official release confirms it.",
            "failure_criteria": "No confirming release exists by the deadline.",
        },
        "evidence": [{"source": "Official", "url": "https://official.example/source"}],
        "tickers": [{"symbol": "TEST"}],
        "market_mapping": [
            {
                "symbol": "TEST",
                "direction": "outperform",
                "benchmark": "SPY",
                "verification_rule": "TEST return exceeds SPY return by the deadline",
                "evaluation_deadline": "2026-07-13",
            }
        ],
    }


def resolved_review(prediction_id: str = "2026-07-12-P01", outcome: int = 1) -> dict:
    return {
        "prediction_id": prediction_id,
        "date": "2026-07-14",
        "status": "validated" if outcome else "wrong",
        "review": {
            "review_date": "2026-07-14",
            "observed_outcome": outcome,
            "evidence": [{"source": "Official", "url": "https://official.example/result"}],
        },
    }


def market_review(
    prediction_id: str = "2026-07-12-P01",
    deadline: str = "2026-07-13",
    review_date: str = "2026-07-14",
) -> dict:
    return {
        "prediction_id": prediction_id,
        "date": review_date,
        "status": "active",
        "review": {
            "resolution_scope": "market",
            "review_date": review_date,
            "market_resolution": [{
                "symbol": "TEST",
                "benchmark": "SPY",
                "evaluation_deadline": deadline,
                "status": "resolved",
                "observed_outcome": 1,
                "symbol_return_pct": 2.0,
                "benchmark_return_pct": 1.0,
                "excess_return_pct": 1.0,
                "start_price_date": "2026-07-10",
                "end_price_date": deadline,
                "evidence": [{"source": "Official market data", "url": "https://market.example/result"}],
            }],
        },
    }


class ResearchQualityTests(unittest.TestCase):
    def test_v2_contract_accepts_pre_registered_numeric_forecast(self) -> None:
        self.assertEqual(MODULE.validate_v2_prediction(v2_prediction()), [])

    def test_v2_contract_rejects_categorical_probability_and_missing_resolution(self) -> None:
        row = v2_prediction()
        row["probability"] = "high"
        row.pop("resolution")

        errors = MODULE.validate_v2_prediction(row)

        self.assertTrue(any("probability must be numeric" in item for item in errors))
        self.assertTrue(any("resolution object is required" in item for item in errors))

    def test_v2_contract_requires_complete_session_aligned_market_mapping(self) -> None:
        row = v2_prediction()
        row["tickers"].append({"symbol": "MISSING"})
        row["market_mapping"][0]["direction"] = "strong_buy"
        row["market_mapping"][0].pop("evaluation_deadline")

        errors = MODULE.validate_v2_prediction(row)

        self.assertTrue(any("direction is invalid" in item for item in errors))
        self.assertTrue(any("evaluation_deadline" in item for item in errors))
        self.assertTrue(any("ticker MISSING has no market_mapping" in item for item in errors))

    def test_machine_market_evaluation_is_required_only_after_its_enforcement_date(self) -> None:
        row = v2_prediction()
        errors = MODULE.validate_v2_prediction(
            row,
            machine_evaluation_enforce_from_date="2026-07-12",
        )
        row["market_mapping"][0]["evaluation"] = {
            "metric": "total_return",
            "window_start": "2026-07-13",
            "price_field": "adjusted_close",
            "comparison": "symbol_gt_benchmark",
        }

        self.assertTrue(any("evaluation is required" in item for item in errors))
        self.assertEqual(
            MODULE.validate_v2_prediction(row, machine_evaluation_enforce_from_date="2026-07-12"),
            [],
        )
        row["market_mapping"][0]["evaluation"]["comparison"] = "symbol_lt_benchmark"
        mismatch = MODULE.validate_v2_prediction(row, machine_evaluation_enforce_from_date="2026-07-12")
        self.assertTrue(any("conflicts with direction" in item for item in mismatch))

    def test_proper_scoring_excludes_legacy_labels_and_scores_resolved_numeric_predictions(self) -> None:
        rows = [
            v2_prediction(probability=0.9),
            resolved_review(),
            {
                "prediction_id": "2026-07-12-P02",
                "date": "2026-07-12",
                "horizon": "1d",
                "probability": "high",
                "status": "open",
            },
            {
                "prediction_id": "2026-07-12-P02",
                "date": "2026-07-13",
                "status": "validated",
                "review": {
                    "review_date": "2026-07-14",
                    "observed_outcome": 1,
                    "evidence": [{"url": "https://example.test/result"}],
                },
            },
        ]

        metrics = MODULE.proper_scoring_metrics(rows, cutoff="2026-07-14", minimum_sample=1)

        self.assertEqual(metrics["eligible_sample_count"], 1)
        self.assertEqual(metrics["brier_score"], 0.01)
        self.assertTrue(metrics["is_research_ready"])
        self.assertTrue(metrics["gates"]["resolved_coverage_at_least_80pct"])
        self.assertEqual(metrics["legacy_matured_prediction_count_excluded"], 1)

    def test_rolling_forecasts_share_one_independent_event_family_weight(self) -> None:
        first = v2_prediction("2026-07-12-P01", probability=0.9)
        second = v2_prediction("2026-07-13-P01", probability=0.7)
        second["date"] = "2026-07-13"
        second["deadline"] = "2026-07-14"
        second["market_mapping"][0]["evaluation_deadline"] = "2026-07-14"
        first_review = resolved_review("2026-07-12-P01", outcome=1)
        second_review = resolved_review("2026-07-13-P01", outcome=1)
        second_review["date"] = "2026-07-15"
        second_review["review"]["review_date"] = "2026-07-15"
        registry = {"event_families": {"ROLLING_EVENT": [first["prediction_id"], second["prediction_id"]]}}

        metrics = MODULE.proper_scoring_metrics(
            [first, first_review, second, second_review],
            cutoff="2026-07-15",
            minimum_sample=2,
            family_registry=registry,
        )

        self.assertEqual(metrics["eligible_sample_count"], 2)
        self.assertEqual(metrics["independent_event_family_count"], 1)
        self.assertEqual(metrics["rolling_restatement_count_downweighted"], 1)
        self.assertFalse(metrics["gates"]["minimum_sample"])
        self.assertAlmostEqual(metrics["brier_score"], 0.05)

    def test_asset_only_prediction_is_excluded_from_event_calibration(self) -> None:
        original = v2_prediction()
        original["scenario"] = "TEST total return exceeds SPY total return by the deadline"
        original["resolution"] = {
            "question": "Did TEST total return exceed SPY total return by the deadline?",
            "success_criteria": "TEST return is greater than SPY return.",
            "failure_criteria": "TEST return is not greater than SPY return.",
        }

        metrics = MODULE.proper_scoring_metrics(
            [original, resolved_review()],
            cutoff="2026-07-14",
            minimum_sample=1,
        )

        self.assertEqual(metrics["matured_prediction_count"], 0)
        self.assertEqual(metrics["asset_only_matured_prediction_count_excluded"], 1)
        self.assertEqual(metrics["eligible_sample_count"], 0)
        self.assertEqual(metrics["exclusion_counts"]["asset_only_prediction"], 1)

    def test_asset_only_event_contract_is_rejected_after_separation_enforcement(self) -> None:
        row = v2_prediction("2026-07-15-P01")
        row["date"] = "2026-07-15"
        row["deadline"] = "2026-07-16"
        row["market_mapping"][0]["evaluation_deadline"] = "2026-07-16"
        row["scenario"] = "TEST return will outperform SPY return"
        row["resolution"] = {
            "question": "Did TEST return outperform SPY return?",
            "success_criteria": "TEST return exceeds SPY return.",
            "failure_criteria": "TEST return does not exceed SPY return.",
        }

        errors = MODULE.validate_v2_prediction(
            row,
            event_asset_separation_enforce_from_date="2026-07-15",
        )

        self.assertTrue(any("event resolution duplicates a market-mapping outcome" in error for error in errors))

    def test_forward_contract_requires_novelty_family_and_reproducible_market_evidence(self) -> None:
        row = v2_prediction("2026-07-15-P01")
        row["date"] = "2026-07-15"
        row["deadline"] = "2026-07-16"
        row["market_mapping"][0]["evaluation_deadline"] = "2026-07-16"
        row["evidence"] = [{"source": "Tencent market quote", "url": "https://qt.gtimg.cn/"}]

        errors = MODULE.validate_v2_prediction(
            row,
            independence_enforce_from_date="2026-07-15",
            evidence_reproducibility_enforce_from_date="2026-07-15",
        )

        self.assertTrue(any("event_family_id" in error for error in errors))
        self.assertTrue(any("direct auditable page" in error for error in errors))
        self.assertTrue(any("artifact_sha256" in error for error in errors))
        self.assertTrue(any("market_thesis_id" in error for error in errors))

    def test_early_review_without_terminal_evidence_is_ineligible(self) -> None:
        original = v2_prediction()
        original["deadline"] = "2026-07-19"
        original["horizon"] = "1w"
        review = resolved_review()

        metrics = MODULE.proper_scoring_metrics([original, review], cutoff="2026-07-20", minimum_sample=1)

        self.assertEqual(metrics["eligible_sample_count"], 0)
        self.assertEqual(metrics["exclusion_counts"]["early_closure_without_terminal_evidence"], 1)

    def test_report_gate_checks_sources_sections_and_thesis_layers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            layers = "\n".join(
                "- **结论：** x\n- **硬证据：** x\n- **机制：** x\n- **反证：** x\n- **证伪：** x"
                for _ in range(3)
            )
            path.write_text(
                "# 核心摘要\n## 昨日预测复盘\n## 今日预测与市场映射\n## 风险信号\n## 来源与质量\n"
                + layers
                + "\n[A](https://a.example/x) [B](https://b.example/y)",
                encoding="utf-8",
            )
            result = MODULE.audit_report(
                path,
                {
                    "minimum_report_characters": 10,
                    "maximum_report_characters": 10000,
                    "minimum_distinct_links": 2,
                    "minimum_distinct_domains": 2,
                },
                enforce=True,
            )

        self.assertTrue(result["passed"])
        self.assertEqual(result["distinct_domain_count"], 2)
        self.assertEqual(result["thesis_layer_counts"]["falsification_signal"], 3)

    def test_missing_enforced_report_is_an_explicit_operational_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "missing.md"
            audit = MODULE.audit_report(report, {}, enforce=True)
            payload = MODULE.build_quality_report(
                date="2026-07-14",
                records=[v2_prediction(), resolved_review(), market_review()],
                report_path=report,
                settings={
                    "prediction_contract": {"enforce_from_date": "2026-07-12"},
                    "research_evaluation": {"minimum_sample": 1},
                    "news_research_policy": {"enforce_from_date": "2026-07-12"},
                },
            )

        self.assertTrue(audit["enforced"])
        self.assertFalse(audit["passed"])
        self.assertIn("dated report is missing", payload["blocking_reasons"])
        self.assertIsNone(payload["report_sha256"])

    def test_report_gate_applies_five_layer_and_source_roles_only_to_marked_core_stories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text(
                "## 2026-07-15 每日全球晨间简报\n"
                "## 核心摘要\n## 昨日预测复盘\n## 今日预测与市场映射\n## 风险信号\n## 来源与质量\n"
                "## 政治与外交\n### 核心主线：可审计主线\n"
                "- **结论：** x\n- **硬证据：** x\n- **机制：** x\n- **反证：** x\n- **证伪：** x\n"
                "- **证据角色：** 一手来源=[Gov](https://agency.gov/release); 事件地区来源=[Local](https://local.example/story); 外部核验=[Wire](https://wire.example/check)\n"
                "## 科技与AI\n这一覆盖段落不需要伪装成第二条核心投资主线。[Source](https://tech.example/story)\n",
                encoding="utf-8",
            )
            policy = {
                "story_evidence_enforce_from_date": "2026-07-15",
                "primary_thesis_min_items": 1,
                "primary_thesis_max_items": 5,
                "minimum_sources_per_core_story": 3,
                "minimum_independent_domains_per_core_story": 2,
                "require_primary_source_for_high_impact_story": True,
                "quality_gate": {
                    "minimum_report_characters": 10,
                    "maximum_report_characters": 10000,
                    "minimum_distinct_links": 3,
                    "minimum_distinct_domains": 3,
                    "primary_domain_suffixes": [".gov"],
                },
            }
            result = MODULE.audit_report(path, policy, enforce=True)

        self.assertTrue(result["passed"])
        self.assertEqual(result["primary_thesis_count"], 1)
        self.assertEqual(result["source_role_coverage_pct"], 100.0)
        self.assertTrue(result["all_core_stories_passed"])

    def test_report_gate_cannot_split_one_host_with_userinfo_or_ports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text(
                "## 2026-07-15 每日全球晨间简报\n"
                "## 核心摘要\n## 昨日预测复盘\n## 今日预测与市场映射\n## 风险信号\n## 来源与质量\n"
                "## 政治与外交\n### 核心主线：域名不可伪造拆分\n"
                "- **结论：** x\n- **硬证据：** x\n- **机制：** x\n- **反证：** x\n- **证伪：** x\n"
                "- **证据角色：** 一手来源=[A](https://agency.gov/a); "
                "事件地区来源=[B](https://agency.gov:443/b); "
                "外部核验=[C](https://user@agency.gov/c)\n",
                encoding="utf-8",
            )
            policy = {
                "story_evidence_enforce_from_date": "2026-07-15",
                "primary_thesis_min_items": 1,
                "primary_thesis_max_items": 5,
                "minimum_sources_per_core_story": 3,
                "minimum_independent_domains_per_core_story": 3,
                "require_primary_source_for_high_impact_story": True,
                "quality_gate": {
                    "minimum_report_characters": 10,
                    "maximum_report_characters": 10000,
                    "minimum_distinct_links": 3,
                    "minimum_distinct_domains": 3,
                    "primary_domain_suffixes": [".gov"],
                },
            }

            result = MODULE.audit_report(path, policy, enforce=True)

        story = result["core_story_audits"][0]
        self.assertFalse(result["passed"])
        self.assertEqual(story["independent_domain_count"], 1)
        self.assertEqual(story["independent_source_family_count"], 1)
        self.assertFalse(story["role_coverage"]["primary"])
        self.assertFalse(story["role_coverage"]["event_region"])
        self.assertFalse(story["role_coverage"]["external_verification"])
        self.assertFalse(MODULE.direct_auditable_url("https://user@agency.gov/c"))
        self.assertFalse(MODULE.direct_auditable_url("https://agency.gov:444/c"))
        self.assertTrue(MODULE.direct_auditable_url("https://agency.gov:443/c"))

    def test_report_gate_rejects_unassigned_body_links_as_evidence_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text(
                "## 2026-07-15 每日全球晨间简报\n"
                "## 核心摘要\n## 昨日预测复盘\n## 今日预测与市场映射\n## 风险信号\n## 来源与质量\n"
                "## 政治与外交\n### 核心主线：角色不可由正文链接代替\n"
                "- **结论：** x\n- **硬证据：** x\n- **机制：** x\n- **反证：** x\n- **证伪：** x\n"
                "- **证据角色：** 一手来源=[Gov](https://agency.gov/release); 事件地区来源=无; 外部核验=无\n"
                "正文另有两个链接，但未被明确分配证据角色。[Local](https://local.example/story) "
                "[Wire](https://wire.example/check)\n",
                encoding="utf-8",
            )
            policy = {
                "story_evidence_enforce_from_date": "2026-07-15",
                "primary_thesis_min_items": 1,
                "primary_thesis_max_items": 5,
                "minimum_sources_per_core_story": 3,
                "minimum_independent_domains_per_core_story": 2,
                "require_primary_source_for_high_impact_story": True,
                "quality_gate": {
                    "minimum_report_characters": 10,
                    "maximum_report_characters": 10000,
                    "minimum_distinct_links": 3,
                    "minimum_distinct_domains": 3,
                    "primary_domain_suffixes": [".gov"],
                },
            }
            result = MODULE.audit_report(path, policy, enforce=True)

        self.assertFalse(result["passed"])
        self.assertIn(
            "missing linked evidence roles: event_region, external_verification",
            result["core_story_audits"][0]["errors"],
        )

    def test_storage_rejects_noncompliant_post_enforcement_prediction_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text("", encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}),
                encoding="utf-8",
            )
            invalid = v2_prediction()
            invalid["probability"] = "high"
            input_path = root / "input.json"
            input_path.write_text(json.dumps(invalid), encoding="utf-8")
            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                with self.assertRaisesRegex(ValueError, "probability must be numeric"):
                    STORE.append_prediction_records(input_path, "2026-07-12", predictions)

            self.assertEqual(predictions.read_text(encoding="utf-8"), "")

    def test_storage_rejects_nonstandard_nan_before_prediction_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text("", encoding="utf-8")
            input_path = root / "input.json"
            input_path.write_text('{"prediction_id":"P1","probability":NaN}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "non-standard numeric constant NaN"):
                STORE.append_prediction_records(input_path, "2026-07-12", predictions)

            self.assertEqual(predictions.read_text(encoding="utf-8"), "")

    def test_quality_reader_rejects_nonstandard_numeric_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            predictions = Path(directory) / "predictions.jsonl"
            predictions.write_text('{"prediction_id":"P1","probability":NaN}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "non-standard numeric constant NaN"):
                MODULE.read_jsonl(predictions)

    def test_storage_allows_legacy_review_after_v2_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text(
                json.dumps(
                    {
                        "prediction_id": "2026-07-11-P01",
                        "date": "2026-07-11",
                        "horizon": "1d",
                        "probability": "high",
                        "status": "open",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}),
                encoding="utf-8",
            )
            review = {
                "prediction_id": "2026-07-11-P01",
                "date": "2026-07-12",
                "status": "partial",
                "review": {"review_date": "2026-07-12", "why": "legacy review"},
            }
            input_path = root / "review.json"
            input_path.write_text(json.dumps(review), encoding="utf-8")
            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                count = STORE.append_prediction_records(input_path, "2026-07-12", predictions)

            self.assertEqual(count, 1)
            self.assertEqual(len(predictions.read_text(encoding="utf-8").splitlines()), 2)

    def test_storage_rejects_payload_date_that_differs_from_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text("", encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}),
                encoding="utf-8",
            )
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps({
                    "prediction_id": "BACKDATED",
                    "date": "2026-07-11",
                    "status": "open",
                    "scenario": "legacy contract bypass",
                }),
                encoding="utf-8",
            )

            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                with self.assertRaisesRegex(ValueError, "must equal run date"):
                    STORE.append_prediction_records(input_path, "2026-07-17", predictions)

            self.assertEqual(predictions.read_text(encoding="utf-8"), "")

    def test_storage_serializes_concurrent_idempotent_appends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text("", encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}),
                encoding="utf-8",
            )
            first = root / "first.json"
            second = root / "second.json"
            payload = v2_prediction()
            first.write_text(json.dumps(payload), encoding="utf-8")
            second.write_text(json.dumps(payload), encoding="utf-8")

            def append(path: Path) -> int:
                return STORE.append_prediction_records(path, "2026-07-12", predictions)

            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                counts = sorted(executor.map(append, (first, second)))

            self.assertEqual(counts, [0, 1])
            rows = [json.loads(line) for line in predictions.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["prediction_id"], payload["prediction_id"])

    def test_due_review_queue_scans_full_ledger_and_does_not_close_same_day_early(self) -> None:
        overdue = v2_prediction("2026-07-11-P01")
        overdue["date"] = "2026-07-11"
        overdue["deadline"] = "2026-07-12"
        overdue["market_mapping"][0]["evaluation_deadline"] = "2026-07-12"
        matures_today = v2_prediction("2026-07-12-P01")
        future = v2_prediction("2026-07-12-P02")
        future["deadline"] = "2026-07-20"
        future["market_mapping"][0]["evaluation_deadline"] = "2026-07-20"

        queue = STORE.due_review_queue("2026-07-13", [overdue, matures_today, future])

        self.assertEqual(queue["counts"]["due_reviews"], 1)
        self.assertEqual(queue["counts"]["matures_today"], 1)
        self.assertEqual(queue["counts"]["open_not_due"], 1)
        self.assertEqual(queue["due_reviews"][0]["prediction_id"], "2026-07-11-P01")

    def test_due_review_queue_uses_one_canonical_row_per_prediction_id(self) -> None:
        first = v2_prediction("2026-07-11-P01")
        first["date"] = "2026-07-11"
        first["deadline"] = "2026-07-12"
        first["market_mapping"][0]["evaluation_deadline"] = "2026-07-12"
        duplicate = dict(first)
        duplicate["scenario"] = "conflicting duplicate that must not consume another queue slot"

        queue = STORE.due_review_queue("2026-07-13", [first, duplicate])

        self.assertEqual(queue["counts"]["due_reviews"], 1)
        self.assertEqual(queue["counts"]["duplicate_original_ids"], 1)
        self.assertEqual(queue["due_reviews"][0]["prediction_id"], "2026-07-11-P01")
        self.assertEqual(queue["due_reviews"][0]["duplicate_original_count"], 2)

    def test_due_review_queue_keeps_invalid_early_review_open_but_skips_mature_review(self) -> None:
        original = v2_prediction()
        original["deadline"] = "2026-07-19"
        original["market_mapping"][0]["evaluation_deadline"] = "2026-07-17"
        early = resolved_review()
        mature = resolved_review()
        mature["date"] = "2026-07-20"
        mature["review"]["review_date"] = "2026-07-20"
        market = market_review(deadline="2026-07-17", review_date="2026-07-20")
        market["review"]["market_resolution"][0].update(
            {
                "symbol_start_price": 100.0,
                "symbol_end_price": 102.0,
                "benchmark_start_price": 100.0,
                "benchmark_end_price": 101.0,
            }
        )

        early_queue = STORE.due_review_queue("2026-07-20", [original, early])
        mature_queue = STORE.due_review_queue("2026-07-20", [original, mature])
        fully_resolved_queue = STORE.due_review_queue(
            "2026-07-20",
            [original, mature, market],
        )

        self.assertEqual(early_queue["counts"]["due_reviews"], 1)
        self.assertEqual(mature_queue["counts"]["due_reviews"], 1)
        self.assertEqual(mature_queue["due_reviews"][0]["event_resolution_status"], "resolved")
        self.assertEqual(fully_resolved_queue["counts"]["due_reviews"], 0)
        self.assertEqual(fully_resolved_queue["counts"]["closed"], 1)

    def test_market_only_review_does_not_close_or_score_event(self) -> None:
        original = v2_prediction()
        market = market_review()

        event_metrics = MODULE.proper_scoring_metrics([original, market], cutoff="2026-07-14", minimum_sample=1)
        mapping_metrics = MODULE.market_mapping_metrics([original, market], cutoff="2026-07-14")

        self.assertEqual(event_metrics["eligible_sample_count"], 0)
        self.assertEqual(event_metrics["exclusion_counts"]["missing_review"], 1)
        self.assertEqual(mapping_metrics["resolved_mapping_count"], 1)
        self.assertEqual(mapping_metrics["hit_rate_pct"], 100.0)
        self.assertEqual(mapping_metrics["mean_signed_performance_pct"], 1.0)

    def test_reciprocal_market_mappings_count_as_one_independent_thesis(self) -> None:
        original = v2_prediction()
        original["tickers"] = [{"symbol": "TEST"}, {"symbol": "SPY"}]
        original["market_mapping"].append({
            "symbol": "SPY",
            "direction": "underperform",
            "benchmark": "TEST",
            "verification_rule": "SPY return is below TEST return by the deadline",
            "evaluation_deadline": "2026-07-13",
        })
        reciprocal_review = market_review()
        reciprocal_review["review"]["market_resolution"].append({
            "symbol": "SPY",
            "benchmark": "TEST",
            "evaluation_deadline": "2026-07-13",
            "status": "resolved",
            "observed_outcome": 1,
            "symbol_return_pct": 1.0,
            "benchmark_return_pct": 2.0,
            "excess_return_pct": -1.0,
            "start_price_date": "2026-07-10",
            "end_price_date": "2026-07-13",
            "evidence": [{"source": "Official market data", "url": "https://market.example/result"}],
        })

        metrics = MODULE.market_mapping_metrics([original, reciprocal_review], cutoff="2026-07-14")

        self.assertEqual(metrics["matured_mapping_count"], 1)
        self.assertEqual(metrics["resolved_mapping_count"], 1)
        self.assertEqual(metrics["reciprocal_duplicate_mapping_count_excluded"], 1)

    def test_rolling_market_mappings_use_registry_as_one_thesis(self) -> None:
        first = v2_prediction("2026-07-12-P01")
        second = v2_prediction("2026-07-13-P01")
        second["date"] = "2026-07-13"
        second["deadline"] = "2026-07-14"
        second["market_mapping"][0]["evaluation_deadline"] = "2026-07-14"
        registry = {
            "market_theses": {
                "ROLLING_TEST_RELATIVE": ["2026-07-12-P01|TEST", "2026-07-13-P01|TEST"]
            }
        }

        metrics = MODULE.market_mapping_metrics(
            [first, second], cutoff="2026-07-15", family_registry=registry
        )

        self.assertEqual(metrics["matured_mapping_count"], 1)
        self.assertEqual(metrics["reciprocal_duplicate_mapping_count_excluded"], 1)

    def test_combined_review_validates_event_and_mapping_dimensions_independently(self) -> None:
        original = v2_prediction()
        original["market_mapping"][0]["evaluation"] = {
            "metric": "total_return",
            "window_start": "2026-07-13",
            "price_field": "adjusted_close",
            "comparison": "symbol_gt_benchmark",
        }
        combined = resolved_review()
        combined["review"]["resolution_scope"] = "combined"
        combined["review"]["market_resolution"] = market_review()["review"]["market_resolution"]

        self.assertEqual(MODULE.validate_v2_review(combined, original), [])
        combined["review"]["market_resolution"][0]["observed_outcome"] = 0
        errors = MODULE.validate_v2_review(combined, original)
        self.assertTrue(any("outcome conflicts" in item for item in errors))

    def test_market_resolution_rejects_weekend_baseline_and_wrong_window(self) -> None:
        original = v2_prediction()
        review = market_review()
        review["review"]["market_resolution"][0]["start_price_date"] = "2026-07-12"

        errors = MODULE.validate_v2_review(review, original)

        self.assertTrue(any("not a comparable market session" in item for item in errors))
        self.assertTrue(any("last comparable session" in item for item in errors))

    def test_later_valid_append_only_market_correction_supersedes_blocking_error(self) -> None:
        original = v2_prediction()
        invalid = market_review(review_date="2026-07-15")
        invalid["review"]["market_resolution"][0]["start_price_date"] = "2026-07-12"
        corrected = market_review(review_date="2026-07-16")
        corrected["review"]["market_resolution"][0].update(
            {
                "symbol_start_price": 100.0,
                "symbol_end_price": 102.0,
                "benchmark_start_price": 100.0,
                "benchmark_end_price": 101.0,
            }
        )
        result = MODULE.audit_prediction_records(
            [original, invalid, corrected],
            cutoff="2026-07-16",
            enforce_from_date="2026-07-12",
            review_policy={
                "block_deployment_on_unresolved_due_v2_market": True,
                "block_deployment_on_unresolved_due_v2_market_from_date": "2026-07-15",
                "market_resolution_price_recompute_enforce_from_date": "2026-07-16",
            },
        )

        self.assertTrue(result["operational_passed"])
        self.assertEqual(result["v2_contract"]["review_errors"], [])
        self.assertTrue(result["v2_contract"]["superseded_review_errors"])
        self.assertEqual(result["unresolved_matured_v2_market_mapping_ids"], [])

        historical = MODULE.audit_prediction_records(
            [original, invalid, corrected],
            cutoff="2026-07-15",
            enforce_from_date="2026-07-12",
            review_policy={
                "market_resolution_price_recompute_enforce_from_date": "2026-07-16",
            },
        )
        self.assertFalse(historical["operational_passed"])
        self.assertTrue(historical["v2_contract"]["review_errors"])

    def test_explicit_revision_cutoff_uses_later_append_only_correction_without_rewriting_history(self) -> None:
        original = v2_prediction()
        invalid = market_review(review_date="2026-07-15")
        invalid["review"]["market_resolution"][0]["start_price_date"] = "2026-07-12"
        corrected = market_review(review_date="2026-07-16")
        corrected["review"]["market_resolution"][0].update(
            {
                "symbol_start_price": 100.0,
                "symbol_end_price": 102.0,
                "benchmark_start_price": 100.0,
                "benchmark_end_price": 101.0,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.md"
            report.write_text("## 2026-07-15 每日全球晨间简报\n修订日期：2026-07-16\n", encoding="utf-8")
            report_sha256 = MODULE.hashlib.sha256(report.read_bytes()).hexdigest()
            payload = MODULE.build_quality_report(
                date="2026-07-15",
                evaluation_cutoff="2026-07-16",
                records=[original, invalid, corrected],
                report_path=report,
                settings={
                    "prediction_contract": {"enforce_from_date": "2026-07-12"},
                    "review_queue": {
                        "block_deployment_on_unresolved_due_v2_market": True,
                        "block_deployment_on_unresolved_due_v2_market_from_date": "2026-07-15",
                        "market_resolution_price_recompute_enforce_from_date": "2026-07-16",
                    },
                    "research_evaluation": {"minimum_sample": 30},
                    "news_research_policy": {"enforce_from_date": "9999-12-31"},
                },
            )

        self.assertTrue(payload["revised"])
        self.assertEqual(payload["report_sha256"], report_sha256)
        self.assertEqual(payload["evaluation_cutoff"], "2026-07-16")
        self.assertTrue(payload["operational_passed"])
        self.assertFalse(payload["research_ready"])
        self.assertTrue(payload["prediction_audit"]["v2_contract"]["superseded_review_errors"])

    def test_market_resolution_recomputes_returns_from_raw_prices_after_enforcement(self) -> None:
        original = v2_prediction()
        original["market_mapping"][0]["evaluation"] = {
            "metric": "total_return",
            "window_start": "2026-07-13",
            "price_field": "adjusted_close",
            "comparison": "symbol_gt_benchmark",
        }
        review = market_review(review_date="2026-07-16")
        result = review["review"]["market_resolution"][0]
        result.update(
            {
                "price_field": "adjusted_close",
                "symbol_start_price": 100.0,
                "symbol_end_price": 102.0,
                "benchmark_start_price": 100.0,
                "benchmark_end_price": 101.0,
            }
        )

        self.assertEqual(MODULE.validate_v2_review(review, original), [])
        result["symbol_return_pct"] = 9.0
        errors = MODULE.validate_v2_review(review, original)
        self.assertTrue(any("recomputed raw-price return" in item for item in errors))

    def test_same_day_review_requires_terminal_evidence_under_end_of_day_deadlines(self) -> None:
        original = v2_prediction()
        same_day = resolved_review()
        same_day["date"] = "2026-07-13"
        same_day["review"]["review_date"] = "2026-07-13"

        errors = MODULE.validate_v2_review(same_day, original)
        same_day["review"]["terminal_evidence"] = True

        self.assertTrue(any("premature review" in item for item in errors))
        self.assertEqual(MODULE.validate_v2_review(same_day, original), [])

    def test_storage_skips_duplicate_review_identity_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = v2_prediction()
            review = resolved_review()
            predictions = root / "predictions.jsonl"
            predictions.write_text(
                json.dumps(original, ensure_ascii=False) + "\n" + json.dumps(review, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            input_path = root / "review.json"
            input_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}), encoding="utf-8")

            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                count = STORE.append_prediction_records(input_path, "2026-07-14", predictions)

            self.assertEqual(count, 0)
            self.assertEqual(len(predictions.read_text(encoding="utf-8").splitlines()), 2)

    def test_storage_rejects_conflicting_review_with_same_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = v2_prediction()
            review = resolved_review()
            predictions = root / "predictions.jsonl"
            original_text = (
                json.dumps(original, ensure_ascii=False)
                + "\n"
                + json.dumps(review, ensure_ascii=False)
                + "\n"
            )
            predictions.write_text(original_text, encoding="utf-8")
            conflicting = json.loads(json.dumps(review))
            conflicting["review"]["notes"] = "same identity, different evidence"
            input_path = root / "review.json"
            input_path.write_text(json.dumps(conflicting, ensure_ascii=False), encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"prediction_contract": {"enforce_from_date": "2026-07-12"}}),
                encoding="utf-8",
            )

            with (
                patch.object(STORE, "DEFAULT_SETTINGS", settings),
                patch.object(STORE, "ensure_files", side_effect=lambda **_: None),
            ):
                with self.assertRaisesRegex(ValueError, "already exists with different content"):
                    STORE.append_prediction_records(input_path, "2026-07-14", predictions)

            self.assertEqual(predictions.read_text(encoding="utf-8"), original_text)

    def test_unresolved_matured_v2_blocks_only_after_deadline_day(self) -> None:
        original = v2_prediction()
        on_deadline = MODULE.audit_prediction_records(
            [original],
            cutoff="2026-07-13",
            enforce_from_date="2026-07-12",
            review_policy={"block_deployment_on_unresolved_due_v2": True},
        )
        after_deadline = MODULE.audit_prediction_records(
            [original],
            cutoff="2026-07-14",
            enforce_from_date="2026-07-12",
            review_policy={"block_deployment_on_unresolved_due_v2": True},
        )
        resolved = MODULE.audit_prediction_records(
            [original, resolved_review()],
            cutoff="2026-07-14",
            enforce_from_date="2026-07-12",
            review_policy={"block_deployment_on_unresolved_due_v2": True},
        )

        self.assertTrue(on_deadline["operational_passed"])
        self.assertFalse(after_deadline["operational_passed"])
        self.assertEqual(after_deadline["unresolved_matured_v2_prediction_ids"], ["2026-07-12-P01"])
        self.assertTrue(resolved["operational_passed"])

    def test_unresolved_market_mapping_gate_has_explicit_forward_cutoff(self) -> None:
        original = v2_prediction()
        policy = {
            "block_deployment_on_unresolved_due_v2_market": True,
            "block_deployment_on_unresolved_due_v2_market_from_date": "2026-07-15",
        }

        shadow = MODULE.audit_prediction_records(
            [original], cutoff="2026-07-14", enforce_from_date="2026-07-12", review_policy=policy
        )
        enforced = MODULE.audit_prediction_records(
            [original], cutoff="2026-07-15", enforce_from_date="2026-07-12", review_policy=policy
        )

        self.assertTrue(shadow["operational_passed"])
        self.assertFalse(enforced["operational_passed"])
        self.assertTrue(any("market mappings" in error for error in enforced["operational_errors"]))


if __name__ == "__main__":
    unittest.main()
