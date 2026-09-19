from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint
from protocol.v1.runtime.native_write_store import ApplyRejected, ApplyValidation, validate_apply


_EVIDENCE_AUTHORITY = {
    "primary", "transcript", "official", "secondary", "user_supplied",
    "derived", "model_generated", "unverified",
}
_EVIDENCE_VERIFICATION = {"verified", "partial", "unverified", "conflicted", "workflow_verified"}
_FRESHNESS = {"current", "recent", "stale", "historical", "unknown"}
_DERIVATION = {"direct_statement", "paraphrase", "inference", "derived", "unknown"}
_REGISTRATION = {"candidate", "canonical", "rejected", "superseded"}
_PROVENANCE = {"verified", "partial", "unverified", "conflicted"}
_FIDELITY = {"direct", "faithful_paraphrase", "inference", "unknown"}
_TRUTH = {"observed_fact", "attributed_opinion", "estimate", "hypothesis", "contradicted", "unknown"}
_APPLICABILITY = {"applicable", "conditional", "stale", "expired", "out_of_scope", "unresolved"}
_CONFIDENCE = {"low", "medium", "high", "unknown"}
_PRECISION = {"source_exact", "date_only", "session", "inferred", "unknown"}


class KnowledgeRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class KnowledgeStoreCorrupt(RuntimeError):
    pass


class KnowledgeStoreConflict(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _identifier(value: Any, label: str) -> str:
    if not _nonempty(value) or len(value) > 256:
        raise KnowledgeRejected(f"{label}_invalid")
    return str(value)


def _timepoint(value: Any, label: str) -> datetime:
    if not isinstance(value, dict) or set(value) - {"value", "precision", "inference_basis"}:
        raise KnowledgeRejected(f"{label}_invalid")
    raw = value.get("value")
    precision = value.get("precision")
    if not _nonempty(raw) or precision not in _PRECISION:
        raise KnowledgeRejected(f"{label}_invalid")
    if precision in {"session", "inferred"} and not _nonempty(value.get("inference_basis")):
        raise KnowledgeRejected(f"{label}_invalid")
    if precision not in {"session", "inferred"} and "inference_basis" in value:
        raise KnowledgeRejected(f"{label}_invalid")
    try:
        if "T" in raw:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            return parsed.astimezone(timezone.utc)
        parsed_date = date.fromisoformat(raw)
        return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    except ValueError as exc:
        raise KnowledgeRejected(f"{label}_invalid") from exc


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise KnowledgeRejected(f"{label}_invalid")
    return value


def _validate_source(source: Any) -> None:
    fields = {"source_type", "provider_or_publisher", "speaker", "document_id", "url_or_external_id"}
    if not isinstance(source, dict) or set(source) - fields:
        raise KnowledgeRejected("evidence_source_invalid")
    if not _nonempty(source.get("source_type")) or not _nonempty(source.get("provider_or_publisher")):
        raise KnowledgeRejected("evidence_source_invalid")
    for key in ("speaker", "document_id", "url_or_external_id"):
        if key in source and source[key] is not None and not _nonempty(source[key]):
            raise KnowledgeRejected("evidence_source_invalid")


def _validate_evidence(row: Any) -> str:
    required = {
        "evidence_id", "evidence_kind", "subject_refs", "source", "authority_class",
        "verification", "freshness", "recorded_at",
    }
    allowed = required | {
        "source_locator", "content_ref_or_value", "published_at", "effective_at", "observed_at",
        "retrieved_at", "content_hash", "lifecycle_scope", "context_id",
    }
    if not isinstance(row, dict) or required - set(row) or set(row) - allowed:
        raise KnowledgeRejected("evidence_fields_invalid")
    evidence_id = _identifier(row.get("evidence_id"), "evidence_identity")
    if not _nonempty(row.get("evidence_kind")):
        raise KnowledgeRejected("evidence_identity_invalid")
    _strings(row.get("subject_refs"), "evidence_subject_refs")
    _validate_source(row.get("source"))
    if row.get("authority_class") not in _EVIDENCE_AUTHORITY:
        raise KnowledgeRejected("evidence_authority_invalid")
    if row.get("verification") not in _EVIDENCE_VERIFICATION:
        raise KnowledgeRejected("evidence_verification_invalid")
    if row.get("freshness") not in _FRESHNESS:
        raise KnowledgeRejected("evidence_freshness_invalid")
    recorded = _timepoint(row.get("recorded_at"), "evidence_recorded_at")
    for key in ("published_at", "effective_at", "observed_at", "retrieved_at"):
        if key in row:
            _timepoint(row[key], f"evidence_{key}")
    retrieved = _timepoint(row["retrieved_at"], "evidence_retrieved_at") if "retrieved_at" in row else None
    if retrieved is not None and recorded < retrieved:
        raise KnowledgeRejected("evidence_recorded_before_retrieved")
    content_hash = row.get("content_hash")
    if content_hash is not None and (
        not isinstance(content_hash, str) or len(content_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in content_hash)
    ):
        raise KnowledgeRejected("evidence_content_hash_invalid")
    if "lifecycle_scope" in row and row.get("lifecycle_scope") not in {"canonical", "context_only"}:
        raise KnowledgeRejected("evidence_lifecycle_invalid")
    for key in ("source_locator", "context_id"):
        if key in row and row[key] is not None and not isinstance(row[key], str):
            raise KnowledgeRejected("evidence_optional_field_invalid")
    return str(evidence_id)


def _validate_claim(row: Any, evidence_ids: set[str]) -> str:
    required = {
        "claim_id", "statement", "subject_refs", "claim_type", "conditions", "invalidation",
        "evidence_refs", "derivation_type", "registration_state", "provenance_verification",
        "semantic_fidelity", "truth_status", "applicability_status",
    }
    allowed = required | {"speaker", "stance", "horizon", "effective_at", "valid_until", "confidence"}
    if not isinstance(row, dict) or required - set(row) or set(row) - allowed:
        raise KnowledgeRejected("claim_fields_invalid")
    claim_id = _identifier(row.get("claim_id"), "claim_identity")
    if not _nonempty(row.get("statement")) or not _nonempty(row.get("claim_type")):
        raise KnowledgeRejected("claim_identity_invalid")
    _strings(row.get("subject_refs"), "claim_subject_refs")
    _strings(row.get("conditions"), "claim_conditions")
    _strings(row.get("invalidation"), "claim_invalidation")
    refs = _strings(row.get("evidence_refs"), "claim_evidence_refs")
    for ref in refs:
        _identifier(ref, "claim_evidence_ref")
    if not refs or len(refs) != len(set(refs)) or not set(refs) <= evidence_ids:
        raise KnowledgeRejected("claim_evidence_refs_invalid")
    if row.get("derivation_type") not in _DERIVATION:
        raise KnowledgeRejected("claim_derivation_invalid")
    if row.get("registration_state") not in _REGISTRATION:
        raise KnowledgeRejected("claim_registration_invalid")
    if row.get("provenance_verification") not in _PROVENANCE:
        raise KnowledgeRejected("claim_provenance_invalid")
    if row.get("semantic_fidelity") not in _FIDELITY:
        raise KnowledgeRejected("claim_fidelity_invalid")
    if row.get("truth_status") not in _TRUTH:
        raise KnowledgeRejected("claim_truth_status_invalid")
    if row.get("applicability_status") not in _APPLICABILITY:
        raise KnowledgeRejected("claim_applicability_invalid")
    if "confidence" in row and row.get("confidence") not in _CONFIDENCE:
        raise KnowledgeRejected("claim_confidence_invalid")
    for key in ("speaker", "stance", "horizon"):
        if key in row and row[key] is not None and not isinstance(row[key], str):
            raise KnowledgeRejected("claim_optional_field_invalid")
    effective = _timepoint(row["effective_at"], "claim_effective_at") if "effective_at" in row else None
    valid_until = _timepoint(row["valid_until"], "claim_valid_until") if "valid_until" in row else None
    if effective is not None and valid_until is not None and valid_until < effective:
        raise KnowledgeRejected("claim_valid_until_before_effective_at")
    return str(claim_id)


def _validate_current_state(value: Any) -> dict[str, Any]:
    allowed = {
        "as_of", "valid_until", "freshness_status", "situational_usable", "market_state", "stance",
        "confidence", "summary", "action_bias", "confirmation_conditions", "invalidation_conditions",
        "outlook", "reconciliation_scope", "data_quality_notes",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise KnowledgeRejected("current_state_fields_invalid")
    if "as_of" in value:
        _timepoint(value["as_of"], "current_state_as_of")
    if "valid_until" in value:
        _timepoint(value["valid_until"], "current_state_valid_until")
    if "as_of" in value and "valid_until" in value:
        if _timepoint(value["valid_until"], "current_state_valid_until") < _timepoint(value["as_of"], "current_state_as_of"):
            raise KnowledgeRejected("current_state_valid_until_before_as_of")
    if "freshness_status" in value and value.get("freshness_status") not in _FRESHNESS:
        raise KnowledgeRejected("current_state_freshness_invalid")
    if "confidence" in value and value.get("confidence") not in _CONFIDENCE:
        raise KnowledgeRejected("current_state_confidence_invalid")
    if "situational_usable" in value and not isinstance(value["situational_usable"], bool):
        raise KnowledgeRejected("current_state_situational_usable_invalid")
    for key in ("market_state", "stance", "summary", "reconciliation_scope"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise KnowledgeRejected("current_state_value_invalid")
    for key in ("action_bias", "confirmation_conditions", "invalidation_conditions", "data_quality_notes"):
        if key in value:
            _strings(value[key], f"current_state_{key}")
    if "outlook" in value and (
        not isinstance(value["outlook"], list) or any(not isinstance(row, dict) for row in value["outlook"])
    ):
        raise KnowledgeRejected("current_state_outlook_invalid")
    return dict(value)


def validate_knowledge_request(request: Any) -> dict[str, Any]:
    required = {
        "protocol_version", "request_type", "generation_id", "generated_at", "current_state", "evidence", "claims",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise KnowledgeRejected("knowledge_request_fields_invalid")
    if request.get("protocol_version") != PROTOCOL_VERSION or request.get("request_type") != "knowledge.commit":
        raise KnowledgeRejected("knowledge_request_version_invalid")
    _identifier(request.get("generation_id"), "knowledge_generation_id")
    _timepoint(request.get("generated_at"), "knowledge_generated_at")
    evidence = request.get("evidence")
    claims = request.get("claims")
    if not isinstance(evidence, list) or not isinstance(claims, list):
        raise KnowledgeRejected("knowledge_records_invalid")
    evidence_ids = [_validate_evidence(row) for row in evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise KnowledgeRejected("knowledge_duplicate_evidence_id")
    claim_ids = [_validate_claim(row, set(evidence_ids)) for row in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise KnowledgeRejected("knowledge_duplicate_claim_id")
    _validate_current_state(request.get("current_state"))
    return request


def build_knowledge_preview(
    request: dict[str, Any],
    *,
    base_generation_id: str | None,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    validate_knowledge_request(request)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if ttl_seconds < 1 or ttl_seconds > 900:
        raise KnowledgeRejected("knowledge_preview_ttl_invalid")
    operation_id = f"operation:{uuid.uuid4().hex}"
    target = {"generation_id": request["generation_id"]}
    payload_digest = digest(request)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "resource_type": "knowledge",
        "operation": {
            "operation_id": operation_id,
            "request_id": f"request:{uuid.uuid4().hex}",
            "idempotency_key": f"knowledge:{request['generation_id']}",
            "actor": "web-gpt",
            "action": "knowledge.commit",
            "target": target,
            "requested_at": timepoint(current.isoformat().replace("+00:00", "Z")),
            "state": "awaiting_approval",
        },
        "preview": {
            "preview_id": f"preview:{uuid.uuid4().hex}",
            "operation_id": operation_id,
            "action": "knowledge.commit",
            "target": target,
            "base_version": base_generation_id,
            "canonical_payload": dict(request),
            "payload_digest": payload_digest,
            "expected_effect": "Append one validated native Knowledge generation for advisory reads.",
            "warnings": ["This updates Knowledge context only; it does not create a Decision or Transaction."],
            "generated_at": timepoint(current.isoformat().replace("+00:00", "Z")),
            "expires_at": timepoint((current + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")),
        },
        "approval_required": True,
        "approval": None,
        "apply_state": "not_applied",
    }


class NativeKnowledgeStore:
    """Small append-only native Knowledge overlay for personal advisory context."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.journal_path = self.root / "knowledge-journal.jsonl"
        self.lock_path = self.root / ".knowledge-journal.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise KnowledgeStoreCorrupt(f"invalid knowledge journal line {line_number}") from exc
                if not isinstance(row, dict) or row.get("journal_version") != 1:
                    raise KnowledgeStoreCorrupt(f"invalid knowledge journal line {line_number}")
                request = row.get("request")
                try:
                    validate_knowledge_request(request)
                except KnowledgeRejected as exc:
                    raise KnowledgeStoreCorrupt(f"invalid knowledge request at line {line_number}") from exc
                if row.get("payload_digest") != digest(request) or row.get("generation_id") != request.get("generation_id"):
                    raise KnowledgeStoreCorrupt(f"knowledge digest mismatch at line {line_number}")
                rows.append(row)
        return rows

    def read_journal(self) -> list[dict[str, Any]]:
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def latest_generation_id(self) -> str | None:
        rows = self.read_journal()
        return str(rows[-1]["generation_id"]) if rows else None

    def has_generations(self) -> bool:
        return bool(self.read_journal())

    def overlay(self) -> dict[str, Any] | None:
        rows = self.read_journal()
        if not rows:
            return None
        evidence: dict[str, dict[str, Any]] = {}
        claims: dict[str, dict[str, Any]] = {}
        current_state: dict[str, Any] = {}
        for row in rows:
            request = row["request"]
            for item in request["evidence"]:
                evidence[str(item["evidence_id"])] = dict(item)
            for item in request["claims"]:
                claims[str(item["claim_id"])] = dict(item)
            current_state.update(dict(request["current_state"]))
        latest = rows[-1]
        latest_request = latest["request"]
        committed_at = latest["committed_at"]
        return {
            "generated_at": latest_request["generated_at"],
            "knowledge_generation": {
                "generation_id": latest_request["generation_id"],
                "commit_status": "committed",
                "committed_at": committed_at,
                "registry_or_input_digest": latest["payload_digest"],
                "artifact_refs": [],
            },
            "evidence": list(evidence.values()),
            "claims": list(claims.values()),
            "current_state": current_state,
        }

    def commit(
        self,
        *,
        preview: dict[str, Any],
        validation: ApplyValidation,
        committed_at: datetime,
    ) -> dict[str, Any]:
        mutation = preview.get("preview") if isinstance(preview, dict) else None
        if not isinstance(mutation, dict) or preview.get("resource_type") != "knowledge":
            raise KnowledgeRejected("knowledge_preview_invalid")
        request = mutation.get("canonical_payload")
        validate_knowledge_request(request)
        payload_digest = digest(request)
        if mutation.get("payload_digest") != payload_digest:
            raise KnowledgeRejected("knowledge_payload_digest_invalid")
        generation_id = str(request["generation_id"])
        with self._locked(exclusive=True):
            rows = self._read_unlocked()
            for row in rows:
                if row.get("generation_id") != generation_id:
                    continue
                if row.get("payload_digest") != payload_digest:
                    raise KnowledgeStoreConflict("knowledge_generation_conflict")
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "generation_id": generation_id,
                    "payload_digest": payload_digest,
                    "committed_at": row["committed_at"],
                    "evidence_count": len(request["evidence"]),
                    "claim_count": len(request["claims"]),
                    "replayed": True,
                }
            current_generation = str(rows[-1]["generation_id"]) if rows else None
            if mutation.get("base_version") != current_generation:
                raise KnowledgeStoreConflict("knowledge_base_version_conflict")
            committed_tp = timepoint(committed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
            entry = {
                "journal_version": 1,
                "event_type": "knowledge_generation_committed",
                "generation_id": generation_id,
                "payload_digest": payload_digest,
                "request": request,
                "approval_verification_ref": validation.approval_verification_ref,
                "committed_at": committed_tp,
            }
            encoded = (canonical_json(entry) + "\n").encode("utf-8")
            fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "generation_id": generation_id,
            "payload_digest": payload_digest,
            "committed_at": committed_tp,
            "evidence_count": len(request["evidence"]),
            "claim_count": len(request["claims"]),
            "replayed": False,
        }


def apply_knowledge_commit(
    preview: dict[str, Any],
    approval: dict[str, Any],
    *,
    principal: dict[str, Any],
    grant: dict[str, Any],
    approval_verifier,
    store: NativeKnowledgeStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if preview.get("resource_type") != "knowledge":
        raise ApplyRejected("unsupported_resource_type")
    validation = validate_apply(
        preview,
        approval,
        principal=principal,
        grant=grant,
        approval_verifier=approval_verifier,
        now=current,
        current_base_version=store.latest_generation_id(),
    )
    if not validation.allowed:
        raise ApplyRejected(validation.reason_code)
    return store.commit(preview=preview, validation=validation, committed_at=current)
