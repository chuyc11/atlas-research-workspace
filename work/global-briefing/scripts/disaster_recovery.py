#!/usr/bin/env python3
"""Create and restore-verify external ATLAS disaster-recovery snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "improvement_tracking.json"
LATEST_PATH = ROOT / "work" / "shared" / "atlas" / "backups" / "latest.json"
DEFAULT_INCLUDES = (
    ".github",
    "atlas.py",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "README.md",
    "QUALITY_AUDIT.md",
    "REPOSITORY_GOVERNANCE.md",
    "RESEARCH_ARCHITECTURE.md",
    "tests",
    "work/global-briefing",
    "work/shared/atlas",
    "outputs",
    "src",
    "work/trading-core",
)
EXCLUDED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    ".next",
    ".wrangler",
    "dist",
    "node_modules",
    ".git",
    "external_research",
}
EXCLUDED_RELATIVE_ROOTS = (Path("work/global-briefing/tmp"),)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def atomic_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding=encoding)
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    config = payload.get("disaster_recovery", {})
    if not isinstance(config, dict) or config.get("enabled") is not True:
        raise ValueError("disaster_recovery must be enabled in improvement_tracking.json")
    return config


def resolve_target(value: str, root: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value))).resolve()
    try:
        expanded.relative_to(root.resolve())
    except ValueError:
        return expanded
    raise ValueError("backup target must be outside the workspace")


def is_different_volume(first: Path, second: Path) -> bool:
    first_anchor = first.resolve().anchor.casefold()
    second_anchor = second.resolve().anchor.casefold()
    return bool(first_anchor and second_anchor and first_anchor != second_anchor)


def iter_files(root: Path, includes: Iterable[str]) -> list[Path]:
    files: set[Path] = set()
    latest_resolved = (root / "work" / "shared" / "atlas" / "backups").resolve()
    for relative in includes:
        candidate = (root / relative).resolve()
        if not candidate.exists():
            continue
        candidates = [candidate] if candidate.is_file() else candidate.rglob("*")
        for path in candidates:
            if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
                continue
            relative_path = path.resolve().relative_to(root.resolve())
            if any(excluded == relative_path or excluded in relative_path.parents for excluded in EXCLUDED_RELATIVE_ROOTS):
                continue
            try:
                path.resolve().relative_to(latest_resolved)
                continue
            except ValueError:
                pass
            files.add(path.resolve())
    return sorted(files, key=lambda value: value.as_posix())


def verify_archive(archive: Path, manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    git = shutil.which("git")
    with tempfile.TemporaryDirectory(prefix="atlas-restore-drill-") as temp:
        restore_root = Path(temp).resolve()
        with zipfile.ZipFile(archive) as bundle:
            bad_member = bundle.testzip()
            if bad_member:
                errors.append(f"corrupt member: {bad_member}")
            for member in bundle.infolist():
                destination = (restore_root / member.filename).resolve()
                try:
                    destination.relative_to(restore_root)
                except ValueError:
                    errors.append(f"unsafe member: {member.filename}")
            if not errors:
                bundle.extractall(restore_root)
                try:
                    embedded = json.loads(bundle.read("_atlas_backup_manifest.json"))
                except (KeyError, json.JSONDecodeError) as exc:
                    errors.append(f"invalid embedded manifest: {exc}")
                else:
                    if stable_json_sha256(embedded) != stable_json_sha256(manifest):
                        errors.append("embedded manifest hash mismatch")
        for item in manifest.get("files", []):
            restored = restore_root / item["path"]
            if not restored.is_file():
                errors.append(f"missing: {item['path']}")
            elif sha256(restored) != item["sha256"]:
                errors.append(f"hash mismatch: {item['path']}")
            elif item.get("kind") == "git_bundle":
                if not git:
                    errors.append(f"cannot verify git bundle without git: {item['path']}")
                    continue
                completed = subprocess.run(
                    [git, "bundle", "list-heads", str(restored)],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                if completed.returncode != 0 or not completed.stdout.strip():
                    errors.append(f"invalid git bundle: {item['path']}")
    return not errors, errors


def create_git_bundles(root: Path, repositories: Iterable[str], stage: Path) -> list[tuple[str, Path, str]]:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git executable is unavailable")
    entries: list[tuple[str, Path, str]] = []
    for relative in repositories:
        repo = (root / relative).resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"configured git repository is missing: {relative}")
        name = "workspace-root" if relative in {"", "."} else str(relative).replace("\\", "-").replace("/", "-")
        bundle_path = stage / f"{name}.bundle"
        completed = subprocess.run(
            [git, "-C", str(repo), "bundle", "create", str(bundle_path), "--all"],
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git bundle failed for {relative}: {completed.stderr.strip()}")
        entries.append((f"_atlas_git_bundles/{bundle_path.name}", bundle_path, str(relative)))
    return entries


def create_snapshot(
    *,
    date: str,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    latest_path: Path = LATEST_PATH,
) -> dict[str, Any]:
    config = load_config(config_path)
    target = resolve_target(str(config["target_directory"]), root)
    different_volume = is_different_volume(target, root)
    if config.get("require_different_volume") is True and not different_volume:
        raise ValueError("backup target must be on a different filesystem volume")
    target.mkdir(parents=True, exist_ok=True)
    files = iter_files(root, config.get("include_paths") or DEFAULT_INCLUDES)
    if not files:
        raise ValueError("no disaster-recovery files selected")
    created_at = utc_now()
    previous = read_json(latest_path) if latest_path.exists() else {}
    legacy_predecessor: dict[str, Any] | None = None
    if previous:
        previous_archive = Path(str(previous.get("archive", "")))
        if not previous_archive.is_file():
            raise ValueError("previous disaster-recovery snapshot is missing")
        if sha256(previous_archive) != previous.get("archive_sha256"):
            raise ValueError("previous disaster-recovery archive integrity check failed")
        schema_version = int(previous.get("schema_version") or 0)
        if schema_version == 2:
            previous_manifest = Path(str(previous.get("manifest_sidecar", "")))
            if not previous_manifest.is_file():
                raise ValueError("previous disaster-recovery snapshot is missing")
            if stable_json_sha256(read_json(previous_manifest)) != previous.get("manifest_sha256"):
                raise ValueError("previous disaster-recovery manifest integrity check failed")
        elif schema_version == 1:
            legacy_predecessor = {
                "schema_version": 1,
                "date": previous.get("date"),
                "archive_sha256": previous["archive_sha256"],
            }
        else:
            raise ValueError(f"unsupported previous disaster-recovery schema: {schema_version}")
    with tempfile.TemporaryDirectory(prefix="atlas-git-bundles-") as staging:
        bundle_entries = create_git_bundles(
            root,
            config.get("git_repositories", []),
            Path(staging),
        )
        archive_entries: list[tuple[str, Path, str, str | None]] = [
            (path.relative_to(root.resolve()).as_posix(), path, "workspace_file", None)
            for path in files
        ]
        archive_entries.extend(
            (archive_path, path, "git_bundle", repository)
            for archive_path, path, repository in bundle_entries
        )
        manifest = {
            "schema_version": 2,
            "date": date,
            "created_at": created_at,
            "workspace": str(root.resolve()),
            "previous_manifest_sha256": previous.get("manifest_sha256") if previous.get("schema_version") == 2 else None,
            **({"legacy_predecessor": legacy_predecessor} if legacy_predecessor is not None else {}),
            "files": [
                {
                    "path": archive_path,
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                    "kind": kind,
                    **({"repository": repository} if repository is not None else {}),
                }
                for archive_path, path, kind, repository in archive_entries
            ],
        }
        archive = target / f"atlas-backup-{date}-{datetime.now(UTC).strftime('%H%M%S%f')}.zip"
        temporary = archive.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for archive_path, path, _kind, _repository in archive_entries:
                bundle.write(path, archive_path)
            bundle.writestr("_atlas_backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        temporary.replace(archive)
    verified, errors = verify_archive(archive, manifest)
    manifest_sha256 = stable_json_sha256(manifest)
    manifest_sidecar = archive.with_suffix(".manifest.json")
    atomic_json(manifest_sidecar, manifest)
    atomic_text(archive.with_suffix(".manifest.sha256"), manifest_sha256 + "\n", encoding="ascii")
    result = {
        "schema_version": 2,
        "date": date,
        "created_at": created_at,
        "archive": str(archive.resolve()),
        "archive_sha256": sha256(archive),
        "manifest_sha256": manifest_sha256,
        "previous_manifest_sha256": manifest.get("previous_manifest_sha256"),
        "legacy_previous_archive_sha256": (
            legacy_predecessor.get("archive_sha256") if legacy_predecessor is not None else None
        ),
        "manifest_sidecar": str(manifest_sidecar.resolve()),
        "file_count": len(manifest["files"]),
        "git_bundle_count": sum(item.get("kind") == "git_bundle" for item in manifest["files"]),
        "total_source_bytes": sum(item["size"] for item in manifest["files"]),
        "verified": verified,
        "restore_verified": verified,
        "verification_errors": errors,
        "retention_days": int(config.get("retention_days") or 14),
        "target_outside_workspace": True,
        "target_on_different_volume": different_volume,
    }
    if not verified:
        raise RuntimeError("restore drill failed: " + "; ".join(errors))
    atomic_json(latest_path, result)
    cutoff = datetime.now(UTC).timestamp() - result["retention_days"] * 86400
    minimum_snapshots = int(config.get("minimum_snapshots_to_keep") or 3)
    archives = sorted(target.glob("atlas-backup-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old in archives[minimum_snapshots:]:
        if old != archive and old.stat().st_mtime < cutoff:
            old.unlink()
            for suffix in (".manifest.json", ".manifest.sha256"):
                old.with_suffix(suffix).unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an external ATLAS backup and run a restore drill.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = create_snapshot(date=args.date)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"backup={result['archive']} files={result['file_count']} restore_verified={result['restore_verified']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
