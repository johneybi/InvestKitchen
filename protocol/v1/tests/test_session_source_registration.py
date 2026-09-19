from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.deployment.session_source_registration_cli import main as registration_cli_main  # noqa: E402
from protocol.v1.session_authority.source_registration import (  # noqa: E402
    SessionSourceRegistrationError,
    publish_registered_bundle,
    register_provenance_source,
    register_source,
    validate_registered_bundle,
)


SESSION = "2026-09-17"
T0 = "2026-09-16T21:25:00Z"
T1 = "2026-09-16T21:26:00Z"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _source(document_id: str, raw: bytes, *, order: int) -> dict:
    return {
        "document_id": document_id,
        "source_sha256": _sha(raw),
        "registration_intent": "SESSION_TRADING_INPUT",
        "applicable_session_date": SESSION,
        "source_role": "PRIMARY_CHECKPOINT" if order == 0 else "PREOPEN_SUPPLEMENT",
        "intent_authority": "USER_EXPLICIT",
        "intent_evidence_ref": f"user:session-input:{document_id}",
    }


def _idea(document_id: str) -> dict:
    locator = f"source:{document_id}:candidate:1"
    return {
        "idea_id": "idea-005930",
        "record_version": 1,
        "candidate": "삼성전자",
        "candidate_ref": {
            "kind": "SYMBOL",
            "source_value": "삼성전자",
            "mapping_status": "RESOLVED",
            "canonical_symbol": "005930",
            "mapping_provenance": {"document_id": document_id, "source_locator": locator},
        },
        "theme": "반도체",
        "catalyst": "장전 확인",
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
        "grounds": [{
            "ground_id": "idea-005930:ground:1",
            "ground_version": "1.0",
            "relation": "UNKNOWN",
            "status": "ACTIVE",
            "source_evidence": {"document_id": document_id, "source_locator": locator},
        }],
        "conditions": [],
        "session_relevance": "ACTIONABLE_TODAY",
        "provenance": {"document_id": document_id, "source_locator": locator},
        "scope_type": "security",
        "document_order": 0,
    }


def _context(document_id: str, *, text: str = "장전 시장 맥락") -> dict:
    return {
        "contract": "MaterialContext",
        "contract_version": "1.0",
        "applicable_session_date": SESSION,
        "materials": [{
            "context_id": "context-preopen",
            "record_version": 1,
            "horizon": "SESSION",
            "supporting_facts": [{
                "fact_id": "context-preopen:fact:1",
                "text": text,
                "provenance": {
                    "document_id": document_id,
                    "source_locator": f"source:{document_id}:context:1",
                },
            }],
            "related_idea_ids": ["idea-005930"],
            "candidate_authority": "NONE",
        }],
        "candidate_authority": "NONE",
    }


def _bundle(primary: bytes, supplement: bytes, *, context_text: str = "장전 시장 맥락") -> dict:
    primary_id = "checkpoint-2026-09-17"
    supplement_id = "supplement-2026-09-17"
    brief = {
        "contract": "SessionBrief",
        "contract_version": "1.0",
        "applicable_session_date": SESSION,
        "market_timezone": "Asia/Seoul",
        "session_disposition": "TRADE_SESSION",
        "source_documents": [
            _source(primary_id, primary, order=0),
            _source(supplement_id, supplement, order=1),
        ],
        "candidate_ideas": [_idea(primary_id)],
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
    return {
        "bundle_version": 1,
        "session_date": SESSION,
        "published_at": T1,
        "events": [
            {"event_type": "SessionBrief", "occurred_at": T0, "effective_at": None, "payload": brief},
            {"event_type": "MaterialContext", "occurred_at": T1, "effective_at": None,
             "payload": _context(supplement_id, text=context_text)},
        ],
    }


def _write(path: Path, raw: bytes) -> Path:
    path.write_bytes(raw)
    return path


def test_registration_copies_exact_source_privately_and_exact_replay_is_idempotent(tmp_path: Path) -> None:
    primary = b"primary raw session material\n"
    supplement = b"supplement raw session material\n"
    bundle = _bundle(primary, supplement)
    source_path = _write(tmp_path / "primary.txt", primary)
    store = tmp_path / "private-registrations"

    first = register_source(
        raw_source=source_path,
        bundle=bundle,
        document_id="checkpoint-2026-09-17",
        registration_root=store,
    )
    second = register_source(
        raw_source=source_path,
        bundle=bundle,
        document_id="checkpoint-2026-09-17",
        registration_root=store,
    )

    assert first["status"] == "registered"
    assert second["status"] == "already_registered"
    assert first["registration_id"] == second["registration_id"]
    registration_dir = store / "registrations" / first["registration_id"]
    assert (registration_dir / "source.bin").read_bytes() == primary
    metadata = json.loads((registration_dir / "registration.json").read_text(encoding="utf-8"))
    assert metadata["source_sha256"] == _sha(primary)
    assert metadata["bundle_sha256"] == first["bundle_sha256"]
    assert "source_path" not in metadata
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE((store / "registrations").stat().st_mode) == 0o700
    assert stat.S_IMODE(registration_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((registration_dir / "source.bin").stat().st_mode) == 0o600
    assert stat.S_IMODE((registration_dir / "registration.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("mutation,match", [
    ("hash", "source hash"),
    ("date", "session"),
    ("intent", "session trading authority"),
])
def test_registration_requires_exact_raw_hash_date_and_session_authority(
    tmp_path: Path, mutation: str, match: str
) -> None:
    primary = b"primary raw\n"
    supplement = b"supplement raw\n"
    bundle = _bundle(primary, supplement)
    source = bundle["events"][0]["payload"]["source_documents"][0]
    if mutation == "hash":
        source["source_sha256"] = "f" * 64
    elif mutation == "date":
        source["applicable_session_date"] = "2026-09-18"
    else:
        source["registration_intent"] = "KNOWLEDGE_ONLY"

    with pytest.raises(SessionSourceRegistrationError, match=match):
        register_source(
            raw_source=_write(tmp_path / "primary.txt", primary),
            bundle=bundle,
            document_id="checkpoint-2026-09-17",
            registration_root=tmp_path / "store",
        )


def test_multi_source_bundle_registers_incrementally_but_publish_requires_all_provenance(tmp_path: Path) -> None:
    primary = b"primary raw\n"
    supplement = b"supplement raw\n"
    bundle = _bundle(primary, supplement)
    store = tmp_path / "store"
    register_source(
        raw_source=_write(tmp_path / "primary.txt", primary), bundle=bundle,
        document_id="checkpoint-2026-09-17", registration_root=store,
    )

    with pytest.raises(SessionSourceRegistrationError, match="not registered"):
        validate_registered_bundle(bundle=bundle, registration_root=store)

    register_source(
        raw_source=_write(tmp_path / "supplement.txt", supplement), bundle=bundle,
        document_id="supplement-2026-09-17", registration_root=store,
    )
    producer, published_at, report = validate_registered_bundle(bundle=bundle, registration_root=store)
    assert published_at == T1
    assert report["registered_document_ids"] == ["checkpoint-2026-09-17", "supplement-2026-09-17"]
    assert report["provenance_document_ids"] == ["checkpoint-2026-09-17", "supplement-2026-09-17"]
    assert len(producer.events) == 1
    assert len(producer.context_events) == 1

    result = publish_registered_bundle(
        bundle=bundle, registration_root=store, exchange_root=tmp_path / "exchange"
    )
    assert result["status"] == "published"
    assert result["registration_status"] == "valid"
    target = tmp_path / "exchange" / SESSION
    assert (target / f"events/authority-{SESSION}/0001-SessionBrief/manifest.json").is_file()
    assert (target / f"events/context-{SESSION}/0001-MaterialContext/manifest.json").is_file()


def test_amendment_introduced_source_can_be_registered_without_rewriting_brief_sources(tmp_path: Path) -> None:
    primary = b"primary raw\n"
    supplement = b"supplement raw\n"
    amendment_raw = b"later amendment source\n"
    amendment_id = "amendment-2026-09-17"
    bundle = _bundle(primary, supplement)
    added = _idea(amendment_id)
    added["idea_id"] = "idea-amendment-005930"
    amendment = {
        "contract": "SessionBriefAmendment",
        "contract_version": "1.0",
        "applicable_session_date": SESSION,
        "changed_record_ids": [added["idea_id"]],
        "reason": "later user-authorized source added one candidate record",
        "source": {
            "document_id": amendment_id,
            "source_hash": _sha(amendment_raw),
            "source_locator": "source:amendment:1",
            "registration_intent": "SESSION_TRADING_INPUT",
            "applicable_session_date": SESSION,
            "source_role": "PREOPEN_SUPPLEMENT",
            "intent_authority": "USER_EXPLICIT",
            "intent_evidence_ref": "user:session-input:amendment",
        },
        "operations": [{
            "operation": "add",
            "record_id": added["idea_id"],
            "record": added,
            "expected_prior_record_version": None,
            "expected_prior_record_hash": None,
        }],
    }
    bundle["events"].insert(1, {
        "event_type": "SessionBriefAmendment",
        "occurred_at": T1,
        "effective_at": None,
        "payload": amendment,
    })
    store = tmp_path / "store"

    for document_id, filename, raw in (
        ("checkpoint-2026-09-17", "primary.txt", primary),
        ("supplement-2026-09-17", "supplement.txt", supplement),
        (amendment_id, "amendment.txt", amendment_raw),
    ):
        result = register_source(
            raw_source=_write(tmp_path / filename, raw),
            bundle=bundle,
            document_id=document_id,
            registration_root=store,
        )
        assert result["status"] == "registered"

    _, _, report = validate_registered_bundle(bundle=bundle, registration_root=store)
    assert report["registered_document_ids"] == [
        amendment_id,
        "checkpoint-2026-09-17",
        "supplement-2026-09-17",
    ]
    assert amendment_id in report["provenance_document_ids"]
    assert [row["document_id"] for row in bundle["events"][0]["payload"]["source_documents"]] == [
        "checkpoint-2026-09-17",
        "supplement-2026-09-17",
    ]


def test_same_source_in_brief_and_amendment_may_omit_descriptive_optional_metadata(tmp_path: Path) -> None:
    primary = b"primary raw\n"
    supplement = b"supplement raw\n"
    bundle = _bundle(primary, supplement)
    primary_id = "checkpoint-2026-09-17"
    bundle["events"][0]["payload"]["source_documents"][0].update({
        "published_at": "2026-09-17T06:25:00+09:00",
        "document_type": "mentor_comment",
    })
    added = _idea(primary_id)
    added["idea_id"] = "idea-amendment-same-source"
    bundle["events"].insert(1, {
        "event_type": "SessionBriefAmendment",
        "occurred_at": T1,
        "effective_at": None,
        "payload": {
            "contract": "SessionBriefAmendment",
            "contract_version": "1.0",
            "applicable_session_date": SESSION,
            "changed_record_ids": [added["idea_id"]],
            "reason": "same registered source supplied a later authorized record",
            "source": {
                "document_id": primary_id,
                "source_hash": _sha(primary),
                "source_locator": "source:checkpoint:later-section",
                "registration_intent": "SESSION_TRADING_INPUT",
                "applicable_session_date": SESSION,
                "source_role": "PRIMARY_CHECKPOINT",
                "intent_authority": "USER_EXPLICIT",
                "intent_evidence_ref": f"user:session-input:{primary_id}",
            },
            "operations": [{
                "operation": "add",
                "record_id": added["idea_id"],
                "record": added,
                "expected_prior_record_version": None,
                "expected_prior_record_hash": None,
            }],
        },
    })
    store = tmp_path / "store"
    result = register_source(
        raw_source=_write(tmp_path / "primary.txt", primary),
        bundle=bundle,
        document_id=primary_id,
        registration_root=store,
    )
    assert result["status"] == "registered"


def test_context_only_provenance_can_be_registered_without_granting_authority(tmp_path: Path) -> None:
    primary = b"primary raw\n"
    context_raw = b"context-only raw\n"
    bundle = _bundle(primary, b"unused supplement\n")
    primary_id = "checkpoint-2026-09-17"
    context_id = "context-source-2026-09-17"
    bundle["events"][0]["payload"]["source_documents"] = [
        bundle["events"][0]["payload"]["source_documents"][0]
    ]
    bundle["events"][1]["payload"] = _context(context_id)
    store = tmp_path / "store"

    register_source(
        raw_source=_write(tmp_path / "primary.txt", primary),
        bundle=bundle,
        document_id=primary_id,
        registration_root=store,
    )
    with pytest.raises(SessionSourceRegistrationError, match="unregistered"):
        validate_registered_bundle(bundle=bundle, registration_root=store)

    provenance = register_provenance_source(
        raw_source=_write(tmp_path / "context.txt", context_raw),
        bundle=bundle,
        document_id=context_id,
        source_registration=_source(context_id, context_raw, order=1),
        registration_root=store,
    )
    assert provenance["status"] == "registered"
    _, _, report = validate_registered_bundle(bundle=bundle, registration_root=store)
    assert context_id in report["registered_document_ids"]
    assert context_id in report["provenance_document_ids"]
    assert context_id not in {
        row["document_id"] for row in bundle["events"][0]["payload"]["source_documents"]
    }


def test_conflicting_document_hash_or_bundle_binding_fails_closed(tmp_path: Path) -> None:
    primary = b"primary raw\n"
    supplement = b"supplement raw\n"
    bundle = _bundle(primary, supplement)
    store = tmp_path / "store"
    source_path = _write(tmp_path / "primary.txt", primary)
    register_source(
        raw_source=source_path, bundle=bundle,
        document_id="checkpoint-2026-09-17", registration_root=store,
    )

    changed_raw = b"changed primary raw\n"
    changed_bundle = _bundle(changed_raw, supplement)
    with pytest.raises(SessionSourceRegistrationError, match="document_id is already bound"):
        register_source(
            raw_source=_write(tmp_path / "changed.txt", changed_raw), bundle=changed_bundle,
            document_id="checkpoint-2026-09-17", registration_root=store,
        )

    changed_semantics = _bundle(primary, supplement, context_text="changed structured context")
    with pytest.raises(SessionSourceRegistrationError, match="document_id is already bound"):
        register_source(
            raw_source=source_path, bundle=changed_semantics,
            document_id="checkpoint-2026-09-17", registration_root=store,
        )


def test_registration_cli_register_validate_and_publish(tmp_path: Path, capsys) -> None:
    primary = b"primary raw\n"
    supplement = b"supplement raw\n"
    bundle = _bundle(primary, supplement)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    store = tmp_path / "store"
    for document_id, filename, raw in (
        ("checkpoint-2026-09-17", "primary.txt", primary),
        ("supplement-2026-09-17", "supplement.txt", supplement),
    ):
        assert registration_cli_main([
            "register", "--source", str(_write(tmp_path / filename, raw)),
            "--bundle", str(bundle_path), "--document-id", document_id,
            "--registration-root", str(store),
        ]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "registered"

    assert registration_cli_main([
        "validate", "--bundle", str(bundle_path), "--registration-root", str(store),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"

    exchange = tmp_path / "exchange"
    assert registration_cli_main([
        "publish", "--bundle", str(bundle_path), "--registration-root", str(store),
        "--exchange-root", str(exchange),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "published"
