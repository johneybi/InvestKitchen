from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.session_authority import (  # noqa: E402
    PRODUCER_ID,
    PRODUCER_RELEASE,
    SessionAuthorityProducer,
    record_hash,
    validate_material_context_payload,
)
from protocol.v1.session_authority.contracts import (  # noqa: E402
    SessionAuthorityContractError,
    event_hash_input_bytes,
)
from protocol.v1.deployment.session_authority_cli import _bundle  # noqa: E402


SESSION = "2026-09-17"
T0 = "2026-09-16T21:25:00Z"
T1 = "2026-09-16T22:10:00Z"
T2 = "2026-09-16T22:10:01Z"


def _source(document_id: str = "checkpoint-2026-09-17") -> dict:
    return {
        "document_id": document_id,
        "source_sha256": "a" * 64,
        "registration_intent": "SESSION_TRADING_INPUT",
        "applicable_session_date": SESSION,
        "source_role": "PRIMARY_CHECKPOINT",
        "intent_authority": "USER_EXPLICIT",
        "intent_evidence_ref": "user:session-input:2026-09-17",
    }


def _idea(*, version: int = 1, catalyst: str = "장전 수급 확인") -> dict:
    locator = "checkpoint:leaders:1"
    return {
        "idea_id": "idea-005930",
        "record_version": version,
        "candidate": "삼성전자",
        "candidate_ref": {
            "kind": "SYMBOL",
            "source_value": "삼성전자",
            "mapping_status": "RESOLVED",
            "canonical_symbol": "005930",
            "mapping_provenance": {
                "document_id": _source()["document_id"],
                "source_locator": locator,
            },
        },
        "theme": "반도체",
        "catalyst": catalyst,
        "source_priority": {
            "scale_id": "source:none",
            "scale_version": "1.0",
            "label": "UNSPECIFIED",
            "source_stated": False,
            "document_order": 0,
            "section_order": 0,
            "item_order": 0,
            "profile_id": None,
            "profile_version": None,
        },
        "confirmation": ["거래량 동반 강세"],
        "invalidation": ["지지선 이탈"],
        "grounds": [
            {
                "ground_id": "idea-005930:ground:1",
                "ground_version": "1.0",
                "relation": "UNKNOWN",
                "status": "ACTIVE",
                "source_evidence": {
                    "document_id": _source()["document_id"],
                    "source_locator": locator,
                },
            }
        ],
        "conditions": [],
        "session_relevance": "ACTIONABLE_TODAY",
        "provenance": {
            "document_id": _source()["document_id"],
            "source_locator": locator,
        },
        "scope_type": "security",
        "document_order": 0,
    }


def _brief() -> dict:
    return {
        "contract": "SessionBrief",
        "contract_version": "1.0",
        "applicable_session_date": SESSION,
        "market_timezone": "Asia/Seoul",
        "session_disposition": "TRADE_SESSION",
        "source_documents": [_source()],
        "candidate_ideas": [_idea()],
        "context_materials": [],
        "extraction": {
            "source_order_preserved": True,
            "source_priority_lossless": True,
            "unresolved_symbols": [],
            "conflicts": [],
            "status": "COMPLETE",
            "blocked_reasons": [],
        },
    }


def _amendment(first_event) -> dict:
    replacement = _idea(version=2, catalyst="장전 수급과 갭 강도 재확인")
    return {
        "contract": "SessionBriefAmendment",
        "contract_version": "1.0",
        "applicable_session_date": SESSION,
        "base_brief_id": first_event.manifest["event_id"],
        "base_brief_hash": first_event.manifest["event_hash"],
        "previous_authority_event": {
            "event_id": first_event.manifest["event_id"],
            "event_hash": first_event.manifest["event_hash"],
        },
        "changed_record_ids": ["idea-005930"],
        "reason": "validated preopen input updated the catalyst",
        "source": {
            "document_id": _source()["document_id"],
            "source_hash": _source()["source_sha256"],
            "source_locator": "checkpoint:leaders:1",
            "registration_intent": "SESSION_TRADING_INPUT",
            "applicable_session_date": SESSION,
            "source_role": "PRIMARY_CHECKPOINT",
            "intent_authority": "USER_EXPLICIT",
            "intent_evidence_ref": "user:session-input:2026-09-17",
        },
        "operations": [
            {
                "operation": "replace",
                "record_id": "idea-005930",
                "record": replacement,
                "expected_prior_record_version": 1,
                "expected_prior_record_hash": record_hash(_idea()),
            }
        ],
    }


def _context(*, context_id: str = "context-005930", text: str = "장전 반도체 수급 맥락") -> dict:
    return {
        "contract": "MaterialContext",
        "contract_version": "1.0",
        "applicable_session_date": SESSION,
        "materials": [
            {
                "context_id": context_id,
                "record_version": 1,
                "horizon": "SESSION",
                "supporting_facts": [
                    {
                        "fact_id": f"{context_id}:fact:1",
                        "text": text,
                        "provenance": {
                            "document_id": _source()["document_id"],
                            "source_locator": "checkpoint:context:1",
                        },
                    }
                ],
                "related_idea_ids": ["idea-005930"],
                "candidate_authority": "NONE",
            }
        ],
        "candidate_authority": "NONE",
    }


def test_brief_event_identity_and_hashes_are_deterministic() -> None:
    first_producer = SessionAuthorityProducer(SESSION)
    first = first_producer.append_brief(_brief(), occurred_at=T0)
    second = SessionAuthorityProducer(SESSION).append_brief(_brief(), occurred_at=T0)

    assert first.manifest_bytes == second.manifest_bytes
    assert first.payload_bytes == second.payload_bytes
    assert first.manifest["producer_id"] == PRODUCER_ID == "investkitchen"
    assert first.manifest["producer_release"] == PRODUCER_RELEASE == "session-authority-v1"
    payload_hash = hashlib.sha256(first.payload_bytes).hexdigest()
    idempotency = f"authority:{SESSION}:SessionBrief:{SESSION}:{payload_hash}"
    expected_event_id = "session-" + hashlib.sha256(idempotency.encode("utf-8")).hexdigest()[:24]
    assert first.manifest["idempotency_key"] == idempotency
    assert first.manifest["event_id"] == expected_event_id
    identity = dict(first.manifest)
    actual_event_hash = identity.pop("event_hash")
    assert actual_event_hash == hashlib.sha256(event_hash_input_bytes(identity, first.payload_bytes)).hexdigest()


def test_amendment_chain_binds_base_predecessor_and_record_hash() -> None:
    producer = SessionAuthorityProducer(SESSION)
    first = producer.append_brief(_brief(), occurred_at=T0)
    second = producer.append_amendment(_amendment(first), occurred_at=T1)

    assert second.manifest["sequence"] == 2
    assert second.manifest["previous_event_hash"] == first.manifest["event_hash"]
    assert second.manifest["causation_id"] == first.manifest["event_id"]
    assert second.payload["base_brief_id"] == first.manifest["event_id"]
    assert second.payload["base_brief_hash"] == first.manifest["event_hash"]
    state = producer.authority_state()
    assert state["event_id"] == second.manifest["event_id"]
    assert state["records"]["idea-005930"]["record_version"] == 2
    assert state["records"]["idea-005930"]["catalyst"] == "장전 수급과 갭 강도 재확인"
    assert state["statuses"]["idea-005930"] is None


def test_material_context_uses_independent_context_stream_and_no_candidate_authority() -> None:
    producer = SessionAuthorityProducer(SESSION)
    brief = producer.append_brief(_brief(), occurred_at=T0)
    context = producer.append_context(_context(), occurred_at=T1)

    assert context.manifest["producer_id"] == "investkitchen"
    assert context.manifest["producer_release"] == "session-authority-v1"
    assert context.manifest["stream_id"] == f"context:{SESSION}"
    assert context.manifest["sequence"] == 1
    assert context.manifest["previous_event_hash"] is None
    assert context.manifest["causation_id"] == brief.manifest["event_id"]
    assert context.manifest["required_features"] == ["material_context.v1"]
    payload_hash = hashlib.sha256(context.payload_bytes).hexdigest()
    idempotency = f"context:{SESSION}:MaterialContext:{SESSION}:{payload_hash}"
    assert context.manifest["idempotency_key"] == idempotency
    assert validate_material_context_payload(context.payload) == context.payload
    assert context.payload["candidate_authority"] == "NONE"
    assert context.payload["materials"][0]["candidate_authority"] == "NONE"
    # Context does not alter authority records or candidate universe state.
    assert producer.authority_state()["records"]["idea-005930"]["record_version"] == 1


def test_material_context_rejects_any_candidate_authority_grant() -> None:
    producer = SessionAuthorityProducer(SESSION)
    producer.append_brief(_brief(), occurred_at=T0)

    top_level = _context()
    top_level["candidate_authority"] = False
    with pytest.raises(SessionAuthorityContractError, match="cannot grant candidate authority"):
        producer.append_context(top_level, occurred_at=T1)

    material = _context()
    material["materials"][0]["candidate_authority"] = False
    with pytest.raises(SessionAuthorityContractError, match="context authority"):
        producer.append_context(material, occurred_at=T1)


def test_exchange_publish_is_date_scoped_hash_complete_and_append_only(tmp_path: Path) -> None:
    producer = SessionAuthorityProducer(SESSION)
    first = producer.append_brief(_brief(), occurred_at=T0)
    exchange_root = tmp_path / "neutral-exchange"

    initial = producer.publish_exchange(exchange_root, published_at=T0)
    target = exchange_root / SESSION
    first_manifest_path = target / f"events/authority-{SESSION}/0001-SessionBrief/manifest.json"
    first_manifest_bytes = first_manifest_path.read_bytes()
    assert initial == {
        "ok": True,
        "status": "published",
        "session_date": SESSION,
        "event_count": 1,
        "last_event_id": first.manifest["event_id"],
    }

    second = producer.append_amendment(_amendment(first), occurred_at=T1)
    appended = producer.publish_exchange(exchange_root, published_at=T1)
    assert appended["status"] == "published"
    assert appended["event_count"] == 2
    assert first_manifest_path.read_bytes() == first_manifest_bytes
    assert (target / f"events/authority-{SESSION}/0002-SessionBriefAmendment/manifest.json").is_file()

    index = json.loads((target / "index.json").read_text(encoding="utf-8"))
    descriptor = json.loads((target / "exchange.json").read_text(encoding="utf-8"))
    assert index["package_id"] == "trademind-session-exchange"
    assert index["producer_id"] == "investkitchen"
    assert index["producer_release"] == "session-authority-v1"
    assert [event["sequence"] for event in index["events"]] == [1, 2]
    assert index["events"][1]["event_hash"] == second.manifest["event_hash"]
    assert descriptor["events"] == index["events"]
    assert descriptor["artifacts"] == index["artifacts"]
    assert descriptor["index_path"] == "index.json"
    for artifact in index["artifacts"]:
        raw = (target / artifact["path"]).read_bytes()
        assert artifact["bytes"] == len(raw)
        assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()

    metadata_bytes = (target / "index.json").read_bytes() + (target / "exchange.json").read_bytes()
    assert b"/Users/" not in metadata_bytes
    assert b"/tmp/" not in metadata_bytes
    assert str(tmp_path).encode("utf-8") not in metadata_bytes

    duplicate = producer.publish_exchange(exchange_root, published_at="2026-09-16T22:11:00Z")
    assert duplicate["status"] == "already_published"
    assert first_manifest_path.read_bytes() == first_manifest_bytes


def test_exchange_preserves_independent_authority_and_context_append_only_chains(tmp_path: Path) -> None:
    producer = SessionAuthorityProducer(SESSION)
    first = producer.append_brief(_brief(), occurred_at=T0)
    context_one = producer.append_context(_context(), occurred_at=T1)
    exchange_root = tmp_path / "neutral-exchange"

    initial = producer.publish_exchange(exchange_root, published_at=T1)
    target = exchange_root / SESSION
    context_one_path = target / f"events/context-{SESSION}/0001-MaterialContext/manifest.json"
    context_one_bytes = context_one_path.read_bytes()
    assert initial["event_count"] == 2

    amendment = producer.append_amendment(_amendment(first), occurred_at=T2)
    context_two = producer.append_context(
        _context(context_id="context-005930-2", text="장전 반도체 맥락 보강"),
        occurred_at="2026-09-16T22:10:02Z",
    )
    appended = producer.publish_exchange(exchange_root, published_at="2026-09-16T22:10:02Z")

    assert appended["event_count"] == 4
    assert context_one_path.read_bytes() == context_one_bytes
    assert context_two.manifest["sequence"] == 2
    assert context_two.manifest["previous_event_hash"] == context_one.manifest["event_hash"]
    assert context_two.manifest["causation_id"] == amendment.manifest["event_id"]

    index = json.loads((target / "index.json").read_text(encoding="utf-8"))
    assert index["required_features"] == [
        "material_context.v1",
        "session_amendment.v1",
        "session_brief.v1",
    ]
    assert [(item["stream_id"], item["sequence"], item["event_type"]) for item in index["events"]] == [
        (f"authority:{SESSION}", 1, "SessionBrief"),
        (f"authority:{SESSION}", 2, "SessionBriefAmendment"),
        (f"context:{SESSION}", 1, "MaterialContext"),
        (f"context:{SESSION}", 2, "MaterialContext"),
    ]
    assert index["events"][2]["schema_path"] == "schemas/material-context.schema.json"
    assert (target / "schemas/material-context.schema.json").is_file()


def test_cli_bundle_accepts_material_context_event_type() -> None:
    producer, published_at = _bundle({
        "bundle_version": 1,
        "session_date": SESSION,
        "published_at": T1,
        "events": [
            {"event_type": "SessionBrief", "occurred_at": T0, "effective_at": None, "payload": _brief()},
            {"event_type": "MaterialContext", "occurred_at": T1, "effective_at": None, "payload": _context()},
        ],
    })
    assert published_at == T1
    assert len(producer.events) == 1
    assert len(producer.context_events) == 1
    assert len(producer.all_events) == 2


def test_private_local_path_in_payload_is_rejected() -> None:
    payload = _brief()
    payload["candidate_ideas"][0]["provenance"]["source_locator"] = "/Users/example/private/checkpoint.md"
    producer = SessionAuthorityProducer(SESSION)
    with pytest.raises(SessionAuthorityContractError, match="private path"):
        producer.append_brief(payload, occurred_at=T0)
