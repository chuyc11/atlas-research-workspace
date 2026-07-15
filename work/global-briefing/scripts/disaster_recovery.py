#!/usr/bin/env python3
"""Create and restore-verify external ATLAS disaster-recovery snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    "atlas.py",
    "REPOSITORY_GOVERNANCE.md",
    "work/global-briefing/config",
    "work/global-briefing/data",
    "work/shared/atlas",
    "outputs",
    "src/app/briefing.generated.json",
)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", "node_modules"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


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
            try:
                path.resolve().relative_to(latest_resolved)
                continue
            except ValueError:
                pass
            path.resolve().relative_to(root.resolve())
            files.add(path.resolve())
    return sorted(files, key=lambda value: value.as_posix())


def verify_archive(archive: Path, manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
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
        for item in manifest.get("files", []):
            restored = restore_root / item["path"]
            if not restored.is_file():
                errors.append(f"missing: {item['path']}")
            elif sha256(restored) != item["sha256"]:
                errors.append(f"hash mismatch: {item['path']}")
    return not errors, errors


def create_snapshot(
    *,
    date: str,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    latest_path: Path = LATEST_PATH,
) -> dict[str, Any]:
    config = load_config(config_path)
    target = resolve_target(str(config["target_directory"]), root)
    target.mkdir(parents=True, exist_ok=True)
    files = iter_files(root, config.get("include_paths") or DEFAULT_INCLUDES)
    if not files:
        raise ValueError("no disaster-recovery files selected")
    created_at = utc_now()
    manifest = {
        "schema_version": 1,
        "date": date,
        "created_at": created_at,
        "workspace": str(root.resolve()),
        "files": [
            {
                "path": path.relative_to(root.resolve()).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files
        ],
    }
    archive = target / f"atlas-backup-{date}-{datetime.now(UTC).strftime('%H%M%S%f')}.zip"
    temporary = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for item, path in zip(manifest["files"], files, strict=True):
            bundle.write(path, item["path"])
        bundle.writestr("_atlas_backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary.replace(archive)
    verified, errors = verify_archive(archive, manifest)
    result = {
        "schema_version": 1,
        "date": date,
        "created_at": created_at,
        "archive": str(archive.resolve()),
        "archive_sha256": sha256(archive),
        "file_count": len(files),
        "total_source_bytes": sum(item["size"] for item in manifest["files"]),
        "verified": verified,
        "restore_verified": verified,
        "verification_errors": errors,
        "retention_days": int(config.get("retention_days") or 14),
        "target_outside_workspace": True,
    }
    atomic_json(latest_path, result)
    cutoff = datetime.now(UTC).timestamp() - result["retention_days"] * 86400
    for old in target.glob("atlas-backup-*.zip"):
        same_snapshot_day = old.name.startswith(f"atlas-backup-{date}-")
        if old != archive and (same_snapshot_day or old.stat().st_mtime < cutoff):
            old.unlink()
    if not verified:
        raise RuntimeError("restore drill failed: " + "; ".join(errors))
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
