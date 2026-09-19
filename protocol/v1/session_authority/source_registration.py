"""Private native registration for raw session-authority source material.

This module is intentionally separate from Portfolio authority and from the
legacy TradeMind workspace.  It binds an exact raw source byte sequence to one
structured session-authority bundle before that bundle may be published.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from .contracts import (
    SOURCE_ROLES,
    SessionAuthorityContractError,
    canonical_json_bytes,
    sha256_bytes,
    validate_amendment_source,
    validate_source_document,
)
from .producer import SessionAuthorityProducer


REGISTRATION_SCHEMA_VERSION = "1.0"
REGISTRATION_INTENT = "SESSION_TRADING_INPUT"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_REGISTRATIONS_DIR = "registrations"
_METADATA_FILE = "registration.json"
_SOURCE_FILE = "source.bin"


class SessionSourceRegistrationError(ValueError):
    """Raw source registration or registered-bundle validation failed."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolved_store_root(root: Path) -> Path:
    expanded = root.expanduser()
    if expanded.is_symlink():
        raise SessionSourceRegistrationError("registration root must not be a symlink")
    resolved = expanded.resolve() if expanded.exists() else expanded.absolute()
    repository = _repo_root().resolve()
    try:
        resolved.relative_to(repository)
    except ValueError:
        pass
    else:
        raise SessionSourceRegistrationError("registration root must be outside the Git repository")
    return resolved


def _read_bundle(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionSourceRegistrationError("session authority bundle is unreadable") from exc
    if not isinstance(value, dict):
        raise SessionSourceRegistrationError("session authority bundle must be an object")
    return value


def _validate_bundle_shape(value: Mapping[str, Any]) -> tuple[str, str, list[Mapping[str, Any]]]:
    if set(value) != {"bundle_version", "session_date", "published_at", "events"} or value.get("bundle_version") != 1:
        raise SessionSourceRegistrationError("session authority bundle fields/version are invalid")
    session_date = value.get("session_date")
    published_at = value.get("published_at")
    events = value.get("events")
    if not isinstance(session_date, str) or not session_date:
        raise SessionSourceRegistrationError("session authority bundle session_date is required")
    if not isinstance(published_at, str) or not published_at:
        raise SessionSourceRegistrationError("session authority bundle published_at is required")
    if not isinstance(events, list) or not events or any(not isinstance(item, Mapping) for item in events):
        raise SessionSourceRegistrationError("session authority bundle events are invalid")
    return session_date, published_at, list(events)


def producer_from_bundle(value: Mapping[str, Any]) -> tuple[SessionAuthorityProducer, str]:
    """Validate the existing structured bundle through the native producer."""

    session_date, published_at, events = _validate_bundle_shape(value)
    producer = SessionAuthorityProducer(session_date)
    for offset, row in enumerate(events):
        if set(row) != {"event_type", "occurred_at", "effective_at", "payload"}:
            raise SessionSourceRegistrationError(f"events[{offset}] fields are invalid")
        event_type = row.get("event_type")
        occurred_at = row.get("occurred_at")
        effective_at = row.get("effective_at")
        payload = row.get("payload")
        if event_type not in {"SessionBrief", "SessionBriefAmendment", "MaterialContext"}:
            raise SessionSourceRegistrationError(f"events[{offset}] event_type is unsupported")
        if not isinstance(occurred_at, str) or not occurred_at:
            raise SessionSourceRegistrationError(f"events[{offset}] occurred_at is required")
        if effective_at is not None and (not isinstance(effective_at, str) or not effective_at):
            raise SessionSourceRegistrationError(f"events[{offset}] effective_at is invalid")
        if not isinstance(payload, Mapping):
            raise SessionSourceRegistrationError(f"events[{offset}] payload must be an object")
        try:
            if event_type == "SessionBrief":
                producer.append_brief(payload, occurred_at=occurred_at, effective_at=effective_at)
            elif event_type == "MaterialContext":
                producer.append_context(payload, occurred_at=occurred_at, effective_at=effective_at)
            else:
                if not producer.events:
                    raise SessionSourceRegistrationError("SessionBriefAmendment requires a prior SessionBrief")
                bound = copy.deepcopy(dict(payload))
                base = producer.events[0]
                previous = producer.events[-1]
                bound.setdefault("base_brief_id", base.manifest["event_id"])
                bound.setdefault("base_brief_hash", base.manifest["event_hash"])
                bound.setdefault(
                    "previous_authority_event",
                    {"event_id": previous.manifest["event_id"], "event_hash": previous.manifest["event_hash"]},
                )
                producer.append_amendment(bound, occurred_at=occurred_at, effective_at=effective_at)
        except SessionAuthorityContractError as exc:
            raise SessionSourceRegistrationError(str(exc)) from exc
    return producer, published_at


def _bundle_sha256(bundle: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(bundle))


def _brief_sources(bundle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    _, _, events = _validate_bundle_shape(bundle)
    sources: list[Mapping[str, Any]] = []
    for offset, event in enumerate(events):
        if event.get("event_type") != "SessionBrief":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or not isinstance(payload.get("source_documents"), list):
            raise SessionSourceRegistrationError(f"events[{offset}] SessionBrief source_documents are invalid")
        for source in payload["source_documents"]:
            if not isinstance(source, Mapping):
                raise SessionSourceRegistrationError("SessionBrief source document is invalid")
            sources.append(source)
    if not sources:
        raise SessionSourceRegistrationError("bundle does not contain a SessionBrief source registration")
    return sources


def _registration_sources(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return all bundle sources that can legitimately authorize raw registration.

    SessionBrief sources use the canonical source-document shape. Amendments may
    introduce additional exact sources later in the same authority chain; those
    carry the amendment wire shape (``source_hash`` + ``source_locator``). Both
    are explicit SESSION_TRADING_INPUT authorities and must be registrable
    without rewriting the historical SessionBrief source set.
    """

    session_date, _, events = _validate_bundle_shape(bundle)
    sources: list[dict[str, Any]] = []
    for offset, event in enumerate(events):
        event_type = event.get("event_type")
        payload = event.get("payload")
        if event_type == "SessionBrief":
            if not isinstance(payload, Mapping) or not isinstance(payload.get("source_documents"), list):
                raise SessionSourceRegistrationError(f"events[{offset}] SessionBrief source_documents are invalid")
            for source in payload["source_documents"]:
                try:
                    sources.append(validate_source_document(source, session_date, label="registered SessionBrief source"))
                except SessionAuthorityContractError as exc:
                    raise SessionSourceRegistrationError(str(exc)) from exc
        elif event_type == "SessionBriefAmendment":
            if not isinstance(payload, Mapping):
                raise SessionSourceRegistrationError(f"events[{offset}] SessionBriefAmendment payload is invalid")
            try:
                checked = validate_amendment_source(payload.get("source"), session_date)
            except SessionAuthorityContractError as exc:
                raise SessionSourceRegistrationError(str(exc)) from exc
            translated = dict(checked)
            translated["source_sha256"] = translated.pop("source_hash")
            translated.pop("source_locator")
            sources.append(translated)
    if not sources:
        raise SessionSourceRegistrationError("bundle does not contain a registrable session source")
    return sources


def _matching_source_registration(
    bundle: Mapping[str, Any], *, document_id: str, source_sha256: str
) -> dict[str, Any]:
    session_date, _, _ = _validate_bundle_shape(bundle)
    matching = [source for source in _registration_sources(bundle) if source.get("document_id") == document_id]
    if not matching:
        raise SessionSourceRegistrationError("document_id is not present in the bundle's authorized session sources")
    normalized: dict[str, Any] | None = None
    normalized_binding: dict[str, Any] | None = None
    for source in matching:
        checked = source
        if checked.get("source_sha256") != source_sha256:
            raise SessionSourceRegistrationError("authorized session source hash does not match raw source bytes")
        if checked.get("registration_intent") != REGISTRATION_INTENT:
            raise SessionSourceRegistrationError("authorized session source lacks SESSION_TRADING_INPUT authority")
        if checked.get("applicable_session_date") != session_date:
            raise SessionSourceRegistrationError("authorized session source session date does not match bundle")
        if checked.get("source_role") not in SOURCE_ROLES:
            raise SessionSourceRegistrationError("authorized session source role cannot grant session authority")
        binding = {
            "document_id": checked.get("document_id"),
            "source_sha256": checked.get("source_sha256"),
            "registration_intent": checked.get("registration_intent"),
            "applicable_session_date": checked.get("applicable_session_date"),
            "source_role": checked.get("source_role"),
            "intent_authority": checked.get("intent_authority"),
            "intent_evidence_ref": checked.get("intent_evidence_ref"),
            "source_profile_id": checked.get("source_profile_id"),
            "source_profile_version": checked.get("source_profile_version"),
        }
        if normalized is None:
            normalized = checked
            normalized_binding = binding
        elif binding != normalized_binding:
            raise SessionSourceRegistrationError("same document_id has conflicting bundle source registrations")
    assert normalized is not None
    return normalized


def _registration_identity(document_id: str, source_sha256: str, bundle_sha256: str, session_date: str) -> str:
    raw = canonical_json_bytes(
        {
            "bundle_sha256": bundle_sha256,
            "document_id": document_id,
            "session_date": session_date,
            "source_sha256": source_sha256,
        }
    )
    return "session-source-" + sha256_bytes(raw)[:24]


def _metadata(
    *, document_id: str, source_bytes: bytes, source: Mapping[str, Any], bundle_sha256: str
) -> dict[str, Any]:
    session_date = str(source["applicable_session_date"])
    source_sha256 = sha256_bytes(source_bytes)
    return {
        "schema_version": REGISTRATION_SCHEMA_VERSION,
        "registration_id": _registration_identity(document_id, source_sha256, bundle_sha256, session_date),
        "document_id": document_id,
        "source_sha256": source_sha256,
        "source_bytes": len(source_bytes),
        "bundle_sha256": bundle_sha256,
        "applicable_session_date": session_date,
        "registration_intent": source["registration_intent"],
        "source_role": source["source_role"],
        "intent_authority": source["intent_authority"],
        "intent_evidence_ref": source["intent_evidence_ref"],
        "source_profile_id": source.get("source_profile_id"),
        "source_profile_version": source.get("source_profile_version"),
    }


def _write_file(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("private session source write failed")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, _FILE_MODE)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_permissions(path: Path, expected: int, label: str) -> None:
    if stat.S_IMODE(path.stat().st_mode) != expected:
        raise SessionSourceRegistrationError(f"{label} permissions are not private")


def _load_registration(directory: Path) -> tuple[dict[str, Any], bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise SessionSourceRegistrationError("registration store contains an unsafe entry")
    _validate_permissions(directory, _DIR_MODE, "registration directory")
    metadata_path = directory / _METADATA_FILE
    source_path = directory / _SOURCE_FILE
    if metadata_path.is_symlink() or source_path.is_symlink() or not metadata_path.is_file() or not source_path.is_file():
        raise SessionSourceRegistrationError("registration is incomplete or unsafe")
    _validate_permissions(metadata_path, _FILE_MODE, "registration metadata")
    _validate_permissions(source_path, _FILE_MODE, "registered source")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionSourceRegistrationError("registration metadata is unreadable") from exc
    if not isinstance(metadata, dict):
        raise SessionSourceRegistrationError("registration metadata is invalid")
    source_bytes = source_path.read_bytes()
    required = {
        "schema_version", "registration_id", "document_id", "source_sha256", "source_bytes",
        "bundle_sha256", "applicable_session_date", "registration_intent", "source_role",
        "intent_authority", "intent_evidence_ref", "source_profile_id", "source_profile_version",
    }
    if set(metadata) != required or metadata.get("schema_version") != REGISTRATION_SCHEMA_VERSION:
        raise SessionSourceRegistrationError("registration metadata fields/version are invalid")
    if metadata.get("registration_id") != directory.name:
        raise SessionSourceRegistrationError("registration directory identity mismatch")
    if metadata.get("source_bytes") != len(source_bytes) or metadata.get("source_sha256") != sha256_bytes(source_bytes):
        raise SessionSourceRegistrationError("registered source bytes do not match metadata")
    expected_id = _registration_identity(
        str(metadata["document_id"]), str(metadata["source_sha256"]), str(metadata["bundle_sha256"]),
        str(metadata["applicable_session_date"]),
    )
    if metadata["registration_id"] != expected_id:
        raise SessionSourceRegistrationError("registration identity is invalid")
    return metadata, source_bytes


def _load_all(root: Path) -> list[tuple[dict[str, Any], bytes]]:
    registrations = root / _REGISTRATIONS_DIR
    if not registrations.exists():
        return []
    if registrations.is_symlink() or not registrations.is_dir():
        raise SessionSourceRegistrationError("registrations path is unsafe")
    _validate_permissions(registrations, _DIR_MODE, "registrations directory")
    return [_load_registration(path) for path in sorted(registrations.iterdir(), key=lambda item: item.name)]


def _persist_registration(
    *,
    registration_root: Path,
    proposed: dict[str, Any],
    source_bytes: bytes,
) -> dict[str, Any]:
    root = _resolved_store_root(registration_root)
    root.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    os.chmod(root, _DIR_MODE)
    registrations = root / _REGISTRATIONS_DIR
    if registrations.is_symlink():
        raise SessionSourceRegistrationError("registrations path must not be a symlink")
    registrations.mkdir(exist_ok=True, mode=_DIR_MODE)
    os.chmod(registrations, _DIR_MODE)
    lock_path = root / ".registration.lock"
    if lock_path.is_symlink():
        raise SessionSourceRegistrationError("registration lock must not be a symlink")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, _FILE_MODE)
    with os.fdopen(lock_fd, "r+") as lock_file:
        os.chmod(lock_path, _FILE_MODE)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            existing = _load_all(root)
            for metadata, existing_bytes in existing:
                if metadata == proposed:
                    if existing_bytes != source_bytes:
                        raise SessionSourceRegistrationError("exact registration metadata has conflicting source bytes")
                    return {
                        "ok": True,
                        "status": "already_registered",
                        "registration_id": proposed["registration_id"],
                        "document_id": proposed["document_id"],
                        "source_sha256": proposed["source_sha256"],
                        "bundle_sha256": proposed["bundle_sha256"],
                    }
                if metadata["document_id"] == proposed["document_id"]:
                    raise SessionSourceRegistrationError("document_id is already bound to a different source or bundle")
                if metadata["source_sha256"] == proposed["source_sha256"]:
                    raise SessionSourceRegistrationError("raw source hash is already bound to a different document or bundle")

            final = registrations / str(proposed["registration_id"])
            if final.exists() or final.is_symlink():
                raise SessionSourceRegistrationError("registration identity already exists with conflicting content")
            staging = registrations / f".staging-{proposed['registration_id']}-{os.getpid()}"
            if staging.exists() or staging.is_symlink():
                raise SessionSourceRegistrationError("registration staging path already exists")
            staging.mkdir(mode=_DIR_MODE)
            try:
                _write_file(staging / _SOURCE_FILE, source_bytes)
                _write_file(staging / _METADATA_FILE, canonical_json_bytes(proposed))
                _fsync_dir(staging)
                os.replace(staging, final)
                os.chmod(final, _DIR_MODE)
                _fsync_dir(registrations)
                _fsync_dir(root)
            finally:
                if staging.exists() and not staging.is_symlink():
                    for child in staging.iterdir():
                        child.unlink()
                    staging.rmdir()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return {
        "ok": True,
        "status": "registered",
        "registration_id": proposed["registration_id"],
        "document_id": proposed["document_id"],
        "source_sha256": proposed["source_sha256"],
        "bundle_sha256": proposed["bundle_sha256"],
    }


def register_source(
    *, raw_source: Path, bundle: Mapping[str, Any], document_id: str, registration_root: Path
) -> dict[str, Any]:
    """Append one exact raw source registration, or return an exact replay."""

    if not isinstance(document_id, str) or not document_id.strip():
        raise SessionSourceRegistrationError("document_id is required")
    if raw_source.is_symlink() or not raw_source.is_file():
        raise SessionSourceRegistrationError("raw source must be a regular non-symlink file")
    source_bytes = raw_source.read_bytes()
    source_sha256 = sha256_bytes(source_bytes)
    producer_from_bundle(bundle)  # validates the structured contract before persistence
    source = _matching_source_registration(bundle, document_id=document_id, source_sha256=source_sha256)
    bundle_digest = _bundle_sha256(bundle)
    proposed = _metadata(
        document_id=document_id, source_bytes=source_bytes, source=source, bundle_sha256=bundle_digest,
    )
    return _persist_registration(
        registration_root=registration_root,
        proposed=proposed,
        source_bytes=source_bytes,
    )


def register_provenance_source(
    *,
    raw_source: Path,
    bundle: Mapping[str, Any],
    document_id: str,
    source_registration: Mapping[str, Any],
    registration_root: Path,
) -> dict[str, Any]:
    """Register exact context-only provenance without granting candidate authority.

    This is intentionally separate from ``register_source``.  The document must
    already be referenced by the validated bundle, must not be one of the
    SessionBrief/Amendment authority sources, and its external registration
    metadata must independently carry SESSION_TRADING_INPUT authority and match
    the exact raw bytes.  Persisting this evidence binding cannot add a candidate
    or alter the session authority chain.
    """

    if not isinstance(document_id, str) or not document_id.strip():
        raise SessionSourceRegistrationError("document_id is required")
    if raw_source.is_symlink() or not raw_source.is_file():
        raise SessionSourceRegistrationError("raw source must be a regular non-symlink file")
    producer_from_bundle(bundle)
    session_date, _, _ = _validate_bundle_shape(bundle)
    provenance_ids = _provenance_document_ids(bundle)
    authority_ids = {str(row.get("document_id")) for row in _registration_sources(bundle)}
    if document_id not in provenance_ids:
        raise SessionSourceRegistrationError("document_id is not referenced by bundle provenance")
    if document_id in authority_ids:
        raise SessionSourceRegistrationError("authority source must use register_source")

    source_bytes = raw_source.read_bytes()
    source_sha256 = sha256_bytes(source_bytes)
    try:
        checked = validate_source_document(source_registration, session_date, label="provenance source registration")
    except SessionAuthorityContractError as exc:
        raise SessionSourceRegistrationError(str(exc)) from exc
    if checked.get("document_id") != document_id:
        raise SessionSourceRegistrationError("provenance registration document_id does not match")
    if checked.get("source_sha256") != source_sha256:
        raise SessionSourceRegistrationError("provenance registration hash does not match raw source bytes")
    bundle_digest = _bundle_sha256(bundle)
    proposed = _metadata(
        document_id=document_id,
        source_bytes=source_bytes,
        source=checked,
        bundle_sha256=bundle_digest,
    )
    return _persist_registration(
        registration_root=registration_root,
        proposed=proposed,
        source_bytes=source_bytes,
    )


def _provenance_document_ids(bundle: Mapping[str, Any]) -> set[str]:
    _, _, events = _validate_bundle_shape(bundle)
    document_ids: set[str] = set()

    def evidence(value: object) -> None:
        if isinstance(value, Mapping):
            document_id = value.get("document_id")
            if isinstance(document_id, str) and document_id:
                document_ids.add(document_id)

    def idea(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        evidence(value.get("provenance"))
        candidate_ref = value.get("candidate_ref")
        if isinstance(candidate_ref, Mapping):
            evidence(candidate_ref.get("mapping_provenance"))
        grounds = value.get("grounds")
        if isinstance(grounds, list):
            for ground in grounds:
                if isinstance(ground, Mapping):
                    evidence(ground.get("source_evidence"))

    def context_items(value: object) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if not isinstance(item, Mapping):
                continue
            facts = item.get("supporting_facts")
            if isinstance(facts, list):
                for fact in facts:
                    if isinstance(fact, Mapping):
                        evidence(fact.get("provenance"))

    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        event_type = event.get("event_type")
        if event_type == "SessionBrief":
            for source in payload.get("source_documents", []):
                if isinstance(source, Mapping):
                    evidence(source)
            for candidate in payload.get("candidate_ideas", []):
                idea(candidate)
            context_items(payload.get("context_materials"))
        elif event_type == "MaterialContext":
            context_items(payload.get("materials"))
        elif event_type == "SessionBriefAmendment":
            evidence(payload.get("source"))
            for operation in payload.get("operations", []):
                if not isinstance(operation, Mapping):
                    continue
                idea(operation.get("record"))
                resolution = operation.get("resolution")
                if isinstance(resolution, Mapping):
                    evidence(resolution.get("source"))
    return document_ids


def validate_registered_bundle(
    *, bundle: Mapping[str, Any], registration_root: Path
) -> tuple[SessionAuthorityProducer, str, dict[str, Any]]:
    """Require every source/provenance document to have an exact private registration."""

    producer, published_at = producer_from_bundle(bundle)
    root = _resolved_store_root(registration_root)
    if not root.is_dir() or root.is_symlink():
        raise SessionSourceRegistrationError("registration root is missing or unsafe")
    _validate_permissions(root, _DIR_MODE, "registration root")
    rows = [metadata for metadata, _ in _load_all(root)]
    bundle_digest = _bundle_sha256(bundle)
    registered_for_bundle = {
        str(row["document_id"]): row for row in rows if row.get("bundle_sha256") == bundle_digest
    }
    session_date, _, _ = _validate_bundle_shape(bundle)
    registration_sources = _registration_sources(bundle)
    for source in registration_sources:
        document_id = source.get("document_id")
        if not isinstance(document_id, str) or document_id not in registered_for_bundle:
            raise SessionSourceRegistrationError("authorized session source document is not registered for this bundle")
        row = registered_for_bundle[document_id]
        checked = source
        if (
            checked["source_sha256"] != row["source_sha256"]
            or checked["registration_intent"] != row["registration_intent"]
            or checked["applicable_session_date"] != row["applicable_session_date"]
            or checked["source_role"] != row["source_role"]
            or checked["intent_authority"] != row["intent_authority"]
            or checked["intent_evidence_ref"] != row["intent_evidence_ref"]
            or checked.get("source_profile_id") != row["source_profile_id"]
            or checked.get("source_profile_version") != row["source_profile_version"]
        ):
            raise SessionSourceRegistrationError("authorized session source does not match its private registration")

    provenance_ids = _provenance_document_ids(bundle)
    missing = sorted(provenance_ids - set(registered_for_bundle))
    if missing:
        raise SessionSourceRegistrationError(
            "bundle provenance references unregistered source document ids: " + ", ".join(missing)
        )
    report = {
        "ok": True,
        "status": "valid",
        "session_date": session_date,
        "bundle_sha256": bundle_digest,
        "registered_document_ids": sorted(registered_for_bundle),
        "provenance_document_ids": sorted(provenance_ids),
    }
    return producer, published_at, report


def publish_registered_bundle(
    *, bundle: Mapping[str, Any], registration_root: Path, exchange_root: Path
) -> dict[str, Any]:
    """Publish only after every source/provenance binding passes registration validation."""

    producer, published_at, report = validate_registered_bundle(
        bundle=bundle, registration_root=registration_root
    )
    result = producer.publish_exchange(exchange_root, published_at=published_at)
    return {**result, "registration_status": report["status"], "bundle_sha256": report["bundle_sha256"]}


__all__ = [
    "REGISTRATION_SCHEMA_VERSION",
    "SessionSourceRegistrationError",
    "producer_from_bundle",
    "publish_registered_bundle",
    "register_provenance_source",
    "register_source",
    "validate_registered_bundle",
    "_read_bundle",
]
