from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint
from protocol.v1.runtime.native_knowledge_store import KnowledgeRejected, validate_knowledge_request
from protocol.v1.runtime.portfolio_checkpoint import CheckpointError, validate_checkpoint_journal_rows


WRITE_REL = Path("native-write/write-journal.jsonl")
KNOWLEDGE_REL = Path("native-write/knowledge-journal.jsonl")
APPROVAL_REL = Path("approvals/approval-journal.jsonl")
CHECKPOINT_REL = Path("portfolio-checkpoints/checkpoints.jsonl")
WRITE_LOCK_REL = Path("native-write/.write-journal.lock")
KNOWLEDGE_LOCK_REL = Path("native-write/.knowledge-journal.lock")
APPROVAL_LOCK_REL = Path("approvals/.approval-journal.lock")
CHECKPOINT_LOCK_REL = Path("portfolio-checkpoints/.checkpoints.lock")
MANIFEST_NAME = "backup-manifest.json"


class BackupError(RuntimeError):
    pass


class BackupIntegrityError(BackupError):
    pass


class RestoreRefused(BackupError):
    pass


@dataclass(frozen=True)
class StorageLayout:
    state_root: Path
    backup_root: Path
    instance_id: str

    @property
    def native_write_root(self) -> Path:
        return self.state_root / "native-write"

    @property
    def approval_root(self) -> Path:
        return self.state_root / "approvals"

    @property
    def checkpoint_root(self) -> Path:
        return self.state_root / "portfolio-checkpoints"


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _jsonl_rows(value: bytes, *, label: str) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupIntegrityError(f"{label}_invalid_utf8") from exc
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BackupIntegrityError(f"{label}_invalid_jsonl_line_{line_number}") from exc
        if not isinstance(row, dict):
            raise BackupIntegrityError(f"{label}_non_object_line_{line_number}")
        rows.append(row)
    return rows


def _approval_verification_ref(row: dict[str, Any]) -> str:
    return (
        "approval-verification:"
        + digest([row["authority_id"], row["approval_id"], row["approval_digest"]])[:24]
    )


def _validate_approval_rows(rows: list[dict[str, Any]]) -> set[str]:
    issued: dict[str, dict[str, Any]] = {}
    revoked: set[str] = set()
    verification_refs: set[str] = set()
    for row in rows:
        event_type = row.get("event_type")
        approval_id = str(row.get("approval_id") or "")
        approval_digest = str(row.get("approval_digest") or "")
        authority_id = str(row.get("authority_id") or "")
        if not approval_id or not authority_id or len(approval_digest) != 64:
            raise BackupIntegrityError("approval_journal_malformed_record")
        if event_type == "approval_issued":
            approval = row.get("approval")
            if not isinstance(approval, dict):
                raise BackupIntegrityError("approval_issue_missing_receipt")
            if digest(approval) != approval_digest:
                raise BackupIntegrityError("approval_digest_mismatch")
            if str(approval.get("approval_id") or "") != approval_id:
                raise BackupIntegrityError("approval_id_mismatch")
            if approval_id in issued:
                raise BackupIntegrityError("duplicate_approval_issue")
            issued[approval_id] = row
            verification_refs.add(_approval_verification_ref(row))
        elif event_type == "approval_revoked":
            if approval_id not in issued:
                raise BackupIntegrityError("approval_revocation_without_issue")
            if row.get("approval") is not None:
                raise BackupIntegrityError("approval_revocation_contains_receipt")
            if approval_id in revoked:
                raise BackupIntegrityError("duplicate_approval_revocation")
            revoked.add(approval_id)
        else:
            raise BackupIntegrityError("approval_journal_unknown_event")
    return verification_refs


def _validate_write_rows(rows: list[dict[str, Any]], approval_refs: set[str]) -> None:
    commit_ids: set[str] = set()
    canonical_resources: set[str] = set()
    for row in rows:
        commit_id = str(row.get("commit_id") or "")
        event_type = row.get("event_type")
        resource_ref = str(row.get("resource_ref") or "")
        receipt = row.get("receipt")
        audit = row.get("audit")
        if not commit_id or commit_id in commit_ids:
            raise BackupIntegrityError("duplicate_or_missing_commit_id")
        commit_ids.add(commit_id)
        if not resource_ref or not isinstance(receipt, dict) or not isinstance(audit, dict):
            raise BackupIntegrityError("write_journal_malformed_record")
        if str(receipt.get("result_ref") or "") != resource_ref:
            raise BackupIntegrityError("write_receipt_resource_mismatch")
        approval_ref = str(audit.get("approval_verification_ref") or "")
        if not approval_ref or approval_ref not in approval_refs:
            raise BackupIntegrityError("write_missing_trusted_approval")

        if event_type == "resource_commit":
            resource = row.get("resource")
            if not isinstance(resource, dict):
                raise BackupIntegrityError("resource_commit_missing_resource")
            if row.get("resource_digest") != digest(resource):
                raise BackupIntegrityError("resource_digest_mismatch")
            resource_id = str(resource.get("decision_id") or resource.get("transaction_id") or "")
            if resource_id != resource_ref:
                raise BackupIntegrityError("canonical_resource_identity_mismatch")
            if resource_ref in canonical_resources:
                raise BackupIntegrityError("duplicate_canonical_resource")
            canonical_resources.add(resource_ref)
        elif event_type == "operation_deduplicated":
            if row.get("resource") is not None:
                raise BackupIntegrityError("deduplicated_operation_contains_resource")
            if resource_ref not in canonical_resources:
                raise BackupIntegrityError("deduplicated_operation_missing_prior_resource")
        else:
            raise BackupIntegrityError("write_journal_unknown_event")


def _validate_knowledge_rows(rows: list[dict[str, Any]], approval_refs: set[str]) -> int:
    generation_ids: set[str] = set()
    for row in rows:
        if row.get("journal_version") != 1 or row.get("event_type") != "knowledge_generation_committed":
            raise BackupIntegrityError("knowledge_journal_malformed_record")
        generation_id = str(row.get("generation_id") or "")
        request = row.get("request")
        if not generation_id or generation_id in generation_ids or not isinstance(request, dict):
            raise BackupIntegrityError("knowledge_journal_malformed_record")
        generation_ids.add(generation_id)
        try:
            validate_knowledge_request(request)
        except KnowledgeRejected as exc:
            raise BackupIntegrityError("knowledge_request_invalid") from exc
        if request.get("generation_id") != generation_id or row.get("payload_digest") != digest(request):
            raise BackupIntegrityError("knowledge_journal_digest_mismatch")
        approval_ref = str(row.get("approval_verification_ref") or "")
        if not approval_ref or approval_ref not in approval_refs:
            raise BackupIntegrityError("knowledge_missing_trusted_approval")
        committed_at = row.get("committed_at")
        if not isinstance(committed_at, dict) or not isinstance(committed_at.get("value"), str):
            raise BackupIntegrityError("knowledge_journal_malformed_record")
    return len(rows)


def _validate_journals(
    write_bytes: bytes,
    approval_bytes: bytes,
    checkpoint_bytes: bytes | None = None,
    knowledge_bytes: bytes | None = None,
) -> tuple[int, int, int, int]:
    approval_rows = _jsonl_rows(approval_bytes, label="approval_journal")
    write_rows = _jsonl_rows(write_bytes, label="write_journal")
    approval_refs = _validate_approval_rows(approval_rows)
    _validate_write_rows(write_rows, approval_refs)
    knowledge_count = 0
    if knowledge_bytes is not None:
        knowledge_rows = _jsonl_rows(knowledge_bytes, label="knowledge_journal")
        knowledge_count = _validate_knowledge_rows(knowledge_rows, approval_refs)
    checkpoint_count = 0
    if checkpoint_bytes is not None:
        checkpoint_rows = _jsonl_rows(checkpoint_bytes, label="checkpoint_journal")
        try:
            checkpoint_count = validate_checkpoint_journal_rows(checkpoint_rows, write_rows)
        except CheckpointError as exc:
            code = exc.code if exc.code.startswith("checkpoint_") else f"checkpoint_{exc.code}"
            raise BackupIntegrityError(code) from exc
    return len(write_rows), len(approval_rows), checkpoint_count, knowledge_count


def _read_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise BackupError("source_symlink_not_allowed")
        if not path.exists():
            return b""
        if not path.is_file():
            raise BackupError("source_not_regular_file")
        return path.read_bytes()
    except OSError as exc:
        raise BackupError("source_read_failed") from exc


def _read_required_snapshot_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise BackupIntegrityError("backup_file_symlink_not_allowed")
        if not path.is_file():
            raise BackupIntegrityError("backup_file_missing")
        return path.read_bytes()
    except OSError as exc:
        raise BackupIntegrityError("backup_file_read_failed") from exc


@contextmanager
def _state_shared_lock(state_root: Path) -> Iterator[None]:
    # Keep a stable global order whenever multiple runtime journals are locked.
    lock_paths = [
        state_root / WRITE_LOCK_REL,
        state_root / KNOWLEDGE_LOCK_REL,
        state_root / APPROVAL_LOCK_REL,
        state_root / CHECKPOINT_LOCK_REL,
    ]
    handles = []
    try:
        for path in lock_paths:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            handle = os.fdopen(fd, "r+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _write_file(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(value)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("snapshot write failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _manifest_file(logical_name: str, relative_path: Path, value: bytes, count: int) -> dict[str, Any]:
    return {
        "logical_name": logical_name,
        "relative_path": relative_path.as_posix(),
        "sha256": _sha256_bytes(value),
        "size_bytes": len(value),
        "jsonl_records": count,
    }


def create_backup(
    layout: StorageLayout,
    *,
    now: datetime | None = None,
) -> Path:
    current = _utc_now(now)
    state_root = layout.state_root.resolve()
    backup_root = layout.backup_root.resolve()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    with _state_shared_lock(state_root):
        write_bytes = _read_bytes(state_root / WRITE_REL)
        knowledge_bytes = _read_bytes(state_root / KNOWLEDGE_REL)
        approval_bytes = _read_bytes(state_root / APPROVAL_REL)
        checkpoint_bytes = _read_bytes(state_root / CHECKPOINT_REL)
        write_count, approval_count, checkpoint_count, knowledge_count = _validate_journals(
            write_bytes,
            approval_bytes,
            checkpoint_bytes,
            knowledge_bytes,
        )

        stamp = current.strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"backup:{stamp}:{uuid.uuid4().hex[:12]}"
        dir_name = backup_id.replace(":", "-")
        final_dir = backup_root / dir_name
        staging = backup_root / f".creating-{uuid.uuid4().hex}"
        if final_dir.exists():
            raise BackupError("backup_destination_exists")
        staging.mkdir(mode=0o700)
        try:
            _write_file(staging / WRITE_REL, write_bytes)
            _write_file(staging / KNOWLEDGE_REL, knowledge_bytes)
            _write_file(staging / APPROVAL_REL, approval_bytes)
            _write_file(staging / CHECKPOINT_REL, checkpoint_bytes)
            manifest = {
                "protocol_version": PROTOCOL_VERSION,
                "backup_format_version": 2,
                "backup_id": backup_id,
                "instance_id": layout.instance_id,
                "created_at": timepoint(current.isoformat().replace("+00:00", "Z")),
                "consistency": "triple_journal_locked_snapshot",
                "layout": {
                    "native_write_journal": WRITE_REL.as_posix(),
                    "native_knowledge_journal": KNOWLEDGE_REL.as_posix(),
                    "approval_journal": APPROVAL_REL.as_posix(),
                    "portfolio_checkpoint_journal": CHECKPOINT_REL.as_posix(),
                },
                "files": [
                    _manifest_file("native_write_journal", WRITE_REL, write_bytes, write_count),
                    _manifest_file("native_knowledge_journal", KNOWLEDGE_REL, knowledge_bytes, knowledge_count),
                    _manifest_file("approval_journal", APPROVAL_REL, approval_bytes, approval_count),
                    _manifest_file(
                        "portfolio_checkpoint_journal",
                        CHECKPOINT_REL,
                        checkpoint_bytes,
                        checkpoint_count,
                    ),
                ],
                "creation_checks": [
                    "jsonl_parse",
                    "write_journal_invariants",
                    "approval_journal_invariants",
                    "approval_write_linkage",
                    "knowledge_journal_invariants",
                    "knowledge_approval_linkage",
                    "checkpoint_journal_invariants",
                    "checkpoint_write_cursor_linkage",
                ],
            }
            _write_file(staging / MANIFEST_NAME, (canonical_json(manifest) + "\n").encode("utf-8"))
            _fsync_dir(staging / "native-write")
            _fsync_dir(staging / "approvals")
            _fsync_dir(staging / "portfolio-checkpoints")
            _fsync_dir(staging)
            os.replace(staging, final_dir)
            _fsync_dir(backup_root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    verify_backup(final_dir, expected_instance_id=layout.instance_id)
    return final_dir


def _load_manifest(snapshot_dir: Path) -> dict[str, Any]:
    path = snapshot_dir / MANIFEST_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup_manifest_invalid") from exc
    if not isinstance(value, dict):
        raise BackupIntegrityError("backup_manifest_invalid")
    if value.get("protocol_version") != PROTOCOL_VERSION or value.get("backup_format_version") not in {1, 2}:
        raise BackupIntegrityError("backup_manifest_version_unsupported")
    return value


def verify_backup(snapshot_dir: Path, *, expected_instance_id: str | None = None) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve()
    manifest = _load_manifest(snapshot_dir)
    if expected_instance_id is not None and manifest.get("instance_id") != expected_instance_id:
        raise BackupIntegrityError("backup_instance_mismatch")
    backup_version = int(manifest["backup_format_version"])
    files = manifest.get("files")
    expected_names = {"native_write_journal", "approval_journal"}
    expected_paths = {
        "native_write_journal": WRITE_REL,
        "approval_journal": APPROVAL_REL,
    }
    expected_consistency = "dual_journal_locked_snapshot"
    expected_checks = {
        "jsonl_parse",
        "write_journal_invariants",
        "approval_journal_invariants",
        "approval_write_linkage",
    }
    if backup_version == 2:
        expected_names.add("portfolio_checkpoint_journal")
        expected_paths["portfolio_checkpoint_journal"] = CHECKPOINT_REL
        expected_consistency = "triple_journal_locked_snapshot"
        expected_checks.update({
            "checkpoint_journal_invariants",
            "checkpoint_write_cursor_linkage",
        })
        # v2 snapshots created before native Knowledge existed remain valid.
        if isinstance(files, list) and any(
            isinstance(row, dict) and row.get("logical_name") == "native_knowledge_journal"
            for row in files
        ):
            expected_names.add("native_knowledge_journal")
            expected_paths["native_knowledge_journal"] = KNOWLEDGE_REL
            expected_checks.update({"knowledge_journal_invariants", "knowledge_approval_linkage"})
    if manifest.get("consistency") != expected_consistency:
        raise BackupIntegrityError("backup_manifest_consistency_mismatch")
    if not isinstance(files, list) or len(files) != len(expected_names):
        raise BackupIntegrityError("backup_manifest_files_invalid")
    by_name = {str(row.get("logical_name")): row for row in files if isinstance(row, dict)}
    if set(by_name) != expected_names:
        raise BackupIntegrityError("backup_manifest_files_invalid")
    layout = manifest.get("layout")
    if not isinstance(layout, dict):
        raise BackupIntegrityError("backup_manifest_layout_invalid")
    expected_layout_keys = {
        "native_write_journal",
        "approval_journal",
    }
    if backup_version == 2:
        expected_layout_keys.add("portfolio_checkpoint_journal")
        if "native_knowledge_journal" in expected_names:
            expected_layout_keys.add("native_knowledge_journal")
    if set(layout) != expected_layout_keys:
        raise BackupIntegrityError("backup_manifest_layout_invalid")
    for name, relative in expected_paths.items():
        layout_key = {
            "native_write_journal": "native_write_journal",
            "approval_journal": "approval_journal",
            "portfolio_checkpoint_journal": "portfolio_checkpoint_journal",
            "native_knowledge_journal": "native_knowledge_journal",
        }[name]
        if layout.get(layout_key) != relative.as_posix():
            raise BackupIntegrityError("backup_manifest_path_mismatch")
    creation_checks = manifest.get("creation_checks")
    if not isinstance(creation_checks, list) or set(creation_checks) != expected_checks:
        raise BackupIntegrityError("backup_manifest_creation_checks_invalid")

    contents: dict[str, bytes] = {}
    for name, relative in expected_paths.items():
        row = by_name[name]
        if row.get("relative_path") != relative.as_posix():
            raise BackupIntegrityError("backup_manifest_path_mismatch")
        value = _read_required_snapshot_bytes(snapshot_dir / relative)
        if row.get("sha256") != _sha256_bytes(value) or row.get("size_bytes") != len(value):
            raise BackupIntegrityError("backup_file_digest_mismatch")
        contents[name] = value

    write_count, approval_count, checkpoint_count, knowledge_count = _validate_journals(
        contents["native_write_journal"],
        contents["approval_journal"],
        contents.get("portfolio_checkpoint_journal") if backup_version == 2 else None,
        contents.get("native_knowledge_journal"),
    )
    if by_name["native_write_journal"].get("jsonl_records") != write_count:
        raise BackupIntegrityError("backup_record_count_mismatch")
    if by_name["approval_journal"].get("jsonl_records") != approval_count:
        raise BackupIntegrityError("backup_record_count_mismatch")
    if backup_version == 2 and by_name["portfolio_checkpoint_journal"].get("jsonl_records") != checkpoint_count:
        raise BackupIntegrityError("backup_record_count_mismatch")
    if "native_knowledge_journal" in by_name and by_name["native_knowledge_journal"].get("jsonl_records") != knowledge_count:
        raise BackupIntegrityError("backup_record_count_mismatch")
    return {
        "ok": True,
        "backup_id": manifest.get("backup_id"),
        "instance_id": manifest.get("instance_id"),
        "backup_format_version": backup_version,
        "write_records": write_count,
        "approval_records": approval_count,
        "checkpoint_records": checkpoint_count,
        "knowledge_records": knowledge_count,
    }


def _target_is_empty(path: Path) -> bool:
    return not path.exists() or (path.is_dir() and next(path.iterdir(), None) is None)


def restore_backup(
    snapshot_dir: Path,
    target_state_root: Path,
    *,
    expected_instance_id: str,
) -> dict[str, Any]:
    verification = verify_backup(snapshot_dir, expected_instance_id=expected_instance_id)
    target = target_state_root.resolve()
    if not _target_is_empty(target):
        raise RestoreRefused("restore_target_not_empty")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = target.parent / f".restore-{target.name}-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        backup_version = int(verification["backup_format_version"])
        relatives = [WRITE_REL, APPROVAL_REL]
        if backup_version == 2:
            relatives.append(CHECKPOINT_REL)
            manifest = _load_manifest(snapshot_dir.resolve())
            if any(
                isinstance(row, dict) and row.get("logical_name") == "native_knowledge_journal"
                for row in manifest.get("files") or []
            ):
                relatives.append(KNOWLEDGE_REL)
        for relative in relatives:
            _write_file(staging / relative, _read_required_snapshot_bytes(snapshot_dir.resolve() / relative))
        _validate_journals(
            (staging / WRITE_REL).read_bytes(),
            (staging / APPROVAL_REL).read_bytes(),
            (staging / CHECKPOINT_REL).read_bytes() if backup_version == 2 else None,
            (staging / KNOWLEDGE_REL).read_bytes() if (staging / KNOWLEDGE_REL).is_file() else None,
        )
        _fsync_dir(staging / "native-write")
        _fsync_dir(staging / "approvals")
        if backup_version == 2:
            _fsync_dir(staging / "portfolio-checkpoints")
        _fsync_dir(staging)
        if target.exists():
            target.rmdir()
        os.replace(staging, target)
        _fsync_dir(target.parent)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        **verification,
        "restored": True,
        "target_state_root": str(target),
    }
