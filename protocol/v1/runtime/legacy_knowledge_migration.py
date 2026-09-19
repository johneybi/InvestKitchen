from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from protocol.v1.session_authority.source_registration import (
    _registration_sources,
    _provenance_document_ids,
    producer_from_bundle,
    register_provenance_source,
    register_source,
    validate_registered_bundle,
)


QUEUE_SCHEMA_VERSION = 1


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive_source_path(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise ValueError("historical source path is invalid")
    archive_root = root.resolve()
    candidate = (archive_root / relative_path).resolve()
    try:
        candidate.relative_to(archive_root)
    except ValueError as exc:
        raise ValueError("historical source path escapes archive root") from exc
    return candidate


def _rebased_amendment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove legacy producer event identities; native producer binds its own chain."""

    value = copy.deepcopy(payload)
    for key in ("base_brief_id", "base_brief_hash", "previous_authority_event"):
        value.pop(key, None)
    return value


def reconstruct_session_bundles(legacy_root: Path) -> dict[str, dict[str, Any]]:
    """Reconstruct historical session bundles from the immutable legacy outbox.

    The content/provenance payloads are preserved. Only producer-specific causal
    identities on amendments are removed so the native producer can deterministically
    bind the migrated chain to its own event ids/hashes.
    """

    knowledge_root = legacy_root / "knowledge"
    index = _read_json(knowledge_root / "events/session_outbox/index.json")
    streams = index.get("streams")
    if not isinstance(streams, dict):
        raise ValueError("legacy session outbox streams are invalid")

    dates = sorted(
        stream_id.removeprefix("authority:")
        for stream_id in streams
        if stream_id.startswith("authority:")
    )
    bundles: dict[str, dict[str, Any]] = {}
    for session_date in dates:
        event_rows: list[tuple[int, int, dict[str, Any]]] = []
        published_at: list[str] = []
        for stream_rank, stream_id in enumerate((f"authority:{session_date}", f"context:{session_date}")):
            stream = streams.get(stream_id) or {}
            events = stream.get("events") if isinstance(stream, dict) else None
            if not isinstance(events, list):
                continue
            for offset, row in enumerate(events):
                if not isinstance(row, dict):
                    continue
                manifest = _read_json(knowledge_root / str(row["manifest_path"]))
                payload = _read_json(knowledge_root / str(row["payload_path"]))
                event_type = str(row.get("event_type") or "")
                if event_type == "SessionBriefAmendment":
                    payload = _rebased_amendment_payload(payload)
                event_rows.append((stream_rank, offset, {
                    "event_type": event_type,
                    "occurred_at": manifest["occurred_at"],
                    "effective_at": manifest.get("effective_at"),
                    "payload": payload,
                }))
                published_at.append(str(manifest.get("published_at") or manifest["occurred_at"]))

        authority = [row for rank, _, row in event_rows if rank == 0]
        context = [row for rank, _, row in event_rows if rank == 1]
        if not authority or authority[0]["event_type"] != "SessionBrief":
            continue
        bundle = {
            "bundle_version": 1,
            "session_date": session_date,
            "published_at": max(published_at),
            "events": authority + context,
        }
        producer_from_bundle(bundle)
        bundles[session_date] = bundle
    return bundles


def _registry_entries(legacy_root: Path) -> dict[str, dict[str, Any]]:
    registry = _read_json(legacy_root / "knowledge/indexes/document_registry.json")
    rows = registry.get("entries")
    if not isinstance(rows, list):
        raise ValueError("legacy document registry entries are invalid")
    return {
        str(row["document_id"]): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("document_id"), str)
    }


def _explicit_registrations(legacy_root: Path) -> dict[str, dict[str, Any]]:
    manifest = _read_json(legacy_root / "knowledge/events/registration_manifest.json")
    rows = manifest.get("documents")
    if not isinstance(rows, list):
        raise ValueError("legacy registration manifest documents are invalid")
    return {
        str(row["document_id"]): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("document_id"), str)
    }


def build_canonical_queue(legacy_root: Path) -> dict[str, Any]:
    legacy_root = legacy_root.resolve()
    registry = _registry_entries(legacy_root)
    explicit = _explicit_registrations(legacy_root)
    bundles = reconstruct_session_bundles(legacy_root)
    published_by_document: dict[str, str] = {}
    for session_date, bundle in bundles.items():
        for document_id in _provenance_document_ids(bundle):
            published_by_document[str(document_id)] = session_date

    entries: list[dict[str, Any]] = []
    included_documents: set[str] = set()
    status_counts: Counter[str] = Counter()
    for document_id, row in sorted(registry.items()):
        source_rel = row.get("canonical_source_path")
        source_kind = "canonical"
        source = legacy_root / source_rel if isinstance(source_rel, str) and source_rel else None
        if source is None or not source.is_file() or source.is_symlink():
            # Do not roam arbitrary legacy source_paths: the old document index also
            # indexed code/dependency files. Only an explicitly registered trading
            # source may recover from its known legacy inbox path.
            source = None
            if document_id in explicit:
                for fallback in row.get("legacy_source_paths") or []:
                    if not isinstance(fallback, str) or not fallback.startswith("local-knowledge/inbox/"):
                        continue
                    candidate = legacy_root / fallback
                    if candidate.is_file() and not candidate.is_symlink():
                        source_rel = fallback
                        source = candidate
                        source_kind = "legacy_inbox_recovery"
                        break
            if source is None:
                continue
        hashes = row.get("sha256_history")
        registered_sha = hashes[-1] if isinstance(hashes, list) and hashes else None
        actual_sha = _sha256(source)
        if registered_sha and registered_sha != actual_sha:
            raise ValueError(f"legacy source hash mismatch: {document_id}")

        metadata = row.get("document_metadata") if isinstance(row.get("document_metadata"), dict) else {}
        if document_id in published_by_document:
            status = "session_authority_published"
            action = "register_native_session_source"
            priority = 0
        elif document_id in explicit:
            status = "explicitly_registered_unpublished"
            action = "preserve_registration_without_publish"
            priority = 1
        else:
            status = "historical_knowledge_source"
            action = "semantic_review_before_native_knowledge_import"
            priority = 2
        status_counts[status] += 1
        entries.append({
            "document_id": document_id,
            "status": status,
            "priority": priority,
            "recommended_action": action,
            "canonical_source_path": source_rel,
            "source_path_kind": source_kind,
            "source_sha256": actual_sha,
            "source_bytes": source.stat().st_size,
            "document_type": metadata.get("document_type"),
            "speaker": metadata.get("speaker"),
            "published_at": metadata.get("published_at"),
            "session_date": published_by_document.get(document_id),
            "explicit_registration": explicit.get(document_id),
        })
        included_documents.add(document_id)

    # Preserve explicit registration authority even when the original raw source
    # bytes were already lost inside the legacy workspace. A staged normalized
    # artifact may help human recovery, but it must never be substituted for the
    # missing raw source hash or published as if it were exact source evidence.
    for document_id, registration in sorted(explicit.items()):
        if document_id in included_documents:
            continue
        row = registry.get(document_id) or {}
        metadata = row.get("document_metadata") if isinstance(row.get("document_metadata"), dict) else {}
        recovery_paths = sorted(
            path.relative_to(legacy_root).as_posix()
            for path in legacy_root.glob(f".work/**/{document_id}.md")
            if path.is_file() and not path.is_symlink()
        )
        status = "explicit_registration_source_missing"
        status_counts[status] += 1
        entries.append({
            "document_id": document_id,
            "status": status,
            "priority": 1,
            "recommended_action": "preserve_registration_metadata_and_recover_raw_source_if_available",
            "canonical_source_path": row.get("canonical_source_path"),
            "source_path_kind": "missing",
            "source_sha256": (row.get("sha256_history") or [None])[-1],
            "source_bytes": None,
            "document_type": metadata.get("document_type"),
            "speaker": metadata.get("speaker"),
            "published_at": metadata.get("published_at"),
            "session_date": registration.get("applicable_session_date"),
            "explicit_registration": registration,
            "recovery_artifacts": recovery_paths,
        })

    return {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "source_system": "legacy-trademind-framework",
        "migration_policy": {
            "current_authority_mutation": False,
            "published_session_sources": "register exact raw source against reconstructed native-valid historical bundle",
            "explicit_unpublished_sources": "preserve registration provenance without inventing publication",
            "other_sources": "review semantics/provenance before creating native Knowledge evidence/claims",
        },
        "summary": {
            "source_documents": len(entries),
            "by_status": dict(sorted(status_counts.items())),
            "reconstructed_session_bundles": len(bundles),
        },
        "session_bundles": bundles,
        "entries": sorted(entries, key=lambda item: (int(item["priority"]), str(item["document_id"]))),
    }


def write_canonical_queue(legacy_root: Path, output: Path) -> dict[str, Any]:
    queue = build_canonical_queue(legacy_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return queue


def register_historical_session_sources(
    *,
    archive_files_root: Path,
    queue: dict[str, Any],
    registration_root: Path,
) -> dict[str, Any]:
    """Register historical session source bytes without publishing any exchange.

    Authority sources use the native SessionBrief/Amendment registration path.
    Context-only provenance uses the separate non-authority provenance path.  The
    function validates every reconstructed bundle after registration but never
    calls ``publish_registered_bundle`` and therefore cannot replace an active
    session exchange.
    """

    if queue.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise ValueError("unsupported canonical migration queue version")
    entries = queue.get("entries")
    bundles = queue.get("session_bundles")
    if not isinstance(entries, list) or not isinstance(bundles, dict):
        raise ValueError("canonical migration queue is incomplete")
    by_id = {
        str(row["document_id"]): row
        for row in entries
        if isinstance(row, dict) and isinstance(row.get("document_id"), str)
    }
    registrations: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []

    for session_date, bundle in sorted(bundles.items()):
        if not isinstance(bundle, dict):
            raise ValueError(f"historical bundle is invalid: {session_date}")
        authority_ids = {str(row.get("document_id")) for row in _registration_sources(bundle)}
        provenance_ids = _provenance_document_ids(bundle)
        for document_id in sorted(provenance_ids):
            row = by_id.get(document_id)
            if row is None:
                raise ValueError(f"historical provenance is missing from migration queue: {document_id}")
            source_rel = row.get("canonical_source_path")
            if not isinstance(source_rel, str) or not source_rel:
                raise ValueError(f"historical source path is missing: {document_id}")
            source = _archive_source_path(archive_files_root, source_rel)
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"historical source bytes are unavailable: {document_id}")

            if document_id in authority_ids:
                result = register_source(
                    raw_source=source,
                    bundle=bundle,
                    document_id=document_id,
                    registration_root=registration_root,
                )
                binding = "authority_source"
            else:
                legacy_registration = row.get("explicit_registration")
                if not isinstance(legacy_registration, dict):
                    raise ValueError(f"context provenance lacks explicit registration metadata: {document_id}")
                source_registration = {
                    "document_id": document_id,
                    "source_sha256": row.get("source_sha256"),
                    "registration_intent": legacy_registration.get("registration_intent"),
                    "applicable_session_date": legacy_registration.get("applicable_session_date"),
                    "source_role": legacy_registration.get("source_role"),
                    "intent_authority": legacy_registration.get("intent_authority"),
                    "intent_evidence_ref": legacy_registration.get("intent_evidence_ref"),
                }
                if legacy_registration.get("source_profile_id") is not None:
                    source_registration["source_profile_id"] = legacy_registration.get("source_profile_id")
                    source_registration["source_profile_version"] = legacy_registration.get("source_profile_version")
                result = register_provenance_source(
                    raw_source=source,
                    bundle=bundle,
                    document_id=document_id,
                    source_registration=source_registration,
                    registration_root=registration_root,
                )
                binding = "context_provenance"
            registrations.append({
                "session_date": session_date,
                "document_id": document_id,
                "binding": binding,
                "status": result["status"],
                "registration_id": result["registration_id"],
            })

        _, _, report = validate_registered_bundle(bundle=bundle, registration_root=registration_root)
        validations.append({
            "session_date": session_date,
            "status": report["status"],
            "registered_document_ids": report["registered_document_ids"],
            "provenance_document_ids": report["provenance_document_ids"],
        })

    return {
        "ok": True,
        "published": False,
        "registrations": registrations,
        "validations": validations,
        "summary": {
            "registrations": len(registrations),
            "authority_sources": sum(1 for row in registrations if row["binding"] == "authority_source"),
            "context_provenance_sources": sum(1 for row in registrations if row["binding"] == "context_provenance"),
            "validated_bundles": len(validations),
        },
    }
