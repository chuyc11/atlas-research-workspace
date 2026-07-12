#!/usr/bin/env python3
"""Build the audited alert payload surfaced by the daily Codex automation."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "improvement_tracking.json"
RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_alert(
    date: str,
    *,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    runtime_root: Path = RUNTIME_ROOT,
) -> dict[str, Any]:
    config = read_json(config_path).get("external_alerting", {})
    destinations = config.get("destinations", []) if isinstance(config, dict) else []
    if not isinstance(config, dict) or config.get("enabled") is not True or not destinations:
        raise ValueError("external_alerting must be enabled with at least one destination")
    improvements = read_json(runtime_root / "improvements" / "latest.json")
    healing = read_json(runtime_root / "self_healing" / "latest.json")
    cycle = read_json(runtime_root / "cycle_state.json")
    findings: list[dict[str, Any]] = []
    for action in improvements.get("actions", []):
        status = str(action.get("status") or "")
        if status not in {"open", "overdue", "regressed", "requires_approval"}:
            continue
        spec = action.get("spec", {})
        findings.append({
            "kind": "improvement",
            "id": action.get("action_id"),
            "severity": spec.get("severity", "medium"),
            "status": status,
            "title": spec.get("title"),
            "summary": action.get("last_evaluation", {}).get("summary"),
        })
    for issue in healing.get("issues", []):
        status = str(issue.get("status") or "")
        if status in {"resolved", "healthy"}:
            continue
        findings.append({
            "kind": "self_healing",
            "id": issue.get("issue_id"),
            "severity": issue.get("severity", "medium"),
            "status": status,
            "title": issue.get("title") or issue.get("probe"),
            "summary": issue.get("summary") or issue.get("detail"),
        })
    for blocker in cycle.get("blocking", []) if isinstance(cycle.get("blocking"), list) else []:
        findings.append({"kind": "cycle", "id": cycle.get("cycle_id"), "severity": "critical", "status": "blocked", "title": "ATLAS cycle blocked", "summary": blocker})
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda item: (severity_rank.get(str(item.get("severity")), 9), str(item.get("id"))))
    payload = {
        "schema_version": 1,
        "date": date,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "attention_required" if findings else "healthy",
        "destinations": destinations,
        "delivery_contract": "The daily Codex automation must include this payload summary in its final response, which is delivered to the Codex task inbox.",
        "finding_count": len(findings),
        "findings": findings,
    }
    alerts_root = runtime_root / "alerts"
    atomic_json(alerts_root / f"alert-{date}.json", payload)
    atomic_json(alerts_root / "latest.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the daily ATLAS external alert payload.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = build_alert(args.date)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        ids = ", ".join(str(item.get("id")) for item in payload["findings"][:8]) or "none"
        print(f"alert_status={payload['status']} findings={payload['finding_count']} ids={ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
