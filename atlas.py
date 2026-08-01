#!/usr/bin/env python3
"""Unified command surface for the ATLAS briefing and research workspace."""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date as Date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ATLAS_WORKSPACE_ROOT_ENV = "ATLAS_WORKSPACE_ROOT"


def resolve_workspace_root() -> Path:
    """Resolve an explicit checkout for installed operational CLI commands.

    A wheel must never guess a workspace from ``site-packages``.  Source-tree
    execution keeps the historical default; an explicit override is accepted
    only when it is absolute and contains every governed component.
    """

    configured = os.environ.get(ATLAS_WORKSPACE_ROOT_ENV)
    if configured is None:
        return Path(__file__).resolve().parent
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise RuntimeError(f"{ATLAS_WORKSPACE_ROOT_ENV} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{ATLAS_WORKSPACE_ROOT_ENV} cannot be resolved: {exc}") from exc
    required = (
        resolved / "atlas.py",
        resolved / "work" / "global-briefing",
        resolved / "work" / "trading-core",
        resolved / "src",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(
            f"{ATLAS_WORKSPACE_ROOT_ENV} is not an ATLAS checkout; missing: "
            + ", ".join(missing)
        )
    return resolved


ROOT = resolve_workspace_root()
BRIEFING_ROOT = ROOT / "work" / "global-briefing"
TRADING_ROOT = ROOT / "work" / "trading-core"
SITE_ROOT = ROOT / "src"
BRIEFING_SETTINGS_PATH = BRIEFING_ROOT / "config" / "settings.json"
PAPER_TRADING_CONFIG_PATH = BRIEFING_ROOT / "config" / "paper_trading.json"
PREDICTION_LEDGER_PATH = BRIEFING_ROOT / "data" / "predictions.jsonl"
IMPROVEMENT_TRACKING_CONFIG_PATH = BRIEFING_ROOT / "config" / "improvement_tracking.json"
OUTPUTS_ROOT = ROOT / "outputs"
ATLAS_RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas"
VIRTUAL_LEDGER_ROOT = ATLAS_RUNTIME_ROOT / "virtual_execution"
VIRTUAL_LEDGER_PATH = VIRTUAL_LEDGER_ROOT / "atlas_virtual_execution_ledger.jsonl"
VIRTUAL_LEDGER_STATE_PATH = VIRTUAL_LEDGER_ROOT / "atlas_virtual_execution_state.json"
VIRTUAL_LEDGER_AUDIT_PATH = VIRTUAL_LEDGER_ROOT / "atlas_virtual_execution_audit.json"
VIRTUAL_LEDGER_HEAD_PATH = VIRTUAL_LEDGER_ROOT / "current.json"
VIRTUAL_LEDGER_GENERATIONS_ROOT = VIRTUAL_LEDGER_ROOT / "generations"
RUN_AUDIT_ROOT = ATLAS_RUNTIME_ROOT / "run_audits"
CYCLE_STATE_PATH = ATLAS_RUNTIME_ROOT / "cycle_state.json"
REPORT_PATTERN = re.compile(r"每日全球晨间简报-(\d{4}-\d{2}-\d{2})\.md$")
REPLAY_EVALUATION_PATTERN = re.compile(
    r"global_briefing_replay_evaluation-(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})\.json$"
)
LEDGER_ID = "atlas-virtual-execution-ledger-v1"
LEDGER_SCHEMA_VERSION = 1
GIT_REMOTE_PROBE_TIMEOUT_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 30 * 60
COMMAND_TIMEOUT_RETURN_CODE = 124
HISTORY_ANCHOR_SCHEMA_VERSION = 2
GLOBAL_HISTORY_ANCHOR_SCHEMA_VERSION = 3
GLOBAL_HISTORY_MIGRATION_SCHEMA_VERSION = 1
GLOBAL_HISTORY_RECOVERY_EVIDENCE_SCHEMA_VERSION = 1
GLOBAL_HISTORY_RECOVERY_PLAN_SCHEMA_VERSION = 1
GLOBAL_HISTORY_MIGRATION_KIND = "unit_test_trust_root_contamination"
LEDGER_ANCHOR_SCHEMA_VERSION = 2
LEGACY_ANCHOR_SCHEMA_VERSION = 1
TRUST_ANCHOR_ROOT_ENV = "ATLAS_TRUST_ANCHOR_ROOT"
TRUST_ANCHOR_HMAC_KEY_ENV = "ATLAS_TRUST_ANCHOR_HMAC_KEY"
TRUST_ANCHOR_NAMESPACE_ENV = "ATLAS_TRUST_ANCHOR_NAMESPACE"
TRUST_ANCHOR_WITNESS_ENV = "ATLAS_TRUST_ANCHOR_WITNESS"
GLOBAL_HISTORY_RECOVERY_EVIDENCE_DIRECTORY = "recovery-evidence"
GLOBAL_HISTORY_RECOVERY_TEST_FILE = "tests/test_atlas_publication_orchestration.py"
GLOBAL_HISTORY_RECOVERY_TEST_CASE = (
    "AtlasPublicationOrchestrationTests."
    "test_default_cli_cycle_returns_blocked_phase_b_without_rewriting_core_success"
)
GLOBAL_HISTORY_MIGRATION_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "source_revision",
        "source_anchor_sha256",
        "source_workspace_uuid",
        "source_entries_sha256",
        "source_entry_count",
        "target_workspace_uuid",
        "target_entries_sha256",
        "target_entry_count",
        "daily_anchor_evidence_sha256",
        "backup_latest_file_sha256",
        "backup_manifest_file_sha256",
        "backup_manifest_sha256",
        "backup_entry_manifest_sha256",
        "backup_archive_sha256",
        "backup_created_at",
        "recovery_evidence_path",
        "recovery_evidence_sha256",
        "recovery_plan_sha256",
    }
)
GLOBAL_HISTORY_RECOVERY_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "cause",
        "source_anchor",
        "target_history",
        "daily_anchors",
        "backup",
    }
)
MINIMUM_NODE_VERSION = (22, 15, 0)
RELEASE_REQUIRED_STAGES = (
    "doctor",
    "sync",
    "canonical_virtual_ledger_audit",
    "replay_shadow_gate",
    "targeted_integration_tests",
    "canonical_virtual_ledger_commit",
)
RELEASE_EVIDENCE_SCHEMA_VERSION = 1
RELEASE_EVIDENCE_WORKFLOW = ".github/workflows/quality.yml"
RELEASE_EVIDENCE_SOURCE_REF = "refs/heads/main"
RELEASE_EVIDENCE_REQUIRED_JOBS = (
    "briefing-control-plane",
    "composed-workspace",
)
RELEASE_EVIDENCE_MAX_BYTES = 64 * 1024
BRIEFING_TEST_INPUT_SCHEMA_VERSION = 1
PUBLICATION_ORCHESTRATION_SCHEMA_VERSION = 1
PUBLICATION_BLOCKED_RETURN_CODE = 3
PUBLICATION_NO_CANDIDATE_RETURN_CODE = 4
PUBLICATION_ERROR_RETURN_CODE = 2
FORBIDDEN_LEDGER_KEYS = {
    "broker_order_id",
    "broker_account",
    "account_number",
    "live_order_id",
    "external_order_id",
    "real_order_id",
}
SAFETY_FLAGS_REQUIRED_TRUE = {
    "paper_trading_only",
    "no_real_broker_order",
}
SAFETY_FLAGS_REQUIRED_FALSE = {
    "external_broker_connection",
    "live_trading",
    "real_broker_orders_allowed",
}
NORMALIZED_FORBIDDEN_LEDGER_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in FORBIDDEN_LEDGER_KEYS
}
NORMALIZED_SAFETY_FLAGS_REQUIRED_TRUE = {
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in SAFETY_FLAGS_REQUIRED_TRUE
}
NORMALIZED_SAFETY_FLAGS_REQUIRED_FALSE = {
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in SAFETY_FLAGS_REQUIRED_FALSE
}
NORMALIZED_LEDGER_NUMERIC_KEYS = {
    "cash",
    "cashafter",
    "equity",
    "equityafter",
    "fee",
    "filledprice",
    "filledquantity",
    "grossvalue",
    "initialcash",
    "notional",
    "price",
    "quantity",
    "realizedpnl",
    "tax",
}
NORMALIZED_NON_NEGATIVE_LEDGER_NUMERIC_KEYS = {
    "fee",
    "filledprice",
    "filledquantity",
    "grossvalue",
    "notional",
    "price",
    "quantity",
    "tax",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    required: bool = True


@dataclass(frozen=True)
class TestGate:
    name: str
    command: tuple[str, ...]
    cwd: Path = ROOT
    trading_core: bool = False
    timeout: float | None = COMMAND_TIMEOUT_SECONDS


def command_env(*, trading_core: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if trading_core:
        source = str(TRADING_ROOT / "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source if not existing else f"{source}{os.pathsep}{existing}"
    return env


def run_command(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    trading_core: bool = False,
    quiet: bool = False,
    timeout: float | None = COMMAND_TIMEOUT_SECONDS,
) -> int:
    if not quiet:
        print(f"\n> {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=command_env(trading_core=trading_core),
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        limit = f"{timeout:g} seconds" if timeout is not None else "the configured timeout"
        print(f"command timed out after {limit}", file=sys.stderr)
        return COMMAND_TIMEOUT_RETURN_CODE
    return completed.returncode


def capture_command(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    trading_core: bool = False,
    timeout: float | None = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=command_env(trading_core=trading_core),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        limit = f"{timeout:g} seconds" if timeout is not None else "the configured timeout"
        return subprocess.CompletedProcess(
            list(command),
            COMMAND_TIMEOUT_RETURN_CODE,
            stdout="",
            stderr=f"command timed out after {limit}",
        )


def probe_git_remotes(
    git: str,
    path: Path,
    remote_names: Sequence[str],
    *,
    head_commit: str,
) -> dict[str, Any]:
    """Verify that the exact local HEAD is advertised by at least one configured remote."""
    probes: list[dict[str, Any]] = []
    for remote in remote_names:
        try:
            result = capture_command(
                [git, "-c", "credential.interactive=never", "ls-remote", remote],
                cwd=path,
                timeout=GIT_REMOTE_PROBE_TIMEOUT_SECONDS,
            )
            advertised_commits = {
                line.split()[0]
                for line in result.stdout.splitlines()
                if len(line.split()) >= 2 and re.fullmatch(r"[0-9a-fA-F]{40}", line.split()[0])
            }
            timed_out = result.returncode == COMMAND_TIMEOUT_RETURN_CODE
            head_advertised = bool(head_commit and head_commit in advertised_commits)
            fetchable = result.returncode == 0 and head_advertised
            probes.append(
                {
                    "remote": remote,
                    "fetchable": fetchable,
                    "head_advertised": head_advertised,
                    "returncode": result.returncode,
                    "detail": (
                        "probe_timeout"
                        if timed_out
                        else "head_advertised"
                        if fetchable
                        else "head_not_advertised"
                        if result.returncode == 0
                        else "probe_failed"
                    ),
                }
            )
        except subprocess.TimeoutExpired:
            probes.append(
                {
                    "remote": remote,
                    "fetchable": False,
                    "returncode": None,
                    "detail": "probe_timeout",
                }
            )
    fetchable_remote_names = sorted(
        str(probe["remote"]) for probe in probes if probe["fetchable"] is True
    )
    return {
        "remote_fetchable": bool(fetchable_remote_names),
        "fetchable_remote_names": fetchable_remote_names,
        "remote_probes": probes,
    }


def component_repository_check(
    name: str,
    path: Path,
    *,
    probe_remotes: bool = True,
) -> Check:
    """Report whether a separately governed workspace component is reproducible."""
    git = shutil.which("git")
    if not path.exists():
        return Check(f"{name} repository", "warn", "component path is missing", required=False)
    if not git:
        return Check(f"{name} repository", "warn", "git is unavailable", required=False)
    if not (path / ".git").exists():
        return Check(
            f"{name} repository",
            "warn",
            "component has no independent .git metadata",
            required=False,
        )

    head = capture_command([git, "rev-parse", "HEAD"], cwd=path)
    branch = capture_command([git, "branch", "--show-current"], cwd=path)
    status = capture_command([git, "status", "--porcelain", "--untracked-files=normal"], cwd=path)
    remotes = capture_command([git, "remote"], cwd=path)
    if any(result.returncode != 0 for result in (head, branch, status, remotes)):
        return Check(f"{name} repository", "warn", "git metadata could not be inspected", required=False)

    dirty_count = len([line for line in status.stdout.splitlines() if line.strip()])
    remote_names = [line.strip() for line in remotes.stdout.splitlines() if line.strip()]
    head_commit = head.stdout.strip()
    remote_status = (
        {
            **probe_git_remotes(git, path, remote_names, head_commit=head_commit),
            "remote_probe_status": "completed",
        }
        if probe_remotes
        else {
            "remote_fetchable": False,
            "fetchable_remote_names": [],
            "remote_probes": [],
            "remote_probe_status": "not_requested_daily",
        }
    )
    details = [
        f"commit={head_commit[:12] or 'unknown'}",
        f"branch={branch.stdout.strip() or 'detached'}",
        f"worktree={'clean' if dirty_count == 0 else f'dirty({dirty_count})'}",
        f"remotes={','.join(remote_names) if remote_names else 'missing'}",
        (
            "fetchable_remotes="
            + (
                ",".join(remote_status["fetchable_remote_names"])
                if remote_status["fetchable_remote_names"]
                else "none"
            )
        ),
        f"remote_probe={remote_status['remote_probe_status']}",
    ]
    repository_ready = dirty_count == 0 and remote_status["remote_fetchable"]
    return Check(
        f"{name} repository",
        "ok" if repository_ready else "warn",
        "; ".join(details),
        required=False,
    )


def relevant_provenance_path(repository: str, relative: str) -> bool:
    """Select producer source while excluding mutable derived datasets."""

    normalized = relative.replace("\\", "/").lstrip("./")
    if not normalized or normalized.startswith("../"):
        return False
    if repository == "root":
        excluded = (
            "outputs/",
            "work/global-briefing/data/",
            "work/global-briefing/tmp/",
            "work/shared/atlas/",
        )
        if normalized in {"src", "work/trading-core"}:
            return False
        return not normalized.startswith(excluded)
    if repository == "site":
        return normalized not in {
            "app/briefing.generated.json",
            "app/publication.generated.json",
        } and not normalized.startswith((".wrangler/", ".next/", "dist/", "node_modules/"))
    if repository == "trading-core":
        return not normalized.startswith(
            ("data/", "outputs/", "artifacts/", ".pytest_cache/", ".ruff_cache/")
        )
    return True


def repository_dirty_source_manifest(git: str, repository: str, path: Path) -> dict[str, Any]:
    """Hash dirty source paths without persisting their potentially sensitive bytes."""

    tracked = capture_command(
        [git, "diff", "--name-only", "--diff-filter=ACDMRTUXB", "HEAD", "--"],
        cwd=path,
    )
    untracked = capture_command(
        [git, "ls-files", "--others", "--exclude-standard"],
        cwd=path,
    )
    if tracked.returncode != 0 or untracked.returncode != 0:
        return {"available": False, "entries": [], "content_sha256": None}
    tracked_names = {line.strip() for line in tracked.stdout.splitlines() if line.strip()}
    untracked_names = {line.strip() for line in untracked.stdout.splitlines() if line.strip()}
    entries: list[dict[str, Any]] = []
    for relative in sorted(tracked_names | untracked_names):
        if not relevant_provenance_path(repository, relative):
            continue
        candidate = (path / relative).resolve()
        try:
            candidate.relative_to(path.resolve())
        except ValueError:
            continue
        kind = "untracked" if relative in untracked_names else "tracked"
        if not candidate.is_file():
            entries.append({"path": relative.replace("\\", "/"), "kind": kind, "deleted": True})
            continue
        entries.append(
            {
                "path": relative.replace("\\", "/"),
                "kind": kind,
                "bytes": candidate.stat().st_size,
                "sha256": file_sha256(candidate),
            }
        )
    return {
        "available": True,
        "entries": entries,
        "entry_count": len(entries),
        "content_sha256": stable_hash(entries),
    }


def repository_derived_artifacts(repository: str, path: Path) -> list[dict[str, Any]]:
    if repository != "site":
        return []
    artifacts = []
    for relative in ("app/briefing.generated.json", "app/publication.generated.json"):
        candidate = path / relative
        if candidate.is_file():
            artifacts.append(
                {
                    "path": relative,
                    "bytes": candidate.stat().st_size,
                    "sha256": file_sha256(candidate),
                }
            )
    return artifacts


def git_repository_provenance(
    name: str,
    path: Path,
    *,
    probe_remotes: bool = True,
) -> dict[str, Any]:
    git = shutil.which("git")
    if not git or not (path / ".git").exists():
        return {"name": name, "path": relative_path(path), "available": False, "release_ready": False}
    head = capture_command([git, "rev-parse", "HEAD"], cwd=path)
    branch = capture_command([git, "branch", "--show-current"], cwd=path)
    status = capture_command([git, "status", "--porcelain", "--untracked-files=normal"], cwd=path)
    remotes = capture_command([git, "remote"], cwd=path)
    commands_passed = all(result.returncode == 0 for result in (head, branch, status, remotes))
    dirty_paths = [line for line in status.stdout.splitlines() if line.strip()] if status.returncode == 0 else []
    remote_names = sorted(line.strip() for line in remotes.stdout.splitlines() if line.strip()) if remotes.returncode == 0 else []
    head_commit = head.stdout.strip() if head.returncode == 0 else ""
    remote_status = (
        {
            **probe_git_remotes(git, path, remote_names, head_commit=head_commit),
            "remote_probe_status": "completed",
        }
        if probe_remotes
        else {
            "remote_fetchable": False,
            "fetchable_remote_names": [],
            "remote_probes": [],
            "remote_probe_status": "not_requested_daily",
        }
    )
    dirty_source = repository_dirty_source_manifest(git, name, path)
    derived_artifacts = repository_derived_artifacts(name, path)
    payload = {
        "name": name,
        "path": relative_path(path),
        "available": commands_passed,
        "commit": head_commit or None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "clean": commands_passed and not dirty_paths,
        "dirty_path_count": len(dirty_paths),
        "remote_names": remote_names,
        "remote_count": len(remote_names),
        "dirty_source": dirty_source,
        "derived_artifacts": derived_artifacts,
        "derived_artifacts_sha256": stable_hash(derived_artifacts),
        **remote_status,
    }
    payload["release_ready"] = bool(
        payload["available"]
        and payload["clean"]
        and payload["commit"]
        and payload["remote_fetchable"]
    )
    return payload


def collect_repository_provenance(
    repositories: Sequence[tuple[str, Path]],
    *,
    probe_remotes: bool,
) -> list[dict[str, Any]]:
    """Inspect independent repositories concurrently while preserving declaration order."""

    if not repositories:
        return []

    def inspect(item: tuple[str, Path]) -> dict[str, Any]:
        name, path = item
        return git_repository_provenance(name, path, probe_remotes=probe_remotes)

    workers = min(3, len(repositories))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="atlas-repo") as pool:
        return list(pool.map(inspect, repositories))


def repository_check_from_provenance(name: str, payload: dict[str, Any]) -> Check:
    if payload.get("available") is not True:
        return Check(
            f"{name} repository",
            "warn",
            "git metadata could not be inspected",
            required=False,
        )
    dirty_count = int(payload.get("dirty_path_count") or 0)
    remote_names = [str(value) for value in payload.get("remote_names", [])]
    fetchable = [str(value) for value in payload.get("fetchable_remote_names", [])]
    detail = "; ".join(
        [
            f"commit={str(payload.get('commit') or '')[:12] or 'unknown'}",
            f"branch={payload.get('branch') or 'detached'}",
            f"worktree={'clean' if dirty_count == 0 else f'dirty({dirty_count})'}",
            f"remotes={','.join(remote_names) if remote_names else 'missing'}",
            f"fetchable_remotes={','.join(fetchable) if fetchable else 'none'}",
            f"remote_probe={payload.get('remote_probe_status', 'unknown')}",
        ]
    )
    return Check(
        f"{name} repository",
        "ok" if payload.get("release_ready") is True else "warn",
        detail,
        required=False,
    )


def build_workspace_lock(*, probe_remotes: bool = True) -> dict[str, Any]:
    repositories = collect_repository_provenance(
        [
            ("root", ROOT),
            ("site", SITE_ROOT),
            ("trading-core", TRADING_ROOT),
        ],
        probe_remotes=probe_remotes,
    )
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "repositories": repositories,
        "release_reproducible": all(repository["release_ready"] for repository in repositories),
    }
    payload["content_sha256"] = stable_hash(
        {key: value for key, value in payload.items() if key != "generated_at"}
    )
    return payload


def github_repository_from_origin() -> str | None:
    """Return ``owner/repository`` for the root GitHub origin, if unambiguous."""

    git = shutil.which("git")
    if not git:
        return None
    result = capture_command([git, "remote", "get-url", "origin"], cwd=ROOT, timeout=10)
    if result.returncode != 0:
        return None
    remote = result.stdout.strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/\s]+)/([^/\s]+?)(?:\.git)?/?",
        remote,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    slug = f"{match.group(1)}/{match.group(2)}"
    return slug if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug) else None


def verify_release_evidence(
    evidence_path: Path,
    *,
    workspace_lock: dict[str, Any],
    expected_repository: str,
    expected_source_ref: str,
) -> dict[str, Any]:
    """Verify commit-bound CI evidence with GitHub's Sigstore trust root.

    The JSON payload is intentionally small and user-readable.  Its contents do
    not become trusted until ``gh attestation verify`` authenticates the exact
    file, signer workflow, source commit/ref, and hosted-runner policy.
    """

    resolved = evidence_path.expanduser().resolve()
    result: dict[str, Any] = {
        "requested": True,
        "verified": False,
        "artifact_path": str(resolved),
        "artifact_sha256": None,
        "artifact_bytes": None,
        "repository": expected_repository,
        "source_ref": expected_source_ref,
        "workflow": RELEASE_EVIDENCE_WORKFLOW,
        "run_id": None,
        "run_attempt": None,
        "verification_count": 0,
        "errors": [],
    }
    errors: list[str] = result["errors"]
    if not resolved.is_file():
        errors.append("release evidence file is missing")
        return result
    try:
        size = resolved.stat().st_size
    except OSError:
        errors.append("release evidence metadata could not be read")
        return result
    result["artifact_bytes"] = size
    if size <= 0 or size > RELEASE_EVIDENCE_MAX_BYTES:
        errors.append("release evidence file size is outside the allowed range")
        return result
    try:
        before_hash = file_sha256(resolved)
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append("release evidence is not readable UTF-8 JSON")
        return result
    result["artifact_sha256"] = before_hash
    if not isinstance(payload, dict):
        errors.append("release evidence must be a JSON object")
        return result

    expected_repository = expected_repository.strip()
    expected_source_ref = expected_source_ref.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected_repository):
        errors.append("expected GitHub repository is invalid")
    if expected_source_ref != RELEASE_EVIDENCE_SOURCE_REF:
        errors.append("expected source ref must be refs/heads/main")
    scalar_requirements = {
        "schema_version": RELEASE_EVIDENCE_SCHEMA_VERSION,
        "provider": "github-actions",
        "workflow": RELEASE_EVIDENCE_WORKFLOW,
        "source_ref": expected_source_ref,
        "test_profile": "full",
    }
    for field, expected in scalar_requirements.items():
        if payload.get(field) != expected:
            errors.append(f"release evidence {field} does not match policy")
    repository = payload.get("repository")
    if not isinstance(repository, str) or repository.casefold() != expected_repository.casefold():
        errors.append("release evidence repository does not match policy")
    run_id = str(payload.get("run_id") or "")
    run_attempt = str(payload.get("run_attempt") or "")
    if not run_id.isdigit() or int(run_id) <= 0:
        errors.append("release evidence run_id is invalid")
    if not run_attempt.isdigit() or int(run_attempt) <= 0:
        errors.append("release evidence run_attempt is invalid")
    result["run_id"] = run_id or None
    result["run_attempt"] = run_attempt or None
    jobs = payload.get("required_jobs")
    if not isinstance(jobs, list) or set(jobs) != set(RELEASE_EVIDENCE_REQUIRED_JOBS):
        errors.append("release evidence required jobs do not match policy")

    repositories = workspace_lock.get("repositories")
    lock_commits = {
        str(item.get("name")): str(item.get("commit") or "").lower()
        for item in repositories
        if isinstance(item, dict)
    } if isinstance(repositories, list) else {}
    evidence_commits = payload.get("repository_commits")
    normalized_evidence_commits = {
        str(name): str(commit).lower()
        for name, commit in evidence_commits.items()
    } if isinstance(evidence_commits, dict) else {}
    required_names = {"root", "site", "trading-core"}
    commits_well_formed = all(
        re.fullmatch(r"[0-9a-f]{40}", lock_commits.get(name, ""))
        and re.fullmatch(r"[0-9a-f]{40}", normalized_evidence_commits.get(name, ""))
        for name in required_names
    )
    if (
        not commits_well_formed
        or set(normalized_evidence_commits) != required_names
        or any(normalized_evidence_commits.get(name) != lock_commits.get(name) for name in required_names)
    ):
        errors.append("repository commits do not match workspace lock")
    if errors:
        return result

    gh = shutil.which("gh")
    if not gh:
        errors.append("GitHub CLI is unavailable for attestation verification")
        return result
    verification = capture_command(
        [
            gh,
            "attestation",
            "verify",
            str(resolved),
            "--repo",
            expected_repository,
            "--source-digest",
            lock_commits["root"],
            "--source-ref",
            expected_source_ref,
            "--signer-workflow",
            f"{expected_repository}/{RELEASE_EVIDENCE_WORKFLOW}",
            "--signer-digest",
            lock_commits["root"],
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ],
        cwd=ROOT,
        timeout=120,
    )
    if verification.returncode != 0:
        errors.append(
            "GitHub artifact attestation verification timed out"
            if verification.returncode == COMMAND_TIMEOUT_RETURN_CODE
            else "GitHub artifact attestation verification failed"
        )
        return result
    try:
        verified = json.loads(verification.stdout)
    except json.JSONDecodeError:
        errors.append("GitHub attestation verifier returned invalid JSON")
        return result
    valid_results = [
        item
        for item in verified
        if isinstance(item, dict) and isinstance(item.get("verificationResult"), dict)
    ] if isinstance(verified, list) else []
    if not valid_results:
        errors.append("GitHub attestation verifier returned no verified statements")
        return result
    try:
        after_hash = file_sha256(resolved)
    except OSError:
        errors.append("release evidence could not be re-hashed after verification")
        return result
    if after_hash != before_hash:
        errors.append("release evidence changed during verification")
        return result
    result["verification_count"] = len(valid_results)
    result["verified"] = True
    return result


def latest_report() -> tuple[str, Path]:
    candidates: list[tuple[str, Path]] = []
    if OUTPUTS_ROOT.exists():
        for path in OUTPUTS_ROOT.glob("每日全球晨间简报-*.md"):
            match = REPORT_PATTERN.match(path.name)
            if not match:
                continue
            try:
                report_date = Date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                continue
            candidates.append((report_date, path))
    if not candidates:
        raise FileNotFoundError(f"No dated briefing report found under {OUTPUTS_ROOT}")
    return max(candidates, key=lambda item: item[0])


def valid_iso_date(value: str) -> str:
    """Return a canonical ISO date or raise an argparse-friendly error."""
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc
    canonical = parsed.isoformat()
    if value != canonical:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected {canonical}.")
    return canonical


def parse_version(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", text)
    if not match:
        return ()
    return tuple(int(value or 0) for value in match.groups())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stable_json(payload: Any) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def build_briefing_test_input_fingerprint(
    root: Path = ROOT,
    date: str | None = None,
) -> dict[str, Any]:
    """Bind the inputs exercised by global-briefing unittest discovery.

    The daily cycle records this before executing tests.  Deep self-healing may
    reuse the result only when it independently rebuilds the same fingerprint.
    Runtime ledgers are deliberately excluded; the suite's checked-in source,
    config, tests, report fixtures, and generated site fixture are included.
    """

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
                "sha256": file_sha256(resolved),
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

    # Some unittest contracts deliberately inspect older dated reports.  Their
    # presence and bytes therefore belong to the exercised runtime fixture.
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


def configured_trust_anchor_value(name: str) -> str | None:
    """Read an explicit process setting, or Windows' current-user fallback.

    Persistent Windows user environment values are not inherited by terminals
    that were already running when they were configured.  Falling back only
    when the variable is *absent* lets a fresh direct ``python atlas.py``
    invocation use the protected user setting without weakening explicit empty
    values used to disable a key or exercise fail-closed checks.
    """
    if name in os.environ:
        return os.environ.get(name)
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def configured_trust_anchor_root() -> tuple[Path | None, str | None]:
    """Return the protected anchor root, rejecting workspace-local storage.

    The mutable workspace and its runtime directory are deliberately not a trust
    boundary.  Operators may set ``ATLAS_TRUST_ANCHOR_ROOT`` to a protected
    volume, WORM mount, or separately administered sync target.  The default is
    outside the workspace so deleting ``work/shared/atlas`` cannot reset trust.
    Filesystem permissions and retention for that root remain an operator
    responsibility; the HMAC below makes an altered checkpoint detectable when
    its key is kept outside the workspace.
    """
    configured = configured_trust_anchor_value(TRUST_ANCHOR_ROOT_ENV)
    candidate = Path(configured).expanduser() if configured else Path.home() / ".atlas-trust"
    try:
        resolved = candidate.resolve()
        workspace = ROOT.resolve()
        runtime = ATLAS_RUNTIME_ROOT.resolve()
    except OSError as exc:
        return None, f"cannot resolve external trust-anchor root: {type(exc).__name__}: {exc}"
    for forbidden, description in ((workspace, "workspace"), (runtime, "mutable runtime")):
        try:
            resolved.relative_to(forbidden)
        except ValueError:
            continue
        return None, f"external trust-anchor root must not be inside the {description}"
    return resolved, None


def trust_anchor_path(kind: str, subject: str) -> Path:
    """Return a deterministic path in the externally protected trust root."""
    root, error = configured_trust_anchor_root()
    if root is None:
        raise RuntimeError(error or "external trust-anchor root is unavailable")
    namespace = str(configured_trust_anchor_value(TRUST_ANCHOR_NAMESPACE_ENV) or "").strip()
    if not namespace:
        namespace = workspace_project_uuid()
    identity = stable_hash(
        {
            "namespace": namespace,
            "kind": kind,
            "subject": subject,
        }
    )[:24]
    return root / "anchors" / f"{kind}-{identity}.json"


def workspace_project_uuid() -> str:
    """Return a stable project UUID without guessing identity from Git state."""

    namespace = str(configured_trust_anchor_value(TRUST_ANCHOR_NAMESPACE_ENV) or "").strip()
    identity = f"{namespace}:{str(ROOT.resolve()).casefold()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atlas-workspace:{identity}"))


def local_trust_key_path() -> Path:
    root, error = configured_trust_anchor_root()
    if root is None:
        raise RuntimeError(error or "external trust-anchor root is unavailable")
    return root / "local-only-hmac.key"


def trust_anchor_hmac_key(*, create: bool = False) -> bytes | None:
    """Read the configured key or a local-only key outside the workspace.

    An explicitly empty environment value still disables signing for fail-closed
    testing and emergency operation.  When no external key is configured, writes
    may create a mode-0600 local key; resulting evidence is labelled local-only
    and never represented as an independent witness.
    """

    raw = configured_trust_anchor_value(TRUST_ANCHOR_HMAC_KEY_ENV)
    if raw is not None:
        return raw.encode("utf-8") if raw.strip() else None
    try:
        path = local_trust_key_path()
    except RuntimeError:
        return None
    if path.is_file():
        try:
            key = path.read_bytes()
        except OSError:
            return None
        return key if len(key) >= 32 else None
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return trust_anchor_hmac_key(create=False)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return key


def trust_scope_payload() -> dict[str, Any]:
    witness = str(configured_trust_anchor_value(TRUST_ANCHOR_WITNESS_ENV) or "").strip()
    return {
        "trust_scope": "local-only",
        "witness": {
            "status": "configured-unverified" if witness else "unavailable",
            "verified": False,
        },
    }


def trust_anchor_key_id(key: bytes) -> str:
    """Expose a non-secret key identifier to make unintended rotation visible."""
    return hashlib.sha256(key).hexdigest()[:16]


def trust_anchor_signature(payload: dict[str, Any], key: bytes) -> str:
    normalized = dict(payload)
    normalized.pop("hmac_sha256", None)
    return hmac.new(key, stable_json(normalized).encode("utf-8"), hashlib.sha256).hexdigest()


def sign_trust_anchor(payload: dict[str, Any]) -> dict[str, Any]:
    """Authenticate an anchor with an externally supplied HMAC key."""
    key = trust_anchor_hmac_key(create=True)
    if key is None:
        raise RuntimeError(
            f"{TRUST_ANCHOR_HMAC_KEY_ENV} is required to create an external trust anchor"
        )
    signed = dict(payload)
    signed["hmac_algorithm"] = "HMAC-SHA256"
    signed["hmac_key_id"] = trust_anchor_key_id(key)
    signed["hmac_sha256"] = trust_anchor_signature(signed, key)
    return signed


def trust_anchor_authentication_errors(payload: dict[str, Any], *, label: str) -> list[str]:
    """Return fail-closed verification errors for an external anchor."""
    root, root_error = configured_trust_anchor_root()
    if root is None:
        return [root_error or "external trust-anchor root is unavailable"]
    key = trust_anchor_hmac_key()
    if key is None:
        return [f"{label} cannot be verified: {TRUST_ANCHOR_HMAC_KEY_ENV} is not configured"]
    if payload.get("hmac_algorithm") != "HMAC-SHA256":
        return [f"{label} has an unsupported or missing HMAC algorithm"]
    if payload.get("hmac_key_id") != trust_anchor_key_id(key):
        return [f"{label} HMAC key identifier mismatch"]
    signature = payload.get("hmac_sha256")
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, trust_anchor_signature(payload, key)
    ):
        return [f"{label} HMAC signature mismatch"]
    return []


def monotonic_anchor_versions_path(anchor_path: Path) -> Path:
    return anchor_path.with_name(f"{anchor_path.name}.versions")


def next_monotonic_anchor_fields(
    anchor_path: Path,
    *,
    head_hash_field: str,
) -> dict[str, Any]:
    previous: dict[str, Any] | None = None
    if anchor_path.is_file():
        loaded = read_json_file(anchor_path)
        if not isinstance(loaded, dict):
            raise RuntimeError(f"monotonic anchor is not a JSON object: {anchor_path}")
        errors = trust_anchor_authentication_errors(loaded, label="previous monotonic anchor")
        if errors:
            raise RuntimeError("; ".join(errors))
        previous = loaded
    revision = int(previous.get("revision") or 0) + 1 if previous else 1
    previous_hash = previous.get(head_hash_field) if previous else None
    if previous is not None and not isinstance(previous_hash, str):
        raise RuntimeError("previous monotonic anchor has no semantic head hash")
    return {
        "workspace_uuid": workspace_project_uuid(),
        "revision": revision,
        "previous_head_sha256": previous_hash,
        **trust_scope_payload(),
    }


def write_monotonic_anchor(
    anchor_path: Path,
    payload: dict[str, Any],
    *,
    head_hash_field: str,
) -> None:
    """Commit an authenticated content-addressed head, then its mutable pointer."""

    current = read_json_file(anchor_path, default=None)
    expected_previous = payload.get("previous_head_sha256")
    if current is None:
        if expected_previous is not None:
            raise RuntimeError("monotonic anchor CAS failed: predecessor disappeared")
    elif not isinstance(current, dict) or current.get(head_hash_field) != expected_previous:
        raise RuntimeError("monotonic anchor CAS failed: predecessor changed")
    elif errors := trust_anchor_authentication_errors(current, label="current monotonic anchor"):
        raise RuntimeError("; ".join(errors))
    payload_errors = trust_anchor_authentication_errors(payload, label="new monotonic anchor")
    if payload_errors:
        raise RuntimeError("; ".join(payload_errors))
    if payload.get("workspace_uuid") != workspace_project_uuid():
        raise RuntimeError("new monotonic anchor workspace UUID mismatch")
    revision = payload.get("revision")
    head_hash = payload.get(head_hash_field)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise RuntimeError("monotonic anchor revision is invalid")
    if not isinstance(head_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", head_hash):
        raise RuntimeError("monotonic anchor head hash is invalid")
    version_path = monotonic_anchor_versions_path(anchor_path) / (
        f"{revision:020d}-{head_hash}.json"
    )
    # Retained heads are the append-only rollback witness.  Reusing a semantic
    # filename with different bytes must fail instead of silently replacing the
    # evidence that a later pointer is supposed to preserve.
    write_immutable_json(version_path, payload)
    observed = read_json_file(anchor_path, default=None)
    if current is None:
        if observed is not None:
            raise RuntimeError("monotonic anchor CAS failed: predecessor appeared")
    elif not isinstance(observed, dict) or observed.get(head_hash_field) != expected_previous:
        raise RuntimeError("monotonic anchor CAS failed: predecessor changed")
    atomic_write_json(anchor_path, payload)


def monotonic_anchor_errors(
    anchor_path: Path,
    payload: dict[str, Any],
    *,
    label: str,
    head_hash_field: str,
    workspace_transition_validator: Callable[
        [dict[str, Any], dict[str, Any]], list[str]
    ]
    | None = None,
) -> list[str]:
    """Detect pointer rollback against retained content-addressed local heads."""

    if payload.get("revision") is None:
        return []  # Authenticated schema-v1 evidence upgrades on its next write.
    errors: list[str] = []
    if payload.get("workspace_uuid") != workspace_project_uuid():
        errors.append(f"{label} workspace UUID mismatch")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append(f"{label} revision is invalid")
    if payload.get("trust_scope") != "local-only":
        errors.append(f"{label} trust scope is unsupported")
    witness = payload.get("witness")
    if not isinstance(witness, dict) or witness.get("verified") is not False:
        errors.append(f"{label} contains an unsupported witness claim")
    versions: list[dict[str, Any]] = []
    for path in sorted(monotonic_anchor_versions_path(anchor_path).glob("*.json")):
        try:
            version = read_json_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{label} version {path.name} is unreadable: {exc}")
            continue
        if not isinstance(version, dict):
            errors.append(f"{label} version {path.name} is invalid")
            continue
        errors.extend(trust_anchor_authentication_errors(version, label=f"{label} version"))
        version_revision = version.get("revision")
        version_head = version.get(head_hash_field)
        if (
            isinstance(version_revision, bool)
            or not isinstance(version_revision, int)
            or version_revision < 1
        ):
            errors.append(f"{label} version {path.name} has an invalid revision")
            continue
        if not isinstance(version_head, str) or not re.fullmatch(r"[0-9a-f]{64}", version_head):
            errors.append(f"{label} version {path.name} has an invalid semantic head")
            continue
        if path.name != f"{version_revision:020d}-{version_head}.json":
            errors.append(f"{label} version filename does not bind its revision and head")
        if not isinstance(version.get("workspace_uuid"), str) or not version.get("workspace_uuid"):
            errors.append(f"{label} version {path.name} has no workspace UUID")
        versions.append(version)
    if not versions:
        errors.append(f"{label} has a revision but no retained head versions")
        return errors
    versions.sort(key=lambda item: int(item["revision"]))
    previous: dict[str, Any] | None = None
    workspace_transition_count = 0
    for version in versions:
        if previous is None:
            if version.get("revision") != 1:
                errors.append(f"{label} retained revisions do not start at one")
        else:
            previous_revision = previous.get("revision")
            current_revision = version.get("revision")
            if (
                isinstance(previous_revision, bool)
                or not isinstance(previous_revision, int)
                or isinstance(current_revision, bool)
                or not isinstance(current_revision, int)
                or current_revision != previous_revision + 1
            ):
                errors.append(f"{label} retained revisions are not contiguous")
            if version.get("previous_head_sha256") != previous.get(head_hash_field):
                errors.append(f"{label} retained predecessor hash mismatch")
            previous_workspace = previous.get("workspace_uuid")
            current_workspace = version.get("workspace_uuid")
            if previous_workspace != current_workspace:
                workspace_transition_count += 1
                if workspace_transition_count > 1:
                    errors.append(f"{label} contains more than one workspace transition")
                if workspace_transition_validator is None:
                    errors.append(f"{label} retained workspace UUID changed")
                else:
                    errors.extend(workspace_transition_validator(previous, version))
            elif version.get("workspace_migration") is not None:
                errors.append(f"{label} has migration metadata without a workspace transition")
        previous = version
    latest = versions[-1]
    if (
        payload.get("revision") != latest.get("revision")
        or payload.get(head_hash_field) != latest.get(head_hash_field)
    ):
        errors.append(f"{label} pointer was rolled back behind its retained head")
    elif payload != latest:
        errors.append(f"{label} pointer differs from its retained head payload")
    return errors


def relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(row, allow_nan=False, ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    )
    atomic_write_text(path, text)


def write_immutable_text(path: Path, text: str) -> None:
    """Create a content-addressed file, rejecting an altered prior generation."""

    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"immutable generation artifact changed: {path}")
        return
    atomic_write_text(path, text)


def write_immutable_json(path: Path, payload: Any) -> None:
    write_immutable_text(
        path,
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )


def write_immutable_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    write_immutable_text(
        path,
        "".join(
            json.dumps(row, allow_nan=False, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
    )


def strict_json_loads(text: str, *, source: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{source} contains non-standard numeric constant {value}")

    return json.loads(text, parse_constant=reject_constant)


def read_json_file(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return strict_json_loads(path.read_text(encoding="utf-8"), source=str(path))


def configured_report_date(
    now: datetime | None = None,
    *,
    settings_path: Path = BRIEFING_SETTINGS_PATH,
) -> Date:
    settings = read_json_file(settings_path, default={})
    timezone_name = (
        str(settings.get("timezone") or "Asia/Shanghai")
        if isinstance(settings, dict)
        else "Asia/Shanghai"
    )
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid briefing timezone: {timezone_name}") from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("report date requires a timezone-aware datetime")
    return current.astimezone(timezone).date()


def audit_record_hash(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    chain = normalized.get("audit_chain")
    if isinstance(chain, dict):
        normalized["audit_chain"] = {key: value for key, value in chain.items() if key != "entry_sha256"}
    return stable_hash(normalized)


def cycle_history_anchor_path(history_root: Path) -> Path:
    """Return a signed checkpoint outside the workspace's mutable runtime."""
    history_key = stable_hash({"history_root": relative_path(history_root)})[:24]
    return trust_anchor_path("run-audit-history", f"{history_root.name}-{history_key}")


def history_file_manifest(history_root: Path) -> list[dict[str, str]]:
    """Hash every JSON record so inserts, removals, and reordering are observable."""
    return [
        {"name": path.name, "sha256": file_sha256(path)}
        for path in sorted(history_root.glob("*.json"))
    ] if history_root.exists() else []


def history_anchor_hash(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized.pop("anchor_sha256", None)
    normalized.pop("hmac_algorithm", None)
    normalized.pop("hmac_key_id", None)
    normalized.pop("hmac_sha256", None)
    return stable_hash(normalized)


def build_cycle_history_anchor(history_root: Path, integrity: dict[str, Any]) -> dict[str, Any]:
    """Create an append-only checkpoint for a verified run-audit history."""
    manifest = history_file_manifest(history_root)
    if not integrity.get("passed"):
        raise ValueError("cannot anchor an invalid run-audit history")
    if integrity.get("legacy_record_count"):
        raise ValueError("cannot anchor legacy run-audit records without an explicit migration")
    anchor_path = cycle_history_anchor_path(history_root)
    payload: dict[str, Any] = {
        "schema_version": HISTORY_ANCHOR_SCHEMA_VERSION,
        **next_monotonic_anchor_fields(anchor_path, head_hash_field="anchor_sha256"),
        "history_root": relative_path(history_root),
        "entries": manifest,
        "entry_count": len(manifest),
        "chained_record_count": integrity.get("chained_record_count", 0),
        "legacy_record_count": integrity.get("legacy_record_count", 0),
        "first_audit_sha256": integrity.get("first_audit_sha256"),
        "last_audit_sha256": integrity.get("last_audit_sha256"),
        "last_sequence": integrity.get("next_sequence", 1) - 1,
    }
    payload["anchor_sha256"] = history_anchor_hash(payload)
    return sign_trust_anchor(payload)


def write_cycle_history_anchor(history_root: Path, integrity: dict[str, Any]) -> dict[str, Any]:
    payload = build_cycle_history_anchor(history_root, integrity)
    write_monotonic_anchor(
        cycle_history_anchor_path(history_root),
        payload,
        head_hash_field="anchor_sha256",
    )
    return payload


def global_cycle_history_anchor_path() -> Path:
    return trust_anchor_path("run-audit-history-global", "all-dates")


def global_cycle_history_manifest() -> list[dict[str, str]]:
    history_root = RUN_AUDIT_ROOT / "history"
    if not history_root.exists():
        return []
    return [
        {
            "path": path.relative_to(history_root).as_posix(),
            "sha256": file_sha256(path),
        }
        for path in sorted(history_root.glob("*/*.json"))
    ]


def global_history_recovery_evidence_path(evidence_sha256: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise ValueError("global history recovery evidence hash is invalid")
    root, error = configured_trust_anchor_root()
    if root is None:
        raise RuntimeError(error or "external trust-anchor root is unavailable")
    return (
        root
        / GLOBAL_HISTORY_RECOVERY_EVIDENCE_DIRECTORY
        / f"global-run-audit-history-{evidence_sha256}.json"
    )


def global_history_recovery_plan_contract(
    migration: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": GLOBAL_HISTORY_RECOVERY_PLAN_SCHEMA_VERSION,
        **{
            key: migration.get(key)
            for key in sorted(GLOBAL_HISTORY_MIGRATION_KEYS - {"recovery_plan_sha256"})
        },
    }


def parse_run_audit_entry_time(path_value: Any) -> datetime | None:
    if not isinstance(path_value, str):
        return None
    matched = re.search(r"-RUN-(\d{8}T\d{6}(?:\d{1,6})?Z)\.json$", path_value)
    if not matched:
        return None
    token = matched.group(1)
    for pattern in ("%Y%m%dT%H%M%S%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(token, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def global_history_workspace_transition_errors(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    """Authorize only the one evidence-bound test-contamination recovery."""

    errors: list[str] = []
    migration = current.get("workspace_migration")
    if not isinstance(migration, dict) or set(migration) != GLOBAL_HISTORY_MIGRATION_KEYS:
        return ["global run-audit workspace migration metadata is missing or malformed"]
    if migration.get("schema_version") != GLOBAL_HISTORY_MIGRATION_SCHEMA_VERSION:
        errors.append("global run-audit workspace migration schema is unsupported")
    if migration.get("kind") != GLOBAL_HISTORY_MIGRATION_KIND:
        errors.append("global run-audit workspace migration kind is unsupported")
    if previous.get("schema_version") != HISTORY_ANCHOR_SCHEMA_VERSION:
        errors.append("global run-audit migration predecessor schema is unsupported")
    if current.get("schema_version") != GLOBAL_HISTORY_ANCHOR_SCHEMA_VERSION:
        errors.append("global run-audit migration target schema is unsupported")
    if previous.get("revision") != 1 or current.get("revision") != 2:
        errors.append("global run-audit recovery must be the revision-one to revision-two transition")

    previous_entries = previous.get("entries")
    current_entries = current.get("entries")
    if not isinstance(previous_entries, list) or len(previous_entries) != 1:
        errors.append("global run-audit recovery predecessor is not the single polluted entry")
        previous_entries = []
    if not isinstance(current_entries, list) or not current_entries:
        errors.append("global run-audit recovery target manifest is empty or invalid")
        current_entries = []
    if any(entry in current_entries for entry in previous_entries):
        errors.append("global run-audit recovery target still contains the polluted entry")
    if previous.get("history_root") != current.get("history_root"):
        errors.append("global run-audit recovery changed the history root")

    expected_values = {
        "source_revision": previous.get("revision"),
        "source_anchor_sha256": previous.get("anchor_sha256"),
        "source_workspace_uuid": previous.get("workspace_uuid"),
        "source_entries_sha256": stable_hash(previous_entries),
        "source_entry_count": len(previous_entries),
        "target_workspace_uuid": current.get("workspace_uuid"),
        "target_entries_sha256": stable_hash(current_entries),
        "target_entry_count": len(current_entries),
    }
    for key, expected in expected_values.items():
        if migration.get(key) != expected:
            errors.append(f"global run-audit workspace migration {key} mismatch")
    if current.get("previous_head_sha256") != previous.get("anchor_sha256"):
        errors.append("global run-audit migration predecessor head mismatch")
    if current.get("workspace_uuid") != workspace_project_uuid():
        errors.append("global run-audit migration target is not the active workspace")

    plan_hash = stable_hash(global_history_recovery_plan_contract(migration))
    if migration.get("recovery_plan_sha256") != plan_hash:
        errors.append("global run-audit recovery plan hash mismatch")

    evidence_hash = migration.get("recovery_evidence_sha256")
    evidence_path_value = migration.get("recovery_evidence_path")
    evidence: dict[str, Any] | None = None
    if not isinstance(evidence_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
        errors.append("global run-audit recovery evidence hash is invalid")
    if not isinstance(evidence_path_value, str):
        errors.append("global run-audit recovery evidence path is invalid")
    else:
        try:
            expected_path = global_history_recovery_evidence_path(str(evidence_hash))
            root, root_error = configured_trust_anchor_root()
            if root is None:
                raise RuntimeError(root_error or "external trust-anchor root is unavailable")
            candidate = (root / Path(evidence_path_value)).resolve()
            candidate.relative_to((root / GLOBAL_HISTORY_RECOVERY_EVIDENCE_DIRECTORY).resolve())
            if candidate != expected_path.resolve():
                raise ValueError("content-addressed recovery evidence path mismatch")
            loaded = read_json_file(candidate)
            if not isinstance(loaded, dict):
                raise ValueError("recovery evidence must be a JSON object")
            evidence = loaded
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"global run-audit recovery evidence is unavailable or invalid: {exc}")

    if evidence is None:
        return errors
    if set(evidence) != GLOBAL_HISTORY_RECOVERY_EVIDENCE_KEYS:
        errors.append("global run-audit recovery evidence fields are malformed")
    if evidence.get("schema_version") != GLOBAL_HISTORY_RECOVERY_EVIDENCE_SCHEMA_VERSION:
        errors.append("global run-audit recovery evidence schema is unsupported")
    if evidence.get("kind") != GLOBAL_HISTORY_MIGRATION_KIND:
        errors.append("global run-audit recovery evidence kind is unsupported")
    if stable_hash(evidence) != evidence_hash:
        errors.append("global run-audit recovery evidence content hash mismatch")

    expected_cause = {
        "classification": "unit_test_inherited_production_trust_configuration",
        "test_file": GLOBAL_HISTORY_RECOVERY_TEST_FILE,
        "test_case": GLOBAL_HISTORY_RECOVERY_TEST_CASE,
    }
    if evidence.get("cause") != expected_cause:
        errors.append("global run-audit recovery cause is not the allowlisted test incident")
    expected_source = {
        "schema_version": previous.get("schema_version"),
        "revision": previous.get("revision"),
        "anchor_sha256": previous.get("anchor_sha256"),
        "anchor_file_sha256": file_sha256(
            monotonic_anchor_versions_path(global_cycle_history_anchor_path())
            / f"{int(previous.get('revision') or 0):020d}-{previous.get('anchor_sha256')}.json"
        ),
        "workspace_uuid": previous.get("workspace_uuid"),
        "hmac_key_id": previous.get("hmac_key_id"),
        "entry_count": len(previous_entries),
        "entries": previous_entries,
    }
    if evidence.get("source_anchor") != expected_source:
        errors.append("global run-audit recovery source evidence mismatch")
    expected_target = {
        "history_root": current.get("history_root"),
        "workspace_uuid": current.get("workspace_uuid"),
        "entry_count": len(current_entries),
        "entries_sha256": stable_hash(current_entries),
        "entries": current_entries,
    }
    if evidence.get("target_history") != expected_target:
        errors.append("global run-audit recovery target evidence mismatch")

    daily = evidence.get("daily_anchors")
    flattened: list[dict[str, Any]] = []
    if not isinstance(daily, list) or not daily:
        errors.append("global run-audit recovery daily-anchor evidence is empty or invalid")
        daily = []
    for item in daily:
        item_entries = item.get("entries") if isinstance(item, dict) else None
        if not isinstance(item_entries, list):
            errors.append("global run-audit recovery daily-anchor entries are invalid")
            continue
        flattened.extend(item_entries)
    if flattened != current_entries:
        errors.append("global run-audit recovery daily anchors do not cover the target manifest")
    if stable_hash(daily) != migration.get("daily_anchor_evidence_sha256"):
        errors.append("global run-audit recovery daily-anchor evidence hash mismatch")

    backup = evidence.get("backup")
    required_backup_keys = {
        "latest_path",
        "latest_file_sha256",
        "manifest_path",
        "manifest_file_sha256",
        "manifest_sha256",
        "archive_sha256",
        "created_at",
        "verified",
        "archive_integrity_verified",
        "restore_verified",
        "encrypted_container_authenticated",
        "snapshot_profile",
        "latest_metadata_authentication_key_id",
        "manifest_metadata_authentication_key_id",
        "entries",
        "entries_sha256",
    }
    if not isinstance(backup, dict) or set(backup) != required_backup_keys:
        errors.append("global run-audit recovery backup evidence is malformed")
        backup = {}
    for key in (
        "verified",
        "archive_integrity_verified",
        "restore_verified",
        "encrypted_container_authenticated",
    ):
        if backup.get(key) is not True:
            errors.append(f"global run-audit recovery backup {key} is not true")
    if backup.get("entries") != current_entries:
        errors.append("global run-audit recovery backup manifest does not match the target history")
    if backup.get("entries_sha256") != stable_hash(current_entries):
        errors.append("global run-audit recovery backup entry manifest hash mismatch")
    backup_bindings = {
        "backup_latest_file_sha256": backup.get("latest_file_sha256"),
        "backup_manifest_file_sha256": backup.get("manifest_file_sha256"),
        "backup_manifest_sha256": backup.get("manifest_sha256"),
        "backup_entry_manifest_sha256": backup.get("entries_sha256"),
        "backup_archive_sha256": backup.get("archive_sha256"),
        "backup_created_at": backup.get("created_at"),
    }
    for key, expected in backup_bindings.items():
        if migration.get(key) != expected:
            errors.append(f"global run-audit recovery {key} mismatch")
    backup_time = parse_utc_timestamp(backup.get("created_at"))
    source_times = [
        parsed
        for parsed in (parse_run_audit_entry_time(item.get("path")) for item in previous_entries)
        if parsed is not None
    ]
    if backup_time is None or len(source_times) != len(previous_entries):
        errors.append("global run-audit recovery chronology evidence is invalid")
    elif any(backup_time >= source_time for source_time in source_times):
        errors.append("global run-audit recovery backup does not predate the polluted entry")
    return errors


def audit_global_cycle_history(*, allow_append: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    history_root = RUN_AUDIT_ROOT / "history"
    for directory in sorted(path for path in history_root.glob("*") if path.is_dir()):
        dated = audit_cycle_history(directory)
        if not dated["passed"]:
            errors.extend(f"{directory.name}: {error}" for error in dated["errors"])
    entries = global_cycle_history_manifest()
    try:
        anchor_path = global_cycle_history_anchor_path()
    except RuntimeError as exc:
        return {
            "passed": False,
            "errors": [str(exc)],
            "entries": entries,
            "anchor_present": False,
        }
    anchor = read_json_file(anchor_path, default=None)
    if anchor is None:
        return {
            "passed": not errors,
            "errors": errors,
            "entries": entries,
            "anchor_present": False,
            "migration_required": bool(entries),
            "append_pending": False,
        }
    if not isinstance(anchor, dict):
        errors.append("global run-audit history anchor is invalid")
    else:
        if anchor.get("schema_version") not in {
            HISTORY_ANCHOR_SCHEMA_VERSION,
            GLOBAL_HISTORY_ANCHOR_SCHEMA_VERSION,
        }:
            errors.append("global run-audit history anchor schema is unsupported")
        if anchor.get("anchor_sha256") != history_anchor_hash(anchor):
            errors.append("global run-audit history anchor hash mismatch")
        errors.extend(
            trust_anchor_authentication_errors(anchor, label="global run-audit history anchor")
        )
        errors.extend(
            monotonic_anchor_errors(
                anchor_path,
                anchor,
                label="global run-audit history anchor",
                head_hash_field="anchor_sha256",
                workspace_transition_validator=global_history_workspace_transition_errors,
            )
        )
        previous_entries = anchor.get("entries")
        if not isinstance(previous_entries, list):
            errors.append("global run-audit history anchor entries are invalid")
        elif previous_entries != entries:
            valid_append = bool(
                allow_append
                and len(entries) > len(previous_entries)
                and entries[: len(previous_entries)] == previous_entries
            )
            if not valid_append:
                errors.append("global run-audit history was deleted, reordered, or mutated")
    return {
        "passed": not errors,
        "errors": errors,
        "entries": entries,
        "anchor_present": isinstance(anchor, dict),
        "migration_required": False,
        "append_pending": bool(
            isinstance(anchor, dict)
            and isinstance(anchor.get("entries"), list)
            and anchor.get("entries") != entries
            and entries[: len(anchor.get("entries", []))] == anchor.get("entries")
        ),
    }


def write_global_cycle_history_anchor(integrity: dict[str, Any]) -> dict[str, Any]:
    if integrity.get("passed") is not True:
        raise ValueError("cannot anchor invalid global run-audit history")
    anchor_path = global_cycle_history_anchor_path()
    core = {
        "schema_version": GLOBAL_HISTORY_ANCHOR_SCHEMA_VERSION,
        "history_root": relative_path(RUN_AUDIT_ROOT / "history"),
        "entries": global_cycle_history_manifest(),
    }
    existing = read_json_file(anchor_path, default=None)
    if isinstance(existing, dict):
        authentication_errors = trust_anchor_authentication_errors(
            existing, label="existing global run-audit history anchor"
        )
        if authentication_errors:
            raise ValueError("; ".join(authentication_errors))
        existing_workspace = existing.get("workspace_uuid")
        if existing_workspace is not None and existing_workspace != workspace_project_uuid():
            raise ValueError(
                "global run-audit history anchor belongs to another workspace; "
                "use the explicit recovery command"
            )
        existing_entries = existing.get("entries")
        if not isinstance(existing_entries, list) or core["entries"][: len(existing_entries)] != existing_entries:
            raise ValueError(
                "global run-audit history is not an append of its existing anchor; "
                "use the explicit recovery command"
            )
    if isinstance(existing, dict) and all(existing.get(key) == value for key, value in core.items()):
        return existing
    payload = {
        **core,
        "entry_count": len(core["entries"]),
        **next_monotonic_anchor_fields(anchor_path, head_hash_field="anchor_sha256"),
    }
    payload["anchor_sha256"] = history_anchor_hash(payload)
    signed = sign_trust_anchor(payload)
    write_monotonic_anchor(anchor_path, signed, head_hash_field="anchor_sha256")
    return signed


def audit_cycle_history(
    history_root: Path,
    *,
    allow_unanchored_genesis: bool = False,
    allow_anchor_append: bool = False,
) -> dict[str, Any]:
    """Verify the hash chain against a checkpoint outside its mutable directory.

    A chain alone cannot detect truncation: deleting its tail simply leaves a valid
    prefix.  The external checkpoint commits the exact file manifest and last hash.
    Normal reads fail closed on an unexpected unanchored record or append; the two
    opt-in modes are only used inside the write transaction before replacing the
    checkpoint.
    """
    errors: list[str] = []
    trust_root, trust_root_error = configured_trust_anchor_root()
    if trust_root is None:
        errors.append(trust_root_error or "external trust-anchor root is unavailable")
    if trust_anchor_hmac_key() is None and (history_root.exists() and any(history_root.glob("*.json"))):
        errors.append(
            f"run audit history cannot be verified: {TRUST_ANCHOR_HMAC_KEY_ENV} is not configured"
        )
    legacy_record_count = 0
    chained_record_count = 0
    previous_hash: str | None = None
    previous_sequence = 0
    first_hash: str | None = None
    for path in sorted(history_root.glob("*.json")) if history_root.exists() else []:
        try:
            payload = read_json_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: unreadable audit JSON: {exc}")
            continue
        chain = payload.get("audit_chain") if isinstance(payload, dict) else None
        if not isinstance(chain, dict):
            if chained_record_count:
                errors.append(f"{path.name}: unchained record appears after hash-chain genesis")
            legacy_record_count += 1
            continue
        chained_record_count += 1
        actual_hash = audit_record_hash(payload)
        if chain.get("entry_sha256") != actual_hash:
            errors.append(f"{path.name}: entry hash mismatch")
        expected_sequence = previous_sequence + 1
        if chain.get("sequence") != expected_sequence:
            errors.append(f"{path.name}: expected sequence {expected_sequence}, got {chain.get('sequence')}")
        if chain.get("previous_audit_sha256") != previous_hash:
            errors.append(f"{path.name}: previous audit hash mismatch")
        if first_hash is None:
            first_hash = actual_hash
        previous_hash = actual_hash
        previous_sequence = expected_sequence
    try:
        manifest = history_file_manifest(history_root)
    except OSError as exc:
        errors.append(f"cannot read run audit history manifest: {type(exc).__name__}: {exc}")
        manifest = []

    try:
        anchor_path: Path | None = cycle_history_anchor_path(history_root)
    except RuntimeError as exc:
        errors.append(str(exc))
        anchor_path = None
    anchor_present = bool(anchor_path and anchor_path.exists())
    anchor: dict[str, Any] | None = None
    if anchor_present:
        try:
            loaded_anchor = read_json_file(anchor_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"cannot read run audit history anchor: {type(exc).__name__}: {exc}")
        else:
            if not isinstance(loaded_anchor, dict):
                errors.append("run audit history anchor must be a JSON object")
            else:
                anchor = loaded_anchor
                if anchor.get("schema_version") not in {
                    LEGACY_ANCHOR_SCHEMA_VERSION,
                    HISTORY_ANCHOR_SCHEMA_VERSION,
                }:
                    errors.append("unsupported run audit history anchor schema")
                if anchor.get("history_root") != relative_path(history_root):
                    errors.append("run audit history anchor points to a different history root")
                if anchor.get("anchor_sha256") != history_anchor_hash(anchor):
                    errors.append("run audit history anchor hash mismatch")
                errors.extend(
                    trust_anchor_authentication_errors(anchor, label="run audit history anchor")
                )
                if anchor.get("schema_version") == HISTORY_ANCHOR_SCHEMA_VERSION:
                    errors.extend(
                        monotonic_anchor_errors(
                            anchor_path,
                            anchor,
                            label="run audit history anchor",
                            head_hash_field="anchor_sha256",
                        )
                    )
                anchor_entries = anchor.get("entries")
                if not isinstance(anchor_entries, list):
                    errors.append("run audit history anchor entries are invalid")
                    anchor_entries = []
                expected_entry_count = anchor.get("entry_count")
                if expected_entry_count != len(anchor_entries):
                    errors.append("run audit history anchor entry count is inconsistent")
                if anchor_entries != manifest:
                    append_is_valid = bool(
                        allow_anchor_append
                        and len(manifest) > len(anchor_entries)
                        and manifest[: len(anchor_entries)] == anchor_entries
                    )
                    if not append_is_valid:
                        errors.append("run audit history manifest differs from its external anchor")
                if not allow_anchor_append or len(manifest) <= len(anchor_entries):
                    if anchor.get("chained_record_count") != chained_record_count:
                        errors.append("run audit history chained record count differs from anchor")
                    if anchor.get("legacy_record_count") != legacy_record_count:
                        errors.append("run audit history legacy record count differs from anchor")
                    if anchor.get("first_audit_sha256") != first_hash:
                        errors.append("run audit history first hash differs from anchor")
                    if anchor.get("last_audit_sha256") != previous_hash:
                        errors.append("run audit history tail hash differs from anchor")
                    if anchor.get("last_sequence") != previous_sequence:
                        errors.append("run audit history tail sequence differs from anchor")
    elif manifest:
        valid_genesis = bool(
            allow_unanchored_genesis
            and legacy_record_count == 0
            and chained_record_count == 1
            and previous_sequence == 1
        )
        if not valid_genesis:
            errors.append("run audit history has records but no external anchor; explicit migration is required")

    if legacy_record_count:
        errors.append("legacy run audit records are not accepted in an anchored hash chain")
    return {
        "passed": not errors,
        "errors": errors,
        "legacy_record_count": legacy_record_count,
        "chained_record_count": chained_record_count,
        "first_audit_sha256": first_hash,
        "last_audit_sha256": previous_hash,
        "next_sequence": previous_sequence + 1,
        "anchor_path": str(anchor_path) if anchor_path is not None else None,
        "anchor_present": anchor_present,
    }


def load_disaster_recovery_module() -> Any:
    script = BRIEFING_ROOT / "scripts" / "disaster_recovery.py"
    spec = importlib.util.spec_from_file_location("atlas_disaster_recovery_verifier", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the disaster-recovery verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backup_global_history_entries(manifest: dict[str, Any]) -> list[dict[str, str]]:
    prefix = "work/shared/atlas/run_audits/history/"
    entries: list[dict[str, str]] = []
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("backup manifest files are missing")
    for item in files:
        if not isinstance(item, dict) or item.get("kind") != "workspace_file":
            continue
        path_value = str(item.get("path") or "").replace("\\", "/")
        if not path_value.startswith(prefix) or not path_value.endswith(".json"):
            continue
        relative = path_value[len(prefix) :]
        digest = item.get("sha256")
        if not relative or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("backup run-audit history entry is malformed")
        entries.append({"path": relative, "sha256": digest})
    ordered = sorted(entries, key=lambda item: item["path"])
    if len({item["path"] for item in ordered}) != len(ordered):
        raise ValueError("backup run-audit history contains duplicate paths")
    return ordered


def metadata_authentication_key_id(payload: dict[str, Any], *, label: str) -> str:
    authentication = payload.get("metadata_authentication")
    key_id = authentication.get("key_id") if isinstance(authentication, dict) else None
    if not isinstance(key_id, str) or not key_id:
        raise ValueError(f"{label} has no metadata authentication key identifier")
    return key_id


def verify_global_history_recovery_backup(latest_path: Path) -> dict[str, Any]:
    """Independently authenticate a restore-verified backup and freeze its history view."""

    resolved_latest = latest_path.expanduser().resolve()
    latest_before = resolved_latest.read_bytes()
    latest = strict_json_loads(latest_before.decode("utf-8"), source=str(resolved_latest))
    if not isinstance(latest, dict):
        raise ValueError("backup latest metadata must be a JSON object")
    manifest_path_value = latest.get("manifest_sidecar")
    if not isinstance(manifest_path_value, str):
        raise ValueError("backup latest metadata has no manifest sidecar")
    manifest_path = Path(manifest_path_value).expanduser().resolve()
    manifest_before = manifest_path.read_bytes()
    manifest = strict_json_loads(manifest_before.decode("utf-8"), source=str(manifest_path))
    if not isinstance(manifest, dict):
        raise ValueError("backup manifest must be a JSON object")

    verifier = load_disaster_recovery_module()
    verification = verifier.verify_latest_snapshot(
        root=ROOT,
        config_path=IMPROVEMENT_TRACKING_CONFIG_PATH,
        latest_path=resolved_latest,
        quick=True,
    )
    if resolved_latest.read_bytes() != latest_before or manifest_path.read_bytes() != manifest_before:
        raise RuntimeError("backup evidence changed during verification")
    required_true = (
        "verified",
        "archive_integrity_verified",
        "restore_verified",
        "encrypted_container_authenticated",
    )
    for key in required_true:
        if verification.get(key) is not True:
            raise ValueError(f"authenticated recovery backup {key} is not true")
    entries = backup_global_history_entries(manifest)
    created_at = latest.get("created_at")
    if parse_utc_timestamp(created_at) is None:
        raise ValueError("authenticated recovery backup creation time is invalid")
    return {
        "latest_path": str(resolved_latest),
        "latest_file_sha256": hashlib.sha256(latest_before).hexdigest(),
        "manifest_path": str(manifest_path),
        "manifest_file_sha256": hashlib.sha256(manifest_before).hexdigest(),
        "manifest_sha256": verification.get("manifest_sha256"),
        "archive_sha256": verification.get("archive_sha256"),
        "created_at": created_at,
        "verified": True,
        "archive_integrity_verified": True,
        "restore_verified": True,
        "encrypted_container_authenticated": True,
        "snapshot_profile": verification.get("snapshot_profile"),
        "latest_metadata_authentication_key_id": metadata_authentication_key_id(
            latest, label="backup latest metadata"
        ),
        "manifest_metadata_authentication_key_id": metadata_authentication_key_id(
            manifest, label="backup manifest"
        ),
        "entries": entries,
        "entries_sha256": stable_hash(entries),
    }


def build_daily_history_anchor_evidence() -> list[dict[str, Any]]:
    history_root = RUN_AUDIT_ROOT / "history"
    trust_root, trust_error = configured_trust_anchor_root()
    if trust_root is None:
        raise RuntimeError(trust_error or "external trust-anchor root is unavailable")
    evidence: list[dict[str, Any]] = []
    for directory in sorted(path for path in history_root.glob("*") if path.is_dir()):
        integrity = audit_cycle_history(directory)
        if not integrity.get("passed"):
            raise ValueError(
                f"dated run-audit history {directory.name} is invalid: "
                + "; ".join(integrity.get("errors", []))
            )
        anchor_path_value = integrity.get("anchor_path")
        if not isinstance(anchor_path_value, str):
            raise ValueError(f"dated run-audit history {directory.name} has no anchor path")
        anchor_path = Path(anchor_path_value).resolve()
        anchor_path.relative_to(trust_root.resolve())
        anchor = read_json_file(anchor_path)
        if not isinstance(anchor, dict):
            raise ValueError(f"dated run-audit history {directory.name} anchor is invalid")
        authentication_errors = trust_anchor_authentication_errors(
            anchor, label=f"dated run-audit history {directory.name} anchor"
        )
        if authentication_errors:
            raise ValueError("; ".join(authentication_errors))
        entries = [
            {"path": f"{directory.name}/{item['name']}", "sha256": item["sha256"]}
            for item in history_file_manifest(directory)
        ]
        evidence.append(
            {
                "date": directory.name,
                "history_root": relative_path(directory),
                "entries": entries,
                "entry_count": len(entries),
                "anchor_path": anchor_path.relative_to(trust_root).as_posix(),
                "anchor_schema_version": anchor.get("schema_version"),
                "anchor_sha256": anchor.get("anchor_sha256"),
                "anchor_file_sha256": file_sha256(anchor_path),
                "hmac_key_id": anchor.get("hmac_key_id"),
                "first_audit_sha256": integrity.get("first_audit_sha256"),
                "last_audit_sha256": integrity.get("last_audit_sha256"),
            }
        )
    return evidence


def validate_polluted_global_history_source(
    anchor_path: Path,
    source: dict[str, Any],
    *,
    expected_head_sha256: str,
) -> None:
    if source.get("schema_version") != HISTORY_ANCHOR_SCHEMA_VERSION:
        raise ValueError("polluted global history predecessor schema is unsupported")
    if source.get("revision") != 1 or source.get("previous_head_sha256") is not None:
        raise ValueError("polluted global history predecessor is not revision one")
    if source.get("anchor_sha256") != expected_head_sha256:
        raise ValueError("global history recovery expected head does not match the pointer")
    if source.get("anchor_sha256") != history_anchor_hash(source):
        raise ValueError("polluted global history predecessor semantic hash mismatch")
    authentication_errors = trust_anchor_authentication_errors(
        source, label="polluted global run-audit history anchor"
    )
    if authentication_errors:
        raise ValueError("; ".join(authentication_errors))
    if source.get("workspace_uuid") == workspace_project_uuid():
        raise ValueError("global history predecessor already belongs to the active workspace")
    entries = source.get("entries")
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("global history recovery only accepts the single-entry test pollution incident")
    entry = entries[0]
    if (
        not isinstance(entry, dict)
        or set(entry) != {"path", "sha256"}
        or parse_run_audit_entry_time(entry.get("path")) is None
        or not isinstance(entry.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    ):
        raise ValueError("polluted global history entry is malformed")
    version_path = monotonic_anchor_versions_path(anchor_path) / (
        f"{source['revision']:020d}-{source['anchor_sha256']}.json"
    )
    retained = read_json_file(version_path, default=None)
    if retained != source:
        raise ValueError("polluted global history predecessor is not retained immutably")
    version_files = sorted(monotonic_anchor_versions_path(anchor_path).glob("*.json"))
    if version_files != [version_path]:
        raise ValueError("polluted global history predecessor has an unexpected version set")
    if anchor_path.read_bytes() != version_path.read_bytes():
        raise ValueError("polluted global history pointer differs from its retained version")


def global_history_recovery_already_applied(
    expected_head_sha256: str,
    expected_plan_sha256: str | None,
) -> dict[str, Any] | None:
    anchor_path = global_cycle_history_anchor_path()
    current = read_json_file(anchor_path, default=None)
    if not isinstance(current, dict) or current.get("anchor_sha256") == expected_head_sha256:
        return None
    versions = [
        read_json_file(path)
        for path in sorted(monotonic_anchor_versions_path(anchor_path).glob("*.json"))
    ]
    migration_version = next(
        (
            version
            for version in versions
            if isinstance(version, dict)
            and isinstance(version.get("workspace_migration"), dict)
            and version["workspace_migration"].get("source_anchor_sha256")
            == expected_head_sha256
        ),
        None,
    )
    if migration_version is None:
        return None
    integrity = audit_global_cycle_history()
    if not integrity.get("passed"):
        raise ValueError(
            "existing global history recovery is invalid: "
            + "; ".join(integrity.get("errors", []))
        )
    migration = migration_version["workspace_migration"]
    plan_hash = migration.get("recovery_plan_sha256")
    if expected_plan_sha256 is not None and plan_hash != expected_plan_sha256:
        raise ValueError("existing global history recovery plan hash mismatch")
    return {
        "schema_version": GLOBAL_HISTORY_RECOVERY_PLAN_SCHEMA_VERSION,
        "status": "already_applied",
        "applied": False,
        "anchor_path": str(anchor_path),
        "source_anchor_sha256": expected_head_sha256,
        "target_anchor_sha256": migration_version.get("anchor_sha256"),
        "recovery_plan_sha256": plan_hash,
        "recovery_evidence_path": migration.get("recovery_evidence_path"),
        "revision": migration_version.get("revision"),
    }


def build_global_history_recovery_plan(
    *,
    expected_head_sha256: str,
    backup_latest_path: Path,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_head_sha256):
        raise ValueError("--expected-head-sha256 must be a lowercase SHA-256 digest")
    anchor_path = global_cycle_history_anchor_path()
    source = read_json_file(anchor_path, default=None)
    if not isinstance(source, dict):
        raise ValueError("global run-audit history anchor is missing or invalid")
    validate_polluted_global_history_source(
        anchor_path,
        source,
        expected_head_sha256=expected_head_sha256,
    )
    target_entries = global_cycle_history_manifest()
    if not target_entries:
        raise ValueError("active global run-audit history is empty")
    source_entries = source["entries"]
    if any(item in target_entries for item in source_entries):
        raise ValueError("active history still contains the polluted predecessor entry")

    daily_anchors = build_daily_history_anchor_evidence()
    daily_entries = [entry for item in daily_anchors for entry in item["entries"]]
    if daily_entries != target_entries:
        raise ValueError("verified dated anchors do not exactly cover the active global history")
    backup = verify_global_history_recovery_backup(backup_latest_path)
    if backup.get("entries") != target_entries:
        raise ValueError("authenticated recovery backup does not exactly match active history")
    backup_time = parse_utc_timestamp(backup.get("created_at"))
    source_times = [parse_run_audit_entry_time(item.get("path")) for item in source_entries]
    if backup_time is None or any(item is None or backup_time >= item for item in source_times):
        raise ValueError("authenticated recovery backup does not predate the polluted entry")

    evidence = {
        "schema_version": GLOBAL_HISTORY_RECOVERY_EVIDENCE_SCHEMA_VERSION,
        "kind": GLOBAL_HISTORY_MIGRATION_KIND,
        "cause": {
            "classification": "unit_test_inherited_production_trust_configuration",
            "test_file": GLOBAL_HISTORY_RECOVERY_TEST_FILE,
            "test_case": GLOBAL_HISTORY_RECOVERY_TEST_CASE,
        },
        "source_anchor": {
            "schema_version": source.get("schema_version"),
            "revision": source.get("revision"),
            "anchor_sha256": source.get("anchor_sha256"),
            "anchor_file_sha256": file_sha256(anchor_path),
            "workspace_uuid": source.get("workspace_uuid"),
            "hmac_key_id": source.get("hmac_key_id"),
            "entry_count": len(source_entries),
            "entries": source_entries,
        },
        "target_history": {
            "history_root": relative_path(RUN_AUDIT_ROOT / "history"),
            "workspace_uuid": workspace_project_uuid(),
            "entry_count": len(target_entries),
            "entries_sha256": stable_hash(target_entries),
            "entries": target_entries,
        },
        "daily_anchors": daily_anchors,
        "backup": backup,
    }
    evidence_sha256 = stable_hash(evidence)
    evidence_path = global_history_recovery_evidence_path(evidence_sha256)
    trust_root, trust_error = configured_trust_anchor_root()
    if trust_root is None:
        raise RuntimeError(trust_error or "external trust-anchor root is unavailable")
    evidence_relative = evidence_path.relative_to(trust_root).as_posix()
    migration = {
        "schema_version": GLOBAL_HISTORY_MIGRATION_SCHEMA_VERSION,
        "kind": GLOBAL_HISTORY_MIGRATION_KIND,
        "source_revision": source.get("revision"),
        "source_anchor_sha256": source.get("anchor_sha256"),
        "source_workspace_uuid": source.get("workspace_uuid"),
        "source_entries_sha256": stable_hash(source_entries),
        "source_entry_count": len(source_entries),
        "target_workspace_uuid": workspace_project_uuid(),
        "target_entries_sha256": stable_hash(target_entries),
        "target_entry_count": len(target_entries),
        "daily_anchor_evidence_sha256": stable_hash(daily_anchors),
        "backup_latest_file_sha256": backup.get("latest_file_sha256"),
        "backup_manifest_file_sha256": backup.get("manifest_file_sha256"),
        "backup_manifest_sha256": backup.get("manifest_sha256"),
        "backup_entry_manifest_sha256": backup.get("entries_sha256"),
        "backup_archive_sha256": backup.get("archive_sha256"),
        "backup_created_at": backup.get("created_at"),
        "recovery_evidence_path": evidence_relative,
        "recovery_evidence_sha256": evidence_sha256,
        "recovery_plan_sha256": None,
    }
    migration["recovery_plan_sha256"] = stable_hash(
        global_history_recovery_plan_contract(migration)
    )
    target_payload: dict[str, Any] = {
        "schema_version": GLOBAL_HISTORY_ANCHOR_SCHEMA_VERSION,
        "history_root": relative_path(RUN_AUDIT_ROOT / "history"),
        "entries": target_entries,
        "entry_count": len(target_entries),
        "workspace_uuid": workspace_project_uuid(),
        "revision": 2,
        "previous_head_sha256": source.get("anchor_sha256"),
        **trust_scope_payload(),
        "workspace_migration": migration,
    }
    target_payload["anchor_sha256"] = history_anchor_hash(target_payload)
    return {
        "schema_version": GLOBAL_HISTORY_RECOVERY_PLAN_SCHEMA_VERSION,
        "status": "ready",
        "applied": False,
        "anchor_path": str(anchor_path),
        "source_anchor_sha256": source.get("anchor_sha256"),
        "source_workspace_uuid": source.get("workspace_uuid"),
        "source_entry_count": len(source_entries),
        "target_anchor_sha256": target_payload["anchor_sha256"],
        "target_workspace_uuid": target_payload["workspace_uuid"],
        "target_entry_count": len(target_entries),
        "target_entries_sha256": stable_hash(target_entries),
        "daily_anchor_evidence_sha256": migration["daily_anchor_evidence_sha256"],
        "backup_manifest_sha256": migration["backup_manifest_sha256"],
        "backup_created_at": migration["backup_created_at"],
        "recovery_evidence_path": evidence_relative,
        "recovery_evidence_sha256": evidence_sha256,
        "recovery_plan_sha256": migration["recovery_plan_sha256"],
        "_evidence": evidence,
        "_target_payload": target_payload,
    }


def public_global_history_recovery_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def apply_global_history_recovery(
    *,
    expected_head_sha256: str,
    expected_plan_sha256: str,
    backup_latest_path: Path,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise ValueError("--expected-plan-sha256 must be a lowercase SHA-256 digest")
    already_applied = global_history_recovery_already_applied(
        expected_head_sha256,
        expected_plan_sha256,
    )
    if already_applied is not None:
        return already_applied
    plan = build_global_history_recovery_plan(
        expected_head_sha256=expected_head_sha256,
        backup_latest_path=backup_latest_path,
    )
    if plan["recovery_plan_sha256"] != expected_plan_sha256:
        raise ValueError("global history recovery plan changed; refusing apply")
    anchor_path = Path(plan["anchor_path"])
    current = read_json_file(anchor_path, default=None)
    if not isinstance(current, dict) or current.get("anchor_sha256") != expected_head_sha256:
        raise RuntimeError("global history recovery CAS failed: predecessor changed")
    evidence_path = global_history_recovery_evidence_path(plan["recovery_evidence_sha256"])
    write_immutable_json(evidence_path, plan["_evidence"])
    signed_target = sign_trust_anchor(plan["_target_payload"])
    write_monotonic_anchor(anchor_path, signed_target, head_hash_field="anchor_sha256")
    integrity = audit_global_cycle_history()
    if not integrity.get("passed"):
        raise RuntimeError(
            "global history recovery post-write verification failed: "
            + "; ".join(integrity.get("errors", []))
        )
    result = public_global_history_recovery_result(plan)
    result.update(
        {
            "status": "applied",
            "applied": True,
            "revision": signed_target.get("revision"),
        }
    )
    return result


def command_recover_global_history_anchor(args: argparse.Namespace) -> int:
    try:
        if args.apply:
            if not args.acknowledge_cross_workspace_recovery:
                raise ValueError("--apply requires --acknowledge-cross-workspace-recovery")
            if not args.expected_plan_sha256:
                raise ValueError("--apply requires --expected-plan-sha256 from a dry run")
            with cycle_lock("global-history-anchor-recovery"):
                result = apply_global_history_recovery(
                    expected_head_sha256=args.expected_head_sha256,
                    expected_plan_sha256=args.expected_plan_sha256,
                    backup_latest_path=args.backup_latest,
                )
        else:
            already_applied = global_history_recovery_already_applied(
                args.expected_head_sha256,
                args.expected_plan_sha256,
            )
            result = already_applied or build_global_history_recovery_plan(
                expected_head_sha256=args.expected_head_sha256,
                backup_latest_path=args.backup_latest,
            )
            result = public_global_history_recovery_result(result)
            if result.get("status") == "ready":
                result["status"] = "dry_run"
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": GLOBAL_HISTORY_RECOVERY_PLAN_SCHEMA_VERSION,
                    "status": "blocked",
                    "applied": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = strict_json_loads(text, source=f"{path}:{line_number}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
        rows.append(payload)
    return rows


def maybe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid ledger numbers")
    if isinstance(value, str) and not value.strip():
        raise ValueError("empty strings are not valid ledger numbers")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid ledger number: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite ledger number: {value!r}")
    return number


def normalized_ledger_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def nested_forbidden_keys(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if normalized_ledger_key(key) in NORMALIZED_FORBIDDEN_LEDGER_KEYS:
                found.append(path)
            found.extend(nested_forbidden_keys(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_forbidden_keys(value, f"{prefix}[{index}]"))
    return found


def nested_unsafe_safety_declarations(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized_key = normalized_ledger_key(key)
            if normalized_key in NORMALIZED_SAFETY_FLAGS_REQUIRED_TRUE and value is not True:
                found.append(f"{path}={value!r}")
            if normalized_key in NORMALIZED_SAFETY_FLAGS_REQUIRED_FALSE and value is not False:
                found.append(f"{path}={value!r}")
            found.extend(nested_unsafe_safety_declarations(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_unsafe_safety_declarations(value, f"{prefix}[{index}]"))
    return found


def nested_nonfinite_values(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.extend(nested_nonfinite_values(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_nonfinite_values(value, f"{prefix}[{index}]"))
    elif isinstance(payload, float) and not math.isfinite(payload):
        found.append(f"{prefix}={payload!r}")
    elif isinstance(payload, str) and payload.strip().casefold() in {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }:
        found.append(f"{prefix}={payload!r}")
    return found


def nested_invalid_numeric_values(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized_key = normalized_ledger_key(key)
            if normalized_key in NORMALIZED_LEDGER_NUMERIC_KEYS and value is not None:
                try:
                    number = maybe_float(value)
                except ValueError as exc:
                    found.append(f"{path}={value!r} ({exc})")
                else:
                    if normalized_key in NORMALIZED_NON_NEGATIVE_LEDGER_NUMERIC_KEYS and number < 0:
                        found.append(f"{path}={value!r} (must be non-negative)")
            found.extend(nested_invalid_numeric_values(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(nested_invalid_numeric_values(value, f"{prefix}[{index}]"))
    return found


def raw_virtual_source_safety_errors(payload: Any, source_path: Path, record: str) -> list[str]:
    locator = f"{relative_path(source_path)}:{record}"
    errors: list[str] = []
    forbidden = sorted(nested_forbidden_keys(payload))
    if forbidden:
        errors.append(f"{locator}: raw virtual source contains forbidden broker/live keys {forbidden}")
    unsafe_declarations = sorted(nested_unsafe_safety_declarations(payload))
    if unsafe_declarations:
        errors.append(f"{locator}: raw virtual source contains unsafe safety declarations {unsafe_declarations}")
    nonfinite = sorted(nested_nonfinite_values(payload))
    if nonfinite:
        errors.append(f"{locator}: raw virtual source contains non-finite numeric values {nonfinite}")
    invalid_numeric = sorted(nested_invalid_numeric_values(payload))
    if invalid_numeric:
        errors.append(f"{locator}: raw virtual source contains invalid numeric values {invalid_numeric}")
    return errors


def is_iso_date(value: Any) -> bool:
    try:
        return Date.fromisoformat(str(value)).isoformat() == str(value)
    except ValueError:
        return False


def source_identity(path: Path, line_number: int | None, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": relative_path(path),
        "record_id": payload.get("trade_id") or payload.get("order_id") or payload.get("replay_id"),
        "sha256": stable_hash(payload),
    }


def ledger_event_id(prefix: str, identity: dict[str, Any]) -> str:
    return f"{prefix}-{stable_hash(identity)[:24].upper()}"


def discover_briefing_temp_files(kind: str) -> list[Path]:
    """Return both legacy underscore and current hyphenated daily temp files."""
    if kind not in {"orders", "prices"}:
        raise ValueError(f"unsupported briefing temp file kind: {kind}")
    briefing_data = BRIEFING_ROOT / "data"
    if not briefing_data.exists():
        return []
    patterns = (f"temp_{kind}_*.json", f"temp-{kind}-*.json")
    return sorted(
        {path for pattern in patterns for path in briefing_data.glob(pattern)},
        key=lambda path: str(path),
    )


def load_locked_paper_execution_snapshot() -> dict[str, Any]:
    """Obtain the paper layer's authoritative cross-account read snapshot."""

    script = BRIEFING_ROOT / "scripts" / "paper_trading.py"
    completed = capture_command(
        [sys.executable, str(script), "snapshot", "--account", "ALL"],
        cwd=ROOT,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "snapshot command failed"
        raise RuntimeError(f"locked paper-trading snapshot unavailable: {detail}")
    payload = strict_json_loads(completed.stdout, source="paper-trading locked snapshot")
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("paper-trading locked snapshot schema is invalid")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not all(isinstance(item, dict) for item in accounts):
        raise ValueError("paper-trading locked snapshot accounts are invalid")
    expected = stable_hash({"accounts": accounts})
    if payload.get("generation_sha256") != expected:
        raise ValueError("paper-trading locked snapshot generation hash mismatch")
    return payload


def prediction_integrity_snapshot() -> dict[str, Any]:
    """Read prediction semantics and stable identities for reference validation."""

    rows = read_jsonl_file(PREDICTION_LEDGER_PATH)
    row_hashes = [stable_hash(row) for row in rows]
    originals: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for line_number, row in enumerate(rows, 1):
        prediction_id = row.get("prediction_id")
        if not isinstance(prediction_id, str) or not prediction_id.strip():
            errors.append(f"prediction ledger line {line_number}: prediction_id is missing")
            continue
        if isinstance(row.get("review"), dict):
            continue
        if prediction_id in originals:
            errors.append(f"prediction ledger has duplicate original prediction_id {prediction_id}")
            continue
        originals[prediction_id] = row
    return {
        "path": relative_path(PREDICTION_LEDGER_PATH),
        "present": PREDICTION_LEDGER_PATH.is_file(),
        "row_count": len(rows),
        "row_hashes": row_hashes,
        "semantic_hash": stable_hash(rows),
        "originals": originals,
        "errors": errors,
    }


def prediction_reference_required_from_date() -> str:
    config = read_json_file(PAPER_TRADING_CONFIG_PATH, default={})
    contract = config.get("order_contract", {}) if isinstance(config, dict) else {}
    boundary = (
        contract.get("prediction_reference_required_from_date")
        if isinstance(contract, dict)
        else None
    )
    if not isinstance(boundary, str) or not is_iso_date(boundary):
        return "9999-12-31"
    return boundary


def read_current_canonical_state() -> dict[str, Any]:
    """Read state from the committed generation head, falling back to schema-v1 projection."""

    head = read_json_file(VIRTUAL_LEDGER_HEAD_PATH, default=None)
    if isinstance(head, dict):
        declared_hash = head.get("head_sha256")
        unsigned = {key: value for key, value in head.items() if key != "head_sha256"}
        if declared_hash != stable_hash(unsigned):
            raise ValueError("canonical generation head hash mismatch")
        state_value = head.get("state_path")
        if not isinstance(state_value, str):
            raise ValueError("canonical generation head has no state path")
        state_path = (ROOT / state_value).resolve()
        try:
            state_path.relative_to(VIRTUAL_LEDGER_GENERATIONS_ROOT.resolve())
        except ValueError as exc:
            raise ValueError("canonical generation state escapes its authority root") from exc
        if not state_path.is_file() or file_sha256(state_path) != head.get("state_sha256"):
            raise ValueError("canonical generation state hash mismatch")
        state = read_json_file(state_path)
        if not isinstance(state, dict):
            raise ValueError("canonical generation state must be a JSON object")
        return state
    state = read_json_file(VIRTUAL_LEDGER_STATE_PATH, default={})
    return state if isinstance(state, dict) else {}


def virtual_ledger_source_files() -> list[Path]:
    files = [
        BRIEFING_ROOT / "data" / "paper_trades_us.jsonl",
        BRIEFING_ROOT / "data" / "paper_trades_china.jsonl",
        BRIEFING_ROOT / "data" / "paper_portfolio_us.json",
        BRIEFING_ROOT / "data" / "paper_portfolio_china.json",
    ]
    files.extend(discover_briefing_temp_files("orders"))
    files.extend(discover_briefing_temp_files("prices"))
    replay_root = TRADING_ROOT / "data" / "replays" / "global_briefing"
    if replay_root.exists():
        files.extend(sorted((replay_root / "trades").glob("*.jsonl")))
        files.extend(sorted((replay_root / "accounts").glob("*.json")))
        files.extend(sorted(replay_root.glob("global_briefing_replay_evaluation-*.json")))
    return [path for path in files if path.exists()]


def build_cycle_fingerprint(date: str) -> dict[str, Any]:
    files = [
        OUTPUTS_ROOT / f"每日全球晨间简报-{date}.md",
        BRIEFING_ROOT / "data" / "predictions.jsonl",
        BRIEFING_ROOT / "data" / f"macro_signals-{date}.jsonl",
        TRADING_ROOT / "data" / "macro_signals" / f"macro_signals-{date}.jsonl",
        SITE_ROOT / "app" / "briefing.generated.json",
        BRIEFING_ROOT / "data" / f"temp_orders_{date}.json",
        BRIEFING_ROOT / "data" / f"temp-orders-{date}.json",
        BRIEFING_ROOT / "data" / f"temp_prices_{date}.json",
        BRIEFING_ROOT / "data" / f"temp-prices-{date}.json",
        *virtual_ledger_source_files(),
    ]
    records = [
        {"path": relative_path(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
        for path in sorted(set(files), key=lambda item: str(item))
        if path.exists()
    ]
    return {
        "date": date,
        "files": records,
        "fingerprint": stable_hash(records),
    }


def canonicalize_global_paper_trade(row: dict[str, Any], source_path: Path, line_number: int, source_ledger: str) -> dict[str, Any]:
    identity = source_identity(source_path, line_number, row)
    action = str(row.get("action") or row.get("side") or "UNKNOWN").upper()
    quantity = maybe_float(row.get("quantity"))
    price = maybe_float(row.get("price"))
    notional = maybe_float(row.get("gross_value"), price * quantity)
    account = str(row.get("account") or row.get("market_scope") or "GLOBAL").upper()
    account_id = str(row.get("account_id") or f"global-briefing-{account.lower()}-paper-trading")
    status = "FILLED" if action in {"BUY", "SELL"} and quantity > 0 else "HELD"
    event_id = ledger_event_id("ATLASGBTRADE", identity)
    timestamp = str(row.get("timestamp") or f"{row.get('date', '1970-01-01')}T00:00:00")
    return {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_event_id": event_id,
        "event_type": "virtual_trade" if status == "FILLED" else "virtual_hold",
        "idempotency_key": event_id,
        "source_system": "global-briefing",
        "source_ledger": source_ledger,
        "source_path": relative_path(source_path),
        "source_line": line_number,
        "source_hash": identity["sha256"],
        "date": str(row.get("date") or timestamp[:10]),
        "timestamp": timestamp,
        "account_id": account_id,
        "virtual_account_scope": account,
        "symbol": str(row.get("symbol") or ""),
        "exchange": row.get("exchange"),
        "market_type": row.get("market_type"),
        "currency": row.get("currency"),
        "side": action,
        "filled_price": price,
        "filled_quantity": quantity,
        "notional": notional,
        "fee": maybe_float(row.get("fee")),
        "tax": maybe_float(row.get("tax")),
        "cash_after": maybe_float(row.get("cash_after")) if row.get("cash_after") is not None else None,
        "equity_after": maybe_float(row.get("equity_after")) if row.get("equity_after") is not None else None,
        "status": status,
        "trade_id": str(row.get("trade_id") or event_id),
        "order_id": str(row.get("order_id") or event_id.replace("TRADE", "ORDER")),
        "prediction_id": row.get("prediction_id"),
        "scenario": row.get("scenario"),
        "reason": row.get("reason"),
        "risk": row.get("risk"),
        "paper_trading_only": row.get("paper_trading_only", True) is True,
        "isolated_replay": False,
        "no_real_broker_order": row.get("no_real_broker_order", True) is True,
    }


def canonicalize_replay_trade(row: dict[str, Any], source_path: Path, line_number: int) -> dict[str, Any]:
    identity = source_identity(source_path, line_number, row)
    event_id = ledger_event_id("ATLASREPLAYTRADE", identity)
    replay_id = str(row.get("replay_id") or "unknown-replay")
    price = maybe_float(row.get("price"))
    quantity = maybe_float(row.get("quantity"))
    return {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_event_id": event_id,
        "event_type": "isolated_replay_trade",
        "idempotency_key": event_id,
        "source_system": "trading-core",
        "source_ledger": "global_briefing_isolated_replay",
        "source_path": relative_path(source_path),
        "source_line": line_number,
        "source_hash": identity["sha256"],
        "date": str(row.get("date") or ""),
        "timestamp": str(row.get("date") or ""),
        "account_id": replay_id,
        "virtual_account_scope": "REPLAY",
        "symbol": str(row.get("symbol") or ""),
        "exchange": None,
        "market_type": "REPLAY",
        "currency": "CNY",
        "side": str(row.get("side") or "UNKNOWN").upper(),
        "filled_price": price,
        "filled_quantity": quantity,
        "notional": maybe_float(row.get("notional"), price * quantity),
        "fee": maybe_float(row.get("fee")),
        "tax": maybe_float(row.get("tax")),
        "status": "FILLED",
        "trade_id": str(row.get("trade_id") or event_id),
        "order_id": str(row.get("order_id") or event_id.replace("TRADE", "ORDER")),
        "prediction_id": None,
        "scenario": "isolated historical replay",
        "reason": row.get("reason"),
        "risk": None,
        "paper_trading_only": row.get("paper_trading_only", True) is True,
        "isolated_replay": bool(row.get("isolated", True)),
        "no_real_broker_order": row.get("no_real_broker_order", True) is True,
    }


def load_temp_orders(path: Path) -> list[dict[str, Any]]:
    raw = read_json_file(path, default={})
    if isinstance(raw, dict):
        orders = raw.get("orders", [])
    elif isinstance(raw, list):
        orders = raw
    else:
        raise ValueError(f"{path}: temp order input must be an object or list")
    if not isinstance(orders, list) or not all(isinstance(order, dict) for order in orders):
        raise ValueError(f"{path}: temp order input must contain JSON object orders")
    return orders


def temp_order_date(path: Path, row: dict[str, Any]) -> str:
    if row.get("date"):
        return str(row["date"])
    match = re.search(r"temp(?:_|-)orders(?:_|-)(\d{4}-\d{2}-\d{2})\.json$", path.name)
    return match.group(1) if match else ""


def is_symbolic_all_temp_quantity(value: Any) -> bool:
    """Recognize only the exact paper-trading sentinel used by persisted order inputs."""
    return isinstance(value, str) and value == "ALL"


def reconcile_symbolic_all_temp_order(
    row: dict[str, Any],
    executed_orders_by_id: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Resolve a persisted ``quantity=ALL`` intent from its one executed paper trade.

    The symbolic input remains the provenance-bearing source record.  Resolution is
    deliberately limited to an already-persisted paper execution with the same stable
    order id and the same decision/execution fields; it never infers a quantity from a
    portfolio snapshot and never creates or replays an economic action.
    """
    if not is_symbolic_all_temp_quantity(row.get("quantity")):
        raise ValueError("symbolic reconciliation requires the exact quantity sentinel 'ALL'")
    order_id = row.get("order_id")
    if not isinstance(order_id, str) or not order_id or order_id != order_id.strip():
        raise ValueError("symbolic ALL quantity requires a non-empty, whitespace-stable order_id")
    if row.get("action") != "SELL":
        raise ValueError("symbolic ALL quantity is only valid for a SELL order")

    matches = executed_orders_by_id.get(order_id, [])
    if not matches:
        raise ValueError(
            f"symbolic ALL order {order_id} has no matching executed paper trade"
        )
    if len(matches) != 1:
        raise ValueError(
            f"symbolic ALL order {order_id} has {len(matches)} matching executed paper trades; "
            "resolution is ambiguous"
        )
    match = matches[0]
    executed = match["row"]

    exact_fields = (
        ("date", "date"),
        ("account", "account"),
        ("action", "action"),
        ("symbol", "symbol"),
        ("exchange", "exchange"),
        ("market", "market_type"),
        ("price_date", "price_date"),
        ("prediction_id", "prediction_id"),
        ("scenario", "scenario"),
        ("reason", "reason"),
        ("risk", "risk"),
        ("source", "source"),
    )
    mismatches: list[str] = []
    for intent_field, executed_field in exact_fields:
        if intent_field not in row or executed_field not in executed:
            mismatches.append(
                f"{intent_field}/{executed_field} missing from intent or execution"
            )
        elif row[intent_field] != executed[executed_field]:
            mismatches.append(
                f"{intent_field} mismatch intent={row[intent_field]!r} "
                f"execution={executed[executed_field]!r}"
            )

    account = row.get("account")
    expected_account_id = (
        row.get("account_id")
        if row.get("account_id") is not None
        else f"global-briefing-{str(account).lower()}-paper-trading"
    )
    if executed.get("account_id") != expected_account_id:
        mismatches.append(
            f"account_id mismatch intent={expected_account_id!r} "
            f"execution={executed.get('account_id')!r}"
        )

    expected_fingerprint = stable_hash(
        {
            "account_id": expected_account_id,
            "date": row.get("date"),
            "order": {
                key: value
                for key, value in row.items()
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
    executed_fingerprint = executed.get("order_fingerprint")
    if not isinstance(executed_fingerprint, str) or executed_fingerprint != expected_fingerprint:
        mismatches.append(
            f"order_fingerprint mismatch expected={expected_fingerprint!r} "
            f"execution={executed_fingerprint!r}"
        )

    try:
        intent_price = maybe_float(row.get("price"))
        executed_price = maybe_float(executed.get("price"))
    except ValueError as exc:
        mismatches.append(f"price is not finite numeric: {exc}")
        intent_price = executed_price = 0.0
    else:
        if intent_price <= 0 or executed_price <= 0 or intent_price != executed_price:
            mismatches.append(
                f"price mismatch intent={row.get('price')!r} execution={executed.get('price')!r}"
            )

    try:
        quantity = maybe_float(executed.get("quantity"))
    except ValueError as exc:
        mismatches.append(f"executed quantity is not finite numeric: {exc}")
        quantity = 0.0
    if quantity <= 0:
        mismatches.append(f"executed quantity must be positive, got {executed.get('quantity')!r}")

    try:
        executed_gross = maybe_float(executed.get("gross_value"))
    except ValueError as exc:
        mismatches.append(f"executed gross_value is not finite numeric: {exc}")
        executed_gross = 0.0
    expected_gross = executed_price * quantity
    if not math.isclose(executed_gross, expected_gross, rel_tol=1e-12, abs_tol=1e-9):
        mismatches.append(
            f"gross_value mismatch execution={executed.get('gross_value')!r} "
            f"price_times_quantity={expected_gross!r}"
        )
    if row.get("gross_value") is not None:
        try:
            intent_gross = maybe_float(row.get("gross_value"))
        except ValueError as exc:
            mismatches.append(f"intent gross_value is not finite numeric: {exc}")
        else:
            if intent_gross != executed_gross:
                mismatches.append(
                    f"gross_value mismatch intent={row.get('gross_value')!r} "
                    f"execution={executed.get('gross_value')!r}"
                )

    if mismatches:
        raise ValueError(
            f"symbolic ALL order {order_id} does not exactly match its executed paper trade: "
            + "; ".join(mismatches)
        )
    return {
        "mode": "executed_order_id",
        "symbolic_quantity": "ALL",
        "resolved_quantity": quantity,
        "matched_order_id": order_id,
        "resolved_source_ledger": str(match["source_ledger"]),
        "resolved_source_path": relative_path(match["path"]),
        "resolved_source_line": int(match["line_number"]),
        "resolved_source_hash": stable_hash(executed),
        "resolved_order_fingerprint": executed_fingerprint,
    }


def canonicalize_temp_order_intent(
    row: dict[str, Any],
    source_path: Path,
    order_index: int,
    *,
    quantity_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = source_identity(source_path, order_index, row)
    event_id = ledger_event_id("ATLASORDERINTENT", identity)
    action = str(row.get("action") or row.get("side") or "HOLD").upper()
    account = str(row.get("account") or row.get("paper_account") or row.get("market_scope") or "AUTO").upper()
    date = temp_order_date(source_path, row)
    quantity = maybe_float(
        quantity_resolution["resolved_quantity"]
        if quantity_resolution is not None
        else row.get("quantity")
    )
    price = maybe_float(row.get("price"))
    declared_notional = (
        row.get("notional")
        if row.get("notional") is not None
        else row.get("gross_value")
    )
    notional = maybe_float(declared_notional, price * quantity)
    has_positive_sizing = quantity > 0 or notional > 0
    event = {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_event_id": event_id,
        "event_type": "virtual_order_intent",
        "idempotency_key": event_id,
        "source_system": "global-briefing",
        "source_ledger": "global_briefing_temp_orders",
        "source_path": relative_path(source_path),
        "source_line": order_index,
        "source_hash": identity["sha256"],
        "date": date,
        "timestamp": str(row.get("timestamp") or f"{date}T00:00:00"),
        "account_id": str(row.get("account_id") or f"global-briefing-{account.lower()}-paper-trading"),
        "virtual_account_scope": account,
        "symbol": str(row.get("symbol") or ""),
        "exchange": row.get("exchange"),
        "market_type": row.get("market_type"),
        "currency": row.get("currency"),
        "side": action,
        "filled_price": price,
        "filled_quantity": quantity,
        "notional": notional,
        "fee": maybe_float(row.get("fee")),
        "tax": maybe_float(row.get("tax")),
        "status": (
            "INTENT_RECORDED"
            if action in {"BUY", "SELL"} and has_positive_sizing
            else "HELD"
        ),
        "trade_id": event_id,
        "order_id": str(row.get("order_id") or event_id),
        "prediction_id": row.get("prediction_id"),
        "scenario": row.get("scenario"),
        "reason": row.get("reason"),
        "risk": row.get("risk"),
        "paper_trading_only": row.get("paper_trading_only", True) is True,
        "isolated_replay": False,
        "no_real_broker_order": row.get("no_real_broker_order", True) is True,
    }
    if quantity_resolution is not None:
        event["quantity_resolution"] = quantity_resolution["mode"]
        event.update(
            {
                key: value
                for key, value in quantity_resolution.items()
                if key != "mode"
            }
        )
    return event


def load_virtual_account_snapshots(
    *,
    paper_snapshot: dict[str, Any] | None = None,
    source_errors: list[str] | None = None,
) -> dict[str, Any]:
    if paper_snapshot is None:
        paper_snapshot = load_locked_paper_execution_snapshot()
    accounts: dict[str, Any] = {}
    for snapshot in paper_snapshot.get("accounts", []):
        account = str(snapshot.get("account") or snapshot.get("market_scope") or "").upper()
        path = Path(str(snapshot.get("portfolio_path") or ""))
        payload = snapshot.get("state")
        if isinstance(payload, dict):
            safety_errors = raw_virtual_source_safety_errors(payload, path, "document")
            if safety_errors:
                if source_errors is None:
                    raise ValueError("; ".join(safety_errors))
                source_errors.extend(safety_errors)
                continue
            account_id = str(
                payload.get("account_id")
                or snapshot.get("account_id")
                or f"global-briefing-{account.lower()}-paper-trading"
            )
            accounts[account_id] = {
                "source_system": "global-briefing",
                "source_path": relative_path(path),
                "virtual_account_scope": account,
                "account_id": account_id,
                "cash": maybe_float(payload.get("cash")),
                "initial_cash": maybe_float(payload.get("initial_cash")),
                "realized_pnl": maybe_float(payload.get("realized_pnl")),
                "positions": payload.get("positions", {}),
                "last_prices": payload.get("last_prices", {}),
                "paper_trading_only": (
                    payload.get("mode") == "paper_trading"
                    and payload.get("paper_trading_only", True) is True
                ),
                "no_real_broker_order": payload.get("no_real_broker_order", True) is True,
            }
    replay_account_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "accounts"
    if replay_account_root.exists():
        for path in sorted(replay_account_root.glob("account-*.json")):
            payload = read_json_file(path, default=None)
            if isinstance(payload, dict):
                safety_errors = raw_virtual_source_safety_errors(payload, path, "document")
                if safety_errors:
                    if source_errors is None:
                        raise ValueError("; ".join(safety_errors))
                    source_errors.extend(safety_errors)
                    continue
                account_id = str(payload.get("replay_id") or path.stem)
                accounts[account_id] = {
                    "source_system": "trading-core",
                    "source_path": relative_path(path),
                    "virtual_account_scope": "REPLAY",
                    "account_id": account_id,
                    "cash": maybe_float(payload.get("cash")),
                    "equity": maybe_float(payload.get("equity")),
                    "positions": payload.get("positions", []),
                    "isolated_replay": payload.get("isolated") is True,
                    "paper_trading_only": payload.get("paper_trading_only", True) is True,
                    "no_real_broker_order": payload.get("no_real_broker_order", True) is True,
                }
    return accounts


def event_source_locator(event: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(event.get("source_ledger") or ""),
        str(event.get("source_path") or ""),
        int(event.get("source_line") or 0),
    )


def virtual_ledger_anchor_path() -> Path:
    """Store the canonical-ledger checkpoint outside the mutable workspace."""
    return trust_anchor_path("virtual-execution-ledger", LEDGER_ID)


def ledger_anchor_hash(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized.pop("anchor_sha256", None)
    normalized.pop("hmac_algorithm", None)
    normalized.pop("hmac_key_id", None)
    normalized.pop("hmac_sha256", None)
    return stable_hash(normalized)


def build_virtual_ledger_anchor(
    events: Sequence[dict[str, Any]],
    account_state: dict[str, Any],
    state_payload: dict[str, Any],
    *,
    generation_id: str,
    generation_ledger_path: Path,
    generation_state_path: Path,
) -> dict[str, Any]:
    """Build a cross-directory baseline for the committed canonical ledger."""
    if not generation_ledger_path.exists():
        raise FileNotFoundError(f"cannot anchor missing canonical ledger: {generation_ledger_path}")
    anchor_path = virtual_ledger_anchor_path()
    core: dict[str, Any] = {
        "schema_version": LEDGER_ANCHOR_SCHEMA_VERSION,
        "ledger_id": LEDGER_ID,
        "canonical_ledger_path": relative_path(VIRTUAL_LEDGER_PATH),
        "canonical_ledger_sha256": file_sha256(generation_ledger_path),
        "generation_id": generation_id,
        "generation_ledger_path": relative_path(generation_ledger_path),
        "generation_state_path": relative_path(generation_state_path),
        "generation_state_sha256": file_sha256(generation_state_path),
        "event_hash": stable_hash(list(events)),
        "account_hash": stable_hash(account_state),
        "content_hash": state_payload.get("content_hash"),
        "event_count": len(events),
        "state_content_hash": state_payload.get("content_hash"),
        "paper_generation_sha256": state_payload.get("paper_generation_sha256"),
        "prediction_semantic_hash": state_payload.get("prediction_semantic_hash"),
    }
    existing = read_json_file(anchor_path, default=None)
    if isinstance(existing, dict) and existing.get("schema_version") == LEDGER_ANCHOR_SCHEMA_VERSION:
        if all(existing.get(key) == value for key, value in core.items()):
            return existing
    payload = {
        **core,
        **next_monotonic_anchor_fields(anchor_path, head_hash_field="anchor_sha256"),
    }
    payload["anchor_sha256"] = ledger_anchor_hash(payload)
    return sign_trust_anchor(payload)


def audit_ledger_continuity(
    events: Sequence[dict[str, Any]],
    account_state: dict[str, Any] | None = None,
    *,
    predictions: dict[str, Any] | None = None,
    paper_generation_sha256: str | None = None,
) -> dict[str, Any]:
    """Fail closed when a committed ledger or its historical source is altered.

    The state file and the external anchor make a removed or truncated canonical
    JSONL observable instead of treating it as a first-run bootstrap.
    """
    blocking: list[str] = []
    current_events = list(events)
    state_present = VIRTUAL_LEDGER_STATE_PATH.exists()
    audit_present = VIRTUAL_LEDGER_AUDIT_PATH.exists()
    trust_root, trust_root_error = configured_trust_anchor_root()
    if trust_root is None:
        blocking.append(trust_root_error or "external trust-anchor root is unavailable")
    try:
        anchor_path: Path | None = virtual_ledger_anchor_path()
    except RuntimeError as exc:
        blocking.append(str(exc))
        anchor_path = None
    anchor_present = bool(anchor_path and anchor_path.exists())
    try:
        cycle_state = read_json_file(CYCLE_STATE_PATH, default={}) if CYCLE_STATE_PATH.exists() else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        cycle_state = {}
        blocking.append(f"cannot verify cycle state ledger evidence: {type(exc).__name__}: {exc}")
    cycle_state_has_ledger = bool(
        isinstance(cycle_state, dict)
        and cycle_state.get("canonical_ledger_write_performed") is True
        and (
            cycle_state.get("ledger_content_hash")
            or cycle_state.get("ledger_anchor_sha256")
        )
    )
    baseline_evidence_present = (
        VIRTUAL_LEDGER_PATH.exists()
        or state_present
        or audit_present
        or anchor_present
        or cycle_state_has_ledger
    )
    if trust_anchor_hmac_key() is None and baseline_evidence_present:
        blocking.append(
            f"canonical ledger cannot be verified: {TRUST_ANCHOR_HMAC_KEY_ENV} is not configured"
        )

    state: dict[str, Any] | None = None
    if state_present:
        try:
            loaded_state = read_json_file(VIRTUAL_LEDGER_STATE_PATH)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blocking.append(f"cannot verify canonical ledger state: {type(exc).__name__}: {exc}")
        else:
            if not isinstance(loaded_state, dict):
                blocking.append("canonical ledger state must be a JSON object")
            else:
                state = loaded_state

    anchor: dict[str, Any] | None = None
    if anchor_present:
        try:
            loaded_anchor = read_json_file(anchor_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blocking.append(f"cannot verify canonical ledger anchor: {type(exc).__name__}: {exc}")
        else:
            if not isinstance(loaded_anchor, dict):
                blocking.append("canonical ledger anchor must be a JSON object")
            else:
                anchor = loaded_anchor
                if anchor.get("schema_version") not in {
                    LEGACY_ANCHOR_SCHEMA_VERSION,
                    LEDGER_ANCHOR_SCHEMA_VERSION,
                }:
                    blocking.append("unsupported canonical ledger anchor schema")
                if anchor.get("anchor_sha256") != ledger_anchor_hash(anchor):
                    blocking.append("canonical ledger anchor hash mismatch")
                if anchor.get("ledger_id") != LEDGER_ID:
                    blocking.append("canonical ledger anchor ledger_id mismatch")
                if anchor.get("canonical_ledger_path") != relative_path(VIRTUAL_LEDGER_PATH):
                    blocking.append("canonical ledger anchor path mismatch")
                blocking.extend(
                    trust_anchor_authentication_errors(anchor, label="canonical ledger anchor")
                )
                if anchor.get("schema_version") == LEDGER_ANCHOR_SCHEMA_VERSION:
                    blocking.extend(
                        monotonic_anchor_errors(
                            anchor_path,
                            anchor,
                            label="canonical ledger anchor",
                            head_hash_field="anchor_sha256",
                        )
                    )

    baseline_ledger_path = VIRTUAL_LEDGER_PATH
    baseline_state_path = VIRTUAL_LEDGER_STATE_PATH
    if isinstance(anchor, dict) and anchor.get("schema_version") == LEDGER_ANCHOR_SCHEMA_VERSION:
        generation_ledger = anchor.get("generation_ledger_path")
        generation_state = anchor.get("generation_state_path")
        if isinstance(generation_ledger, str) and isinstance(generation_state, str):
            candidate_ledger = (ROOT / generation_ledger).resolve()
            candidate_state = (ROOT / generation_state).resolve()
            try:
                candidate_ledger.relative_to(VIRTUAL_LEDGER_GENERATIONS_ROOT.resolve())
                candidate_state.relative_to(VIRTUAL_LEDGER_GENERATIONS_ROOT.resolve())
            except ValueError:
                blocking.append("canonical ledger generation path escapes its authority root")
            else:
                baseline_ledger_path = candidate_ledger
                baseline_state_path = candidate_state
                if baseline_state_path.is_file():
                    try:
                        loaded_state = read_json_file(baseline_state_path)
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        blocking.append(
                            f"cannot verify canonical generation state: {type(exc).__name__}: {exc}"
                        )
                    else:
                        if isinstance(loaded_state, dict):
                            state = loaded_state
                            state_present = True
                        else:
                            blocking.append("canonical generation state must be a JSON object")
                else:
                    blocking.append("canonical ledger generation state is missing")

    if not baseline_ledger_path.exists():
        if baseline_evidence_present:
            blocking.append("canonical ledger is missing while persistent baseline evidence exists")
        return {
            "previous_ledger_present": False,
            "previous_event_count": 0,
            "preserved_event_count": 0,
            "added_event_count": len(events),
            "mutated_locators": [],
            "deleted_locators": [],
            "state_present": state_present,
            "anchor_present": anchor_present,
            "anchor_path": relative_path(anchor_path) if anchor_path is not None else None,
            "baseline_verified": False,
            "blocking_reasons": blocking,
        }
    try:
        previous_events = read_jsonl_file(baseline_ledger_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        blocking.append(f"cannot verify previous canonical ledger: {type(exc).__name__}: {exc}")
        return {
            "previous_ledger_present": True,
            "previous_event_count": None,
            "preserved_event_count": 0,
            "added_event_count": 0,
            "mutated_locators": [],
            "deleted_locators": [],
            "state_present": state_present,
            "anchor_present": anchor_present,
            "anchor_path": relative_path(anchor_path) if anchor_path is not None else None,
            "baseline_verified": False,
            "blocking_reasons": blocking,
        }
    if not state:
        blocking.append("canonical ledger exists without a verifiable state baseline")
    if not anchor:
        blocking.append("canonical ledger exists without an externally authenticated trust anchor")
    previous_hash = stable_hash(previous_events)
    previous_ledger_sha256 = file_sha256(baseline_ledger_path)
    state_accounts: dict[str, Any] | None = None
    if state:
        if state.get("ledger_id") != LEDGER_ID:
            blocking.append("canonical ledger state ledger_id mismatch")
        if state.get("event_count") != len(previous_events):
            blocking.append("canonical ledger state event count mismatch")
        if state.get("event_hash") != previous_hash:
            blocking.append("canonical ledger state event hash mismatch")
        if state.get("canonical_ledger_sha256") != previous_ledger_sha256:
            blocking.append("canonical ledger state file hash mismatch")
        raw_state_accounts = state.get("accounts")
        if not isinstance(raw_state_accounts, dict):
            blocking.append("canonical ledger state accounts are invalid")
        else:
            state_accounts = raw_state_accounts
            state_content_payload: dict[str, Any] = {
                "events": previous_events,
                "accounts": state_accounts,
            }
            if state.get("paper_generation_sha256") is not None:
                state_content_payload["paper_generation_sha256"] = state.get(
                    "paper_generation_sha256"
                )
            if state.get("prediction_semantic_hash") is not None:
                state_content_payload["prediction_semantic_hash"] = state.get(
                    "prediction_semantic_hash"
                )
            state_content_hash = stable_hash(state_content_payload)
            if state.get("content_hash") != state_content_hash:
                blocking.append("canonical ledger state content hash mismatch")
        previous_prediction_rows = state.get("prediction_row_hashes")
        if previous_prediction_rows is not None:
            current_prediction_rows = (
                predictions.get("row_hashes") if isinstance(predictions, dict) else None
            )
            if not isinstance(previous_prediction_rows, list) or not isinstance(
                current_prediction_rows, list
            ):
                blocking.append("canonical ledger prediction continuity metadata is invalid")
            elif current_prediction_rows[: len(previous_prediction_rows)] != previous_prediction_rows:
                blocking.append("prediction ledger history was deleted, reordered, or mutated")
    if anchor:
        if anchor.get("event_count") != len(previous_events):
            blocking.append("canonical ledger anchor event count mismatch")
        if anchor.get("canonical_ledger_sha256") != previous_ledger_sha256:
            blocking.append("canonical ledger anchor file hash mismatch")
        if anchor.get("event_hash") != previous_hash:
            blocking.append("canonical ledger anchor event hash mismatch")
        if state is not None and state_accounts is not None:
            if anchor.get("state_content_hash") != state.get("content_hash"):
                blocking.append("canonical ledger anchor state content hash mismatch")
            if anchor.get("content_hash") != state.get("content_hash"):
                blocking.append("canonical ledger anchor content hash mismatch")
            if anchor.get("account_hash") != stable_hash(state_accounts):
                blocking.append("canonical ledger anchor account hash mismatch")
            anchor_prediction_hash = anchor.get("prediction_semantic_hash")
            state_prediction_hash = state.get("prediction_semantic_hash")
            if anchor_prediction_hash is not None and anchor_prediction_hash != state_prediction_hash:
                blocking.append("canonical ledger anchor prediction hash mismatch")
            anchor_generation = anchor.get("paper_generation_sha256")
            state_generation = state.get("paper_generation_sha256")
            if anchor_generation is not None and anchor_generation != state_generation:
                blocking.append("canonical ledger anchor paper generation mismatch")
    previous_by_locator = {event_source_locator(event): event for event in previous_events}
    current_by_locator = {event_source_locator(event): event for event in events}
    if len(previous_by_locator) != len(previous_events):
        blocking.append("previous canonical ledger has duplicate source locators")
    if len(current_by_locator) != len(current_events):
        blocking.append("current virtual sources have duplicate source locators")
    deleted = sorted(set(previous_by_locator) - set(current_by_locator))
    mutated = sorted(
        locator
        for locator in set(previous_by_locator) & set(current_by_locator)
        if stable_hash(previous_by_locator[locator]) != stable_hash(current_by_locator[locator])
    )
    blocking.extend(f"previous canonical event source deleted: {locator}" for locator in deleted)
    blocking.extend(f"previous canonical event mutated: {locator}" for locator in mutated)
    preserved = len(set(previous_by_locator) & set(current_by_locator)) - len(mutated)
    return {
        "previous_ledger_present": True,
        "previous_event_count": len(previous_events),
        "preserved_event_count": preserved,
        "added_event_count": len(set(current_by_locator) - set(previous_by_locator)),
        "mutated_locators": [list(locator) for locator in mutated],
        "deleted_locators": [list(locator) for locator in deleted],
        "state_present": state_present,
        "anchor_present": anchor_present,
        "anchor_path": relative_path(anchor_path) if anchor_path is not None else None,
        "baseline_verified": bool(state and anchor and not blocking),
        "blocking_reasons": blocking,
    }


def normalized_position_key(exchange: Any, symbol: Any) -> str:
    exchange_text = str(exchange or "").strip().upper()
    symbol_text = str(symbol or "").strip().upper()
    return f"{exchange_text}:{symbol_text}" if exchange_text else symbol_text


def reconcile_virtual_accounts(
    events: Sequence[dict[str, Any]], account_state: dict[str, Any]
) -> dict[str, Any]:
    """Replay filled trades and compare quantities/cash with account snapshots."""
    blocking: list[str] = []
    accounts: list[dict[str, Any]] = []
    for account_id, snapshot in sorted(account_state.items()):
        filled = [
            event
            for event in events
            if event.get("account_id") == account_id
            and event.get("event_type") in {"virtual_trade", "isolated_replay_trade"}
        ]
        replayed: dict[str, float] = {}
        for event in filled:
            key = normalized_position_key(
                event.get("exchange") if snapshot.get("virtual_account_scope") != "REPLAY" else None,
                event.get("symbol"),
            )
            direction = 1.0 if event.get("side") == "BUY" else -1.0
            replayed[key] = replayed.get(key, 0.0) + direction * maybe_float(event.get("filled_quantity"))
        raw_positions = snapshot.get("positions", {})
        if isinstance(raw_positions, dict):
            position_rows = list(raw_positions.values())
        elif isinstance(raw_positions, list):
            position_rows = raw_positions
        else:
            position_rows = []
            blocking.append(f"account {account_id}: positions must be an object or list")
        actual: dict[str, float] = {}
        for position in position_rows:
            if not isinstance(position, dict):
                blocking.append(f"account {account_id}: position row must be an object")
                continue
            key = normalized_position_key(
                position.get("exchange") if snapshot.get("virtual_account_scope") != "REPLAY" else None,
                position.get("symbol"),
            )
            actual[key] = actual.get(key, 0.0) + maybe_float(position.get("quantity"))
        mismatches: list[dict[str, Any]] = []
        for key in sorted(set(replayed) | set(actual)):
            expected_quantity = replayed.get(key, 0.0)
            actual_quantity = actual.get(key, 0.0)
            tolerance = max(1e-8, abs(expected_quantity) * 1e-9)
            if abs(expected_quantity - actual_quantity) > tolerance:
                mismatch = {
                    "position": key,
                    "replayed_quantity": expected_quantity,
                    "snapshot_quantity": actual_quantity,
                }
                mismatches.append(mismatch)
                blocking.append(f"account {account_id}: position reconciliation failed {mismatch}")
        cash_events = [event for event in filled if event.get("cash_after") is not None]
        cash_match: bool | None = None
        replayed_cash: float | None = None
        if cash_events and snapshot.get("virtual_account_scope") != "REPLAY":
            replayed_cash = maybe_float(cash_events[-1].get("cash_after"))
            snapshot_cash = maybe_float(snapshot.get("cash"))
            cash_match = abs(replayed_cash - snapshot_cash) <= max(1e-6, abs(replayed_cash) * 1e-9)
            if not cash_match:
                blocking.append(
                    f"account {account_id}: cash reconciliation failed "
                    f"replayed={replayed_cash} snapshot={snapshot_cash}"
                )
        accounts.append(
            {
                "account_id": account_id,
                "filled_trade_count": len(filled),
                "position_count": len(actual),
                "position_mismatches": mismatches,
                "cash_replayed": replayed_cash,
                "cash_snapshot": snapshot.get("cash"),
                "cash_match": cash_match,
            }
        )
    return {"accounts": accounts, "blocking_reasons": blocking, "passed": not blocking}


def audit_virtual_execution_ledger(
    events: Sequence[dict[str, Any]],
    account_state: dict[str, Any],
    *,
    warnings: Sequence[str] = (),
    source_expectations: Sequence[dict[str, Any]] = (),
    source_errors: Sequence[str] = (),
    predictions: dict[str, Any] | None = None,
    paper_generation_sha256: str | None = None,
) -> dict[str, Any]:
    blocking: list[str] = list(source_errors)
    prediction_snapshot = predictions or {
        "errors": ["prediction integrity snapshot is unavailable"],
        "originals": {},
        "semantic_hash": None,
        "row_count": 0,
    }
    blocking.extend(str(error) for error in prediction_snapshot.get("errors", []))
    prediction_boundary = prediction_reference_required_from_date()
    originals = prediction_snapshot.get("originals", {})
    ids = [str(event.get("ledger_event_id", "")) for event in events]
    if len(ids) != len(set(ids)):
        blocking.append("duplicate ledger_event_id detected")
    allowed_sides = {"BUY", "SELL", "HOLD"}
    required_trade_fields = {"trade_id", "order_id", "date", "account_id", "symbol", "side", "filled_price", "filled_quantity", "status"}
    required_non_empty = {"ledger_event_id", "event_type", "date", "account_id", "side", "status"}
    source_counts: dict[str, int] = {}
    source_path_counts: dict[str, int] = {}
    for event in events:
        event_id = str(event.get("ledger_event_id") or "missing-event-id")
        source_counts[str(event.get("source_ledger"))] = source_counts.get(str(event.get("source_ledger")), 0) + 1
        source_path = str(event.get("source_path") or "")
        source_path_counts[source_path] = source_path_counts.get(source_path, 0) + 1
        missing = sorted(field for field in required_trade_fields if field not in event)
        if missing:
            blocking.append(f"{event_id}: missing required fields {missing}")
        empty = sorted(
            field
            for field in required_non_empty
            if event.get(field) is None or event.get(field) == ""
        )
        if empty:
            blocking.append(f"{event_id}: empty required fields {empty}")
        if event.get("ledger_id") != LEDGER_ID:
            blocking.append(f"{event_id}: wrong ledger_id")
        if event.get("paper_trading_only") is not True:
            blocking.append(f"{event_id}: paper_trading_only is not true")
        if event.get("no_real_broker_order") is not True:
            blocking.append(f"{event_id}: no_real_broker_order is not true")
        if str(event.get("side")).upper() not in allowed_sides:
            blocking.append(f"{event_id}: unsupported side {event.get('side')!r}")
        numeric_values: dict[str, float | None] = {}
        for field in (
            "filled_quantity",
            "filled_price",
            "notional",
            "fee",
            "tax",
            "cash_after",
            "equity_after",
        ):
            if field not in event or event.get(field) is None:
                numeric_values[field] = None
                continue
            try:
                numeric_values[field] = maybe_float(event.get(field))
            except ValueError as exc:
                numeric_values[field] = None
                blocking.append(f"{event_id}: invalid {field}: {exc}")
        quantity = numeric_values.get("filled_quantity")
        price = numeric_values.get("filled_price")
        if quantity is not None and quantity < 0:
            blocking.append(f"{event_id}: negative quantity")
        if price is not None and price < 0:
            blocking.append(f"{event_id}: negative price")
        for field in ("notional", "fee", "tax"):
            value = numeric_values.get(field)
            if value is not None and value < 0:
                blocking.append(f"{event_id}: negative {field}")
        if not is_iso_date(event.get("date")):
            blocking.append(f"{event_id}: invalid date {event.get('date')!r}")
        event_type = event.get("event_type")
        if event_type not in {"virtual_trade", "virtual_hold", "virtual_order_intent", "isolated_replay_trade"}:
            blocking.append(f"{event_id}: unsupported event_type {event_type!r}")
        if event_type in {"virtual_trade", "isolated_replay_trade"}:
            if str(event.get("side")).upper() not in {"BUY", "SELL"}:
                blocking.append(f"{event_id}: filled trade must be BUY or SELL")
            if quantity is None or price is None or quantity <= 0 or price <= 0:
                blocking.append(f"{event_id}: filled trade requires positive quantity and price")
        if event_type == "isolated_replay_trade" and event.get("isolated_replay") is not True:
            blocking.append(f"{event_id}: replay trade is not isolated")
        if event_type == "virtual_hold":
            if str(event.get("side")).upper() != "HOLD" or event.get("status") != "HELD":
                blocking.append(f"{event_id}: virtual hold must use HOLD/HELD")
            if quantity is None or quantity != 0:
                blocking.append(f"{event_id}: virtual hold quantity must be zero")
        if event_type == "virtual_order_intent" and str(event.get("side")).upper() in {"BUY", "SELL"}:
            notional = numeric_values.get("notional")
            if (
                price is None
                or price <= 0
                or (
                    (quantity is None or quantity <= 0)
                    and (notional is None or notional <= 0)
                )
            ):
                blocking.append(
                    f"{event_id}: order intent requires a positive price and "
                    "positive quantity or notional"
                )
        if (
            event.get("source_system") == "global-briefing"
            and event_type in {"virtual_trade", "virtual_hold", "virtual_order_intent"}
            and is_iso_date(event.get("date"))
            and str(event.get("date")) >= prediction_boundary
        ):
            prediction_id = event.get("prediction_id")
            if not isinstance(prediction_id, str) or not prediction_id.strip():
                blocking.append(
                    f"{event_id}: prediction_id is required from {prediction_boundary}"
                )
            elif not isinstance(originals, dict) or prediction_id not in originals:
                blocking.append(
                    f"{event_id}: prediction_id {prediction_id} has no original prediction record"
                )
        forbidden = sorted(nested_forbidden_keys(event))
        if forbidden:
            blocking.append(f"{event_id}: forbidden broker/live keys {forbidden}")
    if not account_state:
        blocking.append("no virtual account snapshots found")
    for account_id, account in account_state.items():
        if account.get("paper_trading_only") is not True:
            blocking.append(f"account {account_id}: paper_trading_only is not true")
        if account.get("no_real_broker_order") is not True:
            blocking.append(f"account {account_id}: no_real_broker_order is not true")
        if account.get("virtual_account_scope") == "REPLAY" and account.get("isolated_replay") is not True:
            blocking.append(f"account {account_id}: replay account is not isolated")
        forbidden = sorted(nested_forbidden_keys(account))
        if forbidden:
            blocking.append(f"account {account_id}: forbidden broker/live keys {forbidden}")
    for expectation in source_expectations:
        path = str(expectation.get("path") or "")
        if expectation.get("required") and not expectation.get("present"):
            blocking.append(f"required virtual ledger source missing: {path}")
        expected_count = expectation.get("expected_event_count")
        if expected_count is not None and source_path_counts.get(path, 0) != expected_count:
            blocking.append(
                f"virtual ledger source count mismatch: {path} "
                f"expected={expected_count} actual={source_path_counts.get(path, 0)}"
            )
    snapshot_paths = {str(account.get("source_path") or "") for account in account_state.values()}
    for expectation in source_expectations:
        if expectation.get("kind") == "account_snapshot" and expectation.get("present"):
            if str(expectation.get("path") or "") not in snapshot_paths:
                blocking.append(f"account snapshot was not loaded: {expectation.get('path')}")
    reconciliation = reconcile_virtual_accounts(events, account_state)
    blocking.extend(reconciliation["blocking_reasons"])
    continuity = audit_ledger_continuity(
        events,
        account_state,
        predictions=prediction_snapshot,
        paper_generation_sha256=paper_generation_sha256,
    )
    blocking.extend(continuity["blocking_reasons"])
    audit_content = {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "blocking_reasons": blocking,
        "warnings": list(warnings),
        "event_hash": stable_hash(list(events)),
        "account_hash": stable_hash(account_state),
        "paper_generation_sha256": paper_generation_sha256,
        "prediction_semantic_hash": prediction_snapshot.get("semantic_hash"),
    }
    audit_content_hash = stable_hash(audit_content)
    payload = {
        "audit_id": f"ATLAS-VIRTUAL-LEDGER-AUDIT-{audit_content_hash[:24].upper()}",
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "content_hash": audit_content_hash,
        "overall_passed": not blocking,
        "blocking_reasons": blocking,
        "warnings": list(warnings),
        "canonical_ledger_path": relative_path(VIRTUAL_LEDGER_PATH),
        "canonical_state_path": relative_path(VIRTUAL_LEDGER_STATE_PATH),
        "event_count": len(events),
        "account_count": len(account_state),
        "source_counts": source_counts,
        "source_path_counts": source_path_counts,
        "source_expectations": list(source_expectations),
        "reconciliation": reconciliation,
        "continuity": continuity,
        "prediction_integrity": {
            "path": prediction_snapshot.get("path"),
            "row_count": prediction_snapshot.get("row_count"),
            "semantic_hash": prediction_snapshot.get("semantic_hash"),
            "reference_required_from_date": prediction_boundary,
        },
        "paper_snapshot": {
            "generation_sha256": paper_generation_sha256,
            "locked_cross_account": bool(paper_generation_sha256),
        },
        "legacy_sources_read_only": [
            relative_path(BRIEFING_ROOT / "data" / "paper_trades_us.jsonl"),
            relative_path(BRIEFING_ROOT / "data" / "paper_trades_china.jsonl"),
            relative_path(TRADING_ROOT / "data" / "replays" / "global_briefing" / "trades"),
        ],
        "boundary": {
            "single_canonical_virtual_ledger": True,
            "legacy_ledgers_are_read_only_sources": True,
            "paper_trading_only": True,
            "real_broker_orders_allowed": False,
            "external_broker_connection": False,
            "live_trading": False,
            "historical_source_mutation_fails_closed": continuity["baseline_verified"],
            "canonical_ledger_anchor_verified": continuity["baseline_verified"],
            "account_snapshots_reconciled": reconciliation["passed"],
        },
    }
    return payload


def build_virtual_execution_ledger(*, write_files: bool = True) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_errors: list[str] = []
    if (
        write_files
        and TRUST_ANCHOR_HMAC_KEY_ENV in os.environ
        and not str(os.environ.get(TRUST_ANCHOR_HMAC_KEY_ENV) or "").strip()
    ):
        source_errors.append(
            f"canonical ledger cannot be verified: {TRUST_ANCHOR_HMAC_KEY_ENV} is not configured"
        )
    source_expectations: list[dict[str, Any]] = []
    executed_orders_by_id: dict[str, list[dict[str, Any]]] = {}
    paper_snapshot = load_locked_paper_execution_snapshot()
    paper_generation_sha256 = str(paper_snapshot["generation_sha256"])
    paper_sources: list[tuple[str, Path, list[dict[str, Any]]]] = []
    for snapshot in paper_snapshot["accounts"]:
        account = str(snapshot.get("account") or snapshot.get("market_scope") or "").upper()
        source_ledger = f"global_briefing_paper_{account.lower()}"
        path = Path(str(snapshot.get("trades_path") or ""))
        rows = snapshot.get("trades")
        if not path.is_absolute() or not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows
        ):
            source_errors.append(f"locked paper snapshot is malformed for account {account or '?'}")
            continue
        paper_sources.append((source_ledger, path, rows))
    for source_ledger, path, rows in paper_sources:
        expectation = {
            "kind": "event_ledger",
            "source_ledger": source_ledger,
            "path": relative_path(path),
            "required": True,
            "present": True,
            "expected_event_count": len(rows),
        }
        source_expectations.append(expectation)
        for line_number, row in enumerate(rows, 1):
            safety_errors = raw_virtual_source_safety_errors(row, path, f"line {line_number}")
            if safety_errors:
                source_errors.extend(safety_errors)
                continue
            events.append(canonicalize_global_paper_trade(row, path, line_number, source_ledger))
            order_id = row.get("order_id")
            if isinstance(order_id, str) and order_id:
                executed_orders_by_id.setdefault(order_id, []).append(
                    {
                        "row": row,
                        "path": path,
                        "line_number": line_number,
                        "source_ledger": source_ledger,
                    }
                )

    temp_order_dates: dict[str, list[Path]] = {}
    for path in discover_briefing_temp_files("orders"):
        rows = load_temp_orders(path)
        source_expectations.append(
            {
                "kind": "event_ledger",
                "source_ledger": "global_briefing_temp_orders",
                "path": relative_path(path),
                "required": True,
                "present": True,
                "expected_event_count": len(rows),
            }
        )
        date = temp_order_date(path, rows[0] if rows else {})
        if date:
            temp_order_dates.setdefault(date, []).append(path)
        for order_index, row in enumerate(rows, 1):
            quantity_resolution: dict[str, Any] | None = None
            safety_payload = row
            if is_symbolic_all_temp_quantity(row.get("quantity")):
                try:
                    quantity_resolution = reconcile_symbolic_all_temp_order(
                        row, executed_orders_by_id
                    )
                except ValueError as exc:
                    source_errors.append(
                        f"{relative_path(path)}:order {order_index}: {exc}"
                    )
                    continue
                safety_payload = dict(row)
                safety_payload["quantity"] = quantity_resolution["resolved_quantity"]
            safety_errors = raw_virtual_source_safety_errors(
                safety_payload, path, f"order {order_index}"
            )
            if safety_errors:
                source_errors.extend(safety_errors)
                continue
            events.append(
                canonicalize_temp_order_intent(
                    row,
                    path,
                    order_index,
                    quantity_resolution=quantity_resolution,
                )
            )
    for date, paths in temp_order_dates.items():
        non_empty = [path for path in paths if load_temp_orders(path)]
        if len(non_empty) > 1:
            source_errors.append(
                f"multiple non-empty temp order files for {date}: "
                + ", ".join(relative_path(path) for path in non_empty)
            )

    replay_trade_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "trades"
    if replay_trade_root.exists():
        for path in sorted(replay_trade_root.glob("*.jsonl")):
            rows = read_jsonl_file(path)
            source_expectations.append(
                {
                    "kind": "event_ledger",
                    "source_ledger": "global_briefing_isolated_replay",
                    "path": relative_path(path),
                    "required": True,
                    "present": True,
                    "expected_event_count": len(rows),
                }
            )
            for line_number, row in enumerate(rows, 1):
                safety_errors = raw_virtual_source_safety_errors(row, path, f"line {line_number}")
                if safety_errors:
                    source_errors.extend(safety_errors)
                    continue
                events.append(canonicalize_replay_trade(row, path, line_number))
    else:
        warnings.append(f"missing replay trade source directory: {relative_path(replay_trade_root)}")
        source_errors.append(f"required replay trade source directory missing: {relative_path(replay_trade_root)}")

    events = sorted(
        events,
        key=lambda event: (
            str(event.get("date", "")),
            str(event.get("timestamp", "")),
            str(event.get("source_ledger", "")),
            int(event.get("source_line") or 0),
            str(event.get("ledger_event_id", "")),
        ),
    )
    account_state = load_virtual_account_snapshots(
        paper_snapshot=paper_snapshot,
        source_errors=source_errors,
    )
    for snapshot in paper_snapshot["accounts"]:
        path = Path(str(snapshot.get("portfolio_path") or ""))
        source_expectations.append(
            {
                "kind": "account_snapshot",
                "path": relative_path(path),
                "required": True,
                "present": True,
                "expected_event_count": None,
            }
        )
    replay_account_root = TRADING_ROOT / "data" / "replays" / "global_briefing" / "accounts"
    for path in sorted(replay_account_root.glob("account-*.json")) if replay_account_root.exists() else []:
        source_expectations.append(
            {
                "kind": "account_snapshot",
                "path": relative_path(path),
                "required": True,
                "present": True,
                "expected_event_count": None,
            }
        )
    predictions = prediction_integrity_snapshot()
    content_hash = stable_hash(
        {
            "events": events,
            "accounts": account_state,
            "paper_generation_sha256": paper_generation_sha256,
            "prediction_semantic_hash": predictions["semantic_hash"],
        }
    )
    previous_state = read_current_canonical_state()
    generated_at = utc_now()
    if isinstance(previous_state, dict) and previous_state.get("content_hash") == content_hash:
        generated_at = str(previous_state.get("generated_at") or generated_at)
    try:
        anchor_path: Path | None = virtual_ledger_anchor_path()
    except RuntimeError:
        # The continuity audit below records the configuration error as a
        # fail-closed result.  Do not turn a missing trust configuration into an
        # uncaught exception for callers that need machine-readable diagnostics.
        anchor_path = None
    state_payload = {
        "ledger_id": LEDGER_ID,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "generated_at": generated_at,
        "content_hash": content_hash,
        "canonical_ledger_path": relative_path(VIRTUAL_LEDGER_PATH),
        "accounts": account_state,
        "event_count": len(events),
        "event_hash": stable_hash(events),
        "paper_generation_sha256": paper_generation_sha256,
        "prediction_ledger_path": predictions["path"],
        "prediction_row_hashes": predictions["row_hashes"],
        "prediction_semantic_hash": predictions["semantic_hash"],
        "anchor_path": relative_path(anchor_path) if anchor_path is not None else None,
    }
    audit = audit_virtual_execution_ledger(
        events,
        account_state,
        warnings=warnings,
        source_expectations=source_expectations,
        source_errors=source_errors,
        predictions=predictions,
        paper_generation_sha256=paper_generation_sha256,
    )
    write_performed = bool(write_files and audit["overall_passed"])
    ledger_anchor: dict[str, Any] | None = None
    generation_id: str | None = None
    if write_performed:
        generation_id = content_hash
        generation_root = VIRTUAL_LEDGER_GENERATIONS_ROOT / generation_id
        generation_ledger_path = generation_root / "ledger.jsonl"
        generation_state_path = generation_root / "state.json"
        generation_audit_path = generation_root / "audit.json"
        generation_audit_markdown_path = generation_root / "audit.md"
        write_immutable_jsonl(generation_ledger_path, events)
        state_payload["canonical_ledger_sha256"] = file_sha256(generation_ledger_path)
        state_payload["generation_id"] = generation_id
        write_immutable_json(generation_state_path, state_payload)
        ledger_anchor = build_virtual_ledger_anchor(
            events,
            account_state,
            state_payload,
            generation_id=generation_id,
            generation_ledger_path=generation_ledger_path,
            generation_state_path=generation_state_path,
        )
        if anchor_path is None:
            raise RuntimeError("cannot write canonical ledger without an external trust anchor path")
        current_anchor = read_json_file(anchor_path, default=None)
        if not (
            isinstance(current_anchor, dict)
            and current_anchor.get("anchor_sha256") == ledger_anchor.get("anchor_sha256")
        ):
            write_monotonic_anchor(
                anchor_path,
                ledger_anchor,
                head_hash_field="anchor_sha256",
            )
        # Re-audit against the just-established canonical baseline so the first
        # successful write and every idempotent replay produce identical audit bytes.
        audit = audit_virtual_execution_ledger(
            events,
            account_state,
            warnings=warnings,
            source_expectations=source_expectations,
            source_errors=source_errors,
            predictions=predictions,
            paper_generation_sha256=paper_generation_sha256,
        )
        if not audit.get("overall_passed"):
            raise RuntimeError(
                "canonical generation failed its post-anchor audit: "
                + "; ".join(audit.get("blocking_reasons", []))
            )
        audit_markdown = build_virtual_ledger_audit_markdown(audit)
        write_immutable_json(generation_audit_path, audit)
        write_immutable_text(generation_audit_markdown_path, audit_markdown)
        generation_head = {
            "schema_version": 1,
            "ledger_id": LEDGER_ID,
            "generation_id": generation_id,
            "ledger_path": relative_path(generation_ledger_path),
            "ledger_sha256": file_sha256(generation_ledger_path),
            "state_path": relative_path(generation_state_path),
            "state_sha256": file_sha256(generation_state_path),
            "audit_path": relative_path(generation_audit_path),
            "audit_sha256": file_sha256(generation_audit_path),
            "anchor_sha256": ledger_anchor.get("anchor_sha256"),
            "content_hash": content_hash,
        }
        generation_head["head_sha256"] = stable_hash(generation_head)
        atomic_write_json(VIRTUAL_LEDGER_HEAD_PATH, generation_head)
        # Backward-compatible projections are repaired only after the single
        # authoritative head points at a fully verified immutable generation.
        atomic_write_text(
            VIRTUAL_LEDGER_PATH,
            generation_ledger_path.read_text(encoding="utf-8"),
        )
        atomic_write_text(
            VIRTUAL_LEDGER_STATE_PATH,
            generation_state_path.read_text(encoding="utf-8"),
        )
        atomic_write_text(
            VIRTUAL_LEDGER_AUDIT_PATH,
            generation_audit_path.read_text(encoding="utf-8"),
        )
        atomic_write_text(VIRTUAL_LEDGER_AUDIT_PATH.with_suffix(".md"), audit_markdown)
    return {
        "ledger_path": str(VIRTUAL_LEDGER_PATH),
        "state_path": str(VIRTUAL_LEDGER_STATE_PATH),
        "audit_path": str(VIRTUAL_LEDGER_AUDIT_PATH),
        "event_count": len(events),
        "account_count": len(account_state),
        "content_hash": content_hash,
        "paper_generation_sha256": paper_generation_sha256,
        "prediction_semantic_hash": predictions["semantic_hash"],
        "generation_id": generation_id,
        "generation_head_path": str(VIRTUAL_LEDGER_HEAD_PATH),
        "write_requested": write_files,
        "write_performed": write_performed,
        "events": events,
        "state": state_payload,
        "anchor_path": str(anchor_path) if anchor_path is not None else None,
        "anchor_sha256": ledger_anchor.get("anchor_sha256") if ledger_anchor else None,
        "audit": audit,
    }


def build_virtual_ledger_audit_markdown(audit: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# ATLAS Virtual Execution Ledger Audit",
            "",
            "## Verdict",
            f"- overall_passed={str(audit['overall_passed']).lower()}",
            f"- blocking_reasons={audit['blocking_reasons']}",
            "",
            "## Canonical Ledger",
            f"- ledger_id={audit['ledger_id']}",
            f"- path={audit['canonical_ledger_path']}",
            f"- event_count={audit['event_count']}",
            f"- account_count={audit['account_count']}",
            "",
            "## Source Counts",
            *[f"- {name}: {count}" for name, count in sorted(audit["source_counts"].items())],
            "",
            "## Boundary",
            "- single canonical virtual ledger",
            "- legacy US/CHINA/replay ledgers are read-only sources",
            "- paper trading only",
            "- no broker connection",
            "- no live trading",
            "",
        ]
    )


def latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    candidates = sorted(directory.glob(pattern), key=lambda path: (path.stat().st_mtime_ns, path.name))
    return candidates[-1] if candidates else None


def select_replay_evaluation(replay_dir: Path, date: str) -> tuple[Path | None, str | None, str | None]:
    cutoff = Date.fromisoformat(date)
    candidates: list[tuple[Date, Date, Path]] = []
    if replay_dir.exists():
        for path in replay_dir.glob("global_briefing_replay_evaluation-*.json"):
            match = REPLAY_EVALUATION_PATTERN.fullmatch(path.name)
            if not match:
                continue
            try:
                start = Date.fromisoformat(match.group(1))
                end = Date.fromisoformat(match.group(2))
            except ValueError:
                continue
            if start <= end <= cutoff:
                candidates.append((end, start, path))
    if not candidates:
        return None, None, None
    end, start, path = max(candidates, key=lambda item: (item[0], item[1], item[2].name))
    return path, start.isoformat(), end.isoformat()


def run_replay_shadow_validation(date: str) -> dict[str, Any]:
    replay_dir = TRADING_ROOT / "data" / "replays" / "global_briefing"
    replay_path, replay_start, replay_end = select_replay_evaluation(replay_dir, date)
    replay_payload = read_json_file(replay_path, default={}) if replay_path else {}
    integrity = replay_payload.get("integrity", {}) if isinstance(replay_payload, dict) else {}
    execution = replay_payload.get("execution", {}) if isinstance(replay_payload, dict) else {}
    replay_execution_passed = bool(
        replay_payload
        and replay_payload.get("overall_status") == "research_review_ready"
        and integrity.get("isolated_ledger_complete") is True
        and integrity.get("isolated_replay") is True
        and integrity.get("main_ledger_written") is False
        and integrity.get("run_daily_called") is False
        and execution.get("mode") == "isolated"
        and execution.get("isolated_outputs_present") is True
        and execution.get("no_trade_fallback") is False
    )
    strategy_validation = replay_payload.get("strategy_validation", {}) if isinstance(replay_payload, dict) else {}
    strategy_evidence_passed = bool(
        isinstance(strategy_validation, dict)
        and strategy_validation.get("passed") is True
        and integrity.get("labels_used") is True
        and (integrity.get("ml_shadow_used") is True or integrity.get("experiments_used") is True)
    )

    promotion_code = (
        "import json\n"
        "from trading_core.evolution.promotion_evidence import evaluate_verified_shadow_promotion\n"
        f"print(json.dumps(evaluate_verified_shadow_promotion('momentum_shadow_v1', {date!r}), ensure_ascii=False))\n"
    )
    promotion_command = capture_command([sys.executable, "-c", promotion_code], cwd=TRADING_ROOT, trading_core=True)
    promotion_payload: dict[str, Any]
    if promotion_command.returncode == 0:
        try:
            promotion_payload = json.loads(promotion_command.stdout.strip())
        except json.JSONDecodeError:
            promotion_payload = {"parse_error": promotion_command.stdout.strip()}
    else:
        promotion_payload = {"command_error": promotion_command.stderr.strip() or promotion_command.stdout.strip()}
    promotion_passed = bool(
        promotion_command.returncode == 0
        and promotion_payload.get("auto_applied") is False
        and promotion_payload.get("recommended_state") in {"shadow", "active_small"}
        and promotion_payload.get("recommended_state") != "active_normal"
    )

    return {
        "overall_passed": replay_execution_passed and promotion_passed,
        "safety_gate_passed": replay_execution_passed and promotion_passed,
        "strategy_evidence_passed": strategy_evidence_passed,
        "replay": {
            "passed": replay_execution_passed,
            "execution_safety_passed": replay_execution_passed,
            "strategy_evidence_passed": strategy_evidence_passed,
            "validation_scope": "execution_isolation_plus_explicit_strategy_evidence" if strategy_evidence_passed else "execution_isolation_only",
            "path": str(replay_path) if replay_path else None,
            "period_start": replay_start,
            "period_end": replay_end,
            "selected_as_of": date,
            "overall_status": replay_payload.get("overall_status") if isinstance(replay_payload, dict) else None,
            "integrity": integrity,
            "execution": execution,
        },
        "shadow_promotion_gate": {
            "passed": promotion_passed,
            "safety_passed": promotion_passed,
            "evidence_passed": strategy_evidence_passed,
            "payload": promotion_payload,
            "returncode": promotion_command.returncode,
        },
        "boundary": {
            "replay_isolated": replay_execution_passed,
            "promotion_auto_apply_allowed": False,
            "active_normal_requires_manual_review": True,
            "execution_safety_is_not_strategy_validation": True,
        },
    }


def build_cycle_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# ATLAS Cycle Run Audit",
        "",
        "## Verdict",
        f"- cycle_id={payload['cycle_id']}",
        f"- run_id={payload.get('run_id')}",
        f"- date={payload['date']}",
        f"- gate_scope={payload.get('gate_scope', 'legacy')}",
        f"- operational_gate_passed={str(payload.get('operational_gate_passed', payload['overall_passed'])).lower()}",
        f"- release_candidate_passed={str(payload.get('release_candidate_passed', False)).lower()}",
        f"- research_promotion_passed={str(payload.get('research_promotion_passed', False)).lower()}",
        f"- blocking_reasons={payload['blocking_reasons']}",
        "",
        "## Stages",
    ]
    for stage in payload["stages"]:
        lines.append(f"- {stage['name']}: status={stage['status']} detail={stage.get('detail')}")
    lines.extend(
        [
            "",
            "## Ledger",
            f"- canonical_ledger={payload['ledger']['ledger_path']}",
            f"- events={payload['ledger']['event_count']}",
            f"- accounts={payload['ledger']['account_count']}",
            f"- content_hash={payload['ledger'].get('content_hash')}",
            f"- write_performed={str(payload['ledger'].get('write_performed', False)).lower()}",
            "",
            "## Replay / Shadow Gate",
            f"- overall_passed={str(payload['replay_shadow_validation']['overall_passed']).lower()}",
            f"- replay_passed={str(payload['replay_shadow_validation']['replay']['passed']).lower()}",
            f"- shadow_promotion_gate_passed={str(payload['replay_shadow_validation']['shadow_promotion_gate']['passed']).lower()}",
            "",
            "## Boundary",
            "- virtual execution only",
            "- single canonical virtual ledger",
            "- legacy ledgers are read-only sources",
            "- no real broker orders",
            "- no automatic active-normal promotion",
            "",
        ]
    )
    return "\n".join(lines)


def doctor_checks(*, probe_repository_remotes: bool = True) -> list[Check]:
    checks: list[Check] = []
    checks.append(
        Check(
            "Python",
            "ok" if sys.version_info >= (3, 11) else "error",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    trust_root, trust_root_error = configured_trust_anchor_root()
    namespace = str(configured_trust_anchor_value(TRUST_ANCHOR_NAMESPACE_ENV) or "").strip()
    trust_key_configured = trust_anchor_hmac_key() is not None
    trust_ready = bool(trust_root is not None and namespace and trust_key_configured)
    trust_detail = (
        f"root={trust_root}; namespace={namespace}; HMAC key=configured"
        if trust_ready
        else trust_root_error
        or (
            f"{TRUST_ANCHOR_NAMESPACE_ENV} is not configured"
            if not namespace
            else f"{TRUST_ANCHOR_HMAC_KEY_ENV} is not configured"
        )
    )
    checks.append(
        Check(
            "external signed trust anchor",
            "ok" if trust_ready else "error",
            trust_detail,
        )
    )

    node = shutil.which("node")
    if node:
        completed = capture_command([node, "--version"])
        version = completed.stdout.strip() or completed.stderr.strip()
        node_ok = completed.returncode == 0 and parse_version(version) >= MINIMUM_NODE_VERSION
        detail = f"{version or 'version unavailable'}; required>=22.15.0"
        checks.append(Check("Node.js", "ok" if node_ok else "error", detail))
    else:
        checks.append(Check("Node.js", "error", "not found"))

    required_paths = {
        "global-briefing": BRIEFING_ROOT / "scripts" / "briefing_store.py",
        "trading-core": TRADING_ROOT / "src" / "trading_core" / "cli.py",
        "ATLAS site": SITE_ROOT / "package.json",
        "prediction adapter": BRIEFING_ROOT / "scripts" / "export_trading_core_signals.py",
        "research quality gate": BRIEFING_ROOT / "scripts" / "research_quality.py",
    }
    for name, path in required_paths.items():
        checks.append(Check(name, "ok" if path.exists() else "error", str(path.relative_to(ROOT))))

    repository_names = [
        ("root", ROOT),
        ("trading-core", TRADING_ROOT),
        ("ATLAS site", SITE_ROOT),
    ]
    repository_provenance = collect_repository_provenance(
        repository_names,
        probe_remotes=probe_repository_remotes,
    )
    checks.extend(
        repository_check_from_provenance(name, payload)
        for (name, _path), payload in zip(
            repository_names,
            repository_provenance,
            strict=True,
        )
    )

    missing_modules = [
        module
        for module in ("yfinance", "akshare", "baostock", "tushare")
        if importlib.util.find_spec(module) is None
    ]
    checks.append(
        Check(
            "Python data dependencies",
            "ok" if not missing_modules else "error",
            "installed" if not missing_modules else f"missing: {', '.join(missing_modules)}",
        )
    )

    if TRADING_ROOT.exists():
        core_version = capture_command(
            [sys.executable, "-m", "trading_core.entrypoint", "--version"],
            cwd=TRADING_ROOT,
            trading_core=True,
        )
        checks.append(
            Check(
                "trading-core CLI",
                "ok" if core_version.returncode == 0 else "error",
                (core_version.stdout or core_version.stderr).strip(),
            )
        )
    else:
        checks.append(Check("trading-core CLI", "error", "component path is missing"))

    try:
        report_date, report_path = latest_report()
        report_age_days = (configured_report_date() - Date.fromisoformat(report_date)).days
        report_fresh = 0 <= report_age_days <= 3
        checks.append(Check(
            "latest briefing",
            "ok" if report_fresh else "error",
            f"{report_date} ({report_path.name}); age_days={report_age_days}; max_age_days=3",
        ))
    except (FileNotFoundError, ValueError) as exc:
        report_date = ""
        checks.append(Check("latest briefing", "error", str(exc)))

    generated_path = SITE_ROOT / "app" / "briefing.generated.json"
    try:
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        generated_date = str(generated.get("reportDate", ""))
        site_status = "ok" if generated_date and generated_date == report_date else "warn"
        checks.append(Check("site data", site_status, f"reportDate={generated_date or 'missing'}", required=False))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(Check("site data", "warn", str(exc), required=False))

    signal_path = BRIEFING_ROOT / "data" / f"macro_signals-{report_date}.jsonl"
    checks.append(
        Check(
            "briefing → trading-core bridge",
            "ok" if report_date and signal_path.exists() else "warn",
            str(signal_path.relative_to(ROOT)) if report_date else "latest date unavailable",
            required=False,
        )
    )
    local_signal_path = TRADING_ROOT / "data" / "macro_signals" / signal_path.name
    if signal_path.exists() and local_signal_path.exists():
        bridge_fresh = file_sha256(signal_path) == file_sha256(local_signal_path)
        bridge_detail = "source and local hashes match" if bridge_fresh else "local copy is stale; run atlas sync"
    else:
        bridge_fresh = False
        bridge_detail = "source or local macro signal file is missing"
    checks.append(Check("macro bridge freshness", "ok" if bridge_fresh else "warn", bridge_detail, required=False))
    checks.append(
        Check(
            "site dependencies",
            "ok" if (SITE_ROOT / "node_modules").exists() else "warn",
            "installed" if (SITE_ROOT / "node_modules").exists() else "run npm install under src",
            required=False,
        )
    )
    source_policy = SITE_ROOT / "security" / "dependency-source-policy.mjs"
    if node and source_policy.exists():
        source_check = capture_command([node, str(source_policy)], cwd=SITE_ROOT)
        source_detail = (source_check.stdout or source_check.stderr).strip()
        checks.append(
            Check(
                "site dependency sources",
                "ok" if source_check.returncode == 0 else "error",
                source_detail or "dependency source policy returned no output",
                required=False,
            )
        )
    return checks


def command_doctor(args: argparse.Namespace) -> int:
    checks = doctor_checks()
    if args.json:
        print(json.dumps({"checks": [asdict(item) for item in checks]}, ensure_ascii=False, indent=2))
    else:
        labels = {"ok": "[OK]", "warn": "[WARN]", "error": "[ERROR]"}
        print("ATLAS workspace doctor")
        for item in checks:
            print(f"{labels[item.status]:7} {item.name}: {item.detail}")
    return 1 if any(item.required and item.status == "error" for item in checks) else 0


def command_sync(args: argparse.Namespace) -> int:
    try:
        date = args.date or latest_report()[0]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    quality_args = argparse.Namespace(date=date, dry_run=args.dry_run, strict=False)
    if command_quality(quality_args) != 0:
        return 1

    exporter = BRIEFING_ROOT / "scripts" / "export_trading_core_signals.py"
    # A valid daily briefing may contain no prediction mapped to trading-core's
    # deliberately narrow China ETF universe.  The standalone exporter remains
    # fail-closed by default; the audited daily orchestration explicitly accepts
    # that transparent zero-signal state and still materializes the dated bridge.
    export_command = [sys.executable, str(exporter), "--date", date, "--allow-empty"]
    if args.dry_run:
        export_command.append("--dry-run")
    if run_command(export_command) != 0:
        return 1

    validation = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "evolution.py"),
        "validate-records",
        "--period",
        "day",
        "--date",
        date,
    ]
    if run_command(validation) != 0:
        return 1

    evolution_policy = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "evolution.py"),
        "update-policy",
        "--period",
        "month",
        "--date",
        date,
    ]
    if args.dry_run:
        evolution_policy.append("--dry-run")
    if run_command(evolution_policy) != 0:
        return 1

    load_macro = [
        sys.executable,
        "-m",
        "trading_core.entrypoint",
        "load-macro",
        "--date",
        date,
        "--allow-empty",
    ]
    if args.dry_run:
        load_macro.append("--dry-run")
    if run_command(load_macro, cwd=TRADING_ROOT, trading_core=True) != 0:
        return 1

    site_sync = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "sync_briefing_site.py"),
        "--date",
        date,
    ]
    if args.dry_run:
        site_sync.append("--dry-run")
    if args.force_site:
        site_sync.append("--force")
    if run_command(site_sync) != 0:
        return 1

    action = "validation" if args.dry_run else "sync"
    print(f"\nATLAS {action} complete for {date}.")
    return 0


def command_quality(args: argparse.Namespace) -> int:
    try:
        date = args.date or latest_report()[0]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    command = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "research_quality.py"),
        "--date",
        date,
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.strict:
        command.append("--strict")
    return run_command(command)


def command_heal(args: argparse.Namespace) -> int:
    """Run the isolated self-healing control plane."""
    command = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "self_healing.py"),
    ]
    if args.status:
        command.append("--status")
    else:
        try:
            date = args.date or latest_report()[0]
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        command.extend(["--date", date])
        if args.apply_safe:
            command.append("--apply-safe")
        if args.deep:
            command.append("--deep")
        if args.strict:
            command.append("--strict")
    if args.json:
        command.append("--json")
    return run_command(command)


def command_improvements(args: argparse.Namespace) -> int:
    """Track retrospective recommendations through cross-run verification."""
    command = [
        sys.executable,
        str(BRIEFING_ROOT / "scripts" / "improvement_tracker.py"),
    ]
    if args.status:
        command.append("--status")
    else:
        try:
            date = args.date or latest_report()[0]
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        command.extend(["--date", date])
        if args.apply_safe:
            command.append("--apply-safe")
        if args.strict:
            command.append("--strict")
    if args.json:
        command.append("--json")
    return run_command(command)


def command_backup(args: argparse.Namespace) -> int:
    command = [sys.executable, str(BRIEFING_ROOT / "scripts" / "disaster_recovery.py"), "--date", args.date]
    full = bool(getattr(args, "full", False))
    if full:
        command.append("--full")
    if args.json:
        command.append("--json")
    return run_command(command, timeout=2 * 60 * 60 if full else COMMAND_TIMEOUT_SECONDS)


def verify_existing_backup(
    *,
    timeout_seconds: float = 300.0,
    quick: bool = True,
) -> dict[str, Any]:
    """Ask the DR implementation to authenticate the exact latest archive.

    The daily bootstrap uses the authenticated quick path because the snapshot's
    full restore drill already ran at creation.  Operators can still request the
    heavyweight path explicitly for periodic recovery exercises.
    """

    try:
        command = [
            sys.executable,
            str(BRIEFING_ROOT / "scripts" / "disaster_recovery.py"),
            "--verify-existing",
            "--json",
        ]
        if quick:
            command.append("--quick")
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"verified": False, "error_type": type(exc).__name__}
    if completed.returncode != 0:
        return {"verified": False, "returncode": completed.returncode}
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"verified": False, "returncode": completed.returncode, "error_type": "InvalidJSON"}
    if not isinstance(payload, dict):
        return {"verified": False, "returncode": completed.returncode, "error_type": "InvalidPayload"}
    return payload


def command_alerts(args: argparse.Namespace) -> int:
    process_due = bool(getattr(args, "process_due", False))
    date = getattr(args, "date", None)
    alert_id = getattr(args, "alert_id", None)
    if not process_due and not date:
        print("atlas alerts requires --date unless --process-due is used", file=sys.stderr)
        return 2

    command = [sys.executable, str(BRIEFING_ROOT / "scripts" / "alert_dispatch.py")]
    if process_due:
        command.append("--process-due")
    else:
        command.extend(["--date", date])
    if alert_id:
        command.extend(["--alert-id", alert_id])
    if args.json:
        command.append("--json")
    if args.ack_by:
        command.extend(["--ack-by", args.ack_by])
    if args.retry:
        command.append("--retry")
    if args.receipt_destination:
        command.extend(["--receipt-destination", args.receipt_destination])
    if args.receipt_id:
        command.extend(["--receipt-id", args.receipt_id])
    return run_command(command)


def publication_orchestration_path(date: str) -> Path:
    """Return the independent, mutable Phase-B audit for one dated candidate.

    Cycle history is deliberately immutable once it has been anchored.  Publication
    happens only after that core audit exists, so its outcome must be recorded in a
    separate artifact instead of rewriting a previously verified cycle record.
    """
    return (
        ATLAS_RUNTIME_ROOT
        / "publication_orchestration"
        / f"atlas-publication-orchestration-{date}.json"
    )


def staged_publication_candidate_path(date: str) -> Path:
    return (
        ATLAS_RUNTIME_ROOT
        / "publication_candidates"
        / f"atlas-publication-candidate-{date}.json"
    )


def publication_gate_artifact_paths() -> dict[str, Path]:
    """Keep Phase-B gate locations derived from the current runtime root.

    Derivation rather than module-level paths keeps the control plane testable and
    avoids accidentally reading a prior workspace when a caller redirects runtime
    paths for recovery or verification.
    """
    return {
        "improvements": ATLAS_RUNTIME_ROOT / "improvements" / "latest.json",
        "self_healing": ATLAS_RUNTIME_ROOT / "self_healing" / "latest.json",
        "alerts": ATLAS_RUNTIME_ROOT / "alerts" / "latest.json",
        "backup": ATLAS_RUNTIME_ROOT / "backups" / "latest.json",
    }


def _publication_artifact_metadata(path: Path, payload: dict[str, Any] | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "path": relative_path(path),
        "present": path.is_file(),
        "sha256": None,
        "date": payload.get("date") if isinstance(payload, dict) else None,
    }
    if path.is_file():
        try:
            metadata["sha256"] = file_sha256(path)
        except OSError:
            metadata["sha256"] = None
    return metadata


def _load_publication_artifact(path: Path, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"{label} artifact is missing: {relative_path(path)}"
    try:
        payload = read_json_file(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"{label} artifact is unreadable: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, f"{label} artifact must be a JSON object"
    return payload, None


def _gate_count(
    payload: dict[str, Any],
    *,
    field: str,
    label: str,
    errors: list[str],
) -> int | None:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{label}.{field} must be a non-negative integer")
        return None
    return value


def _daily_backup_baseline_reasons(
    payload: dict[str, Any],
    *,
    maximum_age_hours: float | None = None,
    now: datetime | None = None,
) -> list[str]:
    if payload.get("snapshot_profile", "full") != "daily":
        return []
    baseline = payload.get("full_snapshot")
    if not isinstance(baseline, dict):
        return ["daily backup is not bound to a full recovery baseline"]
    reasons: list[str] = []
    for field in ("archive_sha256", "manifest_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(baseline.get(field) or "")):
            reasons.append(f"daily backup full baseline {field} is invalid")
    if baseline.get("restore_verified") is not True:
        reasons.append("daily backup full baseline was not restore-verified")
    try:
        datetime.strptime(str(baseline.get("date")), "%Y-%m-%d")
        created = datetime.fromisoformat(str(baseline.get("created_at")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        reasons.append("daily backup full baseline timestamp is invalid")
    else:
        if maximum_age_hours is not None:
            age_hours = (
                ((now or datetime.now(UTC)).astimezone(UTC) - created.astimezone(UTC)).total_seconds()
                / 3600
            )
            if age_hours < 0 or age_hours > maximum_age_hours:
                reasons.append("daily backup full baseline is outside the configured freshness window")
    return reasons


def publication_backup_bootstrap_readiness(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check whether improvement verification can trust the existing backup.

    Improvement verification intentionally runs before the *final* publication
    backup.  When the previous backup is older than policy (a common weekend
    case), that creates a dependency cycle: improvements block because no fresh
    backup exists, while the final backup cannot be bound to passing improvement
    evidence.  A provisional, restore-verified backup breaks that cycle.  It is
    never publication evidence itself; the normal final backup still runs after
    improvements, healing, and alerts and binds their exact bytes.
    """
    reasons: list[str] = []
    evidence: dict[str, Any] = {
        "config_path": relative_path(IMPROVEMENT_TRACKING_CONFIG_PATH),
        "backup_path": relative_path(publication_gate_artifact_paths()["backup"]),
    }
    try:
        config_payload = read_json_file(IMPROVEMENT_TRACKING_CONFIG_PATH, default={})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ready": False,
            "reasons": [f"backup bootstrap configuration is unreadable: {type(exc).__name__}"],
            "evidence": evidence,
        }
    recovery = (
        config_payload.get("disaster_recovery", {})
        if isinstance(config_payload, dict)
        else {}
    )
    if not isinstance(recovery, dict) or recovery.get("enabled") is not True:
        reasons.append("disaster recovery is not enabled")
        recovery = {}
    try:
        maximum_age_hours = float(
            recovery.get("bootstrap_maximum_backup_age_hours")
            or recovery.get("maximum_backup_age_hours")
            or 24
        )
    except (TypeError, ValueError):
        maximum_age_hours = 24.0
        reasons.append("maximum backup age is invalid")
    if not math.isfinite(maximum_age_hours) or maximum_age_hours <= 0:
        maximum_age_hours = 24.0
        reasons.append("maximum backup age is invalid")
    evidence["maximum_age_hours"] = maximum_age_hours

    backup_path = publication_gate_artifact_paths()["backup"]
    try:
        backup = read_json_file(backup_path, default={})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ready": False,
            "reasons": [f"existing backup metadata is unreadable: {type(exc).__name__}"],
            "evidence": evidence,
        }
    if not isinstance(backup, dict):
        backup = {}
        reasons.append("existing backup metadata is not a JSON object")
    requirements = {
        "schema_version": 4,
        "verified": True,
        "encrypted": True,
        "encryption_algorithm": "AES-256-GCM",
        "encrypted_container_authenticated": True,
        "archive_integrity_verified": True,
        "restore_verified": True,
        "restore_scope": "configured_workspace_files_and_git_bundles",
        "target_outside_workspace": True,
    }
    failed_fields = [
        name for name, expected in requirements.items() if backup.get(name) != expected
    ]
    if failed_fields:
        reasons.append("existing backup lacks required controls: " + ", ".join(failed_fields))
    try:
        full_snapshot_maximum_age_hours = float(
            recovery.get("full_snapshot_maximum_age_hours") or maximum_age_hours
        )
    except (TypeError, ValueError):
        full_snapshot_maximum_age_hours = maximum_age_hours
        reasons.append("full snapshot maximum age is invalid")
    reasons.extend(
        _daily_backup_baseline_reasons(
            backup,
            maximum_age_hours=full_snapshot_maximum_age_hours,
            now=now,
        )
    )

    authentication = backup.get("metadata_authentication")
    if (
        not isinstance(authentication, dict)
        or authentication.get("algorithm") != "HMAC-SHA256"
        or not re.fullmatch(r"[0-9a-f]{16}", str(authentication.get("key_id") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(authentication.get("value") or ""))
    ):
        reasons.append("existing backup metadata authentication is missing or malformed")

    created_at = backup.get("created_at")
    age_hours: float | None = None
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("backup timestamp lacks timezone")
        age_hours = (
            ((now or datetime.now(UTC)).astimezone(UTC) - created.astimezone(UTC)).total_seconds()
            / 3600
        )
    except (TypeError, ValueError):
        reasons.append("existing backup timestamp is invalid")
    else:
        if age_hours < 0 or age_hours > maximum_age_hours:
            reasons.append("existing backup is outside the configured freshness window")
    evidence["age_hours"] = age_hours

    archive_value = backup.get("archive")
    archive_path = Path(str(archive_value)) if isinstance(archive_value, str) else Path()
    archive_hash = str(backup.get("archive_sha256") or "")
    archive_ok = False
    if not archive_path.is_absolute() or not archive_path.is_file():
        reasons.append("existing external backup archive is missing")
    else:
        try:
            archive_path.resolve().relative_to(ROOT.resolve())
        except ValueError:
            try:
                archive_ok = (
                    bool(re.fullmatch(r"[0-9a-f]{64}", archive_hash))
                    and file_sha256(archive_path) == archive_hash
                )
            except OSError:
                archive_ok = False
        if not archive_ok:
            reasons.append("existing external backup archive hash is invalid")
    evidence["archive_present"] = archive_path.is_file() if archive_path.is_absolute() else False
    evidence["archive_hash_verified"] = archive_ok
    evidence["date"] = backup.get("date")
    independent_verification = verify_existing_backup(quick=True)
    evidence["independent_restore_verification"] = {
        "verified": independent_verification.get("verified") is True,
        "archive_sha256": independent_verification.get("archive_sha256"),
        "manifest_sha256": independent_verification.get("manifest_sha256"),
        "restore_verified": independent_verification.get("restore_verified") is True,
    }
    if independent_verification.get("verified") is not True:
        reasons.append("existing backup failed independent authentication and restore verification")
    elif independent_verification.get("archive_sha256") != archive_hash:
        reasons.append("independent backup verification selected a different archive")
    return {
        "ready": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "evidence": evidence,
    }


def configured_blocking_alert_severities() -> set[str]:
    config = read_json_file(IMPROVEMENT_TRACKING_CONFIG_PATH, default={})
    configured = config.get("blocking_severities") if isinstance(config, dict) else None
    if not isinstance(configured, list) or not configured:
        return {"critical", "high"}
    severities = {
        str(value).strip().lower()
        for value in configured
        if isinstance(value, str) and value.strip()
    }
    return severities or {"critical", "high"}


def alert_artifact_is_nonblocking(alerts: Any) -> bool:
    """Accept only well-formed alert artifacts with no configured blocking finding."""

    if not isinstance(alerts, dict):
        return False
    status = str(alerts.get("status") or "")
    if status == "healthy":
        return True
    if status != "attention_required":
        return False
    findings = alerts.get("findings")
    finding_count = alerts.get("finding_count")
    if (
        not isinstance(findings, list)
        or not findings
        or isinstance(finding_count, bool)
        or not isinstance(finding_count, int)
        or finding_count != len(findings)
    ):
        return False
    blocking = configured_blocking_alert_severities()
    allowed_severities = {"critical", "high", "medium", "low"}
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        severity = str(finding.get("severity") or "").strip().lower()
        if (
            not str(finding.get("id") or "").strip()
            or not str(finding.get("status") or "").strip()
            or not str(finding.get("summary") or "").strip()
            or severity not in allowed_severities
            or severity in blocking
        ):
            return False
    return True


def publication_gate_artifact_readiness(date: str) -> dict[str, Any]:
    """Perform a conservative local preflight before the authoritative site retry.

    The site synchronizer remains the sole authority for candidate fingerprint and
    backup-content binding.  This preflight exists to avoid asking it to freeze when
    an obvious dated gate is absent, malformed, or still blocking, while retaining
    clear operational diagnostics for each independent control plane.
    """
    paths = publication_gate_artifact_paths()
    blockers: list[str] = []
    errors: list[str] = []
    checks: dict[str, dict[str, Any]] = {}

    def load(name: str) -> dict[str, Any] | None:
        payload, error = _load_publication_artifact(paths[name], name)
        check = {
            "artifact": _publication_artifact_metadata(paths[name], payload),
            "ready": False,
            "blockers": [],
            "errors": [],
        }
        checks[name] = check
        if error:
            check["errors"].append(error)
            errors.append(error)
            return None
        assert payload is not None
        if payload.get("date") != date:
            reason = f"{name} artifact is not date-aligned"
            check["blockers"].append(reason)
            blockers.append(reason)
        return payload

    improvements = load("improvements")
    if improvements is not None:
        check = checks["improvements"]
        counts = improvements.get("counts")
        if not isinstance(counts, dict):
            reason = "improvements.counts must be a JSON object"
            check["errors"].append(reason)
            errors.append(reason)
        else:
            count_errors: list[str] = []
            blocking_count = _gate_count(
                counts,
                field="blocking",
                label="improvements.counts",
                errors=count_errors,
            )
            for reason in count_errors:
                check["errors"].append(reason)
                errors.append(reason)
            if blocking_count is not None and blocking_count != 0:
                reason = "date-aligned improvement verification is blocking"
                check["blockers"].append(reason)
                blockers.append(reason)

    healing = load("self_healing")
    if healing is not None:
        check = checks["self_healing"]
        counts = healing.get("counts")
        if not isinstance(counts, dict):
            reason = "self_healing.counts must be a JSON object"
            check["errors"].append(reason)
            errors.append(reason)
        else:
            count_errors: list[str] = []
            observed_counts = {
                field: _gate_count(
                    counts,
                    field=field,
                    label="self_healing.counts",
                    errors=count_errors,
                )
                for field in ("blocking", "failed", "unresolved")
            }
            for reason in count_errors:
                check["errors"].append(reason)
                errors.append(reason)
            if any(value not in {None, 0} for value in observed_counts.values()):
                reason = "date-aligned deep self-healing is blocking"
                check["blockers"].append(reason)
                blockers.append(reason)
        if healing.get("deep") is not True or healing.get("strict") is not True:
            reason = "date-aligned self-healing was not run with deep and strict gates"
            check["blockers"].append(reason)
            blockers.append(reason)
        if healing.get("overall_status") != "healthy":
            reason = "date-aligned deep self-healing is not healthy"
            check["blockers"].append(reason)
            blockers.append(reason)
        healing_checks = healing.get("checks")
        if not isinstance(healing_checks, list):
            reason = "self_healing.checks must be a JSON array"
            check["errors"].append(reason)
            errors.append(reason)
        else:
            check_index: dict[str, dict[str, Any]] = {}
            malformed = False
            for item in healing_checks:
                if not isinstance(item, dict):
                    malformed = True
                    continue
                check_id = str(item.get("check_id") or "")
                if check_id:
                    check_index[check_id] = item
            if malformed:
                reason = "self_healing.checks contains a non-object entry"
                check["errors"].append(reason)
                errors.append(reason)
            for check_id in ("briefing_tests", "site_quality"):
                item = check_index.get(check_id)
                if item is None or item.get("executed") is not True or item.get("passed") is not True:
                    reason = f"date-aligned self-healing required check did not pass: {check_id}"
                    check["blockers"].append(reason)
                    blockers.append(reason)
            failed_checks = sorted(
                check_id
                for check_id, item in check_index.items()
                if item.get("executed") is not True or item.get("passed") is not True
            )
            if failed_checks:
                reason = "date-aligned self-healing checks did not pass: " + ", ".join(failed_checks)
                check["blockers"].append(reason)
                blockers.append(reason)

    alerts = load("alerts")
    if alerts is not None:
        check = checks["alerts"]
        if not alert_artifact_is_nonblocking(alerts):
            reason = "date-aligned alerts are missing or still require attention"
            check["blockers"].append(reason)
            blockers.append(reason)

    backup = load("backup")
    if backup is not None:
        check = checks["backup"]
        backup_requirements = {
            "schema_version": 4,
            "verified": True,
            "encrypted": True,
            "encryption_algorithm": "AES-256-GCM",
            "encrypted_container_authenticated": True,
            "archive_integrity_verified": True,
            "restore_verified": True,
            "restore_scope": "configured_workspace_files_and_git_bundles",
            "target_outside_workspace": True,
        }
        failed_requirements = [
            name for name, expected in backup_requirements.items() if backup.get(name) != expected
        ]
        if failed_requirements:
            reason = (
                "date-aligned encrypted external backup and restore verification did not pass: "
                + ", ".join(failed_requirements)
            )
            check["blockers"].append(reason)
            blockers.append(reason)
        for reason in _daily_backup_baseline_reasons(backup):
            check["blockers"].append(reason)
            blockers.append(reason)

    for check in checks.values():
        check["ready"] = not check["blockers"] and not check["errors"]
    return {
        "ready": not blockers and not errors,
        "blockers": list(dict.fromkeys(blockers)),
        "errors": list(dict.fromkeys(errors)),
        "checks": checks,
    }


def publication_core_cycle_readiness(date: str) -> dict[str, Any]:
    """Require an anchored, passed Phase-A audit before Phase-B can mutate site data."""
    audit_path = RUN_AUDIT_ROOT / f"atlas-cycle-{date}.json"
    blockers: list[str] = []
    errors: list[str] = []
    audit, load_error = _load_publication_artifact(audit_path, "cycle audit")
    evidence: dict[str, Any] = {
        "audit": _publication_artifact_metadata(audit_path, audit),
        "history": None,
    }
    if load_error:
        # Absence means Phase A has not completed, whereas unreadable data is an
        # integrity failure and must never be treated as an ordinary retry state.
        if not audit_path.exists():
            blockers.append("date-aligned core cycle audit has not been written")
        else:
            errors.append(load_error)
        return {"ready": False, "blockers": blockers, "errors": errors, "evidence": evidence}
    assert audit is not None
    if audit.get("date") != date:
        errors.append("cycle audit date does not match requested publication date")
    stages = audit.get("stages")
    if not isinstance(stages, list):
        errors.append("cycle audit stages must be a JSON array")
    elif not required_cycle_stages_passed(
        [item for item in stages if isinstance(item, dict)]
    ):
        blockers.append("date-aligned core cycle required stages did not pass")
    if audit.get("operational_gate_passed") is not True:
        blockers.append("date-aligned core cycle operational gate did not pass")
    ledger = audit.get("ledger")
    if not isinstance(ledger, dict):
        errors.append("cycle audit ledger evidence must be a JSON object")
    elif ledger.get("write_performed") is not True:
        blockers.append("date-aligned core cycle canonical ledger commit was not performed")

    run_id = audit.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        errors.append("cycle audit run_id is missing")
    else:
        history_root = RUN_AUDIT_ROOT / "history" / date
        history_path = history_root / f"{run_id}.json"
        evidence["history"] = {"path": relative_path(history_path), "present": history_path.is_file()}
        if not history_path.is_file():
            errors.append("anchored cycle history record is missing")
        else:
            try:
                history_integrity = audit_cycle_history(history_root)
            except Exception as exc:
                errors.append(f"cycle history verification raised {type(exc).__name__}")
            else:
                evidence["history"]["integrity_passed"] = history_integrity.get("passed") is True
                evidence["history"]["anchor_path"] = history_integrity.get("anchor_path")
                if history_integrity.get("passed") is not True:
                    errors.append("cycle history integrity verification failed")
            try:
                audit_hash = file_sha256(audit_path)
                history_hash = file_sha256(history_path)
            except OSError as exc:
                errors.append(f"cannot hash core cycle audit history: {type(exc).__name__}")
            else:
                evidence["history"]["audit_sha256"] = audit_hash
                evidence["history"]["record_sha256"] = history_hash
                if audit_hash != history_hash:
                    errors.append("cycle audit differs from its anchored history record")
    return {
        "ready": not blockers and not errors,
        "blockers": list(dict.fromkeys(blockers)),
        "errors": list(dict.fromkeys(errors)),
        "evidence": evidence,
    }


def publication_staged_candidate_readiness(date: str) -> dict[str, Any]:
    """Require an immutable staged candidate; Phase B must never rebuild Phase A."""
    path = staged_publication_candidate_path(date)
    payload, load_error = _load_publication_artifact(path, "staged publication candidate")
    state_path = BRIEFING_ROOT / "data" / "site-sync-state.json"
    state, state_error = _load_publication_artifact(state_path, "site sync state")
    evidence = {
        "candidate": _publication_artifact_metadata(path, payload),
        "site_sync_state": _publication_artifact_metadata(state_path, state),
    }
    if load_error:
        if not path.exists():
            return {
                "ready": False,
                "returncode": PUBLICATION_NO_CANDIDATE_RETURN_CODE,
                "reason": "no staged publication candidate exists for the requested date",
                "evidence": evidence,
            }
        return {
            "ready": False,
            "returncode": PUBLICATION_ERROR_RETURN_CODE,
            "reason": load_error,
            "evidence": evidence,
        }
    assert payload is not None
    if state_error:
        if not state_path.exists():
            return {
                "ready": False,
                "returncode": PUBLICATION_NO_CANDIDATE_RETURN_CODE,
                "reason": "no active staged publication candidate is recorded in site sync state",
                "evidence": evidence,
            }
        return {
            "ready": False,
            "returncode": PUBLICATION_ERROR_RETURN_CODE,
            "reason": state_error,
            "evidence": evidence,
        }
    assert state is not None
    if payload.get("date") != date:
        return {
            "ready": False,
            "returncode": PUBLICATION_NO_CANDIDATE_RETURN_CODE,
            "reason": "staged publication candidate is not date-aligned",
            "evidence": evidence,
        }
    if state.get("staged_date") != date:
        return {
            "ready": False,
            "returncode": PUBLICATION_NO_CANDIDATE_RETURN_CODE,
            "reason": "site sync state does not select the requested staged candidate",
            "evidence": evidence,
        }
    try:
        selected_path = Path(str(state.get("staged_path") or "")).resolve()
    except OSError:
        selected_path = None
    if selected_path != path.resolve():
        return {
            "ready": False,
            "returncode": PUBLICATION_BLOCKED_RETURN_CODE,
            "reason": "site sync state staged candidate path is not canonical",
            "evidence": evidence,
        }
    # Older candidates predate the explicit status field. They remain eligible for
    # the site authority's full hash/fingerprint re-attestation, while any explicit
    # non-retryable status stays fail-closed here.
    if state.get("staged_status") not in {None, "staged", "blocked"}:
        return {
            "ready": False,
            "returncode": PUBLICATION_BLOCKED_RETURN_CODE,
            "reason": "site sync state staged candidate is not retryable",
            "evidence": evidence,
        }
    return {"ready": True, "returncode": 0, "reason": None, "evidence": evidence}


def command_retry_staged_publication(date: str) -> int:
    """Ask the site control plane to re-attest one already-staged candidate only."""
    return run_command(
        [
            sys.executable,
            str(BRIEFING_ROOT / "scripts" / "sync_briefing_site.py"),
            "--retry-staged-candidate",
            "--date",
            date,
        ]
    )


def _publication_stage_status(returncode: int | None, check: dict[str, Any]) -> str:
    if check.get("errors"):
        return "error"
    if check.get("blockers"):
        return "blocked"
    return "passed" if returncode == 0 else "error"


def _write_publication_orchestration_checkpoint(result: dict[str, Any]) -> None:
    result["updated_at"] = utc_now()
    atomic_write_json(publication_orchestration_path(str(result["date"])), result)


def run_post_gate_publication(
    date: str,
    *,
    initiated_by: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the closed-loop Phase-B publisher without rerunning research or trading.

    This intentionally invokes only the independent evidence controls and the site
    retry for an existing candidate.  It does not invoke ``sync``, report creation,
    forecasting, paper-order generation, pricing, or portfolio valuation.
    """
    result: dict[str, Any] = {
        "schema_version": PUBLICATION_ORCHESTRATION_SCHEMA_VERSION,
        "date": date,
        "started_at": utc_now(),
        "finished_at": None,
        "initiated_by": initiated_by,
        "dry_run": dry_run,
        "phase_a_reexecuted": False,
        "forbidden_phase_a_operations": [
            "report generation",
            "forecast generation",
            "paper order generation",
            "market valuation",
        ],
        "status": "running",
        "returncode": None,
        "core_cycle": {},
        "candidate": {},
        "backup_bootstrap": {
            "required": False,
            "attempted": False,
            "returncode": None,
            "status": "not_evaluated",
        },
        "stages": [],
        "publication_retry": {"attempted": False, "returncode": None},
        "blocking_reasons": [],
        "errors": [],
    }
    _write_publication_orchestration_checkpoint(result)

    core = publication_core_cycle_readiness(date)
    result["core_cycle"] = core
    if core["errors"]:
        result["status"] = "error"
        result["returncode"] = PUBLICATION_ERROR_RETURN_CODE
        result["errors"].extend(core["errors"])
    elif core["blockers"]:
        result["status"] = "blocked"
        result["returncode"] = PUBLICATION_BLOCKED_RETURN_CODE
        result["blocking_reasons"].extend(core["blockers"])
    else:
        candidate = publication_staged_candidate_readiness(date)
        result["candidate"] = candidate
        if candidate["ready"] is not True:
            result["status"] = "no_candidate" if candidate["returncode"] == PUBLICATION_NO_CANDIDATE_RETURN_CODE else (
                "blocked" if candidate["returncode"] == PUBLICATION_BLOCKED_RETURN_CODE else "error"
            )
            result["returncode"] = candidate["returncode"]
            target = "blocking_reasons" if result["status"] in {"no_candidate", "blocked"} else "errors"
            result[target].append(str(candidate["reason"]))
        elif dry_run:
            # A dry run must not synthesize alert, backup, or recovery evidence.
            result["status"] = "dry_run"
            result["returncode"] = 0
        else:
            bootstrap_preflight = publication_backup_bootstrap_readiness()
            bootstrap = {
                "required": bootstrap_preflight["ready"] is not True,
                "attempted": False,
                "returncode": None,
                "status": "not_required" if bootstrap_preflight["ready"] is True else "pending",
                "preflight": bootstrap_preflight,
                "final_publication_evidence": False,
            }
            result["backup_bootstrap"] = bootstrap
            if bootstrap["required"]:
                bootstrap["attempted"] = True
                try:
                    bootstrap_returncode = int(
                        command_backup(argparse.Namespace(date=date, json=False))
                    )
                    bootstrap_exception = None
                except Exception as exc:
                    bootstrap_returncode = None
                    bootstrap_exception = type(exc).__name__
                bootstrap["returncode"] = bootstrap_returncode
                bootstrap["exception"] = bootstrap_exception
                bootstrap["status"] = (
                    "passed"
                    if bootstrap_returncode == 0 and bootstrap_exception is None
                    else "failed"
                )
                result["stages"].append(
                    {
                        "name": "backup_bootstrap",
                        "returncode": bootstrap_returncode,
                        "exception": bootstrap_exception,
                        "status": bootstrap["status"],
                        "final_publication_evidence": False,
                    }
                )
                _write_publication_orchestration_checkpoint(result)

            commands: list[tuple[str, Any, argparse.Namespace]] = [
                (
                    "improvements",
                    command_improvements,
                    argparse.Namespace(
                        date=date,
                        apply_safe=True,
                        strict=True,
                        status=False,
                        json=False,
                    ),
                ),
                (
                    "self_healing",
                    command_heal,
                    argparse.Namespace(
                        date=date,
                        apply_safe=True,
                        deep=True,
                        strict=True,
                        status=False,
                        json=False,
                    ),
                ),
                (
                    "alerts",
                    command_alerts,
                    argparse.Namespace(
                        date=date,
                        process_due=False,
                        alert_id=None,
                        json=False,
                        ack_by=None,
                        retry=False,
                        receipt_destination=None,
                        receipt_id=None,
                    ),
                ),
                (
                    "improvements",
                    command_improvements,
                    argparse.Namespace(
                        date=date,
                        apply_safe=True,
                        strict=True,
                        status=False,
                        json=False,
                    ),
                ),
                (
                    "alerts",
                    command_alerts,
                    argparse.Namespace(
                        date=date,
                        process_due=False,
                        alert_id=None,
                        json=False,
                        ack_by=None,
                        retry=False,
                        receipt_destination=None,
                        receipt_id=None,
                    ),
                ),
                (
                    "backup",
                    command_backup,
                    argparse.Namespace(date=date, json=False),
                ),
            ]
            for name, command, command_args in commands:
                try:
                    returncode = int(command(command_args))
                    exception = None
                except Exception as exc:
                    returncode = None
                    # Phase-B audits are retained with backups. Persist only the
                    # exception class so an adapter cannot leak endpoint tokens or
                    # other command text through a durable control artifact.
                    exception = type(exc).__name__
                stage = {
                    "name": name,
                    "returncode": returncode,
                    "exception": exception,
                    "status": "completed" if returncode == 0 else "failed",
                }
                result["stages"].append(stage)
                _write_publication_orchestration_checkpoint(result)

            artifact_gates = publication_gate_artifact_readiness(date)
            result["artifact_gates"] = artifact_gates
            stage_checks = artifact_gates["checks"]
            command_error = False
            for stage in result["stages"]:
                if stage["name"] == "backup_bootstrap":
                    # This archive only makes the disaster-recovery capability
                    # observable to improvement verification.  It is never used
                    # as the final publication gate; the later backup stage must
                    # contain the exact final improvement/healing/alert bytes.
                    continue
                check = stage_checks[stage["name"]]
                stage["gate"] = {
                    "ready": check["ready"],
                    "blockers": check["blockers"],
                    "errors": check["errors"],
                    "artifact": check["artifact"],
                }
                stage["status"] = _publication_stage_status(stage["returncode"], check)
                if stage["exception"] is not None:
                    stage["status"] = "error"
                    command_error = True
                    result["errors"].append(
                        f"{stage['name']} control command raised {stage['exception'].split(':', 1)[0]}"
                    )
                elif stage["returncode"] == COMMAND_TIMEOUT_RETURN_CODE:
                    stage["status"] = "error"
                    command_error = True
                    result["errors"].append(f"{stage['name']} control command timed out")
                elif (
                    stage["returncode"] != 0
                    and stage["status"] == "blocked"
                    and check["artifact"].get("date") != date
                ):
                    # A strict control may return 1 for a current, evidenced gate
                    # block. It must not turn an unavailable/old artifact into a
                    # misleading ordinary publication block.
                    stage["status"] = "error"
                    command_error = True
                    result["errors"].append(
                        f"{stage['name']} control command failed without a date-aligned artifact"
                    )
                # A command that reports failure despite producing a seemingly good
                # artifact is never promoted. Its unknown failure is infrastructure,
                # not a pass inferred from stale bytes.
                if stage["returncode"] != 0 and stage["status"] != "blocked":
                    command_error = True
                    result["errors"].append(
                        f"{stage['name']} control command failed with returncode {stage['returncode']}"
                    )
            result["blocking_reasons"].extend(artifact_gates["blockers"])
            result["errors"].extend(artifact_gates["errors"])
            if result["errors"] or command_error:
                result["status"] = "error"
                result["returncode"] = PUBLICATION_ERROR_RETURN_CODE
            elif result["blocking_reasons"]:
                result["status"] = "blocked"
                result["returncode"] = PUBLICATION_BLOCKED_RETURN_CODE
            else:
                retry_returncode = command_retry_staged_publication(date)
                result["publication_retry"] = {
                    "attempted": True,
                    "returncode": retry_returncode,
                    "command": "retry_staged_candidate",
                }
                result["stages"].append(
                    {
                        "name": "retry_staged_candidate",
                        "returncode": retry_returncode,
                        "status": "passed" if retry_returncode == 0 else "blocked"
                        if retry_returncode in {
                            PUBLICATION_BLOCKED_RETURN_CODE,
                            PUBLICATION_NO_CANDIDATE_RETURN_CODE,
                        }
                        else "error",
                    }
                )
                if retry_returncode == 0:
                    # The retry freezes or reuses a frozen payload. It never claims
                    # that an external deployment has already gone live.
                    result["status"] = "frozen_or_pending_deployment"
                    result["returncode"] = 0
                elif retry_returncode == PUBLICATION_BLOCKED_RETURN_CODE:
                    result["status"] = "blocked"
                    result["returncode"] = retry_returncode
                    result["blocking_reasons"].append(
                        "authoritative staged-candidate re-attestation is still blocking"
                    )
                elif retry_returncode == PUBLICATION_NO_CANDIDATE_RETURN_CODE:
                    result["status"] = "no_candidate"
                    result["returncode"] = retry_returncode
                    result["blocking_reasons"].append(
                        "authoritative staged-candidate retry found no valid candidate"
                    )
                else:
                    result["status"] = "error"
                    result["returncode"] = PUBLICATION_ERROR_RETURN_CODE
                    result["errors"].append(
                        f"authoritative staged-candidate retry failed with returncode {retry_returncode}"
                    )

    result["blocking_reasons"] = list(dict.fromkeys(result["blocking_reasons"]))
    result["errors"] = list(dict.fromkeys(result["errors"]))
    result["finished_at"] = utc_now()
    result["audit_path"] = str(publication_orchestration_path(date))
    _write_publication_orchestration_checkpoint(result)
    return result


def command_publish(args: argparse.Namespace) -> int:
    """Run only Phase B against immutable Phase-A outputs and a staged candidate."""
    try:
        date = args.date or latest_report()[0]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return PUBLICATION_ERROR_RETURN_CODE
    try:
        with cycle_lock(f"publish-{date}"):
            result = run_post_gate_publication(
                date,
                initiated_by="atlas publish",
                dry_run=bool(getattr(args, "dry_run", False)),
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return PUBLICATION_ERROR_RETURN_CODE
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "date": date,
                    "error": f"publication orchestration raised {type(exc).__name__}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return PUBLICATION_ERROR_RETURN_CODE
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["returncode"])


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def parse_cycle_lock_payload(lock_path: Path) -> dict[str, Any]:
    """Read a lock owner record without treating incomplete data as stale."""
    try:
        payload = read_json_file(lock_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"ATLAS cycle lock is unreadable; refusing unsafe takeover: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ATLAS cycle lock is invalid; refusing unsafe takeover")
    raw_pid = payload.get("pid")
    token = payload.get("token")
    started_at = payload.get("started_at")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
        raise RuntimeError("ATLAS cycle lock has an invalid owner pid; refusing unsafe takeover")
    if not isinstance(token, str) or not token:
        raise RuntimeError("ATLAS cycle lock has no owner token; refusing unsafe takeover")
    if not isinstance(started_at, str):
        raise RuntimeError("ATLAS cycle lock has no start time; refusing unsafe takeover")
    try:
        parsed_started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("ATLAS cycle lock has an invalid start time; refusing unsafe takeover") from exc
    if parsed_started_at.tzinfo is None:
        raise RuntimeError("ATLAS cycle lock start time lacks a timezone; refusing unsafe takeover")
    return payload


def publish_cycle_lock(
    lock_path: Path,
    payload: dict[str, Any],
    *,
    replace_existing: bool = False,
) -> bool:
    """Atomically publish a fully written lock by linking a flushed temporary file.

    Unlike create-then-write, another cycle can never observe an empty or partially
    written owner record.  Replacement is allowed only after the caller holds the
    advisory guard for this lock, so stale diagnostic metadata cannot create a
    check-then-unlink acquisition race.
    """
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temporary = lock_path.with_name(
        f".{lock_path.name}.{os.getpid()}.{payload['token'][:16]}.tmp"
    )
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if replace_existing:
            os.replace(temporary, lock_path)
        else:
            try:
                os.link(temporary, lock_path)
            except FileExistsError:
                return False
    except OSError as exc:
        raise RuntimeError(f"unable to atomically publish ATLAS cycle lock: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def cycle_lock_guard_path(lock_path: Path) -> Path:
    """Return the persistent OS-level guard path for a cycle owner record."""
    return lock_path.with_name(f"{lock_path.name}.guard")


def _is_advisory_lock_contention(exc: OSError) -> bool:
    """Whether an advisory-lock failure means another process owns the guard."""
    return exc.errno in {errno.EACCES, errno.EAGAIN} or getattr(exc, "winerror", None) in {
        32,  # ERROR_SHARING_VIOLATION
        33,  # ERROR_LOCK_VIOLATION
    }


def acquire_cycle_lock_guard(guard_path: Path) -> int | None:
    """Acquire an OS-held, non-blocking exclusive guard for a cycle.

    The returned descriptor must remain open for the entire critical section.  A
    ``None`` result is an ordinary contention result; all other failures fail
    closed.  Keeping the authoritative lock separate from JSON owner metadata
    means a crashed writer's stale record can be safely replaced only after the
    operating system has released its lock.
    """
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(guard_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"unable to open ATLAS cycle lock guard: {exc}") from exc

    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt.locking locks a byte range, so ensure byte zero exists.
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if _is_advisory_lock_contention(exc):
            return None
        raise RuntimeError(f"unable to acquire ATLAS cycle lock guard: {exc}") from exc
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return descriptor


def release_cycle_lock_guard(descriptor: int) -> None:
    """Release an advisory cycle guard and close its descriptor."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


@contextmanager
def cycle_lock(date: str):
    lock_path = ATLAS_RUNTIME_ROOT / "cycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    guard_descriptor = acquire_cycle_lock_guard(cycle_lock_guard_path(lock_path))
    if guard_descriptor is None:
        # The OS lock, rather than a pid which could have been reused, is the
        # authority.  Metadata is best-effort diagnostic information only.
        try:
            existing = parse_cycle_lock_payload(lock_path)
        except RuntimeError as exc:
            raise RuntimeError(
                "ATLAS cycle already running; advisory lock guard is held and owner metadata "
                f"cannot be read safely: {exc}"
            ) from exc
        raise RuntimeError(f"ATLAS cycle already running: {existing}")

    started_at = utc_now()
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    payload = {"pid": os.getpid(), "date": date, "started_at": started_at, "token": token}
    try:
        replace_existing = lock_path.exists()
        if replace_existing:
            existing = parse_cycle_lock_payload(lock_path)
            existing_pid = int(existing["pid"])
            if process_is_running(existing_pid):
                raise RuntimeError(f"ATLAS cycle already running: {existing}")
        if not publish_cycle_lock(lock_path, payload, replace_existing=replace_existing):
            # Nothing using the current protocol can publish while this process
            # owns the guard.  Treat an unexpected owner record as unsafe rather
            # than deleting it and risking overlap with an older process.
            raise RuntimeError(
                "ATLAS cycle lock owner record appeared while the advisory guard was held; "
                "refusing unsafe takeover"
            )
        yield
    finally:
        try:
            existing = read_json_file(lock_path, default={})
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}
        if isinstance(existing, dict) and existing.get("token") == token:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        release_cycle_lock_guard(guard_descriptor)


def command_cycle(args: argparse.Namespace) -> int:
    try:
        with cycle_lock(str(args.date or "latest")):
            return _command_cycle_locked(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def required_cycle_stages_passed(stages: Sequence[dict[str, Any]]) -> bool:
    statuses = {str(stage.get("name")): stage.get("status") for stage in stages}
    return all(statuses.get(name) == "passed" for name in RELEASE_REQUIRED_STAGES)


def _command_cycle_locked(args: argparse.Namespace) -> int:
    try:
        date = args.date or latest_report()[0]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cycle_id = f"ATLAS-CYCLE-{date.replace('-', '')}"
    started_at = utc_now()
    fingerprint_before = build_cycle_fingerprint(date)
    stages: list[dict[str, Any]] = []
    blocking: list[str] = []
    # The CLI defaults to the automatic closed loop.  Programmatic legacy callers
    # that construct an older Namespace without this field fail closed by skipping
    # Phase B rather than unexpectedly creating alerts or external backups.
    skip_publication = bool(getattr(args, "skip_publication", True))
    publication_requested = bool(not args.dry_run and not skip_publication)
    full_tests_requested = bool(getattr(args, "full_tests", False))
    release_provenance_required = bool(full_tests_requested or publication_requested)
    publication_audit = publication_orchestration_path(date)

    try:
        checks = doctor_checks(
            probe_repository_remotes=release_provenance_required,
        )
    except Exception as exc:
        checks = [Check("doctor", "error", f"{type(exc).__name__}: {exc}")]
    doctor_blocking = [item for item in checks if item.required and item.status == "error"]
    stages.append(
        {
            "name": "doctor",
            "status": "passed" if not doctor_blocking else "failed",
            "detail": [asdict(item) for item in checks],
        }
    )
    if doctor_blocking:
        blocking.extend(f"doctor:{item.name}:{item.detail}" for item in doctor_blocking)

    sync_rc: int | None = None
    if args.skip_sync:
        stages.append({"name": "sync", "status": "skipped", "detail": "skip_sync=true"})
        blocking.append("sync stage was skipped")
    elif doctor_blocking:
        stages.append({"name": "sync", "status": "blocked", "detail": "doctor gate failed"})
    else:
        sync_args = argparse.Namespace(date=date, dry_run=args.dry_run, force_site=args.force_site)
        try:
            sync_rc = command_sync(sync_args)
        except Exception as exc:
            sync_rc = 1
            blocking.append(f"sync raised {type(exc).__name__}: {exc}")
        stages.append({"name": "sync", "status": "passed" if sync_rc == 0 else "failed", "detail": {"returncode": sync_rc}})
        if sync_rc != 0:
            blocking.append(f"sync failed with returncode {sync_rc}")

    ledger_write_allowed = False
    try:
        ledger_result = build_virtual_execution_ledger(write_files=False)
    except Exception as exc:
        reason = f"ledger build raised {type(exc).__name__}: {exc}"
        ledger_result = {
            "ledger_path": str(VIRTUAL_LEDGER_PATH),
            "state_path": str(VIRTUAL_LEDGER_STATE_PATH),
            "audit_path": str(VIRTUAL_LEDGER_AUDIT_PATH),
            "event_count": 0,
            "account_count": 0,
            "content_hash": None,
            "write_requested": ledger_write_allowed,
            "write_performed": False,
            "events": [],
            "state": {},
            "audit": {
                "audit_id": "ATLAS-VIRTUAL-LEDGER-AUDIT-FAILED",
                "ledger_id": LEDGER_ID,
                "schema_version": LEDGER_SCHEMA_VERSION,
                "overall_passed": False,
                "blocking_reasons": [reason],
                "warnings": [],
                "canonical_ledger_path": relative_path(VIRTUAL_LEDGER_PATH),
                "canonical_state_path": relative_path(VIRTUAL_LEDGER_STATE_PATH),
                "event_count": 0,
                "account_count": 0,
                "source_counts": {},
                "boundary": {"single_canonical_virtual_ledger": True, "real_broker_orders_allowed": False},
            },
        }
    ledger_audit = ledger_result["audit"]
    stages.append(
        {
            "name": "canonical_virtual_ledger_audit",
            "status": "passed" if ledger_audit["overall_passed"] else "failed",
            "detail": {
                "event_count": ledger_result["event_count"],
                "account_count": ledger_result["account_count"],
                "audit_path": ledger_result["audit_path"],
                "write_requested": ledger_result["write_requested"],
                "write_performed": ledger_result["write_performed"],
            },
        }
    )
    if not ledger_audit["overall_passed"]:
        blocking.extend(f"ledger:{reason}" for reason in ledger_audit["blocking_reasons"])

    try:
        replay_shadow = run_replay_shadow_validation(date)
    except Exception as exc:
        replay_shadow = {
            "overall_passed": False,
            "safety_gate_passed": False,
            "strategy_evidence_passed": False,
            "replay": {"passed": False, "execution_safety_passed": False, "strategy_evidence_passed": False, "error": f"{type(exc).__name__}: {exc}"},
            "shadow_promotion_gate": {"passed": False, "safety_passed": False, "evidence_passed": False, "payload": {}, "returncode": None},
            "boundary": {"replay_isolated": False, "promotion_auto_apply_allowed": False},
        }
    stages.append(
        {
            "name": "replay_shadow_gate",
            "status": "passed" if replay_shadow["overall_passed"] else "failed",
            "detail": {
                "replay_passed": replay_shadow["replay"]["passed"],
                "strategy_evidence_passed": replay_shadow["replay"].get("strategy_evidence_passed", False),
                "validation_scope": replay_shadow["replay"].get("validation_scope", "execution_isolation_only"),
                "shadow_promotion_gate_passed": replay_shadow["shadow_promotion_gate"]["passed"],
            },
        }
    )
    if not replay_shadow["overall_passed"]:
        blocking.append("replay/shadow validation gate failed")

    tests_rc: int | None = None
    if args.skip_tests:
        stages.append({"name": "targeted_integration_tests", "status": "skipped", "detail": {"skip_tests": True, "full_suite": False}})
        blocking.append("targeted integration tests were skipped")
    else:
        briefing_test_input = build_briefing_test_input_fingerprint(ROOT, date)
        test_args = argparse.Namespace(
            skip_site=args.skip_site,
            skip_trading_core=args.skip_trading_core,
            full=full_tests_requested,
        )
        try:
            tests_rc = command_test(test_args)
        except Exception as exc:
            tests_rc = 1
            blocking.append(f"continuous tests raised {type(exc).__name__}: {exc}")
        briefing_test_completed_at = utc_now()
        test_scope = {
            "kind": "full_regression_suite" if full_tests_requested else "targeted_integration_suite",
            "full_suite": full_tests_requested,
            "root_unittest_discovery": True,
            "briefing_unittest_discovery": True,
            "trading_core_selected_file_count": 0 if args.skip_trading_core else None if full_tests_requested else 8,
            "site_test_included": not args.skip_site,
        }
        stages.append({
            "name": "targeted_integration_tests",
            "status": "passed" if tests_rc == 0 else "failed",
            "detail": {
                "returncode": tests_rc,
                "scope": test_scope,
                "briefing_test_evidence": {
                    "schema_version": 1,
                    "suite": "python_unittest_discovery",
                    "discovery_root": "work/global-briefing/tests",
                    "pattern": "test_*.py",
                    "completed_at": briefing_test_completed_at,
                    "returncode": tests_rc,
                    "passed": tests_rc == 0,
                    "input": briefing_test_input,
                },
            },
        })
        if tests_rc != 0:
            blocking.append(f"targeted integration tests failed with returncode {tests_rc}")

    history_root = RUN_AUDIT_ROOT / "history" / date
    global_history_integrity_before = audit_global_cycle_history(allow_append=True)
    if global_history_integrity_before.get("anchor_present") is not True:
        blocking.append(
            "global run audit history has no external anchor; explicit bootstrap is required"
        )
    elif (
        global_history_integrity_before.get("passed")
        and not args.dry_run
        and global_history_integrity_before.get("append_pending")
    ):
        write_global_cycle_history_anchor(global_history_integrity_before)
        global_history_integrity_before = audit_global_cycle_history()
    if not global_history_integrity_before.get("passed"):
        blocking.extend(
            f"global run audit history integrity: {error}"
            for error in global_history_integrity_before.get("errors", [])
        )
    history_integrity_before = audit_cycle_history(history_root)
    if not history_integrity_before["passed"]:
        blocking.extend(f"run audit history integrity: {error}" for error in history_integrity_before["errors"])
    # Never append to a chain that has not first been verified.  This is also
    # the recovery boundary after an interrupted prior write: an operator must
    # restore its externally authenticated checkpoint rather than burying the
    # problem under another record.
    history_write_allowed = bool(
        not args.dry_run
        and history_integrity_before["passed"]
        and global_history_integrity_before.get("passed") is True
        and global_history_integrity_before.get("anchor_present") is True
    )

    ledger_write_allowed = bool(
        not args.dry_run
        and not blocking
        and not args.skip_sync
        and sync_rc == 0
        and tests_rc == 0
        and replay_shadow.get("safety_gate_passed", replay_shadow.get("overall_passed")) is True
        and ledger_audit.get("overall_passed") is True
    )
    if ledger_write_allowed:
        try:
            ledger_result = build_virtual_execution_ledger(write_files=True)
            ledger_audit = ledger_result["audit"]
        except Exception as exc:
            blocking.append(f"canonical ledger commit raised {type(exc).__name__}: {exc}")
            ledger_result["write_performed"] = False
        if not ledger_result.get("write_performed"):
            blocking.append("canonical ledger write was not performed after all upstream gates passed")
        if not ledger_audit.get("overall_passed"):
            blocking.extend(
                f"ledger commit:{reason}" for reason in ledger_audit.get("blocking_reasons", [])
            )
    stages.append({
        "name": "canonical_virtual_ledger_commit",
        "status": "passed" if ledger_result.get("write_performed") else "dry_run" if args.dry_run else "blocked",
        "detail": {
            "all_required_gates_passed": ledger_write_allowed,
            "write_performed": ledger_result.get("write_performed", False),
            "blocked_by": list(blocking),
        },
    })

    fingerprint_after = build_cycle_fingerprint(date)
    workspace_lock = build_workspace_lock(
        probe_remotes=release_provenance_required,
    )
    workspace_lock_content_sha256 = str(workspace_lock.get("content_sha256") or "")
    release_evidence_path = getattr(args, "release_evidence", None)
    requested_repository = str(getattr(args, "release_repository", None) or "").strip()
    origin_repository = github_repository_from_origin() if release_evidence_path else None
    expected_repository = origin_repository or requested_repository
    expected_source_ref = RELEASE_EVIDENCE_SOURCE_REF
    release_evidence: dict[str, Any] = {
        "requested": bool(release_evidence_path),
        "verified": False,
        "artifact_path": str(Path(release_evidence_path).expanduser().resolve())
        if release_evidence_path
        else None,
        "artifact_sha256": None,
        "artifact_bytes": None,
        "repository": expected_repository or None,
        "source_ref": expected_source_ref,
        "workflow": RELEASE_EVIDENCE_WORKFLOW,
        "run_id": None,
        "run_attempt": None,
        "verification_count": 0,
        "errors": [],
    }
    if release_evidence_path:
        if origin_repository and requested_repository and (
            origin_repository.casefold() != requested_repository.casefold()
        ):
            release_evidence["errors"].append(
                "configured release repository does not match the root origin"
            )
        elif not expected_repository:
            release_evidence["errors"].append(
                "GitHub repository could not be inferred; use --release-repository OWNER/REPO"
            )
        else:
            release_evidence = verify_release_evidence(
                Path(release_evidence_path),
                workspace_lock=workspace_lock,
                expected_repository=expected_repository,
                expected_source_ref=expected_source_ref,
            )
    stages.append(
        {
            "name": "external_release_attestation",
            "status": (
                "passed"
                if release_evidence.get("verified") is True
                else "failed"
                if release_evidence_path
                else "not_requested"
            ),
            "detail": release_evidence,
        }
    )
    previous_state = read_json_file(CYCLE_STATE_PATH, default={})
    previous_key = previous_state.get("idempotency_key") if isinstance(previous_state, dict) else None
    previous_workspace_lock_sha256 = (
        str(previous_state.get("workspace_lock_content_sha256") or "")
        if isinstance(previous_state, dict)
        else ""
    )
    execution_profile = {
        "dry_run": bool(args.dry_run),
        "skip_sync": bool(args.skip_sync),
        "skip_tests": bool(args.skip_tests),
        "skip_site": bool(args.skip_site),
        "skip_trading_core": bool(args.skip_trading_core),
        "force_site": bool(args.force_site),
        "full_tests": full_tests_requested,
        "skip_publication": skip_publication,
        "post_gate_publication_requested": publication_requested,
        "release_evidence_requested": bool(release_evidence_path),
        "release_evidence_sha256": release_evidence.get("artifact_sha256"),
    }
    idempotency_key = stable_hash(
        {
            "cycle_id": cycle_id,
            "date": date,
            "fingerprint": fingerprint_after["fingerprint"],
            "ledger_content_hash": ledger_result.get("content_hash"),
            "execution_profile": execution_profile,
            "workspace_lock_content_sha256": workspace_lock_content_sha256,
        }
    )
    previous_passed = bool(previous_state.get("overall_passed")) if isinstance(previous_state, dict) else False
    idempotent_replay = bool(
        previous_key == idempotency_key
        and previous_workspace_lock_sha256 == workspace_lock_content_sha256
        and previous_passed
        and not args.force
    )
    finished_at = utc_now()
    run_id = f"{cycle_id}-RUN-{finished_at.replace(':', '').replace('-', '').replace('.', '')}"
    operational_gate_passed = not blocking
    release_required_stages_passed = required_cycle_stages_passed(stages)
    research_promotion_passed = bool(
        replay_shadow.get("strategy_evidence_passed") is True
        and replay_shadow.get("shadow_promotion_gate", {}).get("evidence_passed") is True
    )
    release_candidate_passed = bool(
        operational_gate_passed
        and idempotent_replay
        and full_tests_requested
        and not args.skip_site
        and not args.skip_trading_core
        and sync_rc == 0
        and ledger_result.get("write_performed") is True
        and release_required_stages_passed
        and workspace_lock["release_reproducible"]
        and release_evidence.get("verified") is True
    )

    audit_payload = {
        "cycle_id": cycle_id,
        "run_id": run_id,
        "date": date,
        "started_at": started_at,
        "finished_at": finished_at,
        "dry_run": bool(args.dry_run),
        "execution_profile": execution_profile,
        "idempotency_key": idempotency_key,
        "idempotent_replay": idempotent_replay,
        "workspace_lock_content_sha256": workspace_lock_content_sha256,
        "gate_scope": "daily_operational",
        "overall_passed": operational_gate_passed,
        "operational_gate_passed": operational_gate_passed,
        "release_candidate_passed": release_candidate_passed,
        "release_candidate_evidence": {
            "sync_passed": sync_rc == 0,
            "canonical_ledger_write_performed": ledger_result.get("write_performed") is True,
            "required_stages": list(RELEASE_REQUIRED_STAGES),
            "required_stages_passed": release_required_stages_passed,
            "external_attestation": release_evidence,
        },
        "research_promotion_passed": research_promotion_passed,
        "publication": {
            "requested": publication_requested,
            "status": (
                "awaiting_post_audit_orchestration"
                if publication_requested
                else "not_requested_or_dry_run"
            ),
            "separate_audit_path": str(publication_audit),
            "phase_a_reexecuted": False,
            "contract": (
                "Post-gate publication is recorded separately so the anchored core "
                "cycle audit is never rewritten after backup and candidate binding."
            ),
        },
        "blocking_reasons": blocking,
        "stages": stages,
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
        "ledger": {
            "ledger_path": ledger_result["ledger_path"],
            "state_path": ledger_result["state_path"],
            "audit_path": ledger_result["audit_path"],
            "event_count": ledger_result["event_count"],
            "account_count": ledger_result["account_count"],
            "content_hash": ledger_result.get("content_hash"),
            "anchor_path": ledger_result.get("anchor_path"),
            "anchor_sha256": ledger_result.get("anchor_sha256"),
            "write_requested": ledger_result.get("write_requested", False),
            "write_performed": ledger_result.get("write_performed", False),
        },
        "ledger_audit": ledger_audit,
        "replay_shadow_validation": replay_shadow,
        "test_returncode": tests_rc,
        "sync_returncode": sync_rc,
        "boundary": {
            "unified_cycle_entry": True,
            "single_virtual_execution_ledger": ledger_audit.get("boundary", {}).get("single_canonical_virtual_ledger") is True,
            "idempotent_outputs": idempotent_replay,
            "idempotency_verified_by_repeated_fingerprint": idempotent_replay,
            "safe_fail_closed_gates": bool(
                (ledger_result.get("write_performed") and ledger_write_allowed)
                or (not ledger_result.get("write_performed") and not ledger_write_allowed)
            ),
            "canonical_write_performed": ledger_result.get("write_performed", False),
            "canonical_write_blocked_by_upstream_gate": bool(not ledger_write_allowed and not args.dry_run),
            "release_required_stages_passed": release_required_stages_passed,
            "replay_and_shadow_validation": replay_shadow["overall_passed"],
            "targeted_integration_test_gate": tests_rc == 0,
            "full_test_suite_executed": full_tests_requested and tests_rc == 0,
            "run_audit_written": history_write_allowed,
            "immutable_run_history": history_write_allowed,
            "real_broker_orders_allowed": False,
        },
        "history_integrity_before": history_integrity_before,
        "global_history_integrity_before": global_history_integrity_before,
        "workspace_lock": workspace_lock,
    }
    audit_payload["audit_chain"] = {
        "schema_version": 1,
        "sequence": history_integrity_before["next_sequence"],
        "previous_audit_sha256": history_integrity_before["last_audit_sha256"],
        "legacy_record_count_at_genesis": history_integrity_before["legacy_record_count"],
    }
    audit_payload["audit_chain"]["entry_sha256"] = audit_record_hash(audit_payload)
    audit_json_path = RUN_AUDIT_ROOT / f"atlas-cycle-{date}.json"
    audit_md_path = RUN_AUDIT_ROOT / f"ATLAS_CYCLE_RUN_AUDIT-{date}.md"
    history_json_path = history_root / f"{run_id}.json"
    history_md_path = history_root / f"{run_id}.md"
    history_committed = False
    if history_write_allowed:
        record_sha256: str | None = None
        anchor_committed = False
        try:
            atomic_write_json(history_json_path, audit_payload)
            record_sha256 = file_sha256(history_json_path)
            history_integrity_after = audit_cycle_history(
                history_root,
                allow_unanchored_genesis=(
                    not history_integrity_before["anchor_present"]
                    and history_integrity_before["chained_record_count"] == 0
                    and history_integrity_before["legacy_record_count"] == 0
                ),
                allow_anchor_append=history_integrity_before["anchor_present"],
            )
            if not history_integrity_after["passed"]:
                raise RuntimeError(
                    "run audit history verification failed: "
                    + "; ".join(history_integrity_after["errors"])
                )
            write_cycle_history_anchor(history_root, history_integrity_after)
            anchor_committed = True
            history_integrity_after = audit_cycle_history(history_root)
            if not history_integrity_after["passed"]:
                raise RuntimeError(
                    "run audit history anchor verification failed: "
                    + "; ".join(history_integrity_after["errors"])
                )
            global_history_pending = audit_global_cycle_history(allow_append=True)
            if not global_history_pending["passed"]:
                raise RuntimeError(
                    "global run audit history verification failed: "
                    + "; ".join(global_history_pending["errors"])
                )
            if global_history_pending.get("anchor_present") is not True:
                raise RuntimeError(
                    "global run audit history has no external anchor; explicit bootstrap is required"
                )
            write_global_cycle_history_anchor(global_history_pending)
            global_history_after = audit_global_cycle_history()
            if not global_history_after["passed"]:
                raise RuntimeError(
                    "global run audit history anchor verification failed: "
                    + "; ".join(global_history_after["errors"])
                )
            history_committed = True
        except Exception as exc:
            # An anchor write that fails before its atomic replace must not leave
            # a valid-looking but unanchored tail.  Remove only the exact record
            # we created; if it changed concurrently, preserve it and fail closed.
            try:
                if (
                    not anchor_committed
                    and record_sha256
                    and history_json_path.exists()
                    and file_sha256(history_json_path) == record_sha256
                ):
                    history_json_path.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"run audit history checkpoint transaction failed: {type(exc).__name__}: {exc}"
            ) from exc

    if history_committed:
        atomic_write_text(history_md_path, build_cycle_audit_markdown(audit_payload))
        atomic_write_json(audit_json_path, audit_payload)
        atomic_write_text(audit_md_path, build_cycle_audit_markdown(audit_payload))
        atomic_write_json(ATLAS_RUNTIME_ROOT / "workspace-lock.json", workspace_lock)
        atomic_write_json(
            CYCLE_STATE_PATH,
            {
                "cycle_id": cycle_id,
                "date": date,
                "idempotency_key": idempotency_key,
                "execution_profile": execution_profile,
                "fingerprint": fingerprint_after["fingerprint"],
                "ledger_content_hash": ledger_result.get("content_hash"),
                "ledger_anchor_sha256": ledger_result.get("anchor_sha256"),
                "canonical_ledger_write_performed": ledger_result.get("write_performed") is True,
                "workspace_lock_content_sha256": workspace_lock_content_sha256,
                "last_audit_json": str(audit_json_path),
                "last_audit_markdown": str(audit_md_path),
                "last_history_json": str(history_json_path),
                "last_history_markdown": str(history_md_path),
                "updated_at": audit_payload["finished_at"],
                "overall_passed": audit_payload["overall_passed"],
                "operational_gate_passed": audit_payload["operational_gate_passed"],
                "release_candidate_passed": audit_payload["release_candidate_passed"],
                "blocking_reasons": audit_payload["blocking_reasons"],
                "publication": {
                    "requested": publication_requested,
                    "status": audit_payload["publication"]["status"],
                    "returncode": None,
                    "audit_path": str(publication_audit),
                    "phase_a_reexecuted": False,
                },
            },
        )

    publication_result: dict[str, Any] = {
        "status": "not_requested",
        "returncode": None,
        "audit_path": str(publication_audit),
        "phase_a_reexecuted": False,
    }
    if args.dry_run:
        publication_result["status"] = "skipped_dry_run"
    elif not publication_requested:
        publication_result["status"] = "skipped_by_flag"
    elif not history_committed:
        publication_result["status"] = "not_attempted_core_audit_unavailable"
        publication_result["returncode"] = PUBLICATION_ERROR_RETURN_CODE
    elif not operational_gate_passed:
        # Keep the core cycle result authoritative: Phase B cannot repair or hide
        # a failed Phase A run, and it must not manufacture later control artifacts.
        publication_result["status"] = "not_attempted_core_cycle_blocked"
        publication_result["returncode"] = PUBLICATION_BLOCKED_RETURN_CODE
    else:
        try:
            publication_result = run_post_gate_publication(
                date,
                initiated_by="atlas cycle",
            )
        except Exception as exc:
            publication_result = {
                "schema_version": PUBLICATION_ORCHESTRATION_SCHEMA_VERSION,
                "date": date,
                "initiated_by": "atlas cycle",
                "phase_a_reexecuted": False,
                "status": "error",
                "returncode": PUBLICATION_ERROR_RETURN_CODE,
                "blocking_reasons": [],
                "errors": [f"publication orchestration raised {type(exc).__name__}"],
                "audit_path": str(publication_audit),
                "started_at": utc_now(),
                "finished_at": utc_now(),
            }
            try:
                atomic_write_json(publication_audit, publication_result)
            except OSError:
                pass

    if history_committed:
        state = read_json_file(CYCLE_STATE_PATH, default={})
        if not isinstance(state, dict):
            state = {}
        state["publication"] = {
            "requested": publication_requested,
            "status": publication_result.get("status"),
            "returncode": publication_result.get("returncode"),
            "audit_path": publication_result.get("audit_path", str(publication_audit)),
            "phase_a_reexecuted": False,
            "updated_at": utc_now(),
        }
        atomic_write_json(CYCLE_STATE_PATH, state)

    summary = {
        "cycle_id": cycle_id,
        "date": date,
        "overall_passed": audit_payload["overall_passed"],
        "blocking_reasons": blocking,
        "ledger_path": ledger_result["ledger_path"],
        "event_count": ledger_result["event_count"],
        "run_audit": str(audit_json_path) if history_committed else None,
        "run_history": str(history_json_path) if history_committed else None,
        "idempotent_replay": idempotent_replay,
        "publication": publication_result,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    core_returncode = 0 if audit_payload["overall_passed"] else 1
    if core_returncode != 0:
        return core_returncode
    if publication_requested:
        publication_returncode = publication_result.get("returncode")
        if isinstance(publication_returncode, int) and not isinstance(
            publication_returncode, bool
        ):
            return publication_returncode
        # A requested Phase B without a concrete result is not a successful
        # top-level cycle.  Fail closed so schedulers cannot mistake a missing
        # publication verdict for a completed website hand-off.
        return PUBLICATION_ERROR_RETURN_CODE
    return 0


def npm_command() -> str:
    candidate = "npm.cmd" if os.name == "nt" else "npm"
    return shutil.which(candidate) or candidate


def build_test_plan(args: argparse.Namespace) -> list[TestGate]:
    plan: list[TestGate] = []
    root_tests = ROOT / "tests"
    plan.append(
        TestGate(
            "atlas-control-plane",
            (
                sys.executable,
                "-m",
                "unittest",
                "discover",
                str(root_tests),
                "-p",
                "test_*.py",
            ),
        )
    )

    plan.append(
        TestGate(
            "global-briefing",
            (
                sys.executable,
                "-m",
                "unittest",
                "discover",
                str(BRIEFING_ROOT / "tests"),
                "-p",
                "test_*.py",
            ),
        )
    )

    if not args.skip_trading_core:
        if getattr(args, "full", False):
            core_tests = (
                sys.executable,
                "-m",
                "trading_core.testing.full_test_matrix",
            )
        else:
            core_tests = [sys.executable, "-m", "pytest"]
            core_tests.extend(
                [
                    "tests/test_global_briefing_integration.py",
                    "tests/test_global_briefing_integration_robustness.py",
                    "tests/test_signal_schema.py",
                    "tests/test_entrypoint.py",
                    "tests/test_macro_signal_refresh.py",
                    "tests/test_promotion_gate.py",
                    "tests/test_promotion_evidence.py",
                    "tests/test_cli_stage_boundaries.py",
                ]
            )
        core_timeout = 45 * 60 if getattr(args, "full", False) else COMMAND_TIMEOUT_SECONDS
        plan.append(
            TestGate(
                "trading-core",
                tuple(core_tests),
                cwd=TRADING_ROOT,
                trading_core=True,
                timeout=core_timeout,
            )
        )

    if not args.skip_site:
        full_suite = bool(getattr(args, "full", False))
        plan.append(
            TestGate(
                "atlas-site",
                (npm_command(), "run", "quality") if full_suite else (npm_command(), "test"),
                cwd=SITE_ROOT,
            )
        )
        if full_suite:
            plan.append(
                TestGate(
                    "atlas-site-security-policy",
                    (npm_command(), "run", "audit:policy"),
                    cwd=SITE_ROOT,
                )
            )
    return plan


def validate_required_test_sources() -> list[str]:
    errors: list[str] = []
    for name, directory in (
        ("atlas-control-plane", ROOT / "tests"),
        ("global-briefing", BRIEFING_ROOT / "tests"),
    ):
        if not directory.is_dir():
            errors.append(f"{name} test directory is missing: {directory}")
            continue
        test_files = [
            path
            for path in directory.glob("test_*.py")
            if path.is_file() and path.stat().st_size > 0
        ]
        if not test_files:
            errors.append(f"{name} test discovery would be empty: {directory}")
    return errors


def write_subprocess_output(stream: Any, value: str) -> None:
    if not value:
        return
    text = value if value.endswith("\n") else value + "\n"
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        replacement = text.encode(encoding, errors="replace")
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(replacement)
        else:
            stream.write(replacement.decode(encoding, errors="replace"))
    stream.flush()


def run_daily_test_plan(plan: Sequence[TestGate]) -> int:
    """Run independent daily gates concurrently and emit deterministic logs."""

    if not plan:
        print("daily test plan is empty", file=sys.stderr)
        return 1

    def execute(gate: TestGate) -> tuple[subprocess.CompletedProcess[str], float]:
        started = time.monotonic()
        try:
            result = capture_command(
                list(gate.command),
                cwd=gate.cwd,
                trading_core=gate.trading_core,
                timeout=gate.timeout,
            )
        except Exception as exc:
            result = subprocess.CompletedProcess(
                list(gate.command),
                1,
                stdout="",
                stderr=f"test gate raised {type(exc).__name__}: {exc}\n",
            )
        return result, time.monotonic() - started

    started = time.monotonic()
    workers = min(4, len(plan))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="atlas-test") as pool:
        futures = [pool.submit(execute, gate) for gate in plan]
        results = [future.result() for future in futures]

    passed = True
    for gate, (result, duration) in zip(plan, results, strict=True):
        print(f"\n> [{gate.name}] {' '.join(gate.command)}", flush=True)
        if result.stdout:
            write_subprocess_output(sys.stdout, result.stdout)
        if result.stderr:
            write_subprocess_output(sys.stderr, result.stderr)
        status = "passed" if result.returncode == 0 else "failed"
        print(
            f"[atlas-test] {gate.name}: {status} rc={result.returncode} "
            f"duration={duration:.3f}s",
            flush=True,
        )
        passed = passed and result.returncode == 0
    print(
        f"[atlas-test] parallel_total={time.monotonic() - started:.3f}s workers={workers}",
        flush=True,
    )
    return 0 if passed else 1


def command_test(args: argparse.Namespace) -> int:
    source_errors = validate_required_test_sources()
    if source_errors:
        for error in source_errors:
            print(error, file=sys.stderr)
        return 1
    plan = build_test_plan(args)
    if getattr(args, "full", False):
        # The full trading-core matrix already manages its own bounded shards and
        # can emit large logs. Keep this release/CI path streamed and sequential.
        for gate in plan:
            if run_command(
                list(gate.command),
                cwd=gate.cwd,
                trading_core=gate.trading_core,
                timeout=gate.timeout,
            ) != 0:
                return 1
        return 0
    return run_daily_test_plan(plan)


def command_build_site(_args: argparse.Namespace) -> int:
    return run_command([npm_command(), "test"], cwd=SITE_ROOT)


def command_serve(_args: argparse.Namespace) -> int:
    # Development servers are intentionally long-lived; the cycle timeout is
    # for gated batch subprocesses, not an interactive serve session.
    return run_command([npm_command(), "run", "dev"], cwd=SITE_ROOT, timeout=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ATLAS unified briefing and research workspace.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check the unified workspace and dependencies.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    doctor.set_defaults(handler=command_doctor)

    sync = subparsers.add_parser("sync", help="Bridge the latest briefing into trading-core and the site.")
    sync.add_argument("--date", type=valid_iso_date, help="Briefing date; defaults to the newest dated report.")
    sync.add_argument("--dry-run", action="store_true", help="Validate the bridge without writing files.")
    sync.add_argument("--force-site", action="store_true", help="Regenerate site data even if already deployed.")
    sync.set_defaults(handler=command_sync)

    quality = subparsers.add_parser("quality", help="Audit report content and proper forecast calibration readiness.")
    quality.add_argument("--date", type=valid_iso_date, help="Briefing date; defaults to the newest dated report.")
    quality.add_argument("--dry-run", action="store_true", help="Run the audit without writing its JSON artifact.")
    quality.add_argument("--strict", action="store_true", help="Fail unless all research-readiness gates pass.")
    quality.set_defaults(handler=command_quality)

    heal = subparsers.add_parser("heal", help="Detect, register, and safely repair allowlisted ATLAS issues.")
    heal.add_argument("--date", type=valid_iso_date, help="Briefing date; defaults to the newest dated report.")
    heal.add_argument("--apply-safe", action="store_true", help="Apply only allowlisted low-risk repairs with rollback.")
    heal.add_argument("--deep", action="store_true", help="Also run briefing and website quality suites.")
    heal.add_argument("--strict", action="store_true", help="Fail when a critical issue remains unresolved.")
    heal.add_argument("--status", action="store_true", help="Show the latest self-healing report without running probes.")
    heal.add_argument("--json", action="store_true", help="Print full machine-readable output.")
    heal.set_defaults(handler=command_heal)

    improvements = subparsers.add_parser("improvements", help="Track and verify retrospective improvement actions.")
    improvements.add_argument("--date", type=valid_iso_date, help="Review date; defaults to the newest dated report.")
    improvements.add_argument("--apply-safe", action="store_true", help="Repair allowlisted low-risk derived artifacts.")
    improvements.add_argument("--strict", action="store_true", help="Fail on overdue or regressed critical actions.")
    improvements.add_argument("--status", action="store_true", help="Show the latest improvement report.")
    improvements.add_argument("--json", action="store_true", help="Print full machine-readable output.")
    improvements.set_defaults(handler=command_improvements)

    backup = subparsers.add_parser("backup", help="Create and restore-verify an external disaster-recovery snapshot.")
    backup.add_argument("--date", type=valid_iso_date, required=True)
    backup.add_argument("--json", action="store_true")
    backup.add_argument(
        "--full",
        action="store_true",
        help="Create the periodic full-history snapshot; daily runs use the smaller publication checkpoint.",
    )
    backup.set_defaults(handler=command_backup)

    alerts = subparsers.add_parser("alerts", help="Prepare the audited Codex task-inbox alert payload.")
    alerts.add_argument("--date", type=valid_iso_date)
    alerts.add_argument(
        "--alert-id",
        help="Target a specific immutable alert revision for acknowledgement, receipt, or retry operations.",
    )
    alerts.add_argument("--json", action="store_true")
    alerts.add_argument("--ack-by", help="Record who acknowledged the date-aligned alert payload.")
    alerts.add_argument("--retry", action="store_true", help="Prepare the next delivery attempt or escalate.")
    alerts.add_argument(
        "--receipt-destination",
        help="Configured destination that returned a durable delivery receipt.",
    )
    alerts.add_argument("--receipt-id", help="Provider or connector receipt identifier.")
    alerts.add_argument(
        "--process-due",
        action="store_true",
        help="Consume persisted alert retry and acknowledgement deadlines without generating a new alert.",
    )
    alerts.set_defaults(handler=command_alerts)

    publish = subparsers.add_parser(
        "publish",
        help="Run the post-gate Phase-B publication retry for an already staged candidate.",
    )
    publish.add_argument(
        "--date",
        type=valid_iso_date,
        help="Publication date; defaults to the newest dated report.",
    )
    publish.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify the anchored Phase-A audit and staged candidate without creating gate evidence.",
    )
    publish.set_defaults(handler=command_publish)

    cycle = subparsers.add_parser("cycle", help="Run the gated ATLAS virtual trading/evolution cycle.")
    cycle.add_argument("--date", type=valid_iso_date, help="Cycle date; defaults to the newest dated report.")
    cycle.add_argument("--dry-run", action="store_true", help="Validate the cycle without writing cycle/ledger artifacts.")
    cycle.add_argument("--force", action="store_true", help="Re-run even when the previous idempotency key matches.")
    cycle.add_argument("--force-site", action="store_true", help="Regenerate site data during sync even if already deployed.")
    cycle.add_argument("--skip-sync", action="store_true", help="Skip briefing/trading-core/site sync stage.")
    cycle.add_argument("--skip-tests", action="store_true", help="Skip the continuous test gate.")
    cycle.add_argument("--skip-site", action="store_true", help="Skip site tests inside the continuous test gate.")
    cycle.add_argument("--skip-trading-core", action="store_true", help="Skip trading-core tests inside the continuous test gate.")
    cycle.add_argument(
        "--full-tests",
        action="store_true",
        help="Run the complete regression suite; release status also requires signed external CI evidence.",
    )
    cycle.add_argument(
        "--release-evidence",
        type=Path,
        help="Path to the GitHub-attested atlas-release-evidence.json artifact.",
    )
    cycle.add_argument(
        "--release-repository",
        help="Expected OWNER/REPO when it cannot be inferred from the root origin.",
    )
    cycle.add_argument(
        "--skip-publication",
        action="store_true",
        help=(
            "Do not run the post-audit Phase-B gate sequence and staged-candidate retry. "
            "The default non-dry-run cycle performs it without rerunning Phase A."
        ),
    )
    cycle.set_defaults(handler=command_cycle)

    recover_global_history = subparsers.add_parser(
        "recover-global-history-anchor",
        help=(
            "Plan or explicitly apply the one-time, append-only recovery of a "
            "test-polluted global run-audit anchor."
        ),
    )
    recover_global_history.add_argument(
        "--expected-head-sha256",
        required=True,
        help="Exact signed polluted predecessor head; lowercase SHA-256.",
    )
    recover_global_history.add_argument(
        "--backup-latest",
        type=Path,
        default=ATLAS_RUNTIME_ROOT / "backups" / "latest.json",
        help="Authenticated restore-verified latest.json used to corroborate the real history.",
    )
    recover_global_history.add_argument(
        "--expected-plan-sha256",
        help="Exact plan hash emitted by the dry run; mandatory with --apply.",
    )
    recover_global_history.add_argument(
        "--apply",
        action="store_true",
        help="Apply revision two. Without this flag the command is strictly read-only.",
    )
    recover_global_history.add_argument(
        "--acknowledge-cross-workspace-recovery",
        action="store_true",
        help="Explicit acknowledgement required with --apply.",
    )
    recover_global_history.set_defaults(handler=command_recover_global_history_anchor)

    tests = subparsers.add_parser("test", help="Run the cross-project integration test suite.")
    tests.add_argument("--skip-site", action="store_true")
    tests.add_argument("--skip-trading-core", action="store_true")
    tests.add_argument("--full", action="store_true", help="Run the complete trading-core regression suite.")
    tests.set_defaults(handler=command_test)

    build_site = subparsers.add_parser("build-site", help="Build and test the ATLAS web surface.")
    build_site.set_defaults(handler=command_build_site)

    serve = subparsers.add_parser("serve", help="Start the ATLAS site in development mode.")
    serve.set_defaults(handler=command_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
