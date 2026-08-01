#!/usr/bin/env python3
"""Fail-closed, file-backed self-healing control plane for ATLAS.

The engine deliberately separates detection from mutation. Only allowlisted,
low-risk derived artifacts may be repaired automatically. Source code,
research conclusions, probabilities, virtual orders, and deployments are never
changed by this process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date as Date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
TRADING_ROOT = ROOT / "work" / "trading-core"
SITE_ROOT = ROOT / "src"
OUTPUTS_ROOT = ROOT / "outputs"
RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas" / "self_healing"
POLICY_PATH = BRIEFING_ROOT / "config" / "self_healing.json"
STATE_PATH = RUNTIME_ROOT / "issues.json"
LATEST_PATH = RUNTIME_ROOT / "latest.json"
LATEST_MARKDOWN_PATH = RUNTIME_ROOT / "LATEST_SELF_HEALING_REPORT.md"
AUDIT_LOG_PATH = RUNTIME_ROOT / "audit.jsonl"
LOCK_PATH = RUNTIME_ROOT / "self_healing.lock"

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
BRIEFING_TEST_INPUT_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_briefing_test_input_fingerprint(
    root: Path = ROOT,
    date: str | None = None,
) -> dict[str, Any]:
    """Independently fingerprint the inputs to briefing unittest discovery."""

    resolved_root = root.resolve()
    briefing_root = resolved_root / "work" / "global-briefing"
    entries: list[dict[str, Any]] = []
    unsafe_paths: list[str] = []

    def add_file(path: Path) -> None:
        if path.is_symlink():
            unsafe_paths.append(str(path))
            return
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            unsafe_paths.append(str(path))
            return
        if not resolved.is_file():
            return
        entries.append(
            {
                "path": relative,
                "bytes": resolved.stat().st_size,
                "sha256": file_hash(resolved),
            }
        )

    required_files = [
        resolved_root / "atlas.py",
        briefing_root / "requirements.txt",
        resolved_root / "src" / "app" / "briefing.generated.json",
    ]
    if date:
        required_files.append(
            resolved_root / "outputs" / f"每日全球晨间简报-{date}.md"
        )
    missing_required = [
        path.relative_to(resolved_root).as_posix()
        for path in required_files
        if not path.is_file()
    ]
    for path in required_files:
        add_file(path)

    ignored_parts = {"__pycache__", ".pytest_cache", ".ruff_cache"}
    for directory in (
        briefing_root / "scripts",
        briefing_root / "config",
        briefing_root / "tests",
    ):
        if not directory.is_dir():
            missing_required.append(directory.relative_to(resolved_root).as_posix())
            continue
        for path in sorted(directory.rglob("*")):
            if any(part in ignored_parts for part in path.parts) or path.suffix == ".pyc":
                continue
            if path.is_file():
                add_file(path)

    outputs_root = resolved_root / "outputs"
    for path in sorted(outputs_root.glob("每日全球晨间简报-*.md")):
        if path.is_file():
            add_file(path)

    entries = sorted(
        {entry["path"]: entry for entry in entries}.values(),
        key=lambda item: item["path"],
    )
    test_file_count = sum(
        entry["path"].startswith("work/global-briefing/tests/test_")
        and entry["path"].endswith(".py")
        for entry in entries
    )
    contract = {
        "schema_version": BRIEFING_TEST_INPUT_SCHEMA_VERSION,
        "files": entries,
        "missing_required": sorted(set(missing_required)),
        "unsafe_paths": sorted(set(unsafe_paths)),
    }
    return {
        "schema_version": BRIEFING_TEST_INPUT_SCHEMA_VERSION,
        "fingerprint_sha256": stable_hash(contract),
        "file_count": len(entries),
        "test_file_count": test_file_count,
        "missing_required": contract["missing_required"],
        "unsafe_paths": contract["unsafe_paths"],
    }


def cycle_audit_record_hash(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    chain = normalized.get("audit_chain")
    if isinstance(chain, dict):
        normalized["audit_chain"] = {
            key: value for key, value in chain.items() if key != "entry_sha256"
        }
    return stable_hash(normalized)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(stable_json(value) + "\n")
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


def latest_report_date() -> str:
    candidates: list[str] = []
    for path in OUTPUTS_ROOT.glob("每日全球晨间简报-*.md"):
        value = path.stem.rsplit("-", 3)[-3:]
        candidate = "-".join(value)
        try:
            candidates.append(Date.fromisoformat(candidate).isoformat())
        except ValueError:
            continue
    if not candidates:
        raise FileNotFoundError("no dated global briefing report exists")
    return max(candidates)


def status_view(payload: dict[str, Any], *, read_at: str | None = None) -> dict[str, Any]:
    """Annotate a persisted run snapshot without pretending it is a live probe."""
    view = dict(payload)
    evidence_observed_at = str(
        payload.get("evidence_observed_at") or payload.get("finished_at") or ""
    )
    view["status_view"] = {
        "read_at": read_at or utc_now(),
        "evidence_observed_at": evidence_observed_at or None,
        "historical_snapshot": True,
        "scope": str(payload.get("evidence_scope") or "self_healing_run_snapshot"),
    }
    return view


@dataclass
class ProbeResult:
    check_id: str
    passed: bool
    severity: str
    risk: str
    resource: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    fixer: str | None = None
    executed: bool = True
    reused: bool = False

    @property
    def fingerprint(self) -> str:
        return stable_hash({"check_id": self.check_id, "resource": self.resource})[:24]


@dataclass
class RepairResult:
    status: str
    detail: str
    changed_files: list[str] = field(default_factory=list)
    rolled_back: bool = False


class SelfHealingEngine:
    def __init__(
        self,
        *,
        root: Path = ROOT,
        policy_path: Path = POLICY_PATH,
        runtime_root: Path = RUNTIME_ROOT,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.root = root
        self.briefing_root = root / "work" / "global-briefing"
        self.trading_root = root / "work" / "trading-core"
        self.site_root = root / "src"
        self.outputs_root = root / "outputs"
        self.runtime_root = runtime_root
        self.policy_path = policy_path
        self.policy = read_json(policy_path, {})
        if not isinstance(self.policy, dict) or self.policy.get("schema_version") != 1:
            raise ValueError(f"invalid self-healing policy: {policy_path}")
        self.state_path = runtime_root / "issues.json"
        self.latest_path = runtime_root / "latest.json"
        self.latest_markdown_path = runtime_root / "LATEST_SELF_HEALING_REPORT.md"
        self.audit_log_path = runtime_root / "audit.jsonl"
        self.runs_root = runtime_root / "runs"
        self.lock_path = runtime_root / "self_healing.lock"
        self.runner = runner or self._subprocess
        self.executed_checks: set[str] = set()

    @staticmethod
    def _subprocess(
        command: Sequence[str], *, cwd: Path, timeout: int = 180
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )

    def check_policy(self, check_id: str) -> tuple[str, str]:
        config = self.policy.get("checks", {}).get(check_id, {})
        return str(config.get("severity") or "medium"), str(config.get("risk") or "high")

    def result(
        self,
        check_id: str,
        passed: bool,
        resource: Path | str,
        summary: str,
        *,
        evidence: dict[str, Any] | None = None,
        fixer: str | None = None,
        reused: bool = False,
    ) -> ProbeResult:
        self.executed_checks.add(check_id)
        severity, risk = self.check_policy(check_id)
        try:
            resource_text = str(Path(resource).resolve().relative_to(self.root.resolve()))
        except (ValueError, TypeError):
            resource_text = str(resource)
        return ProbeResult(
            check_id=check_id,
            passed=passed,
            severity=severity,
            risk=risk,
            resource=resource_text,
            summary=summary,
            evidence=evidence or {},
            fixer=fixer,
            reused=reused,
        )

    def command_json(
        self, command: Sequence[str], *, cwd: Path | None = None, timeout: int = 180
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
        completed = self.runner(command, cwd=cwd or self.root, timeout=timeout)
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            payload = None
        return completed, payload if isinstance(payload, dict) else None

    def probe_report(self, date: str) -> ProbeResult:
        path = self.outputs_root / f"每日全球晨间简报-{date}.md"
        exists = path.is_file() and path.stat().st_size > 0
        return self.result(
            "report_exists",
            exists,
            path,
            "dated report is present and non-empty" if exists else "dated report is missing or empty",
            evidence={"exists": path.exists(), "bytes": path.stat().st_size if path.exists() else 0},
        )

    def probe_research_quality(self, date: str) -> list[ProbeResult]:
        script = self.briefing_root / "scripts" / "research_quality.py"
        command = [sys.executable, str(script), "--date", date, "--dry-run"]
        completed, payload = self.command_json(command)
        operational = bool(payload and payload.get("operational_passed") is True)
        gate = self.result(
            "research_operational_gate",
            operational,
            self.outputs_root / f"每日全球晨间简报-{date}.md",
            "research operational gate passed" if operational else "research operational gate failed",
            evidence={
                "returncode": completed.returncode,
                "blocking_reasons": (payload or {}).get("blocking_reasons", []),
                "stderr": completed.stderr[-1000:],
            },
        )
        artifact = self.briefing_root / "data" / f"research-quality-{date}.json"
        stored = read_json(artifact, {})
        inputs = [
            self.outputs_root / f"每日全球晨间简报-{date}.md",
            self.briefing_root / "data" / "predictions.jsonl",
            self.briefing_root / "config" / "settings.json",
        ]
        newest_input_mtime = max(
            (path.stat().st_mtime_ns for path in inputs if path.exists()),
            default=0,
        )
        artifact_ok = bool(
            isinstance(stored, dict)
            and stored.get("date") == date
            and stored.get("operational_passed") == operational
            and artifact.is_file()
            and artifact.stat().st_mtime_ns >= newest_input_mtime
        )
        artifact_result = self.result(
            "research_quality_artifact",
            artifact_ok,
            artifact,
            "research quality artifact is current" if artifact_ok else "research quality artifact is missing or stale",
            evidence={"date": date, "exists": artifact.exists(), "expected_operational_passed": operational},
            fixer="refresh_research_quality",
        )
        return [gate, artifact_result]

    def probe_review_queue(self, date: str) -> ProbeResult:
        artifact = self.briefing_root / "data" / f"review-queue-{date}.json"
        payload = read_json(artifact, {})
        counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
        predictions = self.briefing_root / "data" / "predictions.jsonl"
        settings = read_json(self.briefing_root / "config" / "settings.json", {})
        contract = settings.get("prediction_contract", {}) if isinstance(settings, dict) else {}
        queue_policy = settings.get("review_queue", {}) if isinstance(settings, dict) else {}
        expected_fingerprint = stable_hash({
            "predictions_sha256": file_hash(predictions),
            "deadline_semantics": contract.get("deadline_semantics") if isinstance(contract, dict) else None,
            "mature_for_daily_review_when": contract.get("mature_for_daily_review_when") if isinstance(contract, dict) else None,
            "review_queue": queue_policy if isinstance(queue_policy, dict) else {},
        })
        passed = bool(
            isinstance(payload, dict)
            and payload.get("run_date") == date
            and isinstance(payload.get("review_now"), list)
            and isinstance(payload.get("review_backlog"), list)
            and isinstance(counts, dict)
            and int(counts.get("due_reviews") or 0)
            == int(counts.get("review_now") or 0) + int(counts.get("review_backlog") or 0)
            and payload.get("input_fingerprint") == expected_fingerprint
            and artifact.is_file()
        )
        return self.result(
            "review_queue_freshness",
            passed,
            artifact,
            "full-ledger review queue is current" if passed else "full-ledger review queue is missing, stale, or inconsistent",
            evidence={
                "date": date,
                "exists": artifact.exists(),
                "due_reviews": counts.get("due_reviews") if isinstance(counts, dict) else None,
                "review_now": counts.get("review_now") if isinstance(counts, dict) else None,
                "review_backlog": counts.get("review_backlog") if isinstance(counts, dict) else None,
                "fingerprint_matches": payload.get("input_fingerprint") == expected_fingerprint if isinstance(payload, dict) else False,
            },
            fixer="refresh_review_queue",
        )

    def probe_resolution_evidence(self, date: str) -> ProbeResult:
        artifact = self.briefing_root / "data" / f"resolution-evidence-{date}.json"
        payload = read_json(artifact, {})
        queue = read_json(self.briefing_root / "data" / f"review-queue-{date}.json", {})
        settings = read_json(self.briefing_root / "config" / "settings.json", {})
        policy = settings.get("resolution_evidence", {}) if isinstance(settings, dict) else {}
        expected_fingerprint = stable_hash({
            "schema_version": 1,
            "run_date": date,
            "review_queue_input_fingerprint": queue.get("input_fingerprint") if isinstance(queue, dict) else None,
            "resolution_evidence_policy": policy if isinstance(policy, dict) else {},
        })
        counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
        passed = bool(
            isinstance(payload, dict)
            and payload.get("date") == date
            and payload.get("input_fingerprint") == expected_fingerprint
            and isinstance(payload.get("items"), list)
            and isinstance(counts, dict)
            and int(counts.get("prediction_work_items") or 0) == len(payload.get("items", []))
            and artifact.is_file()
        )
        return self.result(
            "resolution_evidence_freshness",
            passed,
            artifact,
            "resolution evidence workbench is current" if passed else "resolution evidence workbench is missing, stale, or inconsistent",
            evidence={
                "date": date,
                "exists": artifact.exists(),
                "fingerprint_matches": payload.get("input_fingerprint") == expected_fingerprint if isinstance(payload, dict) else False,
                "prediction_work_items": counts.get("prediction_work_items") if isinstance(counts, dict) else None,
                "automatic_ledger_append": False,
            },
            fixer="refresh_resolution_evidence",
        )

    def probe_drift_diagnostics(self, date: str) -> ProbeResult:
        script = self.briefing_root / "scripts" / "drift_diagnostics.py"
        completed, expected = self.command_json([sys.executable, str(script), "--date", date])
        artifact = self.briefing_root / "data" / f"drift-diagnostics-{date}.json"
        stored = read_json(artifact, {})
        required_dimensions = {
            "source_concentration",
            "forecast_calibration",
            "theme_crowding",
            "paper_account_attribution",
        }
        statuses = stored.get("dimension_statuses", {}) if isinstance(stored, dict) else {}
        passed = bool(
            completed.returncode == 0
            and isinstance(expected, dict)
            and isinstance(stored, dict)
            and stored.get("date") == date
            and stored.get("input_fingerprint") == expected.get("input_fingerprint")
            and set(statuses) == required_dimensions
            and stored.get("no_opaque_composite_score") is True
            and stored.get("deployment_blocking") is False
            and artifact.is_file()
        )
        return self.result(
            "drift_diagnostics_freshness",
            passed,
            artifact,
            "point-in-time drift diagnostics are current" if passed else "point-in-time drift diagnostics are missing, stale, or structurally invalid",
            evidence={
                "date": date,
                "exists": artifact.exists(),
                "returncode": completed.returncode,
                "fingerprint_matches": stored.get("input_fingerprint") == (expected or {}).get("input_fingerprint") if isinstance(stored, dict) else False,
                "dimension_statuses": statuses,
                "gate_mode": stored.get("gate_mode") if isinstance(stored, dict) else None,
                "deployment_blocking": False,
                "stderr": completed.stderr[-1000:],
            },
            fixer="refresh_drift_diagnostics",
        )

    def probe_paper_theme_registry(self, date: str) -> ProbeResult:
        script = self.briefing_root / "scripts" / "paper_theme_registry.py"
        completed, payload = self.command_json([sys.executable, str(script), "audit", "--date", date])
        registry = self.briefing_root / "config" / "paper_theme_registry.json"
        coverage = payload.get("coverage", {}) if isinstance(payload, dict) else {}
        accounts = coverage.get("accounts", []) if isinstance(coverage, dict) else []
        passed = bool(
            completed.returncode == 0
            and isinstance(payload, dict)
            and payload.get("date") == date
            and payload.get("valid") is True
            and payload.get("audit_passed") is True
            and payload.get("history_current") is True
            and payload.get("chain_valid") is True
            and payload.get("snapshots_valid") is True
            and coverage.get("all_accounts_fully_covered") is True
            and len(accounts) == 2
            and all(float(row.get("position_value_coverage_pct", 0.0)) == 100.0 for row in accounts if isinstance(row, dict))
            and registry.is_file()
        )
        return self.result(
            "paper_theme_registry_validity",
            passed,
            registry,
            "paper theme registry has a current audited revision chain and covers every open position" if passed else "paper theme registry is invalid, unrecorded, tampered, or leaves open positions unclassified",
            evidence={
                "date": date,
                "returncode": completed.returncode,
                "valid": payload.get("valid") if isinstance(payload, dict) else False,
                "audit_passed": payload.get("audit_passed") if isinstance(payload, dict) else False,
                "current_revision_id": payload.get("current_revision_id") if isinstance(payload, dict) else None,
                "history_current": payload.get("history_current") if isinstance(payload, dict) else False,
                "chain_valid": payload.get("chain_valid") if isinstance(payload, dict) else False,
                "snapshots_valid": payload.get("snapshots_valid") if isinstance(payload, dict) else False,
                "verified_entry_count": payload.get("verified_entry_count") if isinstance(payload, dict) else None,
                "coverage": coverage,
                "errors": payload.get("errors", []) if isinstance(payload, dict) else [],
                "history_errors": payload.get("history_errors", []) if isinstance(payload, dict) else [],
                "snapshot_errors": payload.get("snapshot_errors", []) if isinstance(payload, dict) else [],
                "economic_ledger_mutations": [],
            },
        )

    def probe_site(self, date: str) -> list[ProbeResult]:
        script = self.briefing_root / "scripts" / "sync_briefing_site.py"
        completed, payload = self.command_json(
            [sys.executable, str(script), "--date", date, "--candidate-only"]
        )
        site_data = self.site_root / "app" / "briefing.generated.json"
        generated = read_json(site_data, {})
        expected_hash = str((payload or {}).get("sha256") or "")
        actual_hash = str(generated.get("contentHash") or "") if isinstance(generated, dict) else ""
        candidate_ok = bool(
            completed.returncode == 0
            and (payload or {}).get("status") == "candidate_valid"
            and expected_hash
        )
        deployed_matches = bool(actual_hash == expected_hash and generated.get("reportDate") == date)
        freshness = self.result(
            "site_payload_freshness",
            candidate_ok,
            site_data,
            (
                "site payload matches current report inputs"
                if candidate_ok and deployed_matches
                else "candidate site payload is valid and awaits publication refresh"
                if candidate_ok
                else "candidate site payload is invalid"
            ),
            evidence={
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
                "report_date": generated.get("reportDate") if isinstance(generated, dict) else None,
                "candidate_valid": candidate_ok,
                "deployed_matches": deployed_matches,
                "publication_refresh_required": candidate_ok and not deployed_matches,
                "returncode": completed.returncode,
                "stderr": completed.stderr[-1000:],
            },
            fixer=None,
        )
        state_path = self.briefing_root / "data" / "site-sync-state.json"
        state = read_json(state_path, {})
        pending = str(state.get("pending_sha") or "") if isinstance(state, dict) else ""
        deployed = str(state.get("last_deployed_sha") or "") if isinstance(state, dict) else ""
        pending_payload = str(state.get("pending_payload_sha") or "") if isinstance(state, dict) else ""
        deployed_payload = str(state.get("last_deployed_payload_sha") or "") if isinstance(state, dict) else ""
        state_ok = not pending or pending != deployed or pending_payload != deployed_payload
        consistency = self.result(
            "site_state_consistency",
            state_ok,
            state_path,
            "site state has no already-deployed pending marker" if state_ok else "site state retains an already-deployed pending marker",
            evidence={"pending_sha": pending, "last_deployed_sha": deployed, "pending_payload_sha": pending_payload, "last_deployed_payload_sha": deployed_payload},
            fixer="normalize_site_state",
        )
        return [freshness, consistency]

    def probe_macro_bridge(self, date: str) -> ProbeResult:
        source = self.briefing_root / "data" / f"macro_signals-{date}.jsonl"
        target = self.trading_root / "data" / "macro_signals" / source.name
        source_hash = file_hash(source)
        target_hash = file_hash(target)
        passed = source_hash is not None and source_hash == target_hash
        return self.result(
            "macro_bridge_freshness",
            passed,
            target,
            "macro signal bridge is current" if passed else "macro signal bridge is missing or stale",
            evidence={"source": str(source), "source_hash": source_hash, "target_hash": target_hash},
            fixer="refresh_macro_bridge" if source_hash is not None else None,
        )

    @staticmethod
    def process_is_running(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def lock_is_stale(self, path: Path) -> tuple[bool, dict[str, Any]]:
        payload = read_json(path, {})
        pid = int(payload.get("pid", 0)) if isinstance(payload, dict) else 0
        try:
            started = datetime.fromisoformat(str(payload.get("started_at", "")).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            started = datetime.now(UTC) - timedelta(days=2)
        stale_after = timedelta(minutes=int(self.policy.get("lock_stale_minutes", 120)))
        stale = not self.process_is_running(pid) or datetime.now(UTC) - started > stale_after
        return stale, {"pid": pid, "started_at": str(payload.get("started_at") or ""), "running": self.process_is_running(pid)}

    def probe_cycle_lock(self) -> ProbeResult:
        path = self.root / "work" / "shared" / "atlas" / "cycle.lock"
        if not path.exists():
            return self.result("cycle_lock_health", True, path, "no stale cycle lock exists")
        stale, evidence = self.lock_is_stale(path)
        return self.result(
            "cycle_lock_health",
            not stale,
            path,
            "active cycle lock belongs to a running process" if not stale else "cycle lock is stale",
            evidence=evidence,
            fixer="remove_stale_cycle_lock" if stale else None,
        )

    def probe_cycle_audit(self, date: str) -> ProbeResult:
        path = self.root / "work" / "shared" / "atlas" / "run_audits" / f"atlas-cycle-{date}.json"
        payload = read_json(path, {})
        passed = bool(isinstance(payload, dict) and payload.get("overall_passed") is True)
        return self.result(
            "latest_cycle_audit",
            passed,
            path,
            "date-aligned cycle audit passed" if passed else "date-aligned cycle audit is missing or failed",
            evidence={"exists": path.exists(), "blocking_reasons": payload.get("blocking_reasons", []) if isinstance(payload, dict) else []},
        )

    def probe_improvement_tracker(self, date: str) -> ProbeResult:
        path = self.root / "work" / "shared" / "atlas" / "improvements" / "latest.json"
        payload = read_json(path, {})
        passed = bool(
            isinstance(payload, dict)
            and payload.get("date") == date
            and int(payload.get("counts", {}).get("blocking") or 0) == 0
        )
        return self.result(
            "improvement_tracker_current",
            passed,
            path,
            "retrospective actions are current and nonblocking" if passed else "retrospective action tracker is missing, stale, or blocking",
            evidence={"exists": path.exists(), "date": payload.get("date") if isinstance(payload, dict) else None, "blocking": payload.get("counts", {}).get("blocking") if isinstance(payload, dict) else None},
            fixer="refresh_improvement_tracker",
        )

    def briefing_test_reuse_decision(
        self,
        date: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate cycle-produced briefing test evidence without trusting flags alone."""

        reasons: list[str] = []
        audit_path = (
            self.root
            / "work"
            / "shared"
            / "atlas"
            / "run_audits"
            / f"atlas-cycle-{date}.json"
        )
        audit = read_json(audit_path, {})
        current_input = build_briefing_test_input_fingerprint(self.root, date)
        max_age_value = self.policy.get("briefing_test_reuse_max_age_minutes", 60)
        if (
            isinstance(max_age_value, bool)
            or not isinstance(max_age_value, int)
            or not 1 <= max_age_value <= 240
        ):
            reasons.append("briefing test reuse freshness policy is invalid")
            max_age_minutes = 60
        else:
            max_age_minutes = max_age_value

        stage: dict[str, Any] | None = None
        detail: dict[str, Any] = {}
        scope: dict[str, Any] = {}
        evidence: dict[str, Any] = {}
        recorded_input: dict[str, Any] = {}
        completed_at: str | None = None
        age_seconds: float | None = None
        audit_hash_valid = False
        history_matched = False

        if not isinstance(audit, dict) or not audit:
            reasons.append("same-day cycle audit is missing or invalid")
        else:
            if audit.get("date") != date:
                reasons.append("cycle audit date does not match the requested date")
            if audit.get("overall_passed") is not True or audit.get(
                "operational_gate_passed"
            ) is not True:
                reasons.append("cycle audit operational gate did not pass")
            if audit.get("test_returncode") != 0:
                reasons.append("cycle audit test returncode is not zero")
            profile = audit.get("execution_profile")
            if not isinstance(profile, dict) or profile.get("skip_tests") is not False:
                reasons.append("cycle audit does not prove tests were enabled")

            chain = audit.get("audit_chain")
            audit_hash_valid = bool(
                isinstance(chain, dict)
                and chain.get("entry_sha256") == cycle_audit_record_hash(audit)
            )
            if not audit_hash_valid:
                reasons.append("cycle audit content hash is invalid")

            run_id = str(audit.get("run_id") or "")
            if not run_id or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_." for character in run_id):
                reasons.append("cycle audit run_id is invalid")
            else:
                history_path = audit_path.parent / "history" / date / f"{run_id}.json"
                history = read_json(history_path, {})
                history_matched = bool(isinstance(history, dict) and history == audit)
                if not history_matched:
                    reasons.append("cycle audit does not match its same-day history record")

            stages = audit.get("stages")
            matching_stages = (
                [
                    item
                    for item in stages
                    if isinstance(item, dict)
                    and item.get("name") == "targeted_integration_tests"
                ]
                if isinstance(stages, list)
                else []
            )
            if len(matching_stages) != 1:
                reasons.append("cycle audit has no unique integration test stage")
            else:
                stage = matching_stages[0]
                if stage.get("status") != "passed":
                    reasons.append("cycle integration test stage did not pass")
                raw_detail = stage.get("detail")
                if isinstance(raw_detail, dict):
                    detail = raw_detail
                else:
                    reasons.append("cycle integration test detail is invalid")
                if detail.get("returncode") != 0:
                    reasons.append("cycle integration test returncode is not zero")
                raw_scope = detail.get("scope")
                if isinstance(raw_scope, dict):
                    scope = raw_scope
                else:
                    reasons.append("cycle integration test scope is invalid")
                if (
                    scope.get("briefing_unittest_discovery") is not True
                    or scope.get("kind")
                    not in {"targeted_integration_suite", "full_regression_suite"}
                ):
                    reasons.append("cycle briefing test scope is insufficient")
                raw_evidence = detail.get("briefing_test_evidence")
                if isinstance(raw_evidence, dict):
                    evidence = raw_evidence
                else:
                    reasons.append("cycle briefing test evidence is missing")

        if evidence:
            if evidence.get("schema_version") != 1:
                reasons.append("cycle briefing test evidence schema is unsupported")
            if (
                evidence.get("suite") != "python_unittest_discovery"
                or evidence.get("discovery_root") != "work/global-briefing/tests"
                or evidence.get("pattern") != "test_*.py"
            ):
                reasons.append("cycle briefing test discovery contract is insufficient")
            if evidence.get("returncode") != 0 or evidence.get("passed") is not True:
                reasons.append("cycle briefing test evidence did not pass")
            raw_recorded_input = evidence.get("input")
            if isinstance(raw_recorded_input, dict):
                recorded_input = raw_recorded_input
            else:
                reasons.append("cycle briefing test input fingerprint is missing")
            completed_at = (
                str(evidence.get("completed_at"))
                if evidence.get("completed_at") is not None
                else None
            )

        if current_input.get("missing_required"):
            reasons.append("current briefing test inputs are incomplete")
        if current_input.get("unsafe_paths"):
            reasons.append("current briefing test inputs contain unsafe paths")
        if int(current_input.get("test_file_count") or 0) < 1:
            reasons.append("current briefing unittest discovery would be empty")
        if recorded_input:
            if recorded_input.get("schema_version") != BRIEFING_TEST_INPUT_SCHEMA_VERSION:
                reasons.append("recorded briefing test input schema is unsupported")
            if recorded_input.get("missing_required"):
                reasons.append("recorded briefing test inputs were incomplete")
            if recorded_input.get("unsafe_paths"):
                reasons.append("recorded briefing test inputs contained unsafe paths")
            if int(recorded_input.get("test_file_count") or 0) < 1:
                reasons.append("recorded briefing unittest discovery was empty")
            if (
                recorded_input.get("fingerprint_sha256")
                != current_input.get("fingerprint_sha256")
                or recorded_input.get("file_count") != current_input.get("file_count")
                or recorded_input.get("test_file_count")
                != current_input.get("test_file_count")
            ):
                reasons.append("briefing test input fingerprint changed after cycle")

        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("briefing test reuse clock must be timezone-aware")
        if not completed_at:
            reasons.append("cycle briefing test completion time is missing")
        else:
            try:
                observed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                if observed.tzinfo is None or observed.utcoffset() is None:
                    raise ValueError("timestamp has no timezone")
                age_seconds = (current_time.astimezone(UTC) - observed.astimezone(UTC)).total_seconds()
                if age_seconds < -300:
                    reasons.append("cycle briefing test evidence is from the future")
                elif age_seconds > max_age_minutes * 60:
                    reasons.append("cycle briefing test evidence is stale")
            except ValueError:
                reasons.append("cycle briefing test completion time is invalid")

        return {
            "reused": not reasons,
            "reasons": reasons,
            "execution_mode": "reused_cycle_evidence" if not reasons else "executed",
            "suite_executed_by_heal": False if not reasons else True,
            "cycle_audit": str(audit_path),
            "cycle_run_id": audit.get("run_id") if isinstance(audit, dict) else None,
            "completed_at": completed_at,
            "age_seconds": age_seconds,
            "max_age_minutes": max_age_minutes,
            "audit_hash_valid": audit_hash_valid,
            "history_matched": history_matched,
            "scope": scope,
            "recorded_input_fingerprint": recorded_input.get("fingerprint_sha256"),
            "current_input_fingerprint": current_input.get("fingerprint_sha256"),
            "current_input_file_count": current_input.get("file_count"),
            "current_test_file_count": current_input.get("test_file_count"),
        }

    def probe_deep(
        self,
        date: str | None = None,
        *,
        now: datetime | None = None,
    ) -> list[ProbeResult]:
        probes: list[Callable[[], ProbeResult]] = []
        deep_config = self.policy.get("deep_checks", {})
        if deep_config.get("briefing_tests", True):
            reuse = (
                self.briefing_test_reuse_decision(date, now=now)
                if date is not None
                else {
                    "reused": False,
                    "reasons": ["run date was not provided"],
                    "execution_mode": "executed",
                    "suite_executed_by_heal": True,
                }
            )

            def probe_briefing_tests() -> ProbeResult:
                if reuse["reused"]:
                    return self.result(
                        "briefing_tests",
                        True,
                        self.briefing_root / "tests",
                        "briefing test suite passed using fresh matching cycle evidence",
                        evidence=reuse,
                        reused=True,
                    )
                completed = self.runner(
                    [sys.executable, "-m", "unittest", "discover", str(self.briefing_root / "tests"), "-p", "test_*.py"],
                    cwd=self.root,
                    timeout=300,
                )
                return self.result(
                    "briefing_tests",
                    completed.returncode == 0,
                    self.briefing_root / "tests",
                    "briefing test suite passed" if completed.returncode == 0 else "briefing test suite failed",
                    evidence={
                        "returncode": completed.returncode,
                        "stdout_tail": completed.stdout[-2000:],
                        "stderr_tail": completed.stderr[-2000:],
                        "execution_mode": "executed",
                        "suite_executed_by_heal": True,
                        "reuse_rejected_reasons": reuse.get("reasons", []),
                    },
                )

            probes.append(probe_briefing_tests)
        if deep_config.get("site_quality", True):
            def probe_site_quality() -> ProbeResult:
                npm = "npm.cmd" if os.name == "nt" else "npm"
                completed = self.runner([npm, "run", "quality"], cwd=self.site_root, timeout=420)
                return self.result(
                    "site_quality",
                    completed.returncode == 0,
                    self.site_root,
                    "site quality suite passed" if completed.returncode == 0 else "site quality suite failed",
                    evidence={"returncode": completed.returncode, "stdout_tail": completed.stdout[-3000:], "stderr_tail": completed.stderr[-3000:]},
                )

            probes.append(probe_site_quality)
        if len(probes) < 2:
            return [probe() for probe in probes]
        # Deep checks are evidence-only and operate in separate component roots.
        # Keep repair/mutation phases serial, while reducing the daily gate's
        # critical path. Results are consumed in declaration order for stable
        # report identities regardless of completion order.
        with ThreadPoolExecutor(max_workers=len(probes), thread_name_prefix="atlas-deep") as pool:
            futures = [pool.submit(probe) for probe in probes]
            return [future.result() for future in futures]

    def detect(self, date: str, *, deep: bool) -> list[ProbeResult]:
        results = [self.probe_report(date)]
        if results[0].passed:
            results.extend(self.probe_research_quality(date))
            results.append(self.probe_review_queue(date))
            results.append(self.probe_resolution_evidence(date))
            results.append(self.probe_paper_theme_registry(date))
            results.append(self.probe_drift_diagnostics(date))
            results.extend(self.probe_site(date))
        results.append(self.probe_macro_bridge(date))
        results.append(self.probe_cycle_lock())
        results.append(self.probe_cycle_audit(date))
        results.append(self.probe_improvement_tracker(date))
        if deep:
            results.extend(self.probe_deep(date))
        return results

    def repair_targets(self, finding: ProbeResult) -> list[Path]:
        mapping = {
            "refresh_research_quality": [self.briefing_root / "data" / f"research-quality-{finding.evidence.get('date', '')}.json"],
            "refresh_review_queue": [self.briefing_root / "data" / f"review-queue-{finding.evidence.get('date', '')}.json"],
            "refresh_resolution_evidence": [self.briefing_root / "data" / f"resolution-evidence-{finding.evidence.get('date', '')}.json"],
            "refresh_drift_diagnostics": [self.briefing_root / "data" / f"drift-diagnostics-{finding.evidence.get('date', '')}.json"],
            "regenerate_site_payload": [
                self.site_root / "app" / "briefing.generated.json",
                self.briefing_root / "data" / "site-sync-state.json",
            ],
            "normalize_site_state": [self.briefing_root / "data" / "site-sync-state.json"],
            "refresh_macro_bridge": [self.root / finding.resource],
            "remove_stale_cycle_lock": [self.root / finding.resource],
            "refresh_improvement_tracker": [
                self.root / "work" / "shared" / "atlas" / "improvements" / "actions.json",
                self.root / "work" / "shared" / "atlas" / "improvements" / "latest.json",
            ],
        }
        return mapping.get(finding.fixer or "", [])

    def snapshot(self, paths: list[Path], backup_root: Path) -> dict[str, dict[str, Any]]:
        manifest: dict[str, dict[str, Any]] = {}
        for path in paths:
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(self.root.resolve())
            except ValueError as exc:
                raise ValueError(f"repair target escapes workspace: {resolved}") from exc
            entry = {"existed": resolved.exists(), "sha256": file_hash(resolved), "relative": str(relative)}
            if resolved.is_file():
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(resolved, backup)
                entry["backup"] = str(backup)
            manifest[str(relative)] = entry
        return manifest

    def restore(self, manifest: dict[str, dict[str, Any]]) -> None:
        for relative, entry in manifest.items():
            target = self.root / relative
            if entry.get("existed"):
                backup = Path(str(entry.get("backup") or ""))
                if backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
            elif target.exists() and target.is_file():
                target.unlink()

    def execute_fixer(self, finding: ProbeResult, date: str) -> RepairResult:
        if finding.fixer == "refresh_research_quality":
            script = self.briefing_root / "scripts" / "research_quality.py"
            completed = self.runner([sys.executable, str(script), "--date", date], cwd=self.root, timeout=180)
            return RepairResult("applied" if completed.returncode == 0 else "failed", completed.stderr[-1000:] or completed.stdout[-1000:])
        if finding.fixer == "refresh_review_queue":
            script = self.briefing_root / "scripts" / "briefing_store.py"
            completed = self.runner([sys.executable, str(script), "due-reviews", "--date", date], cwd=self.root, timeout=180)
            return RepairResult("applied" if completed.returncode == 0 else "failed", completed.stderr[-1000:] or completed.stdout[-1000:])
        if finding.fixer == "refresh_resolution_evidence":
            script = self.briefing_root / "scripts" / "resolution_evidence.py"
            completed = self.runner(
                [sys.executable, str(script), "prepare", "--date", date, "--no-network"],
                cwd=self.root,
                timeout=180,
            )
            return RepairResult("applied" if completed.returncode == 0 else "failed", completed.stderr[-1000:] or completed.stdout[-1000:])
        if finding.fixer == "refresh_drift_diagnostics":
            script = self.briefing_root / "scripts" / "drift_diagnostics.py"
            completed = self.runner(
                [sys.executable, str(script), "--date", date, "--write"],
                cwd=self.root,
                timeout=180,
            )
            return RepairResult("applied" if completed.returncode == 0 else "failed", completed.stderr[-1000:] or completed.stdout[-1000:])
        if finding.fixer == "regenerate_site_payload":
            script = self.briefing_root / "scripts" / "sync_briefing_site.py"
            completed = self.runner([sys.executable, str(script), "--date", date], cwd=self.root, timeout=180)
            return RepairResult("applied" if completed.returncode == 0 else "failed", completed.stderr[-1000:] or completed.stdout[-1000:])
        if finding.fixer == "normalize_site_state":
            path = self.briefing_root / "data" / "site-sync-state.json"
            state = read_json(path, {})
            if (
                not isinstance(state, dict)
                or state.get("pending_sha") != state.get("last_deployed_sha")
                or str(state.get("pending_payload_sha") or "") != str(state.get("last_deployed_payload_sha") or "")
            ):
                return RepairResult("noop", "site state no longer needs normalization")
            for key in ("pending_sha", "pending_payload_sha", "pending_report", "pending_date"):
                state.pop(key, None)
            atomic_write_json(path, state)
            return RepairResult("applied", "removed already-deployed pending markers", [str(path.relative_to(self.root))])
        if finding.fixer == "refresh_macro_bridge":
            source = self.briefing_root / "data" / f"macro_signals-{date}.jsonl"
            target = self.trading_root / "data" / "macro_signals" / source.name
            if not source.is_file():
                return RepairResult("failed", "source macro signal file is missing")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            shutil.copy2(source, temporary)
            temporary.replace(target)
            return RepairResult("applied", "refreshed derived macro signal bridge", [str(target.relative_to(self.root))])
        if finding.fixer == "remove_stale_cycle_lock":
            path = self.root / finding.resource
            stale, _ = self.lock_is_stale(path)
            if path.exists() and stale:
                path.unlink()
                return RepairResult("applied", "removed stale cycle lock", [finding.resource])
            return RepairResult("noop", "cycle lock is no longer stale")
        if finding.fixer == "refresh_improvement_tracker":
            script = self.briefing_root / "scripts" / "improvement_tracker.py"
            completed = self.runner(
                [sys.executable, str(script), "--date", date, "--apply-safe", "--strict"],
                cwd=self.root,
                timeout=180,
            )
            return RepairResult("applied" if completed.returncode == 0 else "failed", completed.stderr[-1000:] or completed.stdout[-1000:])
        return RepairResult("blocked", "no allowlisted fixer")

    def verify_finding(self, finding: ProbeResult, date: str) -> bool:
        if finding.check_id == "research_quality_artifact":
            return self.probe_research_quality(date)[1].passed
        if finding.check_id == "review_queue_freshness":
            return self.probe_review_queue(date).passed
        if finding.check_id == "resolution_evidence_freshness":
            return self.probe_resolution_evidence(date).passed
        if finding.check_id == "drift_diagnostics_freshness":
            return self.probe_drift_diagnostics(date).passed
        if finding.check_id == "site_payload_freshness":
            return self.probe_site(date)[0].passed
        if finding.check_id == "site_state_consistency":
            return self.probe_site(date)[1].passed
        if finding.check_id == "macro_bridge_freshness":
            return self.probe_macro_bridge(date).passed
        if finding.check_id == "cycle_lock_health":
            return self.probe_cycle_lock().passed
        if finding.check_id == "improvement_tracker_current":
            return self.probe_improvement_tracker(date).passed
        return False

    def can_auto_fix(self, finding: ProbeResult, issue: dict[str, Any]) -> tuple[bool, str]:
        if not finding.fixer:
            return False, "no allowlisted fixer"
        if finding.risk not in set(self.policy.get("auto_fix_risks", [])):
            return False, f"risk {finding.risk} requires approval"
        if finding.risk != "low":
            return False, "only low-risk repairs are eligible"
        max_attempts = int(self.policy.get("max_attempts_per_issue", 3))
        if int(issue.get("repair_attempts", 0)) >= max_attempts:
            return False, "circuit breaker: maximum repair attempts reached"
        last_attempt = issue.get("last_repair_at")
        if last_attempt:
            try:
                timestamp = datetime.fromisoformat(str(last_attempt).replace("Z", "+00:00"))
                cooldown = timedelta(minutes=int(self.policy.get("cooldown_minutes", 60)))
                if datetime.now(UTC) - timestamp < cooldown and issue.get("last_repair_status") == "failed":
                    return False, "circuit breaker: failed repair is cooling down"
            except ValueError:
                pass
        return True, "eligible"

    def reconcile_issues(self, results: list[ProbeResult]) -> dict[str, Any]:
        state = read_json(self.state_path, {"schema_version": 1, "issues": {}})
        if not isinstance(state, dict):
            state = {"schema_version": 1, "issues": {}}
        issues = state.setdefault("issues", {})
        now = utc_now()
        for result in results:
            issue = issues.get(result.fingerprint)
            if result.passed:
                if isinstance(issue, dict) and issue.get("status") not in {"resolved", "closed"}:
                    issue.update({"status": "resolved", "resolved_at": now, "last_seen": now, "resolution": "probe passed"})
                continue
            if not isinstance(issue, dict):
                issue = {
                    "issue_id": f"ATLAS-{result.fingerprint.upper()}",
                    "fingerprint": result.fingerprint,
                    "first_seen": now,
                    "occurrences": 0,
                    "repair_attempts": 0,
                }
                issues[result.fingerprint] = issue
            issue.update({
                "check_id": result.check_id,
                "resource": result.resource,
                "severity": result.severity,
                "risk": result.risk,
                "summary": result.summary,
                "evidence": result.evidence,
                "fixer": result.fixer,
                "last_seen": now,
                "status": "open",
                "resolved_at": None,
            })
            issue["occurrences"] = int(issue.get("occurrences", 0)) + 1
        state["updated_at"] = now
        return state

    def run(self, date: str, *, apply_safe: bool, deep: bool, strict: bool) -> tuple[int, dict[str, Any]]:
        run_id = f"ATLAS-HEAL-{date.replace('-', '')}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        started_at = utc_now()
        results = self.detect(date, deep=deep)
        state = self.reconcile_issues(results)
        issues = state["issues"]
        repair_records: list[dict[str, Any]] = []
        repair_budget = int(self.policy.get("max_repairs_per_run", 4))
        run_root = self.runs_root / date / run_id

        if apply_safe:
            for finding in [item for item in results if not item.passed]:
                if len(repair_records) >= repair_budget:
                    break
                issue = issues[finding.fingerprint]
                eligible, reason = self.can_auto_fix(finding, issue)
                if not eligible:
                    issue["status"] = "requires_approval" if "approval" in reason or "no allowlisted" in reason else "quarantined"
                    issue["automation_decision"] = reason
                    continue
                issue["status"] = "healing"
                issue["repair_attempts"] = int(issue.get("repair_attempts", 0)) + 1
                issue["last_repair_at"] = utc_now()
                targets = self.repair_targets(finding)
                backup_root = run_root / "backups" / finding.fingerprint
                manifest = self.snapshot(targets, backup_root)
                repair = self.execute_fixer(finding, date)
                verified = repair.status in {"applied", "noop"} and self.verify_finding(finding, date)
                rolled_back = False
                if not verified and self.policy.get("rollback_on_verification_failure", True):
                    self.restore(manifest)
                    rolled_back = True
                repair.rolled_back = rolled_back
                issue["last_repair_status"] = "verified" if verified else "failed"
                issue["automation_decision"] = repair.detail
                if verified:
                    issue.update({"status": "resolved", "resolved_at": utc_now(), "resolution": "automatic repair verified"})
                elif int(issue.get("repair_attempts", 0)) >= int(self.policy.get("max_attempts_per_issue", 3)):
                    issue["status"] = "quarantined"
                else:
                    issue["status"] = "open"
                repair_records.append({
                    "issue_id": issue["issue_id"],
                    "check_id": finding.check_id,
                    "fingerprint": finding.fingerprint,
                    "fixer": finding.fixer,
                    "status": issue["last_repair_status"],
                    "detail": repair.detail,
                    "rolled_back": rolled_back,
                    "manifest": manifest,
                })

        atomic_write_json(self.state_path, state)
        unresolved = [
            issue for issue in issues.values()
            if issue.get("status") not in {"resolved", "closed"}
        ]
        unresolved.sort(key=lambda item: (-SEVERITY_ORDER.get(str(item.get("severity")), 0), str(item.get("issue_id"))))
        blocking_levels = set(self.policy.get("blocking_severities", ["critical"]))
        blocking = [issue for issue in unresolved if issue.get("severity") in blocking_levels]
        detected_failures = sum(not item.passed for item in results)
        verified_repairs = sum(item["status"] == "verified" for item in repair_records)
        remaining_failures = max(0, detected_failures - verified_repairs)
        verified_fingerprints = {
            str(item.get("fingerprint") or "")
            for item in repair_records
            if item.get("status") == "verified"
        }
        checks: list[dict[str, Any]] = []
        for item in results:
            effective_passed = item.passed or item.fingerprint in verified_fingerprints
            checks.append(
                asdict(item)
                | {
                    "fingerprint": item.fingerprint,
                    "effective_passed": effective_passed,
                    "effective_status": (
                        "passed"
                        if item.passed
                        else "repaired"
                        if effective_passed
                        else "failed"
                    ),
                }
            )
        finished_at = utc_now()
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "date": date,
            "started_at": started_at,
            "finished_at": finished_at,
            "evidence_observed_at": finished_at,
            "evidence_scope": "self_healing_run_snapshot",
            "mode": "apply_safe" if apply_safe else "detect_only",
            "deep": deep,
            "strict": strict,
            "overall_status": "blocked" if blocking else "degraded" if unresolved else "healthy",
            "checks": checks,
            "repairs": repair_records,
            "counts": {
                "checks": len(results),
                "passed": len(results) - remaining_failures,
                "failed": remaining_failures,
                "detected_failures": detected_failures,
                "repairs_attempted": len(repair_records),
                "repairs_verified": verified_repairs,
                "unresolved": len(unresolved),
                "blocking": len(blocking),
            },
            "unresolved_issues": unresolved,
            "boundaries": self.policy.get("boundaries", {}),
        }
        atomic_write_json(run_root / "run.json", report)
        atomic_write_json(self.latest_path, report)
        atomic_write_text(self.latest_markdown_path, render_markdown(report))
        append_jsonl(self.audit_log_path, {
            "run_id": run_id,
            "date": date,
            "finished_at": report["finished_at"],
            "overall_status": report["overall_status"],
            "counts": report["counts"],
            "report": str((run_root / "run.json").relative_to(self.root)),
        })
        return (1 if strict and blocking else 0), report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ATLAS Self-Healing Report",
        "",
        f"- run_id: {report['run_id']}",
        f"- date: {report['date']}",
        f"- status: {report['overall_status']}",
        f"- mode: {report['mode']}",
        f"- checks: {report['counts']['passed']}/{report['counts']['checks']} passed",
        f"- repairs: {report['counts']['repairs_verified']}/{report['counts']['repairs_attempted']} verified",
        f"- unresolved: {report['counts']['unresolved']}",
        "",
        "## Unresolved issues",
    ]
    unresolved = report.get("unresolved_issues", [])
    if not unresolved:
        lines.append("- None")
    for issue in unresolved:
        lines.append(
            f"- {issue.get('issue_id')} [{issue.get('severity')}/{issue.get('risk')}] "
            f"{issue.get('summary')} — status={issue.get('status')}"
        )
    lines.extend([
        "",
        "## Safety boundaries",
        "- Auto-repair is restricted to allowlisted low-risk derived artifacts.",
        "- Source code, research conclusions, probabilities, virtual orders, production deployment, and real orders are not auto-modified.",
        "- Every attempted repair is snapshotted, verified, audited, and rolled back on verification failure.",
        "",
    ])
    return "\n".join(lines)


@contextmanager
def self_healing_lock(engine: SelfHealingEngine):
    path = engine.lock_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "started_at": utc_now()}
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            stale, _evidence = engine.lock_is_stale(path)
            if stale:
                path.unlink(missing_ok=True)
                continue
            raise RuntimeError("self-healing run already active") from exc
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            break
    else:
        raise RuntimeError("unable to acquire self-healing lock")
    try:
        yield
    finally:
        current = read_json(path, {})
        if isinstance(current, dict) and current.get("pid") == os.getpid():
            path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATLAS closed-loop self-healing control plane")
    parser.add_argument("--date", type=valid_date)
    parser.add_argument("--apply-safe", action="store_true", help="Apply allowlisted low-risk repairs")
    parser.add_argument("--deep", action="store_true", help="Run briefing and website test suites")
    parser.add_argument("--strict", action="store_true", help="Fail when a blocking issue remains")
    parser.add_argument("--status", action="store_true", help="Print the most recent self-healing report")
    parser.add_argument("--json", action="store_true", help="Print full machine-readable output")
    args = parser.parse_args(argv)
    try:
        engine = SelfHealingEngine()
        if args.status:
            payload = read_json(engine.latest_path, {})
            print(json.dumps(status_view(payload), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if payload else 2
        date = args.date or latest_report_date()
        with self_healing_lock(engine):
            returncode, report = engine.run(date, apply_safe=args.apply_safe, deep=args.deep, strict=args.strict)
    except (FileNotFoundError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps({
            "status": report["overall_status"],
            "run_id": report["run_id"],
            "date": report["date"],
            "counts": report["counts"],
            "latest_report": str(engine.latest_markdown_path),
        }, ensure_ascii=False))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
