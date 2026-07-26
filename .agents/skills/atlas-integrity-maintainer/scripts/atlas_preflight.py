"""Read-only fail-closed ATLAS integrity preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def payload_sha256(payload: Any) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def git_state(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "status", "--short", "--branch"],
        cwd=path,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    lines = result.stdout.splitlines()
    return {
        "passed": result.returncode == 0,
        "branch": lines[0] if lines else None,
        "dirty_entry_count": max(0, len(lines) - 1),
    }


def calendar_check(root: Path, today: date, required_days: int) -> dict[str, Any]:
    path = root / "work" / "trading-core" / "data" / "equity_universe" / "trading_calendar.json"
    rows = load_json(path)
    dates = sorted({str(row.get("date")) for row in rows if isinstance(row, dict) and row.get("date")})
    maximum = date.fromisoformat(dates[-1]) if dates else None
    forward_days = (maximum - today).days if maximum else None
    return {
        "passed": forward_days is not None and forward_days >= required_days,
        "path": str(path),
        "maximum_date": maximum.isoformat() if maximum else None,
        "forward_calendar_days": forward_days,
        "required_forward_days": required_days,
    }


def publication_check(root: Path) -> dict[str, Any]:
    manifest_path = root / "src" / "app" / "publication.generated.json"
    payload_path = root / "src" / "app" / "briefing.generated.json"
    manifest = load_json(manifest_path)
    payload = load_json(payload_path)
    actual = payload_sha256(payload)
    return {
        "passed": (
            manifest.get("frozen") is True
            and manifest.get("payloadSha256") == actual
            and int(manifest.get("snapshotRevision") or 0) > 0
        ),
        "manifest_path": str(manifest_path),
        "frozen": manifest.get("frozen"),
        "declared_payload_sha256": manifest.get("payloadSha256"),
        "actual_payload_sha256": actual,
        "snapshot_revision": manifest.get("snapshotRevision"),
    }


def ledger_check(root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(root))
    try:
        import atlas  # type: ignore

        result = atlas.build_virtual_execution_ledger(write_files=False)
        audit = result.get("audit", {})
        return {
            "passed": audit.get("overall_passed") is True,
            "event_count": result.get("event_count"),
            "blocking_reasons": audit.get("blocking_reasons", []),
        }
    except Exception as exc:  # bounded diagnostic; never include secret values
        return {"passed": False, "error_type": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--required-calendar-days", type=int, default=90)
    args = parser.parse_args()
    root = args.root.resolve()
    as_of = date.fromisoformat(args.as_of)

    checks = {
        "root_git": git_state(root),
        "site_git": git_state(root / "src"),
        "trading_git": git_state(root / "work" / "trading-core"),
        "calendar": calendar_check(root, as_of, args.required_calendar_days),
        "publication": publication_check(root),
        "canonical_ledger": ledger_check(root),
    }
    blocking = [name for name, result in checks.items() if result.get("passed") is not True]
    output = {
        "schema_version": 1,
        "root": str(root),
        "as_of": as_of.isoformat(),
        "passed": not blocking,
        "blocking_checks": blocking,
        "checks": checks,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not blocking else 1


if __name__ == "__main__":
    raise SystemExit(main())
