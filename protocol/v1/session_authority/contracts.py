"""Self-contained SessionBrief/Amendment/MaterialContext v1 semantics.

This module intentionally does not import the legacy ``trademind-framework``.
It preserves only the immutable wire semantics still consumed by the trading
runtime, with stricter fail-closed checks where the current consumer already
requires them (for example actionable candidates must resolve to a symbol).
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "1.0"
SESSION_TIMEZONE = "Asia/Seoul"
MARKET = "KRX"

SOURCE_ROLES = frozenset({"PRIMARY_CHECKPOINT", "PREOPEN_SUPPLEMENT", "INTRADAY_UPDATE"})
INTENT_AUTHORITIES = frozenset({"USER_EXPLICIT", "TRUSTED_SOURCE_PROFILE"})
SESSION_RELEVANCE = frozenset({"ACTIONABLE_TODAY", "CONTEXT_ONLY", "AMBIGUOUS"})
AMENDMENT_OPERATIONS = frozenset(
    {"add", "replace", "withdraw_eligibility", "hard_invalidation", "restore_eligibility"}
)

_HEX = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_SYMBOL = re.compile(r"^[0-9]{6}$")
_STAR_PRIORITY = re.compile(r"^[★☆]{1,5}$")


class SessionAuthorityContractError(ValueError):
    """A payload cannot safely enter the native session-authority stream."""


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the established v1 producer canonical JSON bytes."""

    return (json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def event_hash_input_bytes(manifest_without_event_hash: Mapping[str, Any], payload_bytes: bytes) -> bytes:
    return payload_bytes + b"\n" + canonical_json_bytes(manifest_without_event_hash)


def record_hash(record: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(record))


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _digest(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX.fullmatch(value))


def _session_date(value: object) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def validate_utc_timestamp(value: object, *, label: str = "timestamp") -> str:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise SessionAuthorityContractError(f"{label} must be an exact UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SessionAuthorityContractError(f"{label} is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise SessionAuthorityContractError(f"{label} must be UTC")
    return value


def _reject_floats(value: object, label: str = "payload") -> None:
    # The trading consumer intentionally rejects JSON floats at this boundary.
    if isinstance(value, float):
        raise SessionAuthorityContractError(f"{label} contains a float")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_floats(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{label}[{index}]")


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise SessionAuthorityContractError(f"{label} fields are invalid")


def _validate_source_evidence(value: object, *, source_ids: set[str] | None, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionAuthorityContractError(f"{label} must be an object")
    required = {"document_id", "source_locator"}
    allowed = required | {"text_sha256"}
    if required - set(value) or set(value) - allowed:
        raise SessionAuthorityContractError(f"{label} fields are invalid")
    if not _nonempty(value.get("document_id")) or not _nonempty(value.get("source_locator")):
        raise SessionAuthorityContractError(f"{label} identity is invalid")
    if "text_sha256" in value and not _digest(value["text_sha256"]):
        raise SessionAuthorityContractError(f"{label}.text_sha256 is invalid")
    if source_ids is not None and value["document_id"] not in source_ids:
        raise SessionAuthorityContractError(f"{label} source is not authorized")
    return dict(value)


def validate_source_document(value: object, session_date: str, *, label: str = "source") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionAuthorityContractError(f"{label} must be an object")
    required = {
        "document_id",
        "source_sha256",
        "registration_intent",
        "applicable_session_date",
        "source_role",
        "intent_authority",
        "intent_evidence_ref",
    }
    allowed = required | {"published_at", "document_type", "source_profile_id", "source_profile_version"}
    if required - set(value) or set(value) - allowed:
        raise SessionAuthorityContractError(f"{label} fields are invalid")
    if not _nonempty(value["document_id"]) or not _digest(value["source_sha256"]):
        raise SessionAuthorityContractError(f"{label} identity is invalid")
    if value["registration_intent"] != "SESSION_TRADING_INPUT":
        raise SessionAuthorityContractError(f"{label} lacks session trading authority")
    if value["applicable_session_date"] != session_date:
        raise SessionAuthorityContractError(f"{label} session date is invalid")
    if value["source_role"] not in SOURCE_ROLES:
        raise SessionAuthorityContractError(f"{label} source role is invalid")
    if value["intent_authority"] not in INTENT_AUTHORITIES or not _nonempty(value["intent_evidence_ref"]):
        raise SessionAuthorityContractError(f"{label} intent authority is invalid")
    profile_id = value.get("source_profile_id")
    profile_version = value.get("source_profile_version")
    if (profile_id is None) != (profile_version is None):
        raise SessionAuthorityContractError(f"{label} source profile identity is incomplete")
    if profile_id is not None and (not _nonempty(profile_id) or not _nonempty(profile_version)):
        raise SessionAuthorityContractError(f"{label} source profile identity is invalid")
    if value["intent_authority"] == "TRUSTED_SOURCE_PROFILE" and profile_id is None:
        raise SessionAuthorityContractError(f"{label} trusted source profile is missing")
    for field in ("published_at", "document_type"):
        if field in value and value[field] is not None and not isinstance(value[field], str):
            raise SessionAuthorityContractError(f"{label}.{field} must be a string or null")
    return dict(value)


def _validate_candidate_ref(value: object, candidate: str, source_ids: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionAuthorityContractError(f"{label} must be an object")
    required = {"kind", "source_value", "mapping_status", "canonical_symbol", "mapping_provenance"}
    allowed = required | {"cluster_id", "cluster_version"}
    if required - set(value) or set(value) - allowed:
        raise SessionAuthorityContractError(f"{label} fields are invalid")
    if value["kind"] not in {"SYMBOL", "GROUP"} or value["source_value"] != candidate:
        raise SessionAuthorityContractError(f"{label} candidate identity is invalid")
    status = value["mapping_status"]
    if status == "RESOLVED":
        if not isinstance(value["canonical_symbol"], str) or not _SYMBOL.fullmatch(value["canonical_symbol"]):
            raise SessionAuthorityContractError(f"{label} resolved symbol is invalid")
        _validate_source_evidence(value["mapping_provenance"], source_ids=source_ids, label=f"{label}.mapping_provenance")
    elif status == "UNRESOLVED":
        if value["canonical_symbol"] is not None or value["mapping_provenance"] is not None:
            raise SessionAuthorityContractError(f"{label} unresolved mapping must be empty")
    elif status == "CONFLICT":
        if value["canonical_symbol"] is not None:
            raise SessionAuthorityContractError(f"{label} conflict cannot select a symbol")
        _validate_source_evidence(value["mapping_provenance"], source_ids=source_ids, label=f"{label}.mapping_provenance")
    else:
        raise SessionAuthorityContractError(f"{label} mapping status is invalid")
    cluster_id, cluster_version = value.get("cluster_id"), value.get("cluster_version")
    if (cluster_id is None) != (cluster_version is None):
        raise SessionAuthorityContractError(f"{label} cluster identity is incomplete")
    if cluster_id is not None and (not _nonempty(cluster_id) or not _nonempty(cluster_version)):
        raise SessionAuthorityContractError(f"{label} cluster identity is invalid")
    return dict(value)


def _validate_priority(value: object, *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise SessionAuthorityContractError(f"{label} must be an object")
    fields = {
        "scale_id",
        "scale_version",
        "label",
        "source_stated",
        "document_order",
        "section_order",
        "item_order",
        "profile_id",
        "profile_version",
    }
    _exact(value, fields, label)
    if not all(_nonempty(value[field]) for field in ("scale_id", "scale_version", "label")):
        raise SessionAuthorityContractError(f"{label} identity is invalid")
    if not isinstance(value["source_stated"], bool):
        raise SessionAuthorityContractError(f"{label}.source_stated must be boolean")
    if not all(
        isinstance(value[field], int) and not isinstance(value[field], bool) and value[field] >= 0
        for field in ("document_order", "section_order", "item_order")
    ):
        raise SessionAuthorityContractError(f"{label} order is invalid")
    profile_id, profile_version = value["profile_id"], value["profile_version"]
    if (profile_id is None) != (profile_version is None):
        raise SessionAuthorityContractError(f"{label} profile identity is incomplete")
    if profile_id is not None and (not _nonempty(profile_id) or not _nonempty(profile_version)):
        raise SessionAuthorityContractError(f"{label} profile identity is invalid")
    if str(value["scale_id"]).startswith("source_profile:"):
        if (
            profile_id is None
            or value["source_stated"] is not True
            or not _STAR_PRIORITY.fullmatch(str(value["label"]))
            or "★" not in str(value["label"])
        ):
            raise SessionAuthorityContractError(f"{label} source-profile priority is invalid")


def validate_session_idea(value: object, source_ids: set[str], *, label: str = "idea") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionAuthorityContractError(f"{label} must be an object")
    required = {
        "idea_id",
        "record_version",
        "candidate",
        "candidate_ref",
        "theme",
        "catalyst",
        "source_priority",
        "confirmation",
        "invalidation",
        "grounds",
        "conditions",
        "session_relevance",
        "provenance",
    }
    allowed = required | {"scope_type", "document_order"}
    if required - set(value) or set(value) - allowed:
        raise SessionAuthorityContractError(f"{label} fields are invalid")
    if not _nonempty(value["idea_id"]) or not _nonempty(value["candidate"]):
        raise SessionAuthorityContractError(f"{label} identity is invalid")
    if not isinstance(value["record_version"], int) or isinstance(value["record_version"], bool) or value["record_version"] < 1:
        raise SessionAuthorityContractError(f"{label}.record_version is invalid")
    if value["session_relevance"] not in SESSION_RELEVANCE:
        raise SessionAuthorityContractError(f"{label}.session_relevance is invalid")
    for field in ("theme", "catalyst", "scope_type"):
        if field in value and value[field] is not None and not isinstance(value[field], str):
            raise SessionAuthorityContractError(f"{label}.{field} must be a string or null")
    if "document_order" in value and (
        not isinstance(value["document_order"], int) or isinstance(value["document_order"], bool) or value["document_order"] < 0
    ):
        raise SessionAuthorityContractError(f"{label}.document_order is invalid")
    if not isinstance(value["confirmation"], list) or not isinstance(value["invalidation"], list):
        raise SessionAuthorityContractError(f"{label} confirmation/invalidation must be lists")
    candidate_ref = _validate_candidate_ref(value["candidate_ref"], str(value["candidate"]), source_ids, label=f"{label}.candidate_ref")
    if candidate_ref["mapping_status"] != "RESOLVED" and value["session_relevance"] == "ACTIONABLE_TODAY":
        raise SessionAuthorityContractError(f"{label} unresolved candidate cannot be actionable")
    provenance = value["provenance"]
    if not isinstance(provenance, Mapping) or not _nonempty(provenance.get("document_id")) or provenance["document_id"] not in source_ids:
        raise SessionAuthorityContractError(f"{label}.provenance is unauthorized")
    _validate_priority(value["source_priority"], label=f"{label}.source_priority")
    grounds = value["grounds"]
    if not isinstance(grounds, list) or not grounds:
        raise SessionAuthorityContractError(f"{label}.grounds must be non-empty")
    for index, ground in enumerate(grounds):
        ground_label = f"{label}.grounds[{index}]"
        if not isinstance(ground, Mapping):
            raise SessionAuthorityContractError(f"{ground_label} must be an object")
        _exact(ground, {"ground_id", "ground_version", "relation", "status", "source_evidence"}, ground_label)
        if not _nonempty(ground["ground_id"]) or not _nonempty(ground["ground_version"]):
            raise SessionAuthorityContractError(f"{ground_label} identity is invalid")
        if ground["relation"] not in {"REQUIRED_ALL", "ANY_ONE", "SUPPORTING", "UNKNOWN"}:
            raise SessionAuthorityContractError(f"{ground_label}.relation is invalid")
        if ground["status"] not in {"ACTIVE", "AMBIGUOUS", "WITHDRAWN", "INVALIDATED"}:
            raise SessionAuthorityContractError(f"{ground_label}.status is invalid")
        _validate_source_evidence(ground["source_evidence"], source_ids=source_ids, label=f"{ground_label}.source_evidence")
    conditions = value["conditions"]
    if not isinstance(conditions, list):
        raise SessionAuthorityContractError(f"{label}.conditions must be a list")
    for index, condition in enumerate(conditions):
        condition_label = f"{label}.conditions[{index}]"
        if not isinstance(condition, Mapping):
            raise SessionAuthorityContractError(f"{condition_label} must be an object")
        _exact(condition, {"kind", "text", "source_locator", "executionability", "rule_ref"}, condition_label)
        if condition["kind"] not in {"CONFIRMATION", "WAIT", "NO_CHASE", "EXHAUSTION", "INVALIDATION"}:
            raise SessionAuthorityContractError(f"{condition_label}.kind is invalid")
        if not _nonempty(condition["text"]) or len(condition["text"]) > 512 or not _nonempty(condition["source_locator"]):
            raise SessionAuthorityContractError(f"{condition_label} text/locator is invalid")
        if condition["executionability"] == "NON_EXECUTABLE":
            if condition["rule_ref"] is not None:
                raise SessionAuthorityContractError(f"{condition_label}.rule_ref must be null")
        elif condition["executionability"] == "DETERMINISTIC_REFERENCE":
            if not _nonempty(condition["rule_ref"]):
                raise SessionAuthorityContractError(f"{condition_label}.rule_ref is required")
        else:
            raise SessionAuthorityContractError(f"{condition_label}.executionability is invalid")
    return dict(value)


def _validate_context_item(value: object, source_ids: set[str] | None, *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise SessionAuthorityContractError(f"{label} must be an object")
    fields = {"context_id", "record_version", "horizon", "supporting_facts", "related_idea_ids", "candidate_authority"}
    _exact(value, fields, label)
    if not _nonempty(value["context_id"]) or not isinstance(value["record_version"], int) or isinstance(value["record_version"], bool) or value["record_version"] < 1:
        raise SessionAuthorityContractError(f"{label} identity is invalid")
    if value["horizon"] not in {"INTRADAY", "SESSION", "SWING", "LONG_TERM", "UNKNOWN"} or value["candidate_authority"] != "NONE":
        raise SessionAuthorityContractError(f"{label} context authority/horizon is invalid")
    if not isinstance(value["related_idea_ids"], list) or any(not _nonempty(item) for item in value["related_idea_ids"]):
        raise SessionAuthorityContractError(f"{label}.related_idea_ids is invalid")
    facts = value["supporting_facts"]
    if not isinstance(facts, list) or not facts:
        raise SessionAuthorityContractError(f"{label}.supporting_facts must be non-empty")
    for index, fact in enumerate(facts):
        fact_label = f"{label}.supporting_facts[{index}]"
        if not isinstance(fact, Mapping):
            raise SessionAuthorityContractError(f"{fact_label} must be an object")
        _exact(fact, {"fact_id", "text", "provenance"}, fact_label)
        if not _nonempty(fact["fact_id"]) or not _nonempty(fact["text"]) or len(fact["text"]) > 512:
            raise SessionAuthorityContractError(f"{fact_label} is invalid")
        _validate_source_evidence(fact["provenance"], source_ids=source_ids, label=f"{fact_label}.provenance")


def validate_material_context_payload(
    payload: object,
    *,
    expected_session_date: str | None = None,
) -> dict[str, Any]:
    """Validate the legacy ``MaterialContext`` v1 wire payload exactly.

    ``MaterialContext`` is deliberately a separate non-authority stream. Its
    facts retain provenance, but the payload cannot add or authorize candidates;
    both the payload and every material record must carry
    ``candidate_authority='NONE'``.
    """

    if not isinstance(payload, Mapping):
        raise SessionAuthorityContractError("MaterialContext payload must be an object")
    _reject_floats(payload)
    fields = {
        "contract",
        "contract_version",
        "applicable_session_date",
        "materials",
        "candidate_authority",
    }
    _exact(payload, fields, "MaterialContext payload")
    if payload["contract"] != "MaterialContext" or payload["contract_version"] != CONTRACT_VERSION:
        raise SessionAuthorityContractError("MaterialContext contract version is invalid")
    session_date = payload["applicable_session_date"]
    if not _session_date(session_date) or (expected_session_date is not None and session_date != expected_session_date):
        raise SessionAuthorityContractError("MaterialContext session date is invalid")
    if payload["candidate_authority"] != "NONE":
        raise SessionAuthorityContractError("MaterialContext cannot grant candidate authority")
    materials = payload["materials"]
    if not isinstance(materials, list):
        raise SessionAuthorityContractError("MaterialContext materials must be a list")
    for index, item in enumerate(materials):
        # The legacy v1 context stream is supporting evidence, not registration
        # authority. Provenance still must be well formed, but it is not required
        # to be a member of the SessionBrief source set.
        _validate_context_item(item, None, label=f"materials[{index}]")
    return dict(payload)


def validate_brief_payload(payload: object, *, expected_session_date: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SessionAuthorityContractError("SessionBrief payload must be an object")
    _reject_floats(payload)
    fields = {
        "contract",
        "contract_version",
        "applicable_session_date",
        "market_timezone",
        "session_disposition",
        "source_documents",
        "candidate_ideas",
        "context_materials",
        "extraction",
    }
    _exact(payload, fields, "SessionBrief payload")
    if payload["contract"] != "SessionBrief" or payload["contract_version"] != CONTRACT_VERSION:
        raise SessionAuthorityContractError("SessionBrief contract version is invalid")
    session_date = payload["applicable_session_date"]
    if not _session_date(session_date) or (expected_session_date is not None and session_date != expected_session_date):
        raise SessionAuthorityContractError("SessionBrief session date is invalid")
    if payload["market_timezone"] != SESSION_TIMEZONE or payload["session_disposition"] not in {"TRADE_SESSION", "NO_TRADE_SESSION"}:
        raise SessionAuthorityContractError("SessionBrief market/disposition is invalid")
    sources = payload["source_documents"]
    if not isinstance(sources, list) or not sources:
        raise SessionAuthorityContractError("SessionBrief requires an authorized source")
    checked_sources = [validate_source_document(source, session_date, label=f"source_documents[{index}]") for index, source in enumerate(sources)]
    source_ids = {source["document_id"] for source in checked_sources}
    if len(source_ids) != len(checked_sources):
        raise SessionAuthorityContractError("SessionBrief source document ids must be unique")
    ideas = payload["candidate_ideas"]
    contexts = payload["context_materials"]
    if not isinstance(ideas, list) or not isinstance(contexts, list):
        raise SessionAuthorityContractError("SessionBrief candidate/context collections are invalid")
    checked_ideas = [validate_session_idea(idea, source_ids, label=f"candidate_ideas[{index}]") for index, idea in enumerate(ideas)]
    idea_ids = [idea["idea_id"] for idea in checked_ideas]
    if len(idea_ids) != len(set(idea_ids)):
        raise SessionAuthorityContractError("SessionBrief idea ids must be unique")
    for index, item in enumerate(contexts):
        _validate_context_item(item, source_ids, label=f"context_materials[{index}]")
    actionable = any(idea["session_relevance"] == "ACTIONABLE_TODAY" for idea in checked_ideas)
    if payload["session_disposition"] == "TRADE_SESSION" and not actionable:
        raise SessionAuthorityContractError("TRADE_SESSION requires an actionable candidate")
    if payload["session_disposition"] == "NO_TRADE_SESSION" and actionable:
        raise SessionAuthorityContractError("NO_TRADE_SESSION cannot contain actionable candidates")
    extraction = payload["extraction"]
    extraction_fields = {"source_order_preserved", "source_priority_lossless", "unresolved_symbols", "conflicts", "status", "blocked_reasons"}
    if not isinstance(extraction, Mapping):
        raise SessionAuthorityContractError("SessionBrief extraction metadata is invalid")
    _exact(extraction, extraction_fields, "extraction")
    if extraction["source_order_preserved"] is not True or extraction["source_priority_lossless"] is not True:
        raise SessionAuthorityContractError("SessionBrief extraction is lossy")
    if not isinstance(extraction["unresolved_symbols"], list) or not isinstance(extraction["conflicts"], list) or not isinstance(extraction["blocked_reasons"], list):
        raise SessionAuthorityContractError("SessionBrief extraction issue lists are invalid")
    expected_status = "COMPLETE" if actionable else "NO_ACTIONABLE_SIGNAL"
    if extraction["status"] != expected_status or extraction["blocked_reasons"]:
        raise SessionAuthorityContractError("SessionBrief extraction status is invalid")
    # The native Go authority path is deliberately fail-closed on mapping conflicts.
    if extraction["conflicts"]:
        raise SessionAuthorityContractError("SessionBrief extraction conflicts must be resolved before publication")
    return dict(payload)


def validate_amendment_source(value: object, session_date: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionAuthorityContractError("amendment source must be an object")
    required = {
        "document_id",
        "source_hash",
        "source_locator",
        "registration_intent",
        "applicable_session_date",
        "source_role",
        "intent_authority",
        "intent_evidence_ref",
    }
    allowed = required | {"source_profile_id", "source_profile_version"}
    if required - set(value) or set(value) - allowed:
        raise SessionAuthorityContractError("amendment source fields are invalid")
    translated = dict(value)
    translated["source_sha256"] = translated.pop("source_hash")
    translated.pop("source_locator")
    validate_source_document(translated, session_date, label="amendment source")
    if not _nonempty(value["source_locator"]):
        raise SessionAuthorityContractError("amendment source locator is invalid")
    return dict(value)


def validate_amendment_payload(payload: object, *, expected_session_date: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SessionAuthorityContractError("SessionBriefAmendment payload must be an object")
    _reject_floats(payload)
    fields = {
        "contract",
        "contract_version",
        "applicable_session_date",
        "base_brief_id",
        "base_brief_hash",
        "previous_authority_event",
        "changed_record_ids",
        "reason",
        "source",
        "operations",
    }
    _exact(payload, fields, "SessionBriefAmendment payload")
    if payload["contract"] != "SessionBriefAmendment" or payload["contract_version"] != CONTRACT_VERSION:
        raise SessionAuthorityContractError("SessionBriefAmendment contract version is invalid")
    session_date = payload["applicable_session_date"]
    if not _session_date(session_date) or (expected_session_date is not None and session_date != expected_session_date):
        raise SessionAuthorityContractError("SessionBriefAmendment session date is invalid")
    if not _nonempty(payload["base_brief_id"]) or not _digest(payload["base_brief_hash"]):
        raise SessionAuthorityContractError("SessionBriefAmendment base identity is invalid")
    previous = payload["previous_authority_event"]
    if not isinstance(previous, Mapping):
        raise SessionAuthorityContractError("SessionBriefAmendment predecessor is invalid")
    _exact(previous, {"event_id", "event_hash"}, "previous_authority_event")
    if not _nonempty(previous["event_id"]) or not _digest(previous["event_hash"]):
        raise SessionAuthorityContractError("SessionBriefAmendment predecessor identity is invalid")
    if not _nonempty(payload["reason"]):
        raise SessionAuthorityContractError("SessionBriefAmendment reason is required")
    validate_amendment_source(payload["source"], session_date)
    changed = payload["changed_record_ids"]
    operations = payload["operations"]
    if not isinstance(changed, list) or not changed or any(not _nonempty(item) for item in changed) or len(changed) != len(set(changed)):
        raise SessionAuthorityContractError("changed_record_ids must be unique non-empty strings")
    if not isinstance(operations, list) or not operations:
        raise SessionAuthorityContractError("SessionBriefAmendment operations are required")
    operation_ids: list[str] = []
    base_operation_fields = {"operation", "record_id", "record", "expected_prior_record_version", "expected_prior_record_hash"}
    for index, operation in enumerate(operations):
        label = f"operations[{index}]"
        if not isinstance(operation, Mapping):
            raise SessionAuthorityContractError(f"{label} must be an object")
        kind = operation.get("operation")
        expected_fields = base_operation_fields | ({"resolution"} if kind == "restore_eligibility" else set())
        _exact(operation, expected_fields, label)
        if kind not in AMENDMENT_OPERATIONS or not _nonempty(operation["record_id"]):
            raise SessionAuthorityContractError(f"{label} identity is invalid")
        if kind == "add":
            if operation["expected_prior_record_version"] is not None or operation["expected_prior_record_hash"] is not None or not isinstance(operation["record"], Mapping):
                raise SessionAuthorityContractError(f"{label} add preconditions/record are invalid")
        else:
            version = operation["expected_prior_record_version"]
            if not isinstance(version, int) or isinstance(version, bool) or version < 1 or not _digest(operation["expected_prior_record_hash"]):
                raise SessionAuthorityContractError(f"{label} prior preconditions are invalid")
        if kind == "replace" and not isinstance(operation["record"], Mapping):
            raise SessionAuthorityContractError(f"{label} replacement record is required")
        if kind in {"withdraw_eligibility", "hard_invalidation", "restore_eligibility"} and operation["record"] is not None:
            raise SessionAuthorityContractError(f"{label} status-only operation record must be null")
        if kind == "restore_eligibility":
            resolution = operation["resolution"]
            if not isinstance(resolution, Mapping):
                raise SessionAuthorityContractError(f"{label}.resolution must be an object")
            _exact(resolution, {"reason", "source"}, f"{label}.resolution")
            if not _nonempty(resolution["reason"]):
                raise SessionAuthorityContractError(f"{label}.resolution reason is invalid")
            source = resolution["source"]
            if not isinstance(source, Mapping):
                raise SessionAuthorityContractError(f"{label}.resolution.source must be an object")
            _exact(source, {"document_id", "source_locator", "source_hash"}, f"{label}.resolution.source")
            if not _nonempty(source["document_id"]) or not _nonempty(source["source_locator"]) or not _digest(source["source_hash"]):
                raise SessionAuthorityContractError(f"{label}.resolution.source is invalid")
            amendment_source = payload["source"]
            if source != {
                "document_id": amendment_source["document_id"],
                "source_locator": amendment_source["source_locator"],
                "source_hash": amendment_source["source_hash"],
            }:
                raise SessionAuthorityContractError(f"{label}.resolution.source is not bound to amendment authority")
        operation_ids.append(str(operation["record_id"]))
    if len(operation_ids) != len(set(operation_ids)) or set(operation_ids) != set(changed):
        raise SessionAuthorityContractError("changed_record_ids must exactly match unique operation record_ids")
    return dict(payload)


def replay_authority_payloads(
    brief: Mapping[str, Any],
    amendments: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str | None], set[str]]:
    """Replay already identity-checked payloads using current consumer preconditions."""

    checked_brief = validate_brief_payload(brief)
    source_ids = {str(source["document_id"]) for source in checked_brief["source_documents"]}
    records = {str(item["idea_id"]): dict(item) for item in checked_brief["candidate_ideas"]}
    statuses: dict[str, str | None] = {record_id: None for record_id in records}
    session_date = str(checked_brief["applicable_session_date"])
    for amendment_index, raw_amendment in enumerate(amendments):
        amendment = validate_amendment_payload(raw_amendment, expected_session_date=session_date)
        source_ids.add(str(amendment["source"]["document_id"]))
        for operation_index, operation in enumerate(amendment["operations"]):
            label = f"amendments[{amendment_index}].operations[{operation_index}]"
            kind = operation["operation"]
            record_id = str(operation["record_id"])
            current = records.get(record_id)
            if kind == "add":
                if current is not None:
                    raise SessionAuthorityContractError(f"{label} add record already exists")
                record = validate_session_idea(operation["record"], source_ids, label=f"{label}.record")
                if record["idea_id"] != record_id or record["record_version"] != 1:
                    raise SessionAuthorityContractError(f"{label} add record identity/version is invalid")
                records[record_id] = record
                statuses[record_id] = None
                continue
            if current is None:
                raise SessionAuthorityContractError(f"{label} references a missing record")
            if current["record_version"] != operation["expected_prior_record_version"] or record_hash(current) != operation["expected_prior_record_hash"]:
                raise SessionAuthorityContractError(f"{label} prior version/hash precondition failed")
            status = statuses.get(record_id)
            if status == "hard_invalidation" and kind != "hard_invalidation":
                raise SessionAuthorityContractError(f"{label} hard invalidation is terminal")
            if kind == "replace":
                record = validate_session_idea(operation["record"], source_ids, label=f"{label}.record")
                if (
                    record["idea_id"] != record_id
                    or record["candidate"] != current["candidate"]
                    or record["record_version"] != current["record_version"] + 1
                ):
                    raise SessionAuthorityContractError(f"{label} replacement record identity/version is invalid")
                records[record_id] = record
            elif kind == "withdraw_eligibility":
                if status in {"withdraw_eligibility", "hard_invalidation"}:
                    raise SessionAuthorityContractError(f"{label} eligibility cannot be withdrawn from current state")
                statuses[record_id] = "withdraw_eligibility"
            elif kind == "hard_invalidation":
                if status == "hard_invalidation":
                    raise SessionAuthorityContractError(f"{label} record is already hard-invalidated")
                statuses[record_id] = "hard_invalidation"
            elif kind == "restore_eligibility":
                if status != "withdraw_eligibility":
                    raise SessionAuthorityContractError(f"{label} restore requires prior withdrawal")
                statuses[record_id] = None
    return records, statuses, source_ids


__all__ = [
    "AMENDMENT_OPERATIONS",
    "CONTRACT_VERSION",
    "MARKET",
    "SESSION_TIMEZONE",
    "SessionAuthorityContractError",
    "canonical_json_bytes",
    "event_hash_input_bytes",
    "record_hash",
    "replay_authority_payloads",
    "sha256_bytes",
    "validate_amendment_payload",
    "validate_brief_payload",
    "validate_material_context_payload",
    "validate_session_idea",
    "validate_source_document",
    "validate_utc_timestamp",
]
