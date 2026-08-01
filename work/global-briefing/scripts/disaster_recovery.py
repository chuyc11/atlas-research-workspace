#!/usr/bin/env python3
"""Create and restore-verify external ATLAS disaster-recovery snapshots."""

from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
import unicodedata
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "improvement_tracking.json"
LATEST_PATH = ROOT / "work" / "shared" / "atlas" / "backups" / "latest.json"
FULL_LATEST_PATH = ROOT / "work" / "shared" / "atlas" / "backups" / "full-latest.json"
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
STAGING_CLEANUP_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.25, 0.5, 1.0)
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
SENSITIVE_DIRECTORY_NAMES = {
    ".aws",
    ".azure",
    ".docker",
    ".gnupg",
    ".kube",
    ".password-store",
    ".secrets",
    ".ssh",
    ".terraform",
    ".terraform.d",
    "credentials",
    "gcloud",
    "keys",
    "secrets",
}
SENSITIVE_FILE_PATTERNS = (
    ".env",
    ".env.*",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".terraformrc",
    ".vault-token",
    "_netrc",
    "*.agekey",
    "*.credentials",
    "*.der",
    "*.jks",
    "*.kdbx",
    "*.key",
    "*.keystore",
    "*.p12",
    "*.pfx",
    "*.pem",
    "*.ppk",
    "*.secret",
    "*.token",
    "application_default_credentials.json",
    "azureprofile.json",
    "auth*.json",
    "client_secret*.json",
    "credentials*.json",
    "docker-config*.json",
    "id_dsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_rsa*",
    "oauth*.json",
    "private_key*",
    "refresh_token*",
    "secrets*.json",
    "service-account*.json",
    "token.json",
)
ENCRYPTED_MAGIC = b"ATLASDR1"
ENCRYPTED_HEADER_LENGTH_BYTES = 4
GCM_NONCE_BYTES = 12
GCM_TAG_BYTES = 16
ENCRYPTION_CHUNK_BYTES = 1024 * 1024
RESTORE_SCOPE = "configured_workspace_files_and_git_bundles"
MANIFEST_SCHEMA_VERSION = 4
LEGACY_MANIFEST_SCHEMA_VERSION = 2
METADATA_AUTHENTICATION_ALGORITHM = "HMAC-SHA256"
LEGACY_MIGRATION_REASON = "authenticated_schema_4_genesis_from_verified_legacy_schema_2"
MANIFEST_MEMBER = "_atlas_backup_manifest.json"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_FILES = 100_000
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_REQUIRED_KEYS = {
    "schema_version",
    "date",
    "created_at",
    "workspace",
    "encrypted",
    "encryption_algorithm",
    "restore_scope",
    "full_runtime_restore_expected",
    "excluded_sensitive_file_patterns",
    "previous_manifest_sha256",
    "files",
}
MANIFEST_OPTIONAL_KEYS = {
    "full_snapshot",
    "legacy_migration_genesis",
    "metadata_authentication",
    "previous_head_sha256",
    "revision",
    "snapshot_profile",
    "workspace_uuid",
}
FULL_SNAPSHOT_REFERENCE_KEYS = {
    "archive_sha256",
    "created_at",
    "date",
    "manifest_sha256",
    "restore_verified",
}
LEGACY_MIGRATION_KEYS = {
    "reason",
    "legacy_schema_version",
    "legacy_latest_manifest_path",
    "legacy_latest_manifest_sha256",
    "legacy_archive",
    "legacy_archive_sha256",
    "legacy_manifest_sidecar",
    "legacy_manifest_sha256",
    "legacy_manifest_digest_sidecar",
    "legacy_manifest_digest",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def strict_json_loads(value: str | bytes) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant is forbidden: {constant}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name, item in pairs:
            if name in payload:
                raise ValueError(f"duplicate JSON object key is forbidden: {name}")
            payload[name] = item
        return payload

    return json.loads(
        value,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )


def metadata_key(key: bytes, purpose: str) -> bytes:
    """Derive a purpose-specific metadata MAC key without reusing the AES key directly."""
    return hmac.new(key, f"ATLAS-DR-METADATA-V1:{purpose}".encode("ascii"), hashlib.sha256).digest()


def metadata_authentication(payload: dict[str, Any], key: bytes, purpose: str) -> dict[str, str]:
    unsigned = {name: value for name, value in payload.items() if name != "metadata_authentication"}
    mac = hmac.new(
        metadata_key(key, purpose),
        json.dumps(
            unsigned,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "algorithm": METADATA_AUTHENTICATION_ALGORITHM,
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "value": mac,
    }


def authenticate_metadata(payload: dict[str, Any], key: bytes, purpose: str) -> None:
    authentication = payload.get("metadata_authentication")
    if not isinstance(authentication, dict) or set(authentication) != {"algorithm", "key_id", "value"}:
        raise ValueError(f"{purpose} metadata authentication is missing or malformed")
    expected = metadata_authentication(payload, key, purpose)
    if (
        authentication.get("algorithm") != METADATA_AUTHENTICATION_ALGORITHM
        or authentication.get("key_id") != expected["key_id"]
        or not isinstance(authentication.get("value"), str)
        or not hmac.compare_digest(authentication["value"], expected["value"])
    ):
        raise ValueError(f"{purpose} metadata authentication failed")


def signed_metadata(payload: dict[str, Any], key: bytes | None, purpose: str) -> dict[str, Any]:
    signed = dict(payload)
    if key is not None:
        signed["metadata_authentication"] = metadata_authentication(signed, key, purpose)
    return signed


def retry_staging_cleanup(function: Any, path: str, _error: BaseException) -> None:
    """Retry transient Windows handle/AV races without abandoning plaintext staging."""
    attempts = (0.0, *STAGING_CLEANUP_RETRY_DELAYS_SECONDS)
    last_error: OSError | None = None
    for delay in attempts:
        if delay:
            time.sleep(delay)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        try:
            function(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


@contextlib.contextmanager
def private_staging_directory(parent: Path, *, prefix: str) -> Iterator[Path]:
    """Keep plaintext restore material beside the protected target, never in the OS temp root."""
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()
    try:
        with contextlib.suppress(OSError):
            staging.chmod(0o700)
        yield staging
    finally:
        # Best-effort overwrite is not a guarantee on copy-on-write media, but it limits
        # ordinary recovery; authenticated encryption remains the durable protection.
        if staging.exists():
            candidates = list(staging.rglob("*"))
            for candidate in candidates:
                with contextlib.suppress(OSError):
                    candidate.chmod(0o700 if candidate.is_dir() else 0o600)
            for candidate in candidates:
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                with contextlib.suppress(OSError):
                    size = candidate.stat().st_size
                    with candidate.open("r+b", buffering=0) as handle:
                        zeroes = b"\0" * min(ENCRYPTION_CHUNK_BYTES, max(size, 1))
                        remaining = size
                        while remaining:
                            chunk = zeroes[: min(len(zeroes), remaining)]
                            handle.write(chunk)
                            remaining -= len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())

            shutil.rmtree(staging, ignore_errors=False, onexc=retry_staging_cleanup)


def sanitized_subprocess_environment(
    secret_names: Iterable[str] = (),
    *,
    encryption_key: bytes | None = None,
) -> dict[str, str]:
    explicit = {name.casefold() for name in secret_names if name}
    encoded_key = base64.b64encode(encryption_key).decode("ascii") if encryption_key is not None else None
    return {
        name: value
        for name, value in os.environ.items()
        if name.casefold() not in explicit
        and not ("BACKUP" in name.upper() and ("KEY" in name.upper() or "SECRET" in name.upper()))
        and (encoded_key is None or not hmac.compare_digest(value, encoded_key))
    }


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            with contextlib.suppress(OSError):
                os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_bytes(
        path,
        (
            json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
    )


def atomic_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    atomic_bytes(path, value.encode(encoding))


def backup_lock_metadata_path(target: Path) -> Path:
    return target / "atlas-backup.lock"


def backup_lock_guard_path(target: Path) -> Path:
    return target / "atlas-backup.lock.guard"


def _lock_contention(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN} or getattr(exc, "winerror", None) in {
        32,
        33,
    }


def acquire_backup_lock_guard(path: Path) -> int | None:
    """Acquire the OS-held disaster-recovery writer lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        if _lock_contention(exc):
            return None
        raise RuntimeError(f"unable to acquire disaster-recovery OS lock: {path}") from exc
    return descriptor


def release_backup_lock_guard(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def disaster_recovery_lock(target: Path) -> Iterator[None]:
    """Serialize snapshot creation without stale-file unlink races."""

    target.mkdir(parents=True, exist_ok=True)
    metadata_path = backup_lock_metadata_path(target)
    descriptor = acquire_backup_lock_guard(backup_lock_guard_path(target))
    if descriptor is None:
        raise RuntimeError("disaster-recovery snapshot creation is already running")
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": utc_now(),
        "token": token,
    }
    try:
        atomic_json(metadata_path, payload)
        yield
    finally:
        try:
            current = read_json(metadata_path) if metadata_path.exists() else {}
        except (OSError, ValueError):
            current = {}
        if current.get("token") == token:
            metadata_path.unlink(missing_ok=True)
        release_backup_lock_guard(descriptor)


DR_HEAD_SCHEMA_VERSION = 1
TRUST_ANCHOR_ROOT_ENV = "ATLAS_TRUST_ANCHOR_ROOT"
TRUST_ANCHOR_NAMESPACE_ENV = "ATLAS_TRUST_ANCHOR_NAMESPACE"


def dr_integrity_enabled(root: Path) -> bool:
    """Production checkouts require a local independent monotonic DR head."""

    return (root / "atlas.py").is_file()


def configured_environment_value(name: str) -> str | None:
    if name in os.environ:
        return os.environ.get(name)
    return persistent_user_environment_value(name)


def dr_workspace_uuid(root: Path) -> str:
    namespace = str(configured_environment_value(TRUST_ANCHOR_NAMESPACE_ENV) or "").strip()
    identity = f"{namespace}:{str(root.resolve()).casefold()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atlas-workspace:{identity}"))


def dr_trust_root(root: Path) -> Path:
    configured = configured_environment_value(TRUST_ANCHOR_ROOT_ENV)
    candidate = Path(configured).expanduser() if configured else Path.home() / ".atlas-trust"
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return resolved
    raise ValueError("disaster-recovery trust root must be outside the workspace")


def dr_head_path(root: Path, snapshot_profile: str) -> Path:
    return dr_trust_root(root) / "dr-heads" / (
        f"{dr_workspace_uuid(root)}-{snapshot_profile}.json"
    )


def dr_head_versions_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.versions")


def dr_head_hash(payload: dict[str, Any]) -> str:
    unsigned = {
        key: value
        for key, value in payload.items()
        if key not in {"head_sha256", "metadata_authentication"}
    }
    return stable_json_sha256(unsigned)


def dr_latest_binding(latest: dict[str, Any]) -> str:
    return stable_json_sha256(
        {
            key: latest.get(key)
            for key in (
                "archive_sha256",
                "created_at",
                "date",
                "manifest_sha256",
                "previous_head_sha256",
                "previous_manifest_sha256",
                "revision",
                "snapshot_profile",
                "workspace_uuid",
            )
        }
    )


def verify_dr_integrity_head(
    latest: dict[str, Any],
    *,
    root: Path,
    snapshot_profile: str,
    encryption_key: bytes | None,
) -> dict[str, Any] | None:
    """Authenticate the external high-water mark and exact latest binding."""

    if not dr_integrity_enabled(root):
        return None
    if encryption_key is None:
        raise ValueError("disaster-recovery monotonic head requires an encryption key")
    path = dr_head_path(root, snapshot_profile)
    if not path.is_file():
        if latest.get("workspace_uuid") is not None:
            raise ValueError("disaster-recovery monotonic head is missing")
        return None
    head = read_json(path)
    authenticate_metadata(head, encryption_key, "dr-monotonic-head")
    if head.get("schema_version") != DR_HEAD_SCHEMA_VERSION:
        raise ValueError("disaster-recovery monotonic head schema is invalid")
    if head.get("head_sha256") != dr_head_hash(head):
        raise ValueError("disaster-recovery monotonic head hash mismatch")
    if head.get("workspace_uuid") != dr_workspace_uuid(root):
        raise ValueError("disaster-recovery workspace UUID mismatch")
    if head.get("snapshot_profile") != snapshot_profile:
        raise ValueError("disaster-recovery monotonic head profile mismatch")
    if head.get("latest_binding_sha256") != dr_latest_binding(latest):
        raise ValueError("disaster-recovery latest was rolled back or replaced")
    if latest.get("workspace_uuid") != head.get("workspace_uuid"):
        raise ValueError("disaster-recovery latest workspace UUID mismatch")
    if latest.get("revision") != head.get("revision"):
        raise ValueError("disaster-recovery latest revision mismatch")
    versions: list[dict[str, Any]] = []
    for version_path in sorted(dr_head_versions_path(path).glob("*.json")):
        version = read_json(version_path)
        authenticate_metadata(version, encryption_key, "dr-monotonic-head")
        if version.get("head_sha256") != dr_head_hash(version):
            raise ValueError("disaster-recovery retained head hash mismatch")
        versions.append(version)
    if not versions:
        raise ValueError("disaster-recovery retained head history is missing")
    versions.sort(key=lambda item: int(item.get("revision") or -1))
    for previous, current in zip(versions, versions[1:], strict=False):
        if current.get("revision") != previous.get("revision") + 1:
            raise ValueError("disaster-recovery retained revisions are not contiguous")
        if current.get("previous_head_sha256") != previous.get("head_sha256"):
            raise ValueError("disaster-recovery retained predecessor hash mismatch")
    if (
        versions[-1].get("revision") != head.get("revision")
        or versions[-1].get("head_sha256") != head.get("head_sha256")
    ):
        raise ValueError("disaster-recovery head pointer was rolled back")
    return head


def next_dr_integrity_fields(
    previous: dict[str, Any],
    *,
    date: str,
    root: Path,
    snapshot_profile: str,
    encryption_key: bytes | None,
) -> dict[str, Any]:
    if dr_integrity_enabled(root) and not previous and dr_head_path(root, snapshot_profile).exists():
        raise ValueError("disaster-recovery latest is missing below its authenticated high-water mark")
    head = verify_dr_integrity_head(
        previous,
        root=root,
        snapshot_profile=snapshot_profile,
        encryption_key=encryption_key,
    ) if previous else None
    if head is not None and date < str(head.get("date") or ""):
        raise ValueError("disaster-recovery date is below its authenticated high-water mark")
    return {
        "workspace_uuid": dr_workspace_uuid(root),
        "revision": int(head.get("revision") or 0) + 1 if head else 1,
        "previous_head_sha256": head.get("head_sha256") if head else None,
    }


def commit_dr_integrity_head(
    latest: dict[str, Any],
    *,
    root: Path,
    snapshot_profile: str,
    encryption_key: bytes | None,
) -> dict[str, Any] | None:
    if not dr_integrity_enabled(root):
        return None
    if encryption_key is None:
        raise ValueError("disaster-recovery monotonic head requires an encryption key")
    path = dr_head_path(root, snapshot_profile)
    current = read_json(path) if path.exists() else None
    if current is None:
        if latest.get("previous_head_sha256") is not None:
            raise ValueError("disaster-recovery head CAS predecessor disappeared")
    else:
        authenticate_metadata(current, encryption_key, "dr-monotonic-head")
        if current.get("head_sha256") != latest.get("previous_head_sha256"):
            raise ValueError("disaster-recovery head CAS predecessor changed")
    payload = {
        "schema_version": DR_HEAD_SCHEMA_VERSION,
        "workspace_uuid": latest["workspace_uuid"],
        "revision": latest["revision"],
        "previous_head_sha256": latest.get("previous_head_sha256"),
        "date": latest["date"],
        "snapshot_profile": snapshot_profile,
        "manifest_sha256": latest["manifest_sha256"],
        "archive_sha256": latest["archive_sha256"],
        "latest_binding_sha256": dr_latest_binding(latest),
        "trust_scope": "local-only",
        "witness": {"status": "unavailable", "verified": False},
    }
    payload["head_sha256"] = dr_head_hash(payload)
    head = signed_metadata(payload, encryption_key, "dr-monotonic-head")
    version_path = dr_head_versions_path(path) / (
        f"{latest['revision']:020d}-{head['head_sha256']}.json"
    )
    atomic_json(version_path, head)
    atomic_json(path, head)
    return head


def full_latest_path_for(latest_path: Path) -> Path:
    """Keep the heavyweight recovery baseline separate from daily checkpoints."""

    if latest_path.resolve() == LATEST_PATH.resolve():
        return FULL_LATEST_PATH
    return latest_path.with_name("full-latest.json")


def read_json(path: Path) -> dict[str, Any]:
    payload = strict_json_loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def load_config(path: Path) -> dict[str, Any]:
    payload = strict_json_loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("improvement tracking configuration must be a JSON object")
    config = payload.get("disaster_recovery", {})
    if not isinstance(config, dict) or config.get("enabled") is not True:
        raise ValueError("disaster_recovery must be enabled in improvement_tracking.json")
    if not isinstance(config.get("target_directory"), str) or not config["target_directory"].strip():
        raise ValueError("disaster_recovery.target_directory must be a non-empty string")
    if "require_different_volume" in config and not isinstance(config["require_different_volume"], bool):
        raise ValueError("disaster_recovery.require_different_volume must be boolean")
    for name in ("retention_days", "minimum_snapshots_to_keep"):
        value = config.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"disaster_recovery.{name} must be a positive integer")
    for name in (
        "maximum_backup_age_hours",
        "bootstrap_maximum_backup_age_hours",
        "full_snapshot_maximum_age_hours",
    ):
        value = config.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float) or value <= 0
        ):
            raise ValueError(f"disaster_recovery.{name} must be a positive number")
    for name in ("include_paths", "daily_include_paths", "git_repositories", "daily_git_repositories"):
        value = config.get(name)
        if value is not None and (
            not isinstance(value, list | tuple)
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise ValueError(f"disaster_recovery.{name} must be a list of non-empty strings")
    encryption = config.get("encryption", {})
    if not isinstance(encryption, dict):
        raise ValueError("disaster_recovery.encryption must be a JSON object")
    if not isinstance(encryption.get("required", False), bool):
        raise ValueError("disaster_recovery.encryption.required must be boolean")
    if encryption.get("required") is True:
        if encryption.get("algorithm") != "AES-256-GCM":
            raise ValueError("required disaster-recovery encryption algorithm must be AES-256-GCM")
        if encryption.get("key_encoding", "base64") != "base64":
            raise ValueError("backup encryption key encoding must be base64")
        environment_variable = encryption.get("key_environment_variable")
        if not isinstance(environment_variable, str) or not environment_variable.strip():
            raise ValueError("encrypted disaster recovery requires key_environment_variable")
    return config


def decode_encryption_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("backup encryption key must be standard base64") from exc
    if len(key) != 32:
        raise ValueError("backup encryption key must decode to exactly 32 bytes")
    return key


def persistent_user_environment_value(variable: str) -> str | None:
    """Read a Windows CurrentUser environment value when the parent is stale.

    Windows GUI applications keep the environment block they inherited at
    startup.  A newly provisioned user-scoped backup key therefore may not be
    visible to a long-running Codex process even though it is correctly stored
    for the user.  This fallback reads only the configured value name from the
    current user's own Environment key; it never enumerates or logs variables.
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, value_type = winreg.QueryValueEx(handle, variable)
    except (OSError, ImportError):
        return None
    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
        return None
    return value


def encryption_key_for_config(config: dict[str, Any]) -> bytes | None:
    encryption = config.get("encryption", {})
    if not isinstance(encryption, dict) or encryption.get("required") is not True:
        return None
    variable = str(encryption["key_environment_variable"]).strip()
    encoded = os.environ.get(variable)
    if encoded is None:
        encoded = persistent_user_environment_value(variable)
    if not encoded:
        raise ValueError(f"required backup encryption key environment variable is not set: {variable}")
    return decode_encryption_key(encoded)


def is_encrypted_archive(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(ENCRYPTED_MAGIC)) == ENCRYPTED_MAGIC
    except OSError:
        return False


def encryption_header(key: bytes, nonce: bytes) -> bytes:
    payload = {
        "algorithm": "AES-256-GCM",
        "container_schema_version": 1,
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    return json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")


def encrypt_archive(source: Path, destination: Path, key: bytes) -> dict[str, Any]:
    nonce = os.urandom(GCM_NONCE_BYTES)
    header = encryption_header(key, nonce)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as plain, destination.open("xb") as encrypted:
        encrypted.write(ENCRYPTED_MAGIC)
        encrypted.write(struct.pack(">I", len(header)))
        encrypted.write(header)
        for chunk in iter(lambda: plain.read(ENCRYPTION_CHUNK_BYTES), b""):
            encrypted.write(encryptor.update(chunk))
        encrypted.write(encryptor.finalize())
        encrypted.write(encryptor.tag)
        encrypted.flush()
        os.fsync(encrypted.fileno())
    return strict_json_loads(header)


def decrypt_archive(source: Path, destination: Path, key: bytes) -> dict[str, Any]:
    total_size = source.stat().st_size
    minimum_size = len(ENCRYPTED_MAGIC) + ENCRYPTED_HEADER_LENGTH_BYTES + GCM_TAG_BYTES
    if total_size < minimum_size:
        raise ValueError("encrypted backup container is truncated")
    with source.open("rb") as encrypted:
        if encrypted.read(len(ENCRYPTED_MAGIC)) != ENCRYPTED_MAGIC:
            raise ValueError("backup does not use the ATLAS encrypted container format")
        raw_length = encrypted.read(ENCRYPTED_HEADER_LENGTH_BYTES)
        if len(raw_length) != ENCRYPTED_HEADER_LENGTH_BYTES:
            raise ValueError("encrypted backup header is truncated")
        header_length = struct.unpack(">I", raw_length)[0]
        if header_length <= 0 or header_length > 64 * 1024:
            raise ValueError("encrypted backup header length is invalid")
        header = encrypted.read(header_length)
        if len(header) != header_length:
            raise ValueError("encrypted backup header is truncated")
        try:
            metadata = strict_json_loads(header)
            if not isinstance(metadata, dict) or set(metadata) != {
                "algorithm",
                "container_schema_version",
                "key_id",
                "nonce",
            }:
                raise ValueError
            nonce = base64.b64decode(metadata["nonce"], validate=True)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("encrypted backup header is invalid") from exc
        if (
            metadata.get("algorithm") != "AES-256-GCM"
            or type(metadata.get("container_schema_version")) is not int
            or metadata["container_schema_version"] != 1
            or not isinstance(metadata.get("key_id"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", metadata["key_id"])
            or len(nonce) != GCM_NONCE_BYTES
        ):
            raise ValueError("encrypted backup algorithm or nonce is invalid")
        if metadata.get("key_id") != hashlib.sha256(key).hexdigest()[:16]:
            raise ValueError("backup encryption key does not match the container")
        ciphertext_start = len(ENCRYPTED_MAGIC) + ENCRYPTED_HEADER_LENGTH_BYTES + header_length
        ciphertext_length = total_size - ciphertext_start - GCM_TAG_BYTES
        if ciphertext_length < 0:
            raise ValueError("encrypted backup container is truncated")
        encrypted.seek(total_size - GCM_TAG_BYTES)
        tag = encrypted.read(GCM_TAG_BYTES)
        encrypted.seek(ciphertext_start)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        try:
            with destination.open("xb") as plain:
                remaining = ciphertext_length
                while remaining:
                    chunk = encrypted.read(min(ENCRYPTION_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("encrypted backup ciphertext is truncated")
                    remaining -= len(chunk)
                    plain.write(decryptor.update(chunk))
                plain.write(decryptor.finalize())
                plain.flush()
                os.fsync(plain.fileno())
        except InvalidTag as exc:
            destination.unlink(missing_ok=True)
            raise ValueError("encrypted backup authentication failed") from exc
    return metadata


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


def is_sensitive_path(relative_path: Path, patterns: Iterable[str] = SENSITIVE_FILE_PATTERNS) -> bool:
    directory_parts = {part.casefold() for part in relative_path.parts[:-1]}
    if directory_parts & SENSITIVE_DIRECTORY_NAMES:
        return True
    filename = relative_path.name.casefold()
    return any(fnmatch.fnmatchcase(filename, pattern.casefold()) for pattern in patterns)


def normalized_archive_path(value: Any, *, allow_workspace_root: bool = False) -> str:
    if allow_workspace_root and value == ".":
        return "."
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("archive path must be a non-empty bounded string")
    if "\\" in value or value.startswith("/") or any(ord(character) < 32 for character in value):
        raise ValueError(f"unsafe archive path: {value!r}")
    pure = PurePosixPath(value)
    if pure.as_posix() != value or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"unsafe archive path: {value!r}")
    windows_reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}
    for part in pure.parts:
        if len(part.encode("utf-8")) > 255 or part.endswith((" ", ".")):
            raise ValueError(f"unsafe archive path: {value!r}")
        if part.split(".", 1)[0].upper() in windows_reserved or ":" in part:
            raise ValueError(f"unsafe archive path: {value!r}")
    return value


def canonical_path_identity(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def validate_legacy_migration_genesis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != LEGACY_MIGRATION_KEYS:
        raise ValueError("legacy migration genesis metadata is missing or malformed")
    if value.get("reason") != LEGACY_MIGRATION_REASON:
        raise ValueError("legacy migration genesis reason is invalid")
    if (
        type(value.get("legacy_schema_version")) is not int
        or value["legacy_schema_version"] != LEGACY_MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError("legacy migration genesis schema is unsupported")
    path_names = (
        "legacy_latest_manifest_path",
        "legacy_archive",
        "legacy_manifest_sidecar",
        "legacy_manifest_digest_sidecar",
    )
    paths: dict[str, Path] = {}
    for name in path_names:
        raw_path = value.get(name)
        if not isinstance(raw_path, str) or not raw_path or "\0" in raw_path or not Path(raw_path).is_absolute():
            raise ValueError(f"legacy migration genesis {name} is invalid")
        paths[name] = Path(raw_path).resolve()
    for name in (
        "legacy_latest_manifest_sha256",
        "legacy_archive_sha256",
        "legacy_manifest_sha256",
        "legacy_manifest_digest",
    ):
        digest = value.get(name)
        if not isinstance(digest, str) or not HEX_SHA256.fullmatch(digest):
            raise ValueError(f"legacy migration genesis {name} is invalid")
    archive = paths["legacy_archive"]
    if paths["legacy_manifest_sidecar"] != archive.with_suffix(".manifest.json"):
        raise ValueError("legacy migration genesis manifest sidecar path is inconsistent")
    if paths["legacy_manifest_digest_sidecar"] != archive.with_suffix(".manifest.sha256"):
        raise ValueError("legacy migration genesis digest sidecar path is inconsistent")
    expected_latest = archive.with_suffix(f".legacy-latest-schema-{LEGACY_MANIFEST_SCHEMA_VERSION}.json")
    if paths["legacy_latest_manifest_path"] != expected_latest:
        raise ValueError("legacy migration genesis preserved latest path is inconsistent")
    if value["legacy_manifest_digest"] != value["legacy_manifest_sha256"]:
        raise ValueError("legacy migration genesis manifest digest is inconsistent")
    return value


def validate_full_snapshot_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != FULL_SNAPSHOT_REFERENCE_KEYS:
        raise ValueError("full snapshot reference is missing or malformed")
    for name in ("archive_sha256", "manifest_sha256"):
        if not isinstance(value.get(name), str) or not HEX_SHA256.fullmatch(value[name]):
            raise ValueError(f"full snapshot reference {name} is invalid")
    try:
        if datetime.strptime(str(value.get("date")), "%Y-%m-%d").strftime("%Y-%m-%d") != value["date"]:
            raise ValueError
    except ValueError as exc:
        raise ValueError("full snapshot reference date is invalid") from exc
    try:
        created_at = str(value.get("created_at"))
        if not created_at.endswith("Z"):
            raise ValueError
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("full snapshot reference timestamp is invalid") from exc
    if value.get("restore_verified") is not True:
        raise ValueError("full snapshot reference was not restore-verified")
    return value


def validate_manifest_files(files: Any) -> list[dict[str, Any]]:
    if not isinstance(files, list) or not files:
        raise ValueError("backup manifest files must be a non-empty list")
    if len(files) > MAX_MANIFEST_FILES:
        raise ValueError("backup manifest contains too many file entries")
    identities: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError(f"backup manifest file entry {index} must be an object")
        kind = item.get("kind")
        expected_keys = {"path", "size", "sha256", "kind"}
        if kind == "git_bundle":
            expected_keys.add("repository")
        if set(item) != expected_keys or kind not in {"workspace_file", "git_bundle"}:
            raise ValueError(f"backup manifest file entry {index} has an invalid schema")
        path = normalized_archive_path(item.get("path"))
        if path == MANIFEST_MEMBER:
            raise ValueError("backup manifest member path is reserved")
        identity = canonical_path_identity(path)
        if identity in identities:
            raise ValueError(f"backup manifest contains a duplicate path: {path}")
        identities.add(identity)
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"backup manifest file size is invalid: {path}")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not HEX_SHA256.fullmatch(digest):
            raise ValueError(f"backup manifest file hash is invalid: {path}")
        if kind == "git_bundle":
            normalized_archive_path(item.get("repository"), allow_workspace_root=True)
        validated.append(item)
    return validated


def validate_legacy_manifest_v2(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise ValueError("legacy backup manifest must be a JSON object")
    required = {"schema_version", "date", "created_at", "workspace", "previous_manifest_sha256", "files"}
    optional = {"legacy_predecessor"}
    if set(manifest) - required - optional or required - set(manifest):
        raise ValueError("legacy backup manifest schema mismatch")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != LEGACY_MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported legacy backup manifest schema: {manifest.get('schema_version')!r}")
    date = manifest.get("date")
    try:
        if not isinstance(date, str) or datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("legacy backup manifest date must use YYYY-MM-DD") from exc
    created_at = manifest.get("created_at")
    try:
        if not isinstance(created_at, str) or not created_at.endswith("Z"):
            raise ValueError
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("legacy backup manifest created_at must be an ISO-8601 UTC timestamp") from exc
    workspace = manifest.get("workspace")
    if (
        not isinstance(workspace, str)
        or not workspace.strip()
        or "\0" in workspace
        or not Path(workspace).is_absolute()
    ):
        raise ValueError("legacy backup manifest workspace must be an absolute path")
    previous_hash = manifest.get("previous_manifest_sha256")
    if previous_hash is not None and (not isinstance(previous_hash, str) or not HEX_SHA256.fullmatch(previous_hash)):
        raise ValueError("legacy backup manifest predecessor hash is invalid")
    predecessor = manifest.get("legacy_predecessor")
    if predecessor is not None:
        if not isinstance(predecessor, dict) or set(predecessor) != {"schema_version", "date", "archive_sha256"}:
            raise ValueError("legacy backup manifest predecessor metadata is invalid")
        if type(predecessor.get("schema_version")) is not int or predecessor["schema_version"] != 1:
            raise ValueError("legacy backup manifest predecessor schema is invalid")
        archive_hash = predecessor.get("archive_sha256")
        if not isinstance(archive_hash, str) or not HEX_SHA256.fullmatch(archive_hash):
            raise ValueError("legacy backup manifest predecessor hash is invalid")
    return validate_manifest_files(manifest.get("files"))


def validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise ValueError("backup manifest must be a JSON object")
    keys = set(manifest)
    unexpected = keys - MANIFEST_REQUIRED_KEYS - MANIFEST_OPTIONAL_KEYS
    missing = MANIFEST_REQUIRED_KEYS - keys
    if missing or unexpected:
        raise ValueError(
            "backup manifest schema mismatch"
            + (f"; missing={sorted(missing)}" if missing else "")
            + (f"; unexpected={sorted(unexpected)}" if unexpected else "")
        )
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported backup manifest schema: {manifest.get('schema_version')!r}")
    date = manifest.get("date")
    try:
        if not isinstance(date, str) or datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("backup manifest date must use YYYY-MM-DD") from exc
    created_at = manifest.get("created_at")
    try:
        if not isinstance(created_at, str) or not created_at.endswith("Z"):
            raise ValueError
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("backup manifest created_at must be an ISO-8601 UTC timestamp") from exc
    workspace = manifest.get("workspace")
    if (
        not isinstance(workspace, str)
        or not workspace.strip()
        or "\0" in workspace
        or not Path(workspace).is_absolute()
    ):
        raise ValueError("backup manifest workspace must be an absolute path")
    workspace_uuid = manifest.get("workspace_uuid")
    revision = manifest.get("revision")
    previous_head = manifest.get("previous_head_sha256")
    if any(value is not None for value in (workspace_uuid, revision, previous_head)):
        try:
            if str(uuid.UUID(str(workspace_uuid))) != workspace_uuid:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("backup manifest workspace UUID is invalid") from exc
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("backup manifest monotonic revision is invalid")
        if previous_head is not None and (
            not isinstance(previous_head, str) or not HEX_SHA256.fullmatch(previous_head)
        ):
            raise ValueError("backup manifest previous head hash is invalid")
    encrypted = manifest.get("encrypted")
    if not isinstance(encrypted, bool):
        raise ValueError("backup manifest encrypted must be boolean")
    expected_algorithm = "AES-256-GCM" if encrypted else None
    if manifest.get("encryption_algorithm") != expected_algorithm:
        raise ValueError("backup manifest encryption algorithm is inconsistent")
    if manifest.get("restore_scope") != RESTORE_SCOPE or manifest.get("full_runtime_restore_expected") is not False:
        raise ValueError("backup manifest restore scope is invalid")
    snapshot_profile = manifest.get("snapshot_profile", "full")
    if snapshot_profile not in {"daily", "full"}:
        raise ValueError("backup manifest snapshot profile is invalid")
    full_snapshot = manifest.get("full_snapshot")
    if full_snapshot is not None:
        if snapshot_profile != "daily":
            raise ValueError("only daily snapshots may reference a full snapshot")
        validate_full_snapshot_reference(full_snapshot)
    patterns = manifest.get("excluded_sensitive_file_patterns")
    if not isinstance(patterns, list) or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in patterns
    ):
        raise ValueError("backup manifest sensitive patterns must be non-empty strings")
    previous_hash = manifest.get("previous_manifest_sha256")
    if previous_hash is not None and (not isinstance(previous_hash, str) or not HEX_SHA256.fullmatch(previous_hash)):
        raise ValueError("backup manifest predecessor hash is invalid")
    if encrypted:
        authentication = manifest.get("metadata_authentication")
        if not isinstance(authentication, dict) or set(authentication) != {"algorithm", "key_id", "value"}:
            raise ValueError("encrypted backup manifest authentication is missing or malformed")
        if (
            authentication.get("algorithm") != METADATA_AUTHENTICATION_ALGORITHM
            or not isinstance(authentication.get("key_id"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", authentication["key_id"])
            or not isinstance(authentication.get("value"), str)
            or not HEX_SHA256.fullmatch(authentication["value"])
        ):
            raise ValueError("encrypted backup manifest authentication is malformed")
    elif "metadata_authentication" in manifest:
        raise ValueError("unencrypted backup manifest must not claim metadata authentication")
    migration = manifest.get("legacy_migration_genesis")
    if migration is not None:
        if not encrypted:
            raise ValueError("legacy migration genesis requires an encrypted schema-4 manifest")
        validate_legacy_migration_genesis(migration)
    return validate_manifest_files(manifest.get("files"))


def iter_files(
    root: Path,
    includes: Iterable[str],
    *,
    sensitive_patterns: Iterable[str] = SENSITIVE_FILE_PATTERNS,
) -> list[Path]:
    files: set[Path] = set()
    root_resolved = root.resolve()
    latest_resolved = (root / "work" / "shared" / "atlas" / "backups").resolve()
    for relative in includes:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            continue
        if not candidate.exists():
            continue
        candidates = [candidate] if candidate.is_file() else candidate.rglob("*")
        for path in candidates:
            if (
                not path.is_file()
                or path.is_symlink()
                or any(part.casefold() in EXCLUDED_PARTS for part in path.parts)
            ):
                continue
            try:
                relative_path = path.resolve().relative_to(root_resolved)
            except ValueError:
                continue
            if any(excluded == relative_path or excluded in relative_path.parents for excluded in EXCLUDED_RELATIVE_ROOTS):
                continue
            if is_sensitive_path(relative_path, sensitive_patterns):
                continue
            try:
                path.resolve().relative_to(latest_resolved)
                continue
            except ValueError:
                pass
            files.add(path.resolve())
    return sorted(files, key=lambda value: value.as_posix())


def verify_archive_detailed(
    archive: Path,
    manifest: dict[str, Any],
    *,
    encryption_key: bytes | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    git = shutil.which("git")
    encrypted = is_encrypted_archive(archive)
    encrypted_container_authenticated = False
    manifest_items: list[dict[str, Any]] = []
    try:
        manifest_items = validate_manifest(manifest)
    except ValueError as exc:
        errors.append(str(exc))
    if not errors and bool(manifest["encrypted"]) != encrypted:
        errors.append("backup container encryption does not match the manifest")
    if not errors and encrypted:
        if encryption_key is None:
            errors.append("encrypted backup verification requires the encryption key")
        else:
            try:
                authenticate_metadata(manifest, encryption_key, "manifest")
            except ValueError as exc:
                errors.append(str(exc))
    with private_staging_directory(archive.parent.resolve(), prefix=".atlas-restore-") as temporary_root:
        restore_root = temporary_root / "restore"
        restore_root.mkdir()
        zip_archive = archive
        if not errors and encrypted:
            zip_archive = temporary_root / "decrypted.zip"
            try:
                decrypt_archive(archive, zip_archive, encryption_key)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
            else:
                encrypted_container_authenticated = True
        if not errors:
            try:
                with zipfile.ZipFile(zip_archive) as bundle:
                    members = bundle.infolist()
                    member_identities: set[str] = set()
                    actual_names: set[str] = set()
                    expected_names = {item["path"] for item in manifest_items} | {MANIFEST_MEMBER}
                    for member in members:
                        try:
                            safe_name = normalized_archive_path(member.filename)
                        except ValueError:
                            errors.append(f"unsafe member: {member.filename}")
                            continue
                        identity = canonical_path_identity(safe_name)
                        if identity in member_identities:
                            errors.append(f"duplicate archive member: {member.filename}")
                        member_identities.add(identity)
                        actual_names.add(safe_name)
                        mode = member.external_attr >> 16
                        if member.is_dir() or (member.create_system == 3 and mode and not stat.S_ISREG(mode)):
                            errors.append(f"non-regular archive member: {member.filename}")
                    missing_members = sorted(expected_names - actual_names)
                    unexpected_members = sorted(actual_names - expected_names)
                    if missing_members:
                        errors.append("missing archive members: " + ", ".join(missing_members))
                    if unexpected_members:
                        errors.append("unexpected archive members: " + ", ".join(unexpected_members))
                    info_by_name = {member.filename: member for member in members}
                    embedded_info = info_by_name.get(MANIFEST_MEMBER)
                    if embedded_info is not None and embedded_info.file_size > MAX_MANIFEST_BYTES:
                        errors.append("embedded manifest exceeds the size limit")
                    for item in manifest_items:
                        info = info_by_name.get(item["path"])
                        if info is not None and info.file_size != item["size"]:
                            errors.append(f"size mismatch: {item['path']}")
                    if not errors:
                        bad_member = bundle.testzip()
                        if bad_member:
                            errors.append(f"corrupt member: {bad_member}")
                    if not errors:
                        try:
                            embedded = strict_json_loads(bundle.read(MANIFEST_MEMBER))
                            if not isinstance(embedded, dict):
                                raise ValueError("embedded manifest must be a JSON object")
                        except (KeyError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                            errors.append(f"invalid embedded manifest: {exc}")
                        else:
                            if stable_json_sha256(embedded) != stable_json_sha256(manifest):
                                errors.append("embedded manifest hash mismatch")
                    if not errors:
                        for item in manifest_items:
                            destination = (restore_root / item["path"]).resolve()
                            destination.relative_to(restore_root)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            with bundle.open(item["path"]) as source, destination.open("xb") as output:
                                shutil.copyfileobj(source, output, length=ENCRYPTION_CHUNK_BYTES)
            except (OSError, zipfile.BadZipFile) as exc:
                errors.append(f"invalid backup archive: {exc}")
        if not errors:
            for item in manifest_items:
                restored = restore_root / item["path"]
                if not restored.is_file():
                    errors.append(f"missing: {item['path']}")
                elif restored.stat().st_size != item["size"]:
                    errors.append(f"size mismatch: {item['path']}")
                elif sha256(restored) != item["sha256"]:
                    errors.append(f"hash mismatch: {item['path']}")
        archive_integrity_verified = not errors
        if archive_integrity_verified:
            git_environment = sanitized_subprocess_environment(encryption_key=encryption_key)
            for index, item in enumerate(manifest_items):
                if item.get("kind") == "git_bundle":
                    restored = restore_root / item["path"]
                    if not git:
                        errors.append(f"cannot verify git bundle without git: {item['path']}")
                        continue
                    verification_repository = temporary_root / f"bundle-verify-{index}.git"
                    restored_repository = temporary_root / f"bundle-restore-{index}.git"
                    commands = (
                        [git, "init", "--bare", str(verification_repository)],
                        [git, "-C", str(verification_repository), "bundle", "verify", str(restored)],
                        [git, "clone", "--mirror", str(restored), str(restored_repository)],
                        [git, "-C", str(restored_repository), "fsck", "--full", "--strict"],
                    )
                    try:
                        for command in commands:
                            completed = subprocess.run(
                                command,
                                text=True,
                                capture_output=True,
                                check=False,
                                timeout=300,
                                env=git_environment,
                            )
                            if completed.returncode != 0:
                                detail = completed.stderr.strip() or completed.stdout.strip()
                                errors.append(
                                    f"invalid git bundle: {item['path']}"
                                    + (f" ({detail})" if detail else "")
                                )
                                break
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        errors.append(f"invalid git bundle: {item['path']} ({exc})")
    selected_restore_verified = not errors
    return {
        "verified": selected_restore_verified,
        "archive_integrity_verified": archive_integrity_verified,
        "restore_verified": selected_restore_verified,
        "restore_scope": RESTORE_SCOPE,
        "full_runtime_restore_verified": False,
        "encrypted": encrypted,
        "encrypted_container_authenticated": encrypted_container_authenticated if encrypted else None,
        "verification_errors": errors,
    }


def verify_archive(
    archive: Path,
    manifest: dict[str, Any],
    *,
    encryption_key: bytes | None = None,
) -> tuple[bool, list[str]]:
    verification = verify_archive_detailed(archive, manifest, encryption_key=encryption_key)
    return bool(verification["verified"]), list(verification["verification_errors"])


def verify_legacy_archive_v2(archive: Path, manifest: dict[str, Any]) -> None:
    """Verify a schema-2 ZIP and every referenced member without modifying legacy state."""
    manifest_items = validate_legacy_manifest_v2(manifest)
    if is_encrypted_archive(archive) or archive.suffix.casefold() != ".zip":
        raise ValueError("legacy schema-2 archive must be an unencrypted ZIP")
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            identities: set[str] = set()
            names: set[str] = set()
            expected_names = {item["path"] for item in manifest_items} | {MANIFEST_MEMBER}
            for member in members:
                safe_name = normalized_archive_path(member.filename)
                identity = canonical_path_identity(safe_name)
                if identity in identities:
                    raise ValueError(f"duplicate legacy archive member: {member.filename}")
                identities.add(identity)
                names.add(safe_name)
                mode = member.external_attr >> 16
                if member.is_dir() or (member.create_system == 3 and mode and not stat.S_ISREG(mode)):
                    raise ValueError(f"non-regular legacy archive member: {member.filename}")
            if names != expected_names:
                raise ValueError("legacy archive members do not match its manifest")
            info_by_name = {member.filename: member for member in members}
            embedded_info = info_by_name[MANIFEST_MEMBER]
            if embedded_info.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("legacy embedded manifest exceeds the size limit")
            embedded = strict_json_loads(bundle.read(MANIFEST_MEMBER))
            if not isinstance(embedded, dict) or stable_json_sha256(embedded) != stable_json_sha256(manifest):
                raise ValueError("legacy embedded manifest hash mismatch")
            for item in manifest_items:
                info = info_by_name[item["path"]]
                if info.file_size != item["size"]:
                    raise ValueError(f"legacy archive size mismatch: {item['path']}")
                digest = hashlib.sha256()
                with bundle.open(info) as source:
                    for chunk in iter(lambda: source.read(ENCRYPTION_CHUNK_BYTES), b""):
                        digest.update(chunk)
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError(f"legacy archive hash mismatch: {item['path']}")
    except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise ValueError(f"legacy backup archive integrity check failed: {exc}") from exc


def verify_legacy_snapshot_v2(
    previous: dict[str, Any],
    latest_path: Path,
) -> dict[str, Any]:
    if read_json(latest_path) != previous:
        raise ValueError("legacy latest metadata changed before migration verification")
    if type(previous.get("schema_version")) is not int or previous["schema_version"] != LEGACY_MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported previous disaster-recovery schema: {previous.get('schema_version')!r}")
    if previous.get("verified") is not True or previous.get("restore_verified") is not True:
        raise ValueError("legacy disaster-recovery snapshot was not restore-verified")
    if previous.get("verification_errors") != []:
        raise ValueError("legacy disaster-recovery snapshot has unresolved verification errors")
    archive_value = previous.get("archive")
    archive_hash = previous.get("archive_sha256")
    sidecar_value = previous.get("manifest_sidecar")
    manifest_hash = previous.get("manifest_sha256")
    if (
        not isinstance(archive_value, str)
        or not archive_value
        or not isinstance(archive_hash, str)
        or not HEX_SHA256.fullmatch(archive_hash)
        or not isinstance(sidecar_value, str)
        or not sidecar_value
        or not isinstance(manifest_hash, str)
        or not HEX_SHA256.fullmatch(manifest_hash)
    ):
        raise ValueError("legacy disaster-recovery latest metadata is malformed")
    archive = Path(archive_value).resolve()
    manifest_sidecar = Path(sidecar_value).resolve()
    digest_sidecar = archive.with_suffix(".manifest.sha256")
    if manifest_sidecar != archive.with_suffix(".manifest.json"):
        raise ValueError("legacy disaster-recovery sidecar path is inconsistent")
    if not archive.is_file() or not manifest_sidecar.is_file() or not digest_sidecar.is_file():
        raise ValueError("legacy disaster-recovery snapshot is missing")
    if sha256(archive) != archive_hash:
        raise ValueError("legacy disaster-recovery archive integrity check failed")
    manifest = read_json(manifest_sidecar)
    manifest_items = validate_legacy_manifest_v2(manifest)
    if stable_json_sha256(manifest) != manifest_hash:
        raise ValueError("legacy disaster-recovery manifest integrity check failed")
    try:
        digest = digest_sidecar.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("legacy disaster-recovery manifest digest is unreadable") from exc
    if digest != manifest_hash:
        raise ValueError("legacy disaster-recovery manifest digest sidecar is invalid")
    if previous.get("date") != manifest.get("date"):
        raise ValueError("legacy disaster-recovery metadata date is inconsistent")
    if (
        previous.get("file_count") != len(manifest_items)
        or previous.get("git_bundle_count") != sum(item["kind"] == "git_bundle" for item in manifest_items)
        or previous.get("total_source_bytes") != sum(item["size"] for item in manifest_items)
    ):
        raise ValueError("legacy disaster-recovery latest metadata is inconsistent")
    verify_legacy_archive_v2(archive, manifest)
    preserved_latest = archive.with_suffix(f".legacy-latest-schema-{LEGACY_MANIFEST_SCHEMA_VERSION}.json")
    migration = {
        "reason": LEGACY_MIGRATION_REASON,
        "legacy_schema_version": LEGACY_MANIFEST_SCHEMA_VERSION,
        "legacy_latest_manifest_path": str(preserved_latest),
        "legacy_latest_manifest_sha256": sha256(latest_path),
        "legacy_archive": str(archive),
        "legacy_archive_sha256": archive_hash,
        "legacy_manifest_sidecar": str(manifest_sidecar),
        "legacy_manifest_sha256": manifest_hash,
        "legacy_manifest_digest_sidecar": str(digest_sidecar),
        "legacy_manifest_digest": digest,
    }
    return validate_legacy_migration_genesis(migration)


def preserve_legacy_latest_manifest(latest_path: Path, migration: dict[str, Any]) -> None:
    """Preserve the mutable legacy latest pointer once, without overwriting any artifact."""
    payload = latest_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != migration["legacy_latest_manifest_sha256"]:
        raise ValueError("legacy latest metadata changed during migration")
    preserved = Path(migration["legacy_latest_manifest_path"])
    if preserved.exists():
        if (
            preserved.is_symlink()
            or not preserved.is_file()
            or sha256(preserved) != migration["legacy_latest_manifest_sha256"]
        ):
            raise ValueError("preserved legacy latest metadata conflicts with the migration genesis")
        return
    preserved.parent.mkdir(parents=True, exist_ok=True)
    try:
        with preserved.open("xb") as handle:
            with contextlib.suppress(OSError):
                os.chmod(preserved, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        if (
            preserved.is_symlink()
            or not preserved.is_file()
            or sha256(preserved) != migration["legacy_latest_manifest_sha256"]
        ):
            raise ValueError("preserved legacy latest metadata conflicts with the migration genesis") from exc


def assert_legacy_migration_artifacts_unchanged(
    migration: dict[str, Any],
    *,
    source_latest_path: Path | None = None,
) -> None:
    validate_legacy_migration_genesis(migration)
    checks = (
        (Path(migration["legacy_latest_manifest_path"]), migration["legacy_latest_manifest_sha256"]),
        (Path(migration["legacy_archive"]), migration["legacy_archive_sha256"]),
    )
    for path, expected_hash in checks:
        if path.is_symlink() or not path.is_file() or sha256(path) != expected_hash:
            raise ValueError(f"legacy migration artifact changed: {path}")
    if source_latest_path is not None and sha256(source_latest_path) != migration["legacy_latest_manifest_sha256"]:
        raise ValueError("legacy latest metadata changed during migration")
    manifest = read_json(Path(migration["legacy_manifest_sidecar"]))
    if stable_json_sha256(manifest) != migration["legacy_manifest_sha256"]:
        raise ValueError("legacy migration manifest changed")
    digest = Path(migration["legacy_manifest_digest_sidecar"]).read_text(encoding="ascii").strip()
    if digest != migration["legacy_manifest_digest"]:
        raise ValueError("legacy migration manifest digest changed")


def create_git_bundles(
    root: Path,
    repositories: Iterable[str],
    stage: Path,
    *,
    secret_environment_variables: Iterable[str] = (),
) -> list[tuple[str, Path, str]]:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git executable is unavailable")
    entries: list[tuple[str, Path, str]] = []
    bundle_names: set[str] = set()
    environment = sanitized_subprocess_environment(secret_environment_variables)
    for relative in repositories:
        normalized_archive_path(relative, allow_workspace_root=True)
        repo = (root / relative).resolve()
        try:
            repo.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"configured git repository escapes the workspace: {relative}") from exc
        if not (repo / ".git").exists():
            raise ValueError(f"configured git repository is missing: {relative}")
        name = "workspace-root" if relative in {"", "."} else str(relative).replace("\\", "-").replace("/", "-")
        if canonical_path_identity(name) in bundle_names:
            raise ValueError(f"configured git repositories produce a duplicate bundle name: {relative}")
        bundle_names.add(canonical_path_identity(name))
        bundle_path = stage / f"{name}.bundle"
        completed = subprocess.run(
            [git, "-C", str(repo), "bundle", "create", str(bundle_path), "--all"],
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
            env=environment,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git bundle failed for {relative}: {completed.stderr.strip()}")
        entries.append((f"_atlas_git_bundles/{bundle_path.name}", bundle_path, str(relative)))
    return entries


def verify_previous_snapshot(
    previous: dict[str, Any],
    encryption_key: bytes | None,
    *,
    deep_restore: bool = True,
    expected_workspace: Path | None = None,
) -> str:
    schema_version = previous.get("schema_version")
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            "previous disaster-recovery snapshot predates authenticated schema 4 and cannot be chained"
        )
    previous_encrypted = previous.get("encrypted")
    if not isinstance(previous_encrypted, bool):
        raise ValueError("previous disaster-recovery encryption metadata is invalid")
    if not previous_encrypted:
        raise ValueError("previous snapshot must be AES-256-GCM encrypted and authenticated before chaining")
    if encryption_key is None:
        raise ValueError("previous encrypted disaster-recovery snapshot requires its encryption key")
    authenticate_metadata(previous, encryption_key, "latest")
    previous_migration = previous.get("legacy_migration_genesis")
    if previous_migration is not None:
        validate_legacy_migration_genesis(previous_migration)
    archive_value = previous.get("archive")
    sidecar_value = previous.get("manifest_sidecar")
    archive_hash = previous.get("archive_sha256")
    manifest_hash = previous.get("manifest_sha256")
    if (
        not isinstance(archive_value, str)
        or not archive_value
        or not isinstance(sidecar_value, str)
        or not sidecar_value
        or not isinstance(archive_hash, str)
        or not HEX_SHA256.fullmatch(archive_hash)
        or not isinstance(manifest_hash, str)
        or not HEX_SHA256.fullmatch(manifest_hash)
    ):
        raise ValueError("previous disaster-recovery metadata is malformed")
    previous_archive = Path(archive_value).resolve()
    previous_manifest_path = Path(sidecar_value).resolve()
    expected_sidecar = previous_archive.with_suffix(".manifest.json")
    expected_digest = previous_archive.with_suffix(".manifest.sha256")
    if previous_manifest_path != expected_sidecar:
        raise ValueError("previous disaster-recovery sidecar path is inconsistent")
    if not previous_archive.is_file() or not previous_manifest_path.is_file() or not expected_digest.is_file():
        raise ValueError("previous disaster-recovery snapshot is missing")
    if sha256(previous_archive) != archive_hash:
        raise ValueError("previous disaster-recovery archive integrity check failed")
    previous_manifest = read_json(previous_manifest_path)
    if stable_json_sha256(previous_manifest) != manifest_hash:
        raise ValueError("previous disaster-recovery manifest integrity check failed")
    previous_items = validate_manifest(previous_manifest)
    if expected_workspace is not None and Path(str(previous_manifest.get("workspace"))).resolve() != expected_workspace.resolve():
        raise ValueError("previous disaster-recovery snapshot belongs to a different workspace")
    if expected_digest.read_text(encoding="ascii").strip() != manifest_hash:
        raise ValueError("previous disaster-recovery manifest digest sidecar is invalid")
    if bool(previous_manifest["encrypted"]) != previous_encrypted:
        raise ValueError("previous disaster-recovery metadata encryption state is inconsistent")
    if previous.get("date") != previous_manifest.get("date"):
        raise ValueError("previous disaster-recovery metadata date is inconsistent")
    if previous.get("snapshot_profile", "full") != previous_manifest.get("snapshot_profile", "full"):
        raise ValueError("previous disaster-recovery snapshot profile is inconsistent")
    if previous.get("full_snapshot") != previous_manifest.get("full_snapshot"):
        raise ValueError("previous disaster-recovery full snapshot reference is inconsistent")
    for field in ("workspace_uuid", "revision", "previous_head_sha256"):
        if previous.get(field) != previous_manifest.get(field):
            raise ValueError(f"previous disaster-recovery {field} is inconsistent")
    expected_bundle_count = sum(item["kind"] == "git_bundle" for item in previous_items)
    expected_total_bytes = sum(item["size"] for item in previous_items)
    if (
        previous.get("previous_manifest_sha256") != previous_manifest.get("previous_manifest_sha256")
        or previous_migration != previous_manifest.get("legacy_migration_genesis")
        or previous.get("file_count") != len(previous_items)
        or previous.get("git_bundle_count") != expected_bundle_count
        or previous.get("total_source_bytes") != expected_total_bytes
        or previous.get("archive_format") != "atlas-aes-gcm-v1"
        or previous.get("encryption_algorithm") != "AES-256-GCM"
        or previous.get("verified") is not True
        or previous.get("archive_integrity_verified") is not True
        or previous.get("restore_verified") is not True
        or previous.get("encrypted_container_authenticated") is not True
    ):
        raise ValueError("previous disaster-recovery latest metadata is inconsistent")
    if deep_restore:
        verification = verify_archive_detailed(
            previous_archive,
            previous_manifest,
            encryption_key=encryption_key,
        )
        if not verification["verified"]:
            raise ValueError(
                "previous disaster-recovery restore verification failed: "
                + "; ".join(verification["verification_errors"])
            )
    return manifest_hash


def resolve_full_snapshot_reference(
    reference_value: Any,
    *,
    root: Path,
    config: dict[str, Any],
    encryption_key: bytes | None,
) -> dict[str, Any]:
    """Resolve an immutable full baseline by its signed content hashes.

    ``full-latest.json`` is only the current head. A daily checkpoint must keep
    referring to the exact full archive it was created against even after a newer
    periodic full snapshot advances that head.
    """

    reference = validate_full_snapshot_reference(reference_value)
    if encryption_key is None:
        raise ValueError("referenced full snapshot requires its encryption key")
    target = resolve_target(str(config["target_directory"]), root)
    matching_digests: list[Path] = []
    for digest_path in sorted(target.glob("atlas-backup-*.manifest.sha256")):
        try:
            digest = digest_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if digest == reference["manifest_sha256"]:
            matching_digests.append(digest_path)
    if not matching_digests:
        raise ValueError("referenced full snapshot manifest is missing")

    matches: list[dict[str, Any]] = []
    for digest_path in matching_digests:
        manifest_path = digest_path.with_suffix(".json")
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_json(manifest_path)
            validate_manifest(manifest)
            authenticate_metadata(manifest, encryption_key, "manifest")
        except (OSError, ValueError):
            continue
        if (
            stable_json_sha256(manifest) != reference["manifest_sha256"]
            or manifest.get("snapshot_profile", "full") != "full"
            or manifest.get("full_snapshot") is not None
            or manifest.get("encrypted") is not True
            or manifest.get("date") != reference["date"]
            or manifest.get("created_at") != reference["created_at"]
        ):
            continue
        base_name = digest_path.name.removesuffix(".manifest.sha256")
        for suffix in (".atlasdr", ".zip"):
            archive = (target / f"{base_name}{suffix}").resolve()
            if not archive.is_file():
                continue
            try:
                archive.relative_to(root.resolve())
            except ValueError:
                pass
            else:
                continue
            if sha256(archive) != reference["archive_sha256"]:
                continue
            matches.append(
                {
                    "archive": archive,
                    "archive_sha256": reference["archive_sha256"],
                    "manifest": manifest,
                    "manifest_path": manifest_path.resolve(),
                    "manifest_sha256": reference["manifest_sha256"],
                    "reference": reference,
                }
            )
    if not matches:
        raise ValueError("referenced full snapshot archive is missing or changed")
    unique_archives = {str(item["archive"]) for item in matches}
    if len(unique_archives) != 1:
        raise ValueError("referenced full snapshot resolves ambiguously")
    return matches[0]


def create_snapshot(
    *,
    date: str,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    latest_path: Path = LATEST_PATH,
    snapshot_profile: str = "daily",
) -> dict[str, Any]:
    """Create a snapshot while one OS-held writer lock protects its CAS boundary."""

    config = load_config(config_path)
    target = resolve_target(str(config["target_directory"]), root)
    with disaster_recovery_lock(target):
        return _create_snapshot_locked(
            date=date,
            root=root,
            config_path=config_path,
            latest_path=latest_path,
            snapshot_profile=snapshot_profile,
        )


def _create_snapshot_locked(
    *,
    date: str,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    latest_path: Path = LATEST_PATH,
    snapshot_profile: str = "daily",
) -> dict[str, Any]:
    started_monotonic = time.monotonic()
    if snapshot_profile not in {"daily", "full"}:
        raise ValueError("snapshot_profile must be daily or full")
    config = load_config(config_path)
    encryption = config.get("encryption", {})
    encryption = encryption if isinstance(encryption, dict) else {}
    encryption_key = encryption_key_for_config(config)
    encryption_required = encryption_key is not None
    encryption_key_variable = (
        str(encryption.get("key_environment_variable", "")).strip() if encryption_required else ""
    )
    target = resolve_target(str(config["target_directory"]), root)
    different_volume = is_different_volume(target, root)
    if config.get("require_different_volume") is True and not different_volume:
        raise ValueError("backup target must be on a different filesystem volume")
    target.mkdir(parents=True, exist_ok=True)
    extra_sensitive_patterns = config.get("sensitive_exclude_patterns", [])
    if not isinstance(extra_sensitive_patterns, list) or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in extra_sensitive_patterns
    ):
        raise ValueError("sensitive_exclude_patterns must be a list of non-empty strings")
    sensitive_patterns = (*SENSITIVE_FILE_PATTERNS, *extra_sensitive_patterns)
    include_paths = (
        config.get("daily_include_paths")
        if snapshot_profile == "daily" and config.get("daily_include_paths") is not None
        else config.get("include_paths")
    ) or DEFAULT_INCLUDES
    if not isinstance(include_paths, list | tuple) or not all(
        isinstance(value, str) and value.strip() for value in include_paths
    ):
        raise ValueError("include_paths must be a list of non-empty strings")
    git_repositories = (
        config.get("daily_git_repositories")
        if snapshot_profile == "daily" and config.get("daily_git_repositories") is not None
        else config.get("git_repositories", [])
    )
    if not isinstance(git_repositories, list) or not all(
        isinstance(value, str) and value.strip() for value in git_repositories
    ):
        raise ValueError("git_repositories must be a list of non-empty strings")
    files = iter_files(
        root,
        include_paths,
        sensitive_patterns=sensitive_patterns,
    )
    if not files:
        raise ValueError("no disaster-recovery files selected")
    selection_finished = time.monotonic()
    created_at = utc_now()
    effective_latest_path = (
        full_latest_path_for(latest_path) if snapshot_profile == "full" else latest_path
    )
    expected_latest_sha256 = sha256(effective_latest_path) if effective_latest_path.exists() else None
    previous = read_json(effective_latest_path) if effective_latest_path.exists() else {}
    previous_manifest_sha256: str | None = None
    legacy_migration_genesis: dict[str, Any] | None = None
    legacy_source_latest_path: Path | None = None
    if previous:
        previous_schema = previous.get("schema_version")
        if type(previous_schema) is not int:
            raise ValueError(f"unsupported previous disaster-recovery schema: {previous_schema!r}")
        if previous_schema == MANIFEST_SCHEMA_VERSION:
            previous_manifest_sha256 = verify_previous_snapshot(
                previous,
                encryption_key,
                deep_restore=False,
                expected_workspace=root,
            )
            if (
                snapshot_profile == "daily"
                and previous.get("snapshot_profile", "full") == "full"
            ):
                baseline_path = full_latest_path_for(latest_path)
                if not baseline_path.exists():
                    atomic_json(baseline_path, previous)
            inherited_migration = previous.get("legacy_migration_genesis")
            if inherited_migration is not None:
                legacy_migration_genesis = validate_legacy_migration_genesis(inherited_migration)
                assert_legacy_migration_artifacts_unchanged(legacy_migration_genesis)
        elif previous_schema == LEGACY_MANIFEST_SCHEMA_VERSION:
            if encryption_key is None:
                raise ValueError("legacy schema-2 migration requires a valid AES-256-GCM backup key")
            legacy_migration_genesis = verify_legacy_snapshot_v2(previous, effective_latest_path)
            preserve_legacy_latest_manifest(effective_latest_path, legacy_migration_genesis)
            legacy_source_latest_path = effective_latest_path
        else:
            raise ValueError(f"unsupported previous disaster-recovery schema: {previous_schema!r}")
    integrity_fields = (
        next_dr_integrity_fields(
            previous,
            date=date,
            root=root,
            snapshot_profile=snapshot_profile,
            encryption_key=encryption_key,
        )
        if dr_integrity_enabled(root)
        else {}
    )
    full_snapshot_reference: dict[str, Any] | None = None
    if snapshot_profile == "daily" and config.get("full_snapshot_maximum_age_hours") is not None:
        baseline_path = full_latest_path_for(latest_path)
        if not baseline_path.is_file():
            raise ValueError("daily snapshot requires a verified full recovery baseline")
        baseline = read_json(baseline_path)
        baseline_manifest_hash = verify_previous_snapshot(
            baseline,
            encryption_key,
            deep_restore=False,
            expected_workspace=root,
        )
        if baseline.get("snapshot_profile", "full") != "full":
            raise ValueError("daily snapshot recovery baseline is not a full snapshot")
        try:
            baseline_created = datetime.fromisoformat(
                str(baseline.get("created_at")).replace("Z", "+00:00")
            )
            if baseline_created.tzinfo is None:
                raise ValueError
            baseline_age_hours = (
                datetime.now(UTC) - baseline_created.astimezone(UTC)
            ).total_seconds() / 3600
        except (TypeError, ValueError) as exc:
            raise ValueError("full recovery baseline timestamp is invalid") from exc
        maximum_full_age = float(config["full_snapshot_maximum_age_hours"])
        if baseline_age_hours < 0 or baseline_age_hours > maximum_full_age:
            raise ValueError("full recovery baseline is outside the configured freshness window")
        full_snapshot_reference = validate_full_snapshot_reference(
            {
                "archive_sha256": baseline["archive_sha256"],
                "created_at": baseline["created_at"],
                "date": baseline["date"],
                "manifest_sha256": baseline_manifest_hash,
                "restore_verified": baseline.get("restore_verified") is True,
            }
        )
    predecessor_finished = time.monotonic()
    with private_staging_directory(target, prefix=".atlas-create-") as staging:
        bundle_entries = create_git_bundles(
            root,
            git_repositories,
            staging,
            secret_environment_variables=[encryption_key_variable],
        )
        archive_entries: list[tuple[str, Path, str, str | None]] = [
            (path.relative_to(root.resolve()).as_posix(), path, "workspace_file", None)
            for path in files
        ]
        archive_entries.extend(
            (archive_path, path, "git_bundle", repository)
            for archive_path, path, repository in bundle_entries
        )
        manifest_payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "date": date,
            "created_at": created_at,
            "workspace": str(root.resolve()),
            **integrity_fields,
            "encrypted": encryption_required,
            "encryption_algorithm": "AES-256-GCM" if encryption_required else None,
            "restore_scope": RESTORE_SCOPE,
            "full_runtime_restore_expected": False,
            "snapshot_profile": snapshot_profile,
            **({"full_snapshot": full_snapshot_reference} if full_snapshot_reference is not None else {}),
            "excluded_sensitive_file_patterns": list(sensitive_patterns),
            "previous_manifest_sha256": previous_manifest_sha256,
            **({"legacy_migration_genesis": legacy_migration_genesis} if legacy_migration_genesis is not None else {}),
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
        manifest = signed_metadata(manifest_payload, encryption_key, "manifest")
        validate_manifest(manifest)
        suffix = ".atlasdr" if encryption_required else ".zip"
        archive = target / f"atlas-backup-{date}-{datetime.now(UTC).strftime('%H%M%S%f')}{suffix}"
        temporary_zip = staging / "snapshot.zip"
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for archive_path, path, _kind, _repository in archive_entries:
                bundle.write(path, archive_path)
            bundle.writestr(
                MANIFEST_MEMBER,
                json.dumps(manifest, allow_nan=False, ensure_ascii=False, indent=2),
            )
        temporary_archive = staging / f"final{suffix}"
        try:
            if encryption_required:
                encryption_metadata = encrypt_archive(temporary_zip, temporary_archive, encryption_key)
            else:
                encryption_metadata = {}
                with temporary_zip.open("rb") as source, temporary_archive.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=ENCRYPTION_CHUNK_BYTES)
                    destination.flush()
                    os.fsync(destination.fileno())
            temporary_archive.replace(archive)
        finally:
            temporary_archive.unlink(missing_ok=True)
    archive_finished = time.monotonic()
    archive_sha256 = sha256(archive)
    verification_started = time.monotonic()
    verification = verify_archive_detailed(archive, manifest, encryption_key=encryption_key)
    verification_finished = time.monotonic()
    if not archive.is_file() or sha256(archive) != archive_sha256:
        verification["verified"] = False
        verification["archive_integrity_verified"] = False
        verification["restore_verified"] = False
        verification["verification_errors"].append("backup archive changed during restore verification")
    if legacy_migration_genesis is not None:
        try:
            assert_legacy_migration_artifacts_unchanged(
                legacy_migration_genesis,
                source_latest_path=legacy_source_latest_path,
            )
        except (OSError, ValueError) as exc:
            archive.unlink(missing_ok=True)
            raise ValueError(f"legacy migration verification failed: {exc}") from exc
    verified = bool(verification["verified"])
    errors = list(verification["verification_errors"])
    manifest_sha256 = stable_json_sha256(manifest)
    manifest_sidecar = archive.with_suffix(".manifest.json")
    manifest_digest_sidecar = archive.with_suffix(".manifest.sha256")
    if not verified:
        archive.unlink(missing_ok=True)
        raise RuntimeError("restore drill failed: " + "; ".join(errors))
    try:
        atomic_json(manifest_sidecar, manifest)
        atomic_text(manifest_digest_sidecar, manifest_sha256 + "\n", encoding="ascii")
    except Exception:
        archive.unlink(missing_ok=True)
        manifest_sidecar.unlink(missing_ok=True)
        manifest_digest_sidecar.unlink(missing_ok=True)
        raise
    result_payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "date": date,
        "created_at": created_at,
        **integrity_fields,
        "snapshot_profile": snapshot_profile,
        **({"full_snapshot": full_snapshot_reference} if full_snapshot_reference is not None else {}),
        "archive": str(archive.resolve()),
        "archive_sha256": archive_sha256,
        "archive_format": "atlas-aes-gcm-v1" if encryption_required else "zip",
        "encrypted": encryption_required,
        "encryption_algorithm": "AES-256-GCM" if encryption_required else None,
        "encryption_key_environment_variable": (
            str(encryption.get("key_environment_variable")) if encryption_required else None
        ),
        "encryption_key_id": encryption_metadata.get("key_id"),
        "manifest_sha256": manifest_sha256,
        "previous_manifest_sha256": previous_manifest_sha256,
        "legacy_previous_archive_sha256": None,
        **({"legacy_migration_genesis": legacy_migration_genesis} if legacy_migration_genesis is not None else {}),
        "manifest_sidecar": str(manifest_sidecar.resolve()),
        "file_count": len(manifest["files"]),
        "git_bundle_count": sum(item.get("kind") == "git_bundle" for item in manifest["files"]),
        "total_source_bytes": sum(item["size"] for item in manifest["files"]),
        "verified": verified,
        "archive_integrity_verified": verification["archive_integrity_verified"],
        "restore_verified": verification["restore_verified"],
        "restore_scope": verification["restore_scope"],
        "full_runtime_restore_verified": verification["full_runtime_restore_verified"],
        "encrypted_container_authenticated": verification["encrypted_container_authenticated"],
        "verification_errors": errors,
        "retention_days": int(config.get("retention_days") or 14),
        "target_outside_workspace": True,
        "target_on_different_volume": different_volume,
        "timings_seconds": {
            "selection": round(selection_finished - started_monotonic, 3),
            "predecessor_authentication": round(predecessor_finished - selection_finished, 3),
            "archive_build_and_encryption": round(archive_finished - predecessor_finished, 3),
            "archive_hash": round(verification_started - archive_finished, 3),
            "restore_verification": round(verification_finished - verification_started, 3),
            "total": round(verification_finished - started_monotonic, 3),
        },
    }
    result = signed_metadata(result_payload, encryption_key, "latest")
    actual_latest_sha256 = sha256(effective_latest_path) if effective_latest_path.exists() else None
    if actual_latest_sha256 != expected_latest_sha256:
        raise RuntimeError("disaster-recovery latest CAS failed: latest changed during snapshot creation")
    previous_latest_bytes = effective_latest_path.read_bytes() if effective_latest_path.exists() else None
    atomic_json(effective_latest_path, result)
    try:
        commit_dr_integrity_head(
            result,
            root=root,
            snapshot_profile=snapshot_profile,
            encryption_key=encryption_key,
        )
    except Exception:
        if previous_latest_bytes is None:
            effective_latest_path.unlink(missing_ok=True)
        else:
            atomic_bytes(effective_latest_path, previous_latest_bytes)
        raise
    cutoff = datetime.now(UTC).timestamp() - result["retention_days"] * 86400
    minimum_snapshots = int(config.get("minimum_snapshots_to_keep") or 3)
    archives = sorted(
        [*target.glob("atlas-backup-*.zip"), *target.glob("atlas-backup-*.atlasdr")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    full_latest_path = full_latest_path_for(latest_path)
    full_latest = read_json(full_latest_path) if full_latest_path.exists() else {}
    full_archive_value = full_latest.get("archive") if isinstance(full_latest, dict) else None
    protected_archives = {
        Path(full_archive_value).resolve()
        for full_archive_value in [full_archive_value]
        if isinstance(full_archive_value, str) and full_archive_value
    }
    retention_cleanup_allowed = True
    if latest_path.is_file():
        try:
            daily_latest = read_json(latest_path)
            if daily_latest.get("snapshot_profile", "full") == "daily" and daily_latest.get(
                "full_snapshot"
            ) is not None:
                authenticate_metadata(daily_latest, encryption_key, "latest")
                resolved_baseline = resolve_full_snapshot_reference(
                    daily_latest["full_snapshot"],
                    root=root,
                    config=config,
                    encryption_key=encryption_key,
                )
                protected_archives.add(resolved_baseline["archive"])
        except (KeyError, OSError, TypeError, ValueError):
            # Retention must never delete through an unresolved dependency. The
            # newly verified snapshot remains usable; cleanup waits for repair.
            retention_cleanup_allowed = False
    for old in archives[minimum_snapshots:]:
        protected_legacy_archive = (
            Path(legacy_migration_genesis["legacy_archive"]).resolve() if legacy_migration_genesis is not None else None
        )
        if (
            retention_cleanup_allowed
            and old != archive
            and old.resolve() != protected_legacy_archive
            and old.resolve() not in protected_archives
            and old.stat().st_mtime < cutoff
        ):
            old.unlink()
            for suffix in (".manifest.json", ".manifest.sha256"):
                old.with_suffix(suffix).unlink(missing_ok=True)
    return result


def verify_latest_snapshot(
    *,
    root: Path = ROOT,
    config_path: Path = CONFIG_PATH,
    latest_path: Path = LATEST_PATH,
    quick: bool = False,
) -> dict[str, Any]:
    """Authenticate latest.json, optionally re-running the full restore drill.

    Quick verification still hashes the exact encrypted archive and authenticates
    both metadata layers.  It reuses the signed restore result produced when the
    snapshot was created; it never upgrades an unverified snapshot to a pass.
    """

    config = load_config(config_path)
    encryption_key = encryption_key_for_config(config)
    if encryption_key is None:
        raise ValueError("existing backup verification requires an AES-256-GCM key")
    latest = read_json(latest_path)
    if not isinstance(latest, dict):
        raise ValueError("latest backup metadata must be a JSON object")
    authenticate_metadata(latest, encryption_key, "latest")

    archive = Path(str(latest.get("archive") or ""))
    if not archive.is_absolute() or not archive.is_file():
        raise ValueError("latest backup archive is missing")
    try:
        archive.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("latest backup archive must be outside the workspace")
    archive_hash = str(latest.get("archive_sha256") or "")
    if not HEX_SHA256.fullmatch(archive_hash) or sha256(archive) != archive_hash:
        raise ValueError("latest backup archive hash mismatch")

    manifest_path = Path(str(latest.get("manifest_sidecar") or ""))
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        raise ValueError("latest backup manifest sidecar is missing")
    manifest = read_json(manifest_path)
    validate_manifest(manifest)
    if Path(str(manifest.get("workspace"))).resolve() != root.resolve():
        raise ValueError("latest backup belongs to a different workspace")
    authenticate_metadata(manifest, encryption_key, "manifest")
    manifest_hash = stable_json_sha256(manifest)
    if manifest_hash != latest.get("manifest_sha256"):
        raise ValueError("latest backup manifest hash mismatch")
    digest_path = archive.with_suffix(".manifest.sha256")
    if not digest_path.is_file() or digest_path.read_text(encoding="ascii").strip() != manifest_hash:
        raise ValueError("latest backup manifest digest sidecar mismatch")

    if manifest.get("snapshot_profile", "full") != latest.get("snapshot_profile", "full"):
        raise ValueError("latest backup snapshot profile is inconsistent")
    if manifest.get("full_snapshot") != latest.get("full_snapshot"):
        raise ValueError("latest backup full snapshot reference is inconsistent")
    for field in ("workspace_uuid", "revision", "previous_head_sha256"):
        if manifest.get(field) != latest.get(field):
            raise ValueError(f"latest backup {field} is inconsistent")
    verify_dr_integrity_head(
        latest,
        root=root,
        snapshot_profile=str(latest.get("snapshot_profile", "full")),
        encryption_key=encryption_key,
    )
    full_baseline_authenticated = False
    if (
        latest.get("snapshot_profile", "full") == "daily"
        and latest.get("full_snapshot") is not None
    ):
        try:
            reference = validate_full_snapshot_reference(latest.get("full_snapshot"))
            resolved_baseline = resolve_full_snapshot_reference(
                reference,
                root=root,
                config=config,
                encryption_key=encryption_key,
            )
            authenticated_reference = validate_full_snapshot_reference(
                {
                    "archive_sha256": resolved_baseline["archive_sha256"],
                    "created_at": resolved_baseline["manifest"].get("created_at"),
                    "date": resolved_baseline["manifest"].get("date"),
                    "manifest_sha256": resolved_baseline["manifest_sha256"],
                    "restore_verified": reference["restore_verified"],
                }
            )
            if authenticated_reference != reference:
                raise ValueError("daily reference does not match the authenticated full snapshot")
            maximum_age = config.get("full_snapshot_maximum_age_hours")
            if maximum_age is not None:
                baseline_created = datetime.fromisoformat(
                    authenticated_reference["created_at"].replace("Z", "+00:00")
                )
                baseline_age_hours = (
                    datetime.now(UTC) - baseline_created.astimezone(UTC)
                ).total_seconds() / 3600
                if baseline_age_hours < 0 or baseline_age_hours > float(maximum_age):
                    raise ValueError("authenticated full snapshot is outside the freshness window")
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ValueError("full recovery baseline authentication failed") from exc
        full_baseline_authenticated = True
    elif (
        latest.get("snapshot_profile", "full") == "daily"
        and config.get("full_snapshot_maximum_age_hours") is not None
    ):
        raise ValueError("full recovery baseline authentication failed")
    if quick:
        verification = {
            "verified": latest.get("verified") is True,
            "archive_integrity_verified": latest.get("archive_integrity_verified") is True,
            "restore_verified": latest.get("restore_verified") is True,
            "encrypted_container_authenticated": latest.get("encrypted_container_authenticated") is True,
            "restore_scope": latest.get("restore_scope"),
        }
    else:
        verification = verify_archive_detailed(
            archive,
            manifest,
            encryption_key=encryption_key,
        )
    if verification.get("verified") is not True:
        raise ValueError("latest backup restore verification failed")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "date": latest.get("date"),
        "verified": True,
        "archive_sha256": archive_hash,
        "manifest_sha256": manifest_hash,
        "archive_integrity_verified": verification.get("archive_integrity_verified") is True,
        "restore_verified": verification.get("restore_verified") is True,
        "encrypted_container_authenticated": verification.get("encrypted_container_authenticated") is True,
        "restore_scope": verification.get("restore_scope"),
        "snapshot_profile": latest.get("snapshot_profile", "full"),
        "full_snapshot": latest.get("full_snapshot"),
        "full_baseline_authenticated": full_baseline_authenticated,
        "verification_mode": "authenticated_archive" if quick else "full_restore",
    }


def cli_failure_payload(date: str, exc: Exception) -> dict[str, Any]:
    """Return a bounded, machine-readable failure without exposing configuration values.

    The command is called by the root publication orchestrator, whose output is
    often retained with long-lived audit evidence.  In particular, a missing or
    malformed encryption key must not turn into a Python traceback (or copy a
    provider/configuration value) in those records.  ``create_snapshot`` still
    raises to preserve its library API and fail-closed tests; this boundary only
    normalizes expected operational failures for the CLI.
    """
    if isinstance(exc, ValueError):
        # Deliberately inspect but never retain the original text: config values
        # can include paths or provider data, and the key itself must never be
        # written even if an upstream implementation accidentally includes it.
        message = str(exc).lower()
        if "encryption key" in message:
            reason = "backup_encryption_key_unavailable"
        else:
            reason = "backup_configuration_invalid"
    elif isinstance(exc, OSError):
        reason = "backup_storage_unavailable"
    else:
        reason = "backup_or_restore_verification_failed"
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "date": date,
        "status": "blocked",
        "reason": reason,
        "error_type": type(exc).__name__,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an external ATLAS backup and run a restore drill.")
    parser.add_argument("--date")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verify-existing", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Authenticate and hash the existing archive without repeating its restore drill.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Create the periodic full-history snapshot instead of the daily publication checkpoint.",
    )
    args = parser.parse_args()
    try:
        if args.verify_existing:
            result = verify_latest_snapshot(
                latest_path=FULL_LATEST_PATH if args.full else LATEST_PATH,
                quick=args.quick,
            )
        else:
            if not args.date:
                raise ValueError("--date is required unless --verify-existing is used")
            if args.quick:
                raise ValueError("--quick requires --verify-existing")
            result = create_snapshot(
                date=args.date,
                snapshot_profile="full" if args.full else "daily",
            )
    except (OSError, RuntimeError, ValueError) as exc:
        # A nonzero exit is important: callers must not infer a passing backup
        # from this diagnostic payload.  Do not write ``latest.json`` here;
        # only a fully encrypted, restore-verified snapshot may replace it.
        print(json.dumps(cli_failure_payload(args.date or "unknown", exc), ensure_ascii=False))
        return 1
    if args.json or args.verify_existing:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"backup={result['archive']} profile={result.get('snapshot_profile', 'full')} "
            f"files={result['file_count']} restore_verified={result['restore_verified']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
