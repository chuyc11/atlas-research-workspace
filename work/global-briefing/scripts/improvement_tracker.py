#!/usr/bin/env python3
"""Track retrospective recommendations through cross-run verification.

Each recommendation becomes a durable action with a stable id, due date,
acceptance rule, evidence, and lifecycle. A later run verifies the action;
regressions reopen previously verified actions. Only explicitly allowlisted
low-risk derived artifacts may be repaired automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date as Date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
OUTPUTS_ROOT = ROOT / "outputs"
SITE_DATA = ROOT / "src" / "app" / "briefing.generated.json"
CONFIG_PATH = BRIEFING_ROOT / "config" / "improvement_tracking.json"
RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas" / "improvements"


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stable_hash(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def valid_date(value: str) -> str:
    try:
        canonical = Date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc
    if canonical != value:
        raise argparse.ArgumentTypeError(f"date must be {canonical}")
    return canonical


@dataclass
class ActionSpec:
    source_key: str
    domain: str
    title: str
    recommendation: str
    acceptance_key: str
    acceptance_criteria: str
    severity: str = "medium"
    risk: str = "high"
    origin: str = "retrospective"
    auto_fixer: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def action_id(self) -> str:
        return f"ATLAS-IMP-{stable_hash({'source_key': self.source_key, 'domain': self.domain})[:20].upper()}"


@dataclass
class Evaluation:
    outcome: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)


class ImprovementTracker:
    def __init__(self, *, root: Path = ROOT, config_path: Path = CONFIG_PATH, runtime_root: Path = RUNTIME_ROOT) -> None:
        self.root = root
        self.briefing_root = root / "work" / "global-briefing"
        self.outputs_root = root / "outputs"
        self.site_data = root / "src" / "app" / "briefing.generated.json"
        self.config = read_json(config_path, {})
        if not isinstance(self.config, dict) or self.config.get("schema_version") != 1:
            raise ValueError(f"invalid improvement tracking config: {config_path}")
        self.runtime_root = runtime_root
        self.actions_path = runtime_root / "actions.json"
        self.latest_path = runtime_root / "latest.json"
        self.latest_markdown_path = runtime_root / "LATEST_IMPROVEMENT_REPORT.md"
        self.audit_path = runtime_root / "audit.jsonl"

    def retrospective_specs(self) -> list[ActionSpec]:
        evolution = read_json(self.briefing_root / "data" / "evolution_state.json", {})
        mapping = self.config.get("acceptance_rules", {})
        criteria = {
            "v2_contract": "下一批 v2 预测继续满足概率、证据、截止日、解析标准和资产映射契约。",
            "source_health": "来源健康分数达到 80，陈旧或未知 RSS 不超过 20%，且所有核心主线通过独立来源、证据角色和限制披露审计。",
            "manual_evidence": "下一次复盘提供可审计的执行证据，由人工确认建议已落实。",
            "new_review_integrity": "下一批到期 v2 复盘不得提前结案，并包含显式结果、失败原因和证据。",
            "no_stale_market_data": "不得使用过期市场价格解析新的资产预测。",
            "report_quality": "下一期报告继续通过决策密度、来源和结构质量门禁。",
        }
        specs: list[ActionSpec] = []
        for rule in evolution.get("active_rules", []) if isinstance(evolution, dict) else []:
            if not isinstance(rule, dict) or not rule.get("rule_id"):
                continue
            rule_id = str(rule["rule_id"])
            acceptance_key = str(mapping.get(rule_id) or "manual_evidence")
            specs.append(ActionSpec(
                source_key=rule_id,
                domain="forecast_review" if not rule_id.startswith("report-") else "report_quality",
                title=f"落实复盘规则：{rule_id}",
                recommendation=str(rule.get("instruction") or rule_id),
                acceptance_key=acceptance_key,
                acceptance_criteria=criteria[acceptance_key],
                severity="high" if rule_id in {"failure-source_quality_issue", "failure-time_window_too_short"} else "medium",
                risk="high",
                metadata={"trigger_count": int(rule.get("trigger_count") or 0)},
            ))
        return specs

    def capability_specs(self, date: str) -> list[ActionSpec]:
        return [
            ActionSpec(
                source_key="capability-source-health",
                domain="source_health",
                title="恢复多源证据健康度",
                recommendation="修复 RSS/结构化行情失败，降低全量备用报价依赖，并保持限制披露。",
                acceptance_key="source_health",
                acceptance_criteria="来源健康分数达到 80，陈旧或未知 RSS 不超过 20%。",
                severity="high",
                risk="high",
                origin="capability_audit",
            ),
            ActionSpec(
                source_key="capability-paper-attribution",
                domain="paper_trading",
                title="补齐每日虚拟账户归因",
                recommendation="每天生成独立 US/CHINA 账户的点时归因产物。",
                acceptance_key="paper_attribution",
                acceptance_criteria=f"存在日期为 {date} 的每日纸面归因文件。",
                severity="medium",
                risk="low",
                origin="capability_audit",
                auto_fixer="write_paper_attribution",
            ),
            ActionSpec(
                source_key="capability-drift-diagnostics",
                domain="research_governance",
                title="保持四维漂移诊断可复现",
                recommendation="每日生成点时漂移诊断，并保持来源、事件校准、主题拥挤和双账户归因四个维度独立。",
                acceptance_key="drift_diagnostics",
                acceptance_criteria=f"存在日期为 {date}、指纹有效且无不透明综合分的漂移诊断。",
                severity="medium",
                risk="low",
                origin="capability_audit",
                auto_fixer="write_drift_diagnostics",
            ),
            ActionSpec(
                source_key="capability-paper-theme-provenance",
                domain="paper_trading",
                title="保持虚拟仓位主题归因与版本历史可审计",
                recommendation="双账户保持完整主题覆盖；每次注册表变更必须记录原因、内容寻址快照和哈希链版本，未来BUY固化当前版本。",
                acceptance_key="paper_theme_provenance",
                acceptance_criteria="US与CHINA主题归因覆盖率分别达到80%，且当前注册表版本链与快照审计通过。",
                severity="medium",
                risk="high",
                origin="capability_audit",
            ),
            ActionSpec(
                source_key="capability-core-version-control",
                domain="delivery_governance",
                title="将核心工作区纳入版本控制与 CI",
                recommendation="把 atlas.py、global-briefing、trading-core 和配置纳入统一受保护仓库及持续集成。",
                acceptance_key="core_version_control",
                acceptance_criteria="工作区根目录存在版本库并覆盖核心 Python 项目。",
                severity="high",
                risk="high",
                origin="capability_audit",
            ),
            ActionSpec(
                source_key="capability-external-alerting",
                domain="observability",
                title="配置外部告警通道",
                recommendation="为阻断、回退和熔断事件配置至少一个受控外部通知目的地。",
                acceptance_key="external_alerting",
                acceptance_criteria="外部告警已启用且至少配置一个目的地。",
                severity="medium",
                risk="high",
                origin="capability_audit",
            ),
            ActionSpec(
                source_key="capability-disaster-recovery",
                domain="resilience",
                title="建立核心数据灾备与恢复演练",
                recommendation="对预测、虚拟账本、配置、审计和报告建立定期快照、保留策略与恢复验证。",
                acceptance_key="disaster_recovery",
                acceptance_criteria="灾备已启用且存在不超过策略时限的已验证快照。",
                severity="high",
                risk="high",
                origin="capability_audit",
            ),
        ]

    def source_health(self) -> dict[str, Any]:
        payload = read_json(self.site_data, {})
        return payload.get("metrics", {}).get("sourceHealth", {}) if isinstance(payload, dict) else {}

    def quality(self, date: str) -> dict[str, Any]:
        return read_json(self.briefing_root / "data" / f"research-quality-{date}.json", {})

    def evaluate(self, spec: ActionSpec, action: dict[str, Any], date: str) -> Evaluation:
        current_day = Date.fromisoformat(date)
        eligible_from = Date.fromisoformat(str(action.get("eligible_from") or date))
        if spec.origin == "retrospective" and current_day < eligible_from:
            return Evaluation("not_due", f"跨期验收从 {eligible_from.isoformat()} 开始")
        if spec.acceptance_key == "v2_contract":
            quality = self.quality(date)
            contract = quality.get("prediction_audit", {}).get("v2_contract", {}) if isinstance(quality, dict) else {}
            errors = list(contract.get("errors") or []) + list(contract.get("review_errors") or [])
            return Evaluation("pass" if not errors else "fail", "v2 契约通过" if not errors else "v2 契约仍有错误", {"errors": errors})
        if spec.acceptance_key == "source_health":
            health = self.source_health()
            score = int(health.get("score") or 0) if isinstance(health, dict) else 0
            report_audit = self.quality(date).get("report_audit", {})
            core_stories_passed = report_audit.get("all_core_stories_passed") is True
            roles_enforced = report_audit.get("story_evidence_enforced") is True
            role_coverage = float(report_audit.get("source_role_coverage_pct") or 0.0)
            roles_passed = not roles_enforced or role_coverage == 100.0
            limitations_disclosed = isinstance(health, dict) and isinstance(health.get("limitations"), list)
            stale_or_unknown_pct = float(health.get("rssStaleOrUnknownPct") or 0.0) if isinstance(health, dict) else 100.0
            missing_market_dates = int(health.get("chinaMissingPriceDateItemCount") or 0) if isinstance(health, dict) else 1
            passed = (
                score >= 80
                and stale_or_unknown_pct <= 20.0
                and missing_market_dates == 0
                and core_stories_passed
                and roles_passed
                and limitations_disclosed
            )
            evidence = {
                "score": score,
                "label": health.get("label"),
                "limitations": health.get("limitations", []),
                "limitations_disclosed": limitations_disclosed,
                "all_core_stories_passed": core_stories_passed,
                "story_evidence_enforced": roles_enforced,
                "source_role_coverage_pct": role_coverage,
                "rss_stale_or_unknown_pct": stale_or_unknown_pct,
                "china_missing_price_date_items": missing_market_dates,
            }
            detail = (
                f"来源健康 {score}/100；核心主线审计={'通过' if core_stories_passed else '失败'}；"
                f"证据角色覆盖 {role_coverage:.0f}%"
            )
            return Evaluation("pass" if passed else "fail", detail, evidence)
        if spec.acceptance_key == "no_stale_market_data":
            health = self.source_health()
            stale = int(health.get("staleMarketItemCount") or 0) if isinstance(health, dict) else 0
            return Evaluation("pass" if stale == 0 else "fail", f"过期市场数据 {stale} 条", {"stale_market_items": stale})
        if spec.acceptance_key == "report_quality":
            audit = self.quality(date).get("report_audit", {})
            passed = audit.get("passed") is True
            return Evaluation("pass" if passed else "fail", "报告质量门禁通过" if passed else "报告质量门禁失败", {"errors": audit.get("errors", [])})
        if spec.acceptance_key == "new_review_integrity":
            records = read_jsonl(self.briefing_root / "data" / "predictions.jsonl")
            original_v2 = {str(row.get("prediction_id")): row for row in records if row.get("schema_version") == 2 and not isinstance(row.get("review"), dict)}
            eligible_reviews = [
                row for row in records
                if isinstance(row.get("review"), dict)
                and str(row.get("prediction_id")) in original_v2
                and str(row.get("review", {}).get("review_date") or row.get("date") or "") >= eligible_from.isoformat()
            ]
            if not eligible_reviews:
                return Evaluation("not_due", "尚无到期的新版预测复盘样本", {"eligible_reviews": 0})
            errors: list[str] = []
            for row in eligible_reviews:
                review = row.get("review", {})
                prediction_id = str(row.get("prediction_id") or "")
                original = original_v2[prediction_id]
                if review.get("observed_outcome") not in {0, 1}:
                    errors.append(f"{prediction_id}: missing observed_outcome")
                if not review.get("evidence") and not review.get("resolution_evidence") and not row.get("evidence"):
                    errors.append(f"{prediction_id}: missing resolution evidence")
                review_date = str(review.get("review_date") or row.get("date") or "")[:10]
                deadline = str(original.get("deadline") or "")[:10]
                if deadline and review_date and review_date < deadline and review.get("terminal_evidence") is not True:
                    errors.append(f"{prediction_id}: closed before deadline without terminal_evidence")
                if str(row.get("status") or "").lower() in {"partial", "wrong", "expired"}:
                    score = row.get("score") if isinstance(row.get("score"), dict) else review.get("score")
                    required_scores = {"direction", "timing", "transmission", "calibration"}
                    if not isinstance(score, dict) or not required_scores.issubset(score):
                        errors.append(f"{prediction_id}: missing explicit component scores")
                    failure_reasons = row.get("failure_reasons") or review.get("failure_reasons")
                    if not isinstance(failure_reasons, list) or not failure_reasons:
                        errors.append(f"{prediction_id}: missing failure_reasons")
            return Evaluation("pass" if not errors else "fail", "新版复盘完整" if not errors else "新版复盘仍有缺口", {"errors": errors, "eligible_reviews": len(eligible_reviews)})
        if spec.acceptance_key == "paper_attribution":
            path = self.briefing_root / "data" / f"paper-attribution-day-{date}.json"
            payload = read_json(path, {})
            passed = path.is_file() and isinstance(payload, dict) and payload.get("end") == date
            return Evaluation("pass" if passed else "fail", "每日纸面归因存在" if passed else "每日纸面归因缺失", {"path": str(path), "exists": path.exists()})
        if spec.acceptance_key == "drift_diagnostics":
            path = self.briefing_root / "data" / f"drift-diagnostics-{date}.json"
            payload = read_json(path, {})
            dimensions = payload.get("dimension_statuses", {}) if isinstance(payload, dict) else {}
            required = {"source_concentration", "forecast_calibration", "theme_crowding", "paper_account_attribution"}
            passed = bool(
                path.is_file()
                and isinstance(payload, dict)
                and payload.get("date") == date
                and set(dimensions) == required
                and payload.get("input_fingerprint")
                and payload.get("no_opaque_composite_score") is True
                and payload.get("deployment_blocking") is False
            )
            return Evaluation(
                "pass" if passed else "fail",
                "四维漂移诊断可复现" if passed else "漂移诊断缺失或结构无效",
                {"path": str(path), "exists": path.exists(), "dimension_statuses": dimensions},
            )
        if spec.acceptance_key == "paper_theme_provenance":
            path = self.briefing_root / "data" / f"drift-diagnostics-{date}.json"
            payload = read_json(path, {})
            paper = payload.get("paper_account_attribution", {}) if isinstance(payload, dict) else {}
            coverage = {
                str(row.get("account")): row.get("explicit_theme_attribution_coverage_pct")
                for row in paper.get("accounts", []) if isinstance(row, dict)
            } if isinstance(paper, dict) else {}
            script = self.briefing_root / "scripts" / "paper_theme_registry.py"
            registry_audit: dict[str, Any] = {}
            audit_returncode: int | None = None
            if script.is_file():
                completed = subprocess.run(
                    [sys.executable, str(script), "audit", "--date", date],
                    cwd=str(self.root),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                )
                audit_returncode = completed.returncode
                try:
                    registry_audit = json.loads(completed.stdout)
                except json.JSONDecodeError:
                    registry_audit = {}
            coverage_passed = bool(coverage) and all(
                value is not None and float(value) >= 80.0 for value in coverage.values()
            )
            audit_passed = bool(
                audit_returncode == 0
                and registry_audit.get("audit_passed") is True
                and registry_audit.get("history_current") is True
                and registry_audit.get("chain_valid") is True
                and registry_audit.get("snapshots_valid") is True
            )
            passed = coverage_passed and audit_passed
            return Evaluation(
                "pass" if passed else "fail",
                "双账户主题覆盖与注册表版本审计均达标" if passed else "主题覆盖或注册表版本审计未达标",
                {
                    "path": str(path),
                    "coverage_pct_by_account": coverage,
                    "required_pct": 80.0,
                    "registry_audit_returncode": audit_returncode,
                    "registry_audit_passed": registry_audit.get("audit_passed", False),
                    "registry_revision_id": registry_audit.get("current_revision_id"),
                    "history_current": registry_audit.get("history_current", False),
                    "chain_valid": registry_audit.get("chain_valid", False),
                    "snapshots_valid": registry_audit.get("snapshots_valid", False),
                },
            )
        if spec.acceptance_key == "core_version_control":
            repositories = {
                "control_plane": self.root,
                "site": self.root / "src",
                "trading_core": self.root / "work" / "trading-core",
            }
            evidence: dict[str, Any] = {}
            passed = True
            for name, repository in repositories.items():
                completed = subprocess.run(
                    ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                )
                top_level = Path(completed.stdout.strip()).resolve() if completed.returncode == 0 and completed.stdout.strip() else None
                workflow = repository / ".github" / "workflows" / "quality.yml"
                valid = top_level == repository.resolve() and workflow.is_file()
                evidence[name] = {"top_level": str(top_level) if top_level else None, "quality_workflow": str(workflow), "valid": valid}
                passed = passed and valid
            return Evaluation(
                "pass" if passed else "fail",
                "三个核心仓库均有有效版本控制与质量门禁" if passed else "核心仓库或质量门禁仍不完整",
                evidence,
            )
        if spec.acceptance_key == "external_alerting":
            config = self.config.get("external_alerting", {})
            destinations = config.get("destinations", []) if isinstance(config, dict) else []
            dispatch = read_json(self.root / "work" / "shared" / "atlas" / "alerts" / "latest.json", {})
            routed = bool(set(destinations) & set(dispatch.get("destinations", []))) if isinstance(dispatch, dict) else False
            passed = config.get("enabled") is True and bool(destinations) and dispatch.get("date") == date and routed if isinstance(config, dict) else False
            return Evaluation(
                "pass" if passed else "fail",
                "外部告警路由已验证" if passed else "外部告警未配置或当日路由未验证",
                {"destinations": destinations, "dispatch_date": dispatch.get("date"), "dispatch_status": dispatch.get("status"), "routed": routed},
            )
        if spec.acceptance_key == "disaster_recovery":
            config = self.config.get("disaster_recovery", {})
            manifest = read_json(self.root / "work" / "shared" / "atlas" / "backups" / "latest.json", {})
            archive = Path(str(manifest.get("archive") or "")) if isinstance(manifest, dict) else Path()
            outside_workspace = False
            if archive.is_absolute():
                try:
                    archive.resolve().relative_to(self.root.resolve())
                except ValueError:
                    outside_workspace = True
            age_hours: float | None = None
            try:
                created = datetime.fromisoformat(str(manifest.get("created_at")).replace("Z", "+00:00"))
                age_hours = max(0.0, (datetime.now(UTC) - created.astimezone(UTC)).total_seconds() / 3600)
            except (TypeError, ValueError):
                pass
            maximum_age = float(config.get("maximum_backup_age_hours") or 24) if isinstance(config, dict) else 24
            passed = bool(
                isinstance(config, dict)
                and isinstance(manifest, dict)
                and config.get("enabled") is True
                and manifest.get("verified") is True
                and manifest.get("restore_verified") is True
                and archive.is_file()
                and outside_workspace
                and age_hours is not None
                and age_hours <= maximum_age
            )
            return Evaluation(
                "pass" if passed else "fail",
                "外部灾备快照与恢复演练已验证" if passed else "灾备快照、时效或恢复演练未达标",
                {"archive": str(archive), "outside_workspace": outside_workspace, "age_hours": age_hours, "maximum_age_hours": maximum_age, "restore_verified": manifest.get("restore_verified")},
            )
        return Evaluation("manual", "需要人工提供执行证据")

    def apply_fixer(self, spec: ActionSpec, date: str) -> tuple[bool, str]:
        if spec.auto_fixer == "write_paper_attribution":
            command = [
                sys.executable,
                str(self.briefing_root / "scripts" / "evolution.py"),
                "paper-attribution",
                "--period", "day",
                "--date", date,
                "--write",
            ]
        elif spec.auto_fixer == "write_drift_diagnostics":
            command = [
                sys.executable,
                str(self.briefing_root / "scripts" / "drift_diagnostics.py"),
                "--date", date,
                "--write",
            ]
        else:
            return False, "no allowlisted fixer"
        completed = subprocess.run(command, cwd=self.root, text=True, encoding="utf-8", errors="replace", capture_output=True)
        return completed.returncode == 0, completed.stderr[-1000:] or completed.stdout[-1000:]

    def run(self, date: str, *, apply_safe: bool, strict: bool) -> tuple[int, dict[str, Any]]:
        now = utc_now()
        state = read_json(self.actions_path, {"schema_version": 1, "actions": {}})
        if not isinstance(state, dict):
            state = {"schema_version": 1, "actions": {}}
        actions = state.setdefault("actions", {})
        current_specs = self.retrospective_specs() + self.capability_specs(date)
        specs_by_id = {spec.action_id: spec for spec in current_specs}
        for action_id, action in list(actions.items()):
            if action_id not in specs_by_id and isinstance(action, dict):
                specs_by_id[action_id] = ActionSpec(**action["spec"])

        repairs: list[dict[str, Any]] = []
        for action_id, spec in specs_by_id.items():
            action = actions.get(action_id)
            if not isinstance(action, dict):
                created = Date.fromisoformat(date)
                eligible = created + timedelta(days=1) if spec.origin == "retrospective" else created
                action = {
                    "action_id": action_id,
                    "spec": asdict(spec),
                    "created_date": date,
                    "eligible_from": eligible.isoformat(),
                    "due_date": (created + timedelta(days=int(self.config.get("default_due_days", 7)))).isoformat(),
                    "status": "monitoring" if spec.origin == "retrospective" else "open",
                    "verification_history": [],
                    "occurrences": 0,
                }
                actions[action_id] = action
            else:
                action["spec"] = asdict(spec)

            evaluation = self.evaluate(spec, action, date)
            if evaluation.outcome == "fail" and apply_safe and spec.auto_fixer and spec.risk in set(self.config.get("auto_fix_risks", [])):
                fixed, detail = self.apply_fixer(spec, date)
                repairs.append({"action_id": action_id, "fixer": spec.auto_fixer, "applied": fixed, "detail": detail})
                if fixed:
                    evaluation = self.evaluate(spec, action, date)

            previous_status = str(action.get("status") or "open")
            if evaluation.outcome == "pass":
                action["status"] = "verified"
                action["verified_at"] = now
            elif evaluation.outcome in {"not_due", "manual"}:
                action["status"] = "monitoring" if evaluation.outcome == "not_due" else "requires_evidence"
            else:
                action["occurrences"] = int(action.get("occurrences", 0)) + 1
                action["status"] = (
                    "regressed"
                    if previous_status in {"verified", "regressed"}
                    else "overdue"
                    if previous_status == "overdue" or date > str(action.get("due_date"))
                    else "open"
                )
            action["last_checked_date"] = date
            action["last_checked_at"] = now
            action["last_evaluation"] = asdict(evaluation)
            history = action.setdefault("verification_history", [])
            history.append({"date": date, "checked_at": now, **asdict(evaluation), "status_after": action["status"]})
            action["verification_history"] = history[-30:]

        state["updated_at"] = now
        atomic_write_json(self.actions_path, state)
        unresolved = [a for a in actions.values() if a.get("status") not in {"verified", "closed"}]
        regressed = [a for a in unresolved if a.get("status") == "regressed"]
        overdue = [a for a in unresolved if a.get("status") == "overdue"]
        blocking_levels = set(self.config.get("blocking_severities", ["critical", "high"]))
        blocking = [a for a in unresolved if a.get("spec", {}).get("severity") in blocking_levels and a.get("status") in {"regressed", "overdue"}]
        capability_gaps = [a for a in unresolved if a.get("spec", {}).get("origin") == "capability_audit"]
        report = {
            "schema_version": 1,
            "date": date,
            "generated_at": utc_now(),
            "status": "blocked" if blocking else "degraded" if unresolved else "healthy",
            "counts": {
                "total": len(actions),
                "verified": sum(a.get("status") == "verified" for a in actions.values()),
                "monitoring": sum(a.get("status") in {"monitoring", "requires_evidence"} for a in actions.values()),
                "open": sum(a.get("status") == "open" for a in actions.values()),
                "regressed": len(regressed),
                "overdue": len(overdue),
                "blocking": len(blocking),
                "capability_gaps": len(capability_gaps),
            },
            "repairs": repairs,
            "actions": sorted(actions.values(), key=lambda a: (a.get("status") == "verified", a.get("action_id"))),
            "capability_gaps": capability_gaps,
        }
        atomic_write_json(self.latest_path, report)
        atomic_write_text(self.latest_markdown_path, render_markdown(report))
        append_jsonl(self.audit_path, {"date": date, "generated_at": report["generated_at"], "status": report["status"], "counts": report["counts"]})
        return (1 if strict and blocking else 0), report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ATLAS Improvement Verification Report", "",
        f"- date: {report['date']}",
        f"- status: {report['status']}",
        f"- total: {report['counts']['total']}",
        f"- verified: {report['counts']['verified']}",
        f"- monitoring: {report['counts']['monitoring']}",
        f"- open: {report['counts']['open']}",
        f"- regressed: {report['counts']['regressed']}",
        f"- overdue: {report['counts']['overdue']}", "",
        "## Actions",
    ]
    for action in report["actions"]:
        spec = action["spec"]
        lines.append(f"- {action['action_id']} [{action['status']}] {spec['title']} — {action['last_evaluation']['summary']}")
    lines.extend(["", "## Capability gaps"])
    if not report["capability_gaps"]:
        lines.append("- None")
    for action in report["capability_gaps"]:
        lines.append(f"- {action['action_id']} [{action['spec']['severity']}] {action['spec']['title']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATLAS retrospective action tracker")
    parser.add_argument("--date", type=valid_date)
    parser.add_argument("--apply-safe", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        tracker = ImprovementTracker()
        if args.status:
            report = read_json(tracker.latest_path, {})
            if not report:
                return 2
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.date:
            raise ValueError("--date is required unless --status is used")
        returncode, report = tracker.run(args.date, apply_safe=args.apply_safe, strict=args.strict)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps({"status": report["status"], "date": report["date"], "counts": report["counts"], "report": str(tracker.latest_markdown_path)}, ensure_ascii=False))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
