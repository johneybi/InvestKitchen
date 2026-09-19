from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from .legacy_knowledge_migration import QUEUE_SCHEMA_VERSION


STORE_SCHEMA_VERSION = 1
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolved_store_root(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError("historical Knowledge store root must not be a symlink")
    resolved = expanded.resolve() if expanded.exists() else expanded.absolute()
    try:
        resolved.relative_to(_repo_root().resolve())
    except ValueError:
        return resolved
    raise ValueError("historical Knowledge store must live outside the Git repository")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _storage_key(document_id: str) -> str:
    return "document-" + hashlib.sha256(document_id.encode("utf-8")).hexdigest()


def _validate_document_id(document_id: object) -> str:
    if not isinstance(document_id, str) or not document_id or len(document_id) > 4096:
        raise ValueError("historical Knowledge document_id is invalid")
    if any(ord(char) < 32 for char in document_id):
        raise ValueError("historical Knowledge document_id contains control characters")
    return document_id


def _archive_source_path(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise ValueError("historical Knowledge source path is invalid")
    archive_root = root.resolve()
    candidate = (archive_root / relative_path).resolve()
    try:
        candidate.relative_to(archive_root)
    except ValueError as exc:
        raise ValueError("historical Knowledge source path escapes archive root") from exc
    return candidate


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_exclusive(path: Path, value: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        view = memoryview(value)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("historical Knowledge store write failed")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, _FILE_MODE)


def _metadata(row: dict[str, Any], *, source_missing: bool) -> dict[str, Any]:
    source_locator = row.get("canonical_source_path")
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "authority": "historical_source_only",
        "document_id": row["document_id"],
        "migration_status": row.get("status"),
        "recommended_action": row.get("recommended_action"),
        "document_type": row.get("document_type"),
        "speaker": row.get("speaker"),
        "published_at": row.get("published_at"),
        "session_date": row.get("session_date"),
        "source_sha256": row.get("source_sha256"),
        "source_bytes": row.get("source_bytes"),
        "source_missing": source_missing,
        "source_name": Path(source_locator).name if isinstance(source_locator, str) and source_locator else None,
        "legacy_source_locator": source_locator,
        "explicit_registration": row.get("explicit_registration"),
        "recovery_artifacts": row.get("recovery_artifacts") or [],
    }


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"historical Knowledge metadata is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != STORE_SCHEMA_VERSION:
        raise ValueError(f"historical Knowledge metadata is invalid: {path}")
    return value


def import_historical_knowledge(
    *,
    archive_files_root: Path,
    queue: dict[str, Any],
    store_root: Path,
) -> dict[str, Any]:
    """Copy exact historical Knowledge sources into a non-current immutable store."""

    if queue.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise ValueError("unsupported canonical migration queue version")
    entries = queue.get("entries")
    if not isinstance(entries, list):
        raise ValueError("canonical migration queue entries are invalid")

    root = _resolved_store_root(store_root)
    root.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    os.chmod(root, _DIR_MODE)
    documents = root / "documents"
    documents.mkdir(exist_ok=True, mode=_DIR_MODE)
    os.chmod(documents, _DIR_MODE)

    imported = 0
    replayed = 0
    missing = 0
    manifest_rows: list[dict[str, Any]] = []
    for row in sorted((item for item in entries if isinstance(item, dict)), key=lambda item: str(item.get("document_id") or "")):
        document_id = _validate_document_id(row.get("document_id"))
        storage_key = _storage_key(document_id)
        source_locator = row.get("canonical_source_path")
        source = _archive_source_path(archive_files_root, source_locator) if isinstance(source_locator, str) and source_locator else None
        source_missing = source is None or not source.is_file() or source.is_symlink()
        expected_sha = row.get("source_sha256")
        expected_size = row.get("source_bytes")
        source_bytes: bytes | None = None
        if source_missing:
            if row.get("status") != "explicit_registration_source_missing":
                raise ValueError(f"historical Knowledge source bytes are unexpectedly missing: {document_id}")
            missing += 1
        else:
            source_bytes = source.read_bytes()
            if not isinstance(expected_sha, str) or _sha256_bytes(source_bytes) != expected_sha:
                raise ValueError(f"historical Knowledge source hash mismatch: {document_id}")
            if not isinstance(expected_size, int) or len(source_bytes) != expected_size:
                raise ValueError(f"historical Knowledge source size mismatch: {document_id}")

        metadata = _metadata(row, source_missing=source_missing)
        metadata_bytes = _canonical_json(metadata)
        target = documents / storage_key
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ValueError(f"historical Knowledge document path is unsafe: {document_id}")
            existing = _load_metadata(target / "metadata.json")
            if _canonical_json(existing) != metadata_bytes:
                raise ValueError(f"historical Knowledge metadata conflict: {document_id}")
            stored_source = target / "source.bin"
            if source_missing:
                if stored_source.exists():
                    raise ValueError(f"historical Knowledge missing-source record has unexpected bytes: {document_id}")
            else:
                if not stored_source.is_file() or stored_source.is_symlink() or stored_source.read_bytes() != source_bytes:
                    raise ValueError(f"historical Knowledge source replay conflict: {document_id}")
            replayed += 1
        else:
            staging = documents / f".staging-{document_id}-{os.getpid()}"
            if staging.exists() or staging.is_symlink():
                raise ValueError("historical Knowledge staging path already exists")
            staging.mkdir(mode=_DIR_MODE)
            try:
                _write_exclusive(staging / "metadata.json", metadata_bytes)
                if source_bytes is not None:
                    _write_exclusive(staging / "source.bin", source_bytes)
                os.replace(staging, target)
                os.chmod(target, _DIR_MODE)
            finally:
                if staging.exists() and not staging.is_symlink():
                    for child in staging.iterdir():
                        child.unlink()
                    staging.rmdir()
            imported += 1

        manifest_rows.append({
            "document_id": document_id,
            "storage_key": storage_key,
            "metadata_sha256": _sha256_bytes(metadata_bytes),
            "source_sha256": expected_sha,
            "source_missing": source_missing,
        })

    manifest = {
        "schema_version": STORE_SCHEMA_VERSION,
        "authority": "historical_source_only",
        "source_system": queue.get("source_system"),
        "documents": manifest_rows,
        "summary": {
            "documents": len(manifest_rows),
            "sources_available": len(manifest_rows) - missing,
            "sources_missing": missing,
        },
    }
    manifest_path = root / "manifest.json"
    tmp = root / f".manifest-{os.getpid()}.tmp"
    if tmp.exists():
        tmp.unlink()
    _write_exclusive(tmp, _canonical_json(manifest))
    os.replace(tmp, manifest_path)
    os.chmod(manifest_path, _FILE_MODE)
    validate_historical_knowledge_store(root)
    return {
        "ok": True,
        "imported": imported,
        "already_present": replayed,
        **manifest["summary"],
    }


def validate_historical_knowledge_store(store_root: Path) -> dict[str, Any]:
    root = _resolved_store_root(store_root)
    if not root.is_dir() or root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != _DIR_MODE:
        raise ValueError("historical Knowledge store root is missing or not private")
    manifest_path = root / "manifest.json"
    manifest = _load_metadata(manifest_path)
    if manifest.get("authority") != "historical_source_only" or not isinstance(manifest.get("documents"), list):
        raise ValueError("historical Knowledge manifest is invalid")
    documents_root = root / "documents"
    if not documents_root.is_dir() or documents_root.is_symlink():
        raise ValueError("historical Knowledge documents directory is invalid")
    available = 0
    missing = 0
    for row in manifest["documents"]:
        if not isinstance(row, dict):
            raise ValueError("historical Knowledge manifest row is invalid")
        document_id = _validate_document_id(row.get("document_id"))
        storage_key = row.get("storage_key")
        if storage_key != _storage_key(document_id):
            raise ValueError("historical Knowledge storage key is invalid")
        target = documents_root / storage_key
        metadata_path = target / "metadata.json"
        metadata = _load_metadata(metadata_path)
        if stat.S_IMODE(target.stat().st_mode) != _DIR_MODE or stat.S_IMODE(metadata_path.stat().st_mode) != _FILE_MODE:
            raise ValueError("historical Knowledge document permissions are not private")
        if _sha256_bytes(_canonical_json(metadata)) != row.get("metadata_sha256"):
            raise ValueError(f"historical Knowledge metadata hash mismatch: {row['document_id']}")
        source = target / "source.bin"
        if row.get("source_missing"):
            if source.exists():
                raise ValueError(f"historical Knowledge missing source unexpectedly exists: {row['document_id']}")
            missing += 1
        else:
            if not source.is_file() or source.is_symlink() or stat.S_IMODE(source.stat().st_mode) != _FILE_MODE:
                raise ValueError(f"historical Knowledge source is missing or unsafe: {row['document_id']}")
            if _sha256_bytes(source.read_bytes()) != row.get("source_sha256"):
                raise ValueError(f"historical Knowledge source hash mismatch: {row['document_id']}")
            available += 1
    expected = manifest.get("summary") or {}
    if expected.get("documents") != available + missing or expected.get("sources_available") != available or expected.get("sources_missing") != missing:
        raise ValueError("historical Knowledge manifest summary mismatch")
    return {"ok": True, "documents": available + missing, "sources_available": available, "sources_missing": missing}
