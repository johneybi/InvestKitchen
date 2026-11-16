"""Conservative note ingestion; clients supply content, never storage metadata."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from urllib.parse import urlsplit
from protocol.v1.adapters.common import canonical_json
from protocol.v1.runtime.native_knowledge_store import KnowledgeRejected, validate_knowledge_request

NOTE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "source_text", "statements"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "source_text": {"type": "string", "minLength": 1, "maxLength": 24000,
                        "description": "Exact user-supplied note or source text; do not rewrite or shorten."},
        "statements": {"type": "array", "minItems": 1, "maxItems": 20,
                       "items": {"type": "string", "minLength": 1, "maxLength": 1500},
                       "description": "Claims faithfully extracted from the source, not instructions to execute."},
        "source_label": {"type": "string", "minLength": 1, "maxLength": 300},
        "source_url": {"type": "string", "minLength": 1, "maxLength": 2000},
        "published_date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                           "description": "Source publication date only when explicitly known; otherwise omit. Not the trading session date."},
    },
}


def validate_note(note):
    try:
        if not isinstance(note, dict) or set(NOTE_SCHEMA["required"]) - set(note) or set(note) - set(NOTE_SCHEMA["properties"]):
            raise ValueError
        for key, value in note.items():
            spec = NOTE_SCHEMA["properties"][key]
            if key == "statements":
                if not isinstance(value, list) or not spec["minItems"] <= len(value) <= spec["maxItems"]:
                    raise ValueError
                if any(not isinstance(item, str) or not 1 <= len(item) <= 1500 for item in value):
                    raise ValueError
            elif not isinstance(value, str) or not spec.get("minLength", 1) <= len(value) <= spec.get("maxLength", 10):
                raise ValueError
        if any(not value.strip() for value in [note["title"], note["source_text"], *note["statements"],
                *[note[k] for k in ("source_label", "source_url") if k in note]]):
            raise ValueError
        if "published_date" in note:
            if date.fromisoformat(note["published_date"]).isoformat() != note["published_date"]:
                raise ValueError
        if "source_url" in note:
            u = urlsplit(note["source_url"])
            if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password:
                raise ValueError
        if len(canonical_json(note).encode("utf-8")) > 48000:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise KnowledgeRejected("knowledge_note_invalid") from None
    return note


def note_generation_id(note):
    # Rephrasing a title/summary, changing a URL, or retrying tomorrow cannot
    # silently create a second generation for the same exact source text.
    return "generation:note-v1:" + hashlib.sha256(note["source_text"].encode("utf-8")).hexdigest()


def build_note_update(note):
    validate_note(note)
    identity = note_generation_id(note)
    suffix = identity.rsplit(":", 1)[1]
    stamp = {"value": datetime.now(timezone.utc).isoformat(), "precision": "source_exact"}
    evidence_id = "evidence:note-v1:" + suffix
    evidence = {
        "evidence_id": evidence_id, "evidence_kind": "user_supplied_note", "subject_refs": [],
        "source": {"source_type": "user_supplied_note",
                   "provider_or_publisher": note.get("source_label", "user-supplied note"),
                   "document_id": note["title"]},
        "authority_class": "user_supplied", "verification": "unverified", "freshness": "unknown",
        "recorded_at": stamp, "content_ref_or_value": note["source_text"],
        "content_hash": hashlib.sha256(note["source_text"].encode("utf-8")).hexdigest(),
        "lifecycle_scope": "context_only",
    }
    if "source_url" in note:
        evidence["source"]["url_or_external_id"] = note["source_url"]
    if "published_date" in note:
        evidence["published_at"] = {"value": note["published_date"], "precision": "date_only"}
    claims = [{"claim_id": "claim:note-v1:" + suffix + ":" + str(i),
               "statement": statement, "subject_refs": [], "claim_type": "source_note",
               "conditions": [], "invalidation": [], "evidence_refs": [evidence_id],
               "derivation_type": "paraphrase", "registration_state": "canonical",
               "provenance_verification": "unverified", "semantic_fidelity": "unknown",
               "truth_status": "unknown", "applicability_status": "unresolved", "confidence": "unknown"}
              for i, statement in enumerate(dict.fromkeys(note["statements"]))]
    update = {"protocol_version": "1.0-draft", "request_type": "knowledge.commit",
              "generation_id": identity, "generated_at": stamp, "current_state": {},
              "evidence": [evidence], "claims": claims}
    if len(canonical_json(update).encode("utf-8")) > 65536:
        raise KnowledgeRejected("knowledge_note_too_large")
    return validate_knowledge_request(update)
