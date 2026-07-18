#!/usr/bin/env python3
"""Auditable, fail-closed normalization for the prediction JSONL ledger.

The repair is deliberately separate from the normal append path.  Exact semantic
duplicates are deterministic first-source-line wins.  Conflicting variants are
never resolved automatically: a decision manifest must bind the selected source
line and record digest to independently hashed report evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SCRIPT_PATH = Path(__file__).resolve()
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from briefing_store import prediction_ledger_lock, strict_json_loads  # noqa: E402

ROOT = SCRIPT_PATH.parents[3]
DEFAULT_LEDGER = ROOT / "work" / "global-briefing" / "data" / "predictions.jsonl"
DEFAULT_AUDIT_DIR = ROOT / "work" / "global-briefing" / "data" / "prediction-ledger-repairs"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DECISION_SCHEMA_VERSION = 1
AUDIT_SCHEMA_VERSION = 1


class RepairError(ValueError):
    """The ledger cannot be repaired safely under the supplied authority."""


class DecisionRequiredError(RepairError):
    """At least one conflicting identity has no valid human decision."""

    def __init__(self, message: str, requirements: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.requirements = requirements


@dataclass(frozen=True)
class LedgerRow:
    line_number: int
    raw_line: bytes
    record: dict[str, Any]
    record_sha256: str
    identity: dict[str, str] | None


@dataclass(frozen=True)
class DecisionBundle:
    path: Path
    raw: bytes
    sha256: str
    decisions: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class RepairPlan:
    original: bytes
    canonical: bytes
    manifest: dict[str, Any]
    quarantined_rows: tuple[LedgerRow, ...]
    decision_bundle: DecisionBundle | None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def record_sha256(record: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(record))


def validate_run_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise RepairError(f"Run date must be canonical YYYY-MM-DD: {value!r}") from exc
    if parsed.isoformat() != value:
        raise RepairError(f"Run date must be canonical YYYY-MM-DD: {value!r}")
    return value


def identity_for(record: dict[str, Any]) -> dict[str, str] | None:
    prediction_id = str(record.get("prediction_id") or "")
    review = record.get("review")
    if isinstance(review, dict):
        return {
            "kind": "review",
            "prediction_id": prediction_id,
            "status": str(record.get("status") or ""),
            "review_date": str(review.get("review_date") or record.get("date") or ""),
        }
    if prediction_id:
        return {"kind": "original", "prediction_id": prediction_id}
    return None


def identity_token(identity: dict[str, str]) -> str:
    return canonical_json_bytes(identity).decode("utf-8")


def parse_ledger(raw: bytes, source: Path) -> tuple[list[bytes], list[LedgerRow]]:
    segments = raw.splitlines(keepends=True)
    rows: list[LedgerRow] = []
    for line_number, raw_line in enumerate(segments, start=1):
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepairError(f"Invalid UTF-8 at {source}:{line_number}: {exc}") from exc
        stripped = text.strip()
        if not stripped:
            continue
        try:
            value = strict_json_loads(stripped, source=f"{source}:{line_number}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise RepairError(f"Invalid JSONL at {source}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise RepairError(f"Invalid JSONL at {source}:{line_number}: row must be an object")
        rows.append(
            LedgerRow(
                line_number=line_number,
                raw_line=raw_line,
                record=value,
                record_sha256=record_sha256(value),
                identity=identity_for(value),
            )
        )
    return segments, rows


def group_rows(rows: list[LedgerRow]) -> dict[str, list[LedgerRow]]:
    groups: dict[str, list[LedgerRow]] = {}
    for row in rows:
        if row.identity is None:
            continue
        groups.setdefault(identity_token(row.identity), []).append(row)
    return groups


def duplicate_scan(raw: bytes, source: Path) -> dict[str, Any]:
    _segments, rows = parse_ledger(raw, source)
    groups = group_rows(rows)
    duplicate_groups: list[dict[str, Any]] = []
    exact_duplicate_rows = 0
    conflicting_variant_rows = 0
    for grouped in groups.values():
        if len(grouped) < 2:
            continue
        variants = {row.record_sha256 for row in grouped}
        exact_duplicate_rows += len(grouped) - len(variants)
        if len(variants) > 1:
            largest_variant = max(sum(row.record_sha256 == digest for row in grouped) for digest in variants)
            conflicting_variant_rows += len(grouped) - largest_variant
        duplicate_groups.append(
            {
                "identity": grouped[0].identity,
                "source_lines": [row.line_number for row in grouped],
                "record_sha256s": [row.record_sha256 for row in grouped],
                "distinct_variants": len(variants),
            }
        )
    return {
        "record_count": len(rows),
        "duplicate_identity_groups": len(duplicate_groups),
        "variant_conflict_groups": sum(item["distinct_variants"] > 1 for item in duplicate_groups),
        "exact_duplicate_rows": exact_duplicate_rows,
        "conflicting_variant_rows_lower_bound": conflicting_variant_rows,
        "groups": duplicate_groups,
    }


def _json_pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


_MISSING = object()


def field_differences(kept: Any, quarantined: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(kept, dict) and isinstance(quarantined, dict):
        differences: list[dict[str, Any]] = []
        for key in sorted(set(kept) | set(quarantined)):
            child = f"{path}/{_json_pointer_part(str(key))}"
            differences.extend(field_differences(kept.get(key, _MISSING), quarantined.get(key, _MISSING), child))
        return differences
    if isinstance(kept, list) and isinstance(quarantined, list):
        differences = []
        for index in range(max(len(kept), len(quarantined))):
            left = kept[index] if index < len(kept) else _MISSING
            right = quarantined[index] if index < len(quarantined) else _MISSING
            differences.extend(field_differences(left, right, f"{path}/{index}"))
        return differences
    if kept is not _MISSING and quarantined is not _MISSING and kept == quarantined:
        return []
    item: dict[str, Any] = {
        "path": path or "/",
        "kept_present": kept is not _MISSING,
        "quarantined_present": quarantined is not _MISSING,
    }
    if kept is not _MISSING:
        item["kept_value"] = kept
    if quarantined is not _MISSING:
        item["quarantined_value"] = quarantined
    return [item]


def _normalize_identity(value: Any, *, source: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RepairError(f"{source}.identity must be an object")
    kind = str(value.get("kind") or "")
    if kind == "original":
        identity = {"kind": kind, "prediction_id": str(value.get("prediction_id") or "")}
        if not identity["prediction_id"]:
            raise RepairError(f"{source}.identity.prediction_id is required")
        return identity
    if kind == "review":
        required = ("prediction_id", "status", "review_date")
        identity = {"kind": kind, **{key: str(value.get(key) or "") for key in required}}
        if any(not identity[key] for key in required):
            raise RepairError(f"{source}.identity requires prediction_id, status, and review_date")
        return identity
    raise RepairError(f"{source}.identity.kind must be original or review")


def _validate_report_evidence(value: Any, *, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RepairError(f"{source}.report_evidence must be a non-empty list")
    verified: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        label = f"{source}.report_evidence[{index}]"
        if not isinstance(item, dict):
            raise RepairError(f"{label} must be an object")
        path_text = str(item.get("report_path") or "").strip()
        expected_sha = str(item.get("report_sha256") or "").lower()
        reason = str(item.get("reason") or "").strip()
        line_numbers = item.get("line_numbers")
        if not path_text or not reason or not SHA256_RE.fullmatch(expected_sha):
            raise RepairError(f"{label} requires report_path, report_sha256, and reason")
        if (
            not isinstance(line_numbers, list)
            or not line_numbers
            or any(isinstance(line, bool) or not isinstance(line, int) or line < 1 for line in line_numbers)
        ):
            raise RepairError(f"{label}.line_numbers must contain positive integers")
        report_path = Path(path_text)
        resolved = report_path if report_path.is_absolute() else ROOT / report_path
        if not resolved.is_file():
            raise RepairError(f"{label} report does not exist: {resolved}")
        report_raw = resolved.read_bytes()
        observed_sha = sha256_bytes(report_raw)
        if observed_sha != expected_sha:
            raise RepairError(
                f"{label} report SHA mismatch: expected {expected_sha}, observed {observed_sha}"
            )
        line_count = len(report_raw.splitlines())
        normalized_lines = sorted(set(line_numbers))
        if normalized_lines[-1] > line_count:
            raise RepairError(f"{label} cites line {normalized_lines[-1]} but report has {line_count} lines")
        verified.append(
            {
                "report_path": path_text,
                "resolved_path": str(resolved.resolve()),
                "report_sha256": observed_sha,
                "line_numbers": normalized_lines,
                "reason": reason,
                "verified": True,
            }
        )
    return verified


def load_decision_bundle(path: Path, run_date: str) -> DecisionBundle:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepairError(f"Unable to read decision manifest {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepairError(f"Decision manifest is not UTF-8: {path}") from exc
    try:
        payload = strict_json_loads(text, source=str(path))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RepairError(f"Invalid decision manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RepairError("Decision manifest must be an object")
    if payload.get("schema_version") != DECISION_SCHEMA_VERSION:
        raise RepairError(f"Decision manifest schema_version must be {DECISION_SCHEMA_VERSION}")
    if payload.get("run_date") != run_date:
        raise RepairError(f"Decision manifest run_date must equal {run_date}")
    values = payload.get("decisions")
    if not isinstance(values, list):
        raise RepairError("Decision manifest decisions must be a list")
    decisions: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(values):
        source = f"decisions[{index}]"
        if not isinstance(item, dict):
            raise RepairError(f"{source} must be an object")
        identity = _normalize_identity(item.get("identity"), source=source)
        token = identity_token(identity)
        if token in decisions:
            raise RepairError(f"Decision manifest contains duplicate identity {identity!r}")
        keep = item.get("keep")
        if not isinstance(keep, dict):
            raise RepairError(f"{source}.keep must be an object")
        source_line = keep.get("source_line")
        digest = str(keep.get("record_sha256") or "").lower()
        if isinstance(source_line, bool) or not isinstance(source_line, int) or source_line < 1:
            raise RepairError(f"{source}.keep.source_line must be a positive integer")
        if not SHA256_RE.fullmatch(digest):
            raise RepairError(f"{source}.keep.record_sha256 must be a lowercase SHA-256")
        decision_reason = str(item.get("decision_reason") or "").strip()
        if not decision_reason:
            raise RepairError(f"{source}.decision_reason is required")
        report_evidence = _validate_report_evidence(item.get("report_evidence"), source=source)
        decisions[token] = {
            "identity": identity,
            "keep": {"source_line": source_line, "record_sha256": digest},
            "decision_reason": decision_reason,
            "report_evidence": report_evidence,
        }
    return DecisionBundle(path.resolve(), raw, sha256_bytes(raw), decisions)


def _decision_requirements(groups: dict[str, list[LedgerRow]]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for grouped in groups.values():
        if len({row.record_sha256 for row in grouped}) <= 1:
            continue
        requirements.append(
            {
                "identity": grouped[0].identity,
                "candidates": [
                    {"source_line": row.line_number, "record_sha256": row.record_sha256}
                    for row in grouped
                ],
                "required_decision_fields": [
                    "identity",
                    "keep.source_line",
                    "keep.record_sha256",
                    "decision_reason",
                    "report_evidence",
                ],
            }
        )
    return requirements


def build_repair_plan(
    ledger: Path,
    run_date: str,
    authorization_reason: str,
    decision_path: Path | None = None,
) -> RepairPlan:
    validate_run_date(run_date)
    authorization_reason = authorization_reason.strip()
    if not authorization_reason:
        raise RepairError("An explicit authorization reason is required")
    try:
        original = ledger.read_bytes()
    except OSError as exc:
        raise RepairError(f"Unable to read prediction ledger {ledger}: {exc}") from exc
    segments, rows = parse_ledger(original, ledger)
    groups = group_rows(rows)
    requirements = _decision_requirements(groups)
    bundle = load_decision_bundle(decision_path, run_date) if decision_path is not None else None
    if requirements and bundle is None:
        raise DecisionRequiredError(
            "Conflicting prediction variants require an explicit decision manifest",
            requirements,
        )

    variant_tokens = {
        identity_token(item["identity"])
        for item in requirements
        if isinstance(item.get("identity"), dict)
    }
    supplied_tokens = set(bundle.decisions) if bundle is not None else set()
    missing_tokens = variant_tokens - supplied_tokens
    extra_tokens = supplied_tokens - variant_tokens
    if missing_tokens:
        missing = [item for item in requirements if identity_token(item["identity"]) in missing_tokens]
        raise DecisionRequiredError("Decision manifest does not resolve every conflicting identity", missing)
    if extra_tokens:
        extra = [bundle.decisions[token]["identity"] for token in sorted(extra_tokens)] if bundle else []
        raise RepairError(f"Decision manifest contains stale or non-conflicting identities: {extra!r}")

    keep_by_token: dict[str, LedgerRow] = {}
    consumed_decisions: list[dict[str, Any]] = []
    for token, grouped in groups.items():
        variants = {row.record_sha256 for row in grouped}
        if len(grouped) == 1:
            keep_by_token[token] = grouped[0]
            continue
        if len(variants) == 1:
            keep_by_token[token] = grouped[0]
            continue
        assert bundle is not None
        decision = bundle.decisions[token]
        keep_spec = decision["keep"]
        candidates = [
            row
            for row in grouped
            if row.line_number == keep_spec["source_line"] and row.record_sha256 == keep_spec["record_sha256"]
        ]
        if len(candidates) != 1:
            raise RepairError(
                f"Decision for {decision['identity']!r} does not bind one matching source line and record SHA"
            )
        keep_by_token[token] = candidates[0]
        consumed_decisions.append(decision)

    quarantined: list[LedgerRow] = []
    quarantine_details: list[dict[str, Any]] = []
    kept_lines: list[int] = []
    exact_count = 0
    variant_count = 0
    for row in rows:
        if row.identity is None:
            kept_lines.append(row.line_number)
            continue
        token = identity_token(row.identity)
        kept = keep_by_token[token]
        if row.line_number == kept.line_number:
            kept_lines.append(row.line_number)
            continue
        quarantined.append(row)
        same_variant = row.record_sha256 == kept.record_sha256
        variant_representative = next(
            candidate for candidate in groups[token] if candidate.record_sha256 == row.record_sha256
        )
        duplicate_of_variant = same_variant or variant_representative.line_number != row.line_number
        if same_variant:
            classification = "exact_duplicate"
        elif duplicate_of_variant:
            classification = "conflicting_variant_exact_duplicate"
        else:
            classification = "conflicting_variant"
        exact_count += int(duplicate_of_variant)
        variant_count += int(not same_variant)
        equivalent = kept.line_number if same_variant else (
            variant_representative.line_number if duplicate_of_variant else None
        )
        quarantine_details.append(
            {
                "source_line": row.line_number,
                "identity": row.identity,
                "classification": classification,
                "kept_source_line": kept.line_number,
                "kept_record_sha256": kept.record_sha256,
                "record_sha256": row.record_sha256,
                "raw_line_sha256": sha256_bytes(row.raw_line),
                "semantic_equivalent_source_line": equivalent,
                "field_differences": field_differences(kept.record, row.record),
            }
        )

    removed_lines = {row.line_number for row in quarantined}
    canonical = b"".join(segment for number, segment in enumerate(segments, start=1) if number not in removed_lines)
    before_scan = duplicate_scan(original, ledger)
    after_scan = duplicate_scan(canonical, ledger)
    if after_scan["duplicate_identity_groups"] or after_scan["variant_conflict_groups"]:
        raise RepairError("Internal planning error: canonical ledger still contains duplicate identities")

    original_sha = sha256_bytes(original)
    canonical_sha = sha256_bytes(canonical)
    manifest = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "run_date": run_date,
        "status": "planned",
        "ledger_path": str(ledger.resolve()),
        "authorization_reason": authorization_reason,
        "policy": {
            "exact_semantic_duplicates": "automatic_first_source_line_wins",
            "conflicting_variants": "explicit_source_line_and_record_sha256_decision_required",
            "research_field_mutation": "forbidden; retained physical JSONL lines are byte-preserved",
        },
        "original": {
            "sha256": original_sha,
            "byte_count": len(original),
            "record_count": len(rows),
            "backup_path": None,
        },
        "canonical": {
            "sha256": canonical_sha,
            "byte_count": len(canonical),
            "record_count": len(rows) - len(quarantined),
        },
        "kept_source_lines": kept_lines,
        "quarantined_source_lines": [row.line_number for row in quarantined],
        "quarantined_records": quarantine_details,
        "counts": {
            "exact_duplicate_rows": exact_count,
            "conflicting_variant_rows": variant_count,
            "total_quarantined_rows": len(quarantined),
        },
        "decision_manifest": (
            {
                "source_path": str(bundle.path),
                "sha256": bundle.sha256,
                "backup_path": None,
                "decisions": consumed_decisions,
            }
            if bundle is not None
            else None
        ),
        "verification": {"before": before_scan, "after_planning": after_scan, "after_apply": None},
        "rollback": {"performed": False, "verified_original_sha256": False, "error": None},
    }
    return RepairPlan(original, canonical, manifest, tuple(quarantined), bundle)


def _write_once(path: Path, payload: bytes) -> bool:
    """Create immutable content-addressed data, or verify an identical prior copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("xb")
    except FileExistsError:
        existing = path.read_bytes()
        if existing != payload:
            raise RepairError(f"Refusing to overwrite non-matching immutable artifact: {path}") from None
        return False
    with handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.repair-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _write_manifest(audit_dir: Path, manifest: dict[str, Any]) -> tuple[Path, str]:
    payload = _manifest_bytes(manifest)
    digest = sha256_bytes(payload)
    path = audit_dir / "manifests" / f"{digest}.json"
    _write_once(path, payload)
    return path.resolve(), digest


def _load_audit_manifest(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = sha256_bytes(raw)
    if path.stem != digest:
        raise RepairError(f"Audit manifest filename/content SHA mismatch: {path}")
    try:
        payload = strict_json_loads(raw.decode("utf-8"), source=str(path))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RepairError(f"Invalid audit manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RepairError(f"Audit manifest must be an object: {path}")
    return payload, digest


def _unresolved_prepared_manifests(audit_dir: Path, ledger: Path) -> list[tuple[dict[str, Any], Path, str]]:
    manifest_dir = audit_dir / "manifests"
    if not manifest_dir.is_dir():
        return []
    ledger_path = str(ledger.resolve())
    prepared: dict[str, tuple[dict[str, Any], Path, str]] = {}
    terminal_prepared: set[str] = set()
    terminal_statuses = {"applied", "recovered_applied", "rolled_back"}
    for path in sorted(manifest_dir.glob("*.json")):
        payload, digest = _load_audit_manifest(path)
        if payload.get("ledger_path") != ledger_path:
            continue
        if payload.get("status") == "prepared":
            prepared[digest] = (payload, path.resolve(), digest)
            continue
        pointer = payload.get("prepared_manifest")
        if payload.get("status") in terminal_statuses and isinstance(pointer, dict):
            prepared_sha = str(pointer.get("sha256") or "")
            if SHA256_RE.fullmatch(prepared_sha):
                terminal_prepared.add(prepared_sha)
    return [item for digest, item in prepared.items() if digest not in terminal_prepared]


def _verified_prepared_payload(
    prepared: dict[str, Any],
    *,
    prepared_path: Path,
    prepared_sha: str,
    ledger: Path,
    run_date: str,
    authorization_reason: str,
    decision_path: Path | None,
    expected_original_sha256: str | None,
) -> tuple[bytes, bytes]:
    if prepared.get("schema_version") != AUDIT_SCHEMA_VERSION or prepared.get("status") != "prepared":
        raise RepairError(f"Unsupported prepared repair manifest: {prepared_path}")
    if prepared.get("ledger_path") != str(ledger.resolve()):
        raise RepairError(f"Prepared repair targets a different ledger: {prepared_path}")
    if prepared.get("run_date") != run_date:
        raise RepairError(
            f"Unfinished prepared repair uses run date {prepared.get('run_date')!r}; retry with that exact date"
        )
    if str(prepared.get("authorization_reason") or "") != authorization_reason.strip():
        raise RepairError("Authorization reason does not match the unfinished prepared repair")
    original = prepared.get("original")
    canonical = prepared.get("canonical")
    if not isinstance(original, dict) or not isinstance(canonical, dict):
        raise RepairError(f"Prepared repair lacks original/canonical metadata: {prepared_path}")
    original_sha = str(original.get("sha256") or "")
    canonical_sha = str(canonical.get("sha256") or "")
    if not SHA256_RE.fullmatch(original_sha) or not SHA256_RE.fullmatch(canonical_sha):
        raise RepairError(f"Prepared repair contains invalid ledger digests: {prepared_path}")
    if expected_original_sha256 is not None and expected_original_sha256 != original_sha:
        raise RepairError("Expected original SHA does not match the unfinished prepared repair")
    backup_path = Path(str(original.get("backup_path") or ""))
    if not backup_path.is_file():
        raise RepairError(f"Prepared repair original backup is missing: {backup_path}")
    original_raw = backup_path.read_bytes()
    if sha256_bytes(original_raw) != original_sha:
        raise RepairError(f"Prepared repair original backup SHA mismatch: {backup_path}")

    quarantined = prepared.get("quarantined_records")
    if not isinstance(quarantined, list) or not quarantined:
        raise RepairError(f"Prepared repair has no quarantined records: {prepared_path}")
    removed_lines: set[int] = set()
    for index, item in enumerate(quarantined):
        if not isinstance(item, dict):
            raise RepairError(f"Prepared repair quarantined_records[{index}] must be an object")
        source_line = item.get("source_line")
        if isinstance(source_line, bool) or not isinstance(source_line, int) or source_line < 1:
            raise RepairError(f"Prepared repair has invalid quarantine source line at index {index}")
        if source_line in removed_lines:
            raise RepairError(f"Prepared repair repeats quarantined source line {source_line}")
        artifact_path = Path(str(item.get("artifact_path") or ""))
        raw_line_sha = str(item.get("raw_line_sha256") or "")
        if not artifact_path.is_file() or not SHA256_RE.fullmatch(raw_line_sha):
            raise RepairError(f"Prepared repair quarantine artifact is missing or invalid: {artifact_path}")
        artifact = artifact_path.read_bytes()
        if sha256_bytes(artifact) != raw_line_sha:
            raise RepairError(f"Prepared repair quarantine artifact SHA mismatch: {artifact_path}")
        removed_lines.add(source_line)

    segments = original_raw.splitlines(keepends=True)
    if removed_lines and max(removed_lines) > len(segments):
        raise RepairError("Prepared repair quarantine line exceeds the original ledger length")
    canonical_raw = b"".join(
        segment for line_number, segment in enumerate(segments, start=1) if line_number not in removed_lines
    )
    if sha256_bytes(canonical_raw) != canonical_sha:
        raise RepairError("Prepared repair cannot reproduce its authorized canonical ledger")

    decision = prepared.get("decision_manifest")
    if decision is not None:
        if not isinstance(decision, dict):
            raise RepairError("Prepared repair decision_manifest must be an object or null")
        decision_sha = str(decision.get("sha256") or "")
        decision_backup = Path(str(decision.get("backup_path") or ""))
        if not decision_backup.is_file() or not SHA256_RE.fullmatch(decision_sha):
            raise RepairError(f"Prepared repair decision backup is missing or invalid: {decision_backup}")
        if sha256_bytes(decision_backup.read_bytes()) != decision_sha:
            raise RepairError(f"Prepared repair decision backup SHA mismatch: {decision_backup}")
        if decision_path is not None and sha256_bytes(decision_path.read_bytes()) != decision_sha:
            raise RepairError("Supplied decision manifest does not match the unfinished prepared repair")
    elif decision_path is not None:
        raise RepairError("Supplied decision manifest is not part of the unfinished prepared repair")

    if not SHA256_RE.fullmatch(prepared_sha) or sha256_bytes(prepared_path.read_bytes()) != prepared_sha:
        raise RepairError(f"Prepared repair manifest integrity check failed: {prepared_path}")
    return original_raw, canonical_raw


def _terminalize_recovered_repair(
    *,
    ledger: Path,
    audit_dir: Path,
    prepared: dict[str, Any],
    prepared_path: Path,
    prepared_sha: str,
    original_raw: bytes,
    canonical_raw: bytes,
    verifier: Callable[[Path, str], dict[str, Any]],
) -> dict[str, Any]:
    original_sha = str(prepared["original"]["sha256"])
    canonical_sha = str(prepared["canonical"]["sha256"])
    current_sha = sha256_bytes(ledger.read_bytes())
    if current_sha not in {original_sha, canonical_sha}:
        raise RepairError(
            "Prediction ledger SHA matches neither the prepared original nor canonical state; manual audit required"
        )
    resumed_from = "original" if current_sha == original_sha else "canonical"
    replacement_attempted = False
    try:
        if current_sha == original_sha:
            replacement_attempted = True
            _atomic_replace(ledger, canonical_raw)
        verified = verifier(ledger, canonical_sha)
        recovered = json.loads(json.dumps(prepared, ensure_ascii=False, allow_nan=False))
        recovered["status"] = "recovered_applied"
        recovered["verification"]["after_apply"] = verified
        recovered["prepared_manifest"] = {"path": str(prepared_path), "sha256": prepared_sha}
        recovered["recovery"] = {
            "performed": True,
            "resumed_from": resumed_from,
            "ledger_replacement_performed": replacement_attempted,
            "terminal_disposition": "canonical_verified_and_audited",
        }
        recovered["writes_performed"] = True
        recovered_path, recovered_sha = _write_manifest(audit_dir, recovered)
        recovered["audit_manifest_path"] = str(recovered_path)
        recovered["audit_manifest_sha256"] = recovered_sha
        return recovered
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            if sha256_bytes(ledger.read_bytes()) != original_sha:
                _atomic_replace(ledger, original_raw)
            if sha256_bytes(ledger.read_bytes()) != original_sha:
                raise RepairError("Recovery rollback did not restore the prepared original SHA")
        except Exception as caught:
            rollback_error = caught
        rolled_back = json.loads(json.dumps(prepared, ensure_ascii=False, allow_nan=False))
        rolled_back["status"] = "rolled_back"
        rolled_back["prepared_manifest"] = {"path": str(prepared_path), "sha256": prepared_sha}
        rolled_back["recovery"] = {
            "performed": True,
            "resumed_from": resumed_from,
            "terminal_disposition": "verification_failed_original_restored",
        }
        rolled_back["rollback"] = {
            "performed": True,
            "verified_original_sha256": rollback_error is None,
            "error": str(rollback_error) if rollback_error is not None else str(exc),
        }
        try:
            _write_manifest(audit_dir, rolled_back)
        except Exception:
            pass
        if rollback_error is not None:
            raise RepairError(
                f"Prepared repair recovery failed ({exc}); automatic rollback also failed ({rollback_error})"
            ) from exc
        raise RepairError(f"Prepared repair recovery failed and original ledger was restored: {exc}") from exc


def verify_ledger(ledger: Path, expected_sha256: str) -> dict[str, Any]:
    raw = ledger.read_bytes()
    observed = sha256_bytes(raw)
    if observed != expected_sha256:
        raise RepairError(f"Canonical ledger SHA mismatch: expected {expected_sha256}, observed {observed}")
    scan = duplicate_scan(raw, ledger)
    if scan["duplicate_identity_groups"] != 0 or scan["variant_conflict_groups"] != 0:
        raise RepairError("Canonical ledger verification found residual duplicate or conflicting identities")
    return {"sha256": observed, "zero_duplicate_identities": True, "scan": scan}


def _artifact_locations(plan: RepairPlan, audit_dir: Path) -> tuple[Path, list[tuple[LedgerRow, Path]], Path | None]:
    original_sha = plan.manifest["original"]["sha256"]
    backup_path = audit_dir / "backups" / f"{original_sha}.jsonl"
    quarantine = [
        (row, audit_dir / "quarantine" / f"{sha256_bytes(row.raw_line)}.jsonl")
        for row in plan.quarantined_rows
    ]
    decision_path = (
        audit_dir / "decisions" / f"{plan.decision_bundle.sha256}.json"
        if plan.decision_bundle is not None
        else None
    )
    return backup_path, quarantine, decision_path


def _manifest_with_artifacts(plan: RepairPlan, audit_dir: Path, status: str) -> dict[str, Any]:
    manifest = json.loads(json.dumps(plan.manifest, ensure_ascii=False, allow_nan=False))
    manifest["status"] = status
    backup_path, quarantine, decision_path = _artifact_locations(plan, audit_dir)
    manifest["original"]["backup_path"] = str(backup_path.resolve())
    quarantine_paths = {row.line_number: str(path.resolve()) for row, path in quarantine}
    for item in manifest["quarantined_records"]:
        item["artifact_path"] = quarantine_paths[item["source_line"]]
    if manifest["decision_manifest"] is not None and decision_path is not None:
        manifest["decision_manifest"]["backup_path"] = str(decision_path.resolve())
    return manifest


def run_repair(
    *,
    mode: str,
    ledger: Path,
    audit_dir: Path,
    run_date: str,
    authorization_reason: str,
    decision_path: Path | None = None,
    expected_original_sha256: str | None = None,
    verifier: Callable[[Path, str], dict[str, Any]] = verify_ledger,
) -> dict[str, Any]:
    if mode not in {"dry-run", "apply"}:
        raise RepairError("mode must be dry-run or apply")
    validate_run_date(run_date)
    if not authorization_reason.strip():
        raise RepairError("An explicit authorization reason is required")
    if expected_original_sha256 is not None and not SHA256_RE.fullmatch(expected_original_sha256):
        raise RepairError("expected_original_sha256 must be a lowercase SHA-256")

    if mode == "dry-run":
        plan = build_repair_plan(ledger, run_date, authorization_reason, decision_path)
        if expected_original_sha256 and plan.manifest["original"]["sha256"] != expected_original_sha256:
            raise RepairError("Prediction ledger changed since authorization")
        manifest = _manifest_with_artifacts(plan, audit_dir, "dry_run")
        manifest["writes_performed"] = False
        return manifest

    with prediction_ledger_lock(ledger):
        unfinished = _unresolved_prepared_manifests(audit_dir, ledger)
        if len(unfinished) > 1:
            raise RepairError(
                "Multiple unfinished prepared repairs target this ledger; manual audit is required before retry"
            )
        if unfinished:
            prepared, prepared_path, prepared_sha = unfinished[0]
            original_raw, canonical_raw = _verified_prepared_payload(
                prepared,
                prepared_path=prepared_path,
                prepared_sha=prepared_sha,
                ledger=ledger,
                run_date=run_date,
                authorization_reason=authorization_reason,
                decision_path=decision_path,
                expected_original_sha256=expected_original_sha256,
            )
            return _terminalize_recovered_repair(
                ledger=ledger,
                audit_dir=audit_dir,
                prepared=prepared,
                prepared_path=prepared_path,
                prepared_sha=prepared_sha,
                original_raw=original_raw,
                canonical_raw=canonical_raw,
                verifier=verifier,
            )
        plan = build_repair_plan(ledger, run_date, authorization_reason, decision_path)
        original_sha = plan.manifest["original"]["sha256"]
        canonical_sha = plan.manifest["canonical"]["sha256"]
        if expected_original_sha256 and original_sha != expected_original_sha256:
            raise RepairError("Prediction ledger changed since authorization")
        if not plan.quarantined_rows:
            verified = verifier(ledger, canonical_sha)
            manifest = _manifest_with_artifacts(plan, audit_dir, "unchanged")
            manifest["verification"]["after_apply"] = verified
            manifest["writes_performed"] = False
            return manifest

        backup_path, quarantine, decision_backup = _artifact_locations(plan, audit_dir)
        _write_once(backup_path, plan.original)
        for row, path in quarantine:
            _write_once(path, row.raw_line)
        if decision_backup is not None and plan.decision_bundle is not None:
            _write_once(decision_backup, plan.decision_bundle.raw)

        prepared = _manifest_with_artifacts(plan, audit_dir, "prepared")
        prepared_path, prepared_sha = _write_manifest(audit_dir, prepared)
        replacement_attempted = False
        try:
            replacement_attempted = True
            _atomic_replace(ledger, plan.canonical)
            verified = verifier(ledger, canonical_sha)
            applied = _manifest_with_artifacts(plan, audit_dir, "applied")
            applied["verification"]["after_apply"] = verified
            applied["prepared_manifest"] = {
                "path": str(prepared_path),
                "sha256": prepared_sha,
            }
            applied["writes_performed"] = True
            applied_path, applied_sha = _write_manifest(audit_dir, applied)
            applied["audit_manifest_path"] = str(applied_path)
            applied["audit_manifest_sha256"] = applied_sha
            return applied
        except Exception as exc:
            rollback_error: Exception | None = None
            try:
                if replacement_attempted and sha256_bytes(ledger.read_bytes()) != original_sha:
                    _atomic_replace(ledger, plan.original)
                if sha256_bytes(ledger.read_bytes()) != original_sha:
                    raise RepairError("Rollback did not restore the authorized original SHA")
            except Exception as caught:
                rollback_error = caught
            rolled_back = _manifest_with_artifacts(plan, audit_dir, "rolled_back")
            rolled_back["prepared_manifest"] = {
                "path": str(prepared_path),
                "sha256": prepared_sha,
            }
            rolled_back["rollback"] = {
                "performed": replacement_attempted,
                "verified_original_sha256": rollback_error is None,
                "error": str(rollback_error) if rollback_error is not None else str(exc),
            }
            rollback_manifest_path: Path | None = None
            try:
                rollback_manifest_path, _rollback_sha = _write_manifest(audit_dir, rolled_back)
            except Exception:
                pass
            if rollback_error is not None:
                raise RepairError(
                    f"Repair verification failed ({exc}); automatic rollback also failed ({rollback_error})"
                ) from exc
            suffix = f"; rollback audit: {rollback_manifest_path}" if rollback_manifest_path else ""
            raise RepairError(f"Repair verification failed and original ledger was restored: {exc}{suffix}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("dry-run", "apply"):
        command = subparsers.add_parser(mode)
        command.add_argument("--date", required=True, dest="run_date")
        command.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
        command.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
        command.add_argument("--decisions", type=Path, dest="decision_path")
        command.add_argument("--authorization-reason", required=True)
        command.add_argument("--expected-original-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_repair(
            mode=args.mode,
            ledger=args.ledger,
            audit_dir=args.audit_dir,
            run_date=args.run_date,
            authorization_reason=args.authorization_reason,
            decision_path=args.decision_path,
            expected_original_sha256=args.expected_original_sha256,
        )
    except DecisionRequiredError as exc:
        print(
            json.dumps(
                {"status": "decision_required", "error": str(exc), "requirements": exc.requirements},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except (OSError, RepairError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
