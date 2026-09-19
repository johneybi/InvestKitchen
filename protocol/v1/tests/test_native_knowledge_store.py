from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"

from protocol.v1.adapters.native_personal import get_current_knowledge, search_knowledge  # noqa: E402
from protocol.v1.runtime.native_knowledge_store import (  # noqa: E402
    KnowledgeRejected,
    NativeKnowledgeStore,
    apply_knowledge_commit,
    build_knowledge_preview,
)
from protocol.v1.runtime.personal_data_store import PersonalDataStore  # noqa: E402
from protocol.v1.runtime.reference_composition import build_reference_gateway  # noqa: E402
from protocol.v1.security.trusted_approval import TrustedApprovalStore  # noqa: E402
from protocol.v1.tests.test_personal_data_migration import _synthetic_bundle  # noqa: E402


NOW = datetime(2026, 9, 17, 0, 0, 0, tzinfo=timezone.utc)


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def _validate(value: Any, schema_name: str) -> None:
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(value)


def _request(*, generation_id: str = "knowledge-generation:2026-09-17-1") -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "request_type": "knowledge.commit",
        "generation_id": generation_id,
        "generated_at": _tp("2026-09-17T00:00:00Z"),
        "current_state": {
            "as_of": _tp("2026-09-17T00:00:00Z"),
            "valid_until": _tp("2026-09-17T06:00:00Z"),
            "freshness_status": "current",
            "situational_usable": True,
            "summary": "Native advisory context is current.",
            "action_bias": ["확인 전 추격 금지"],
            "confirmation_conditions": ["거래량 동반 회복"],
            "invalidation_conditions": ["핵심 지지 이탈"],
            "outlook": [{"thesis_id": "native-thesis", "status": "active"}],
            "data_quality_notes": [],
        },
        "evidence": [{
            "evidence_id": "evidence:native:alpha",
            "evidence_kind": "user_supplied_structured_note",
            "subject_refs": ["asset-alpha"],
            "source": {
                "source_type": "chatgpt_structured_input",
                "provider_or_publisher": "user-session",
                "speaker": "mentor-alpha",
                "document_id": "doc:native:alpha",
                "url_or_external_id": None,
            },
            "source_locator": "conversation:knowledge-write",
            "authority_class": "user_supplied",
            "observed_at": _tp("2026-09-17T00:00:00Z"),
            "recorded_at": _tp("2026-09-17T00:00:00Z"),
            "verification": "verified",
            "freshness": "current",
            "lifecycle_scope": "canonical",
        }],
        "claims": [{
            "claim_id": "claim-alpha",
            "statement": "Asset Alpha now has native advisory context.",
            "subject_refs": ["asset-alpha"],
            "speaker": "mentor-alpha",
            "claim_type": "outlook",
            "stance": "watch",
            "horizon": "session",
            "conditions": ["confirmation required"],
            "invalidation": ["support break"],
            "effective_at": _tp("2026-09-17T00:00:00Z"),
            "valid_until": _tp("2026-09-17T06:00:00Z"),
            "evidence_refs": ["evidence:native:alpha"],
            "derivation_type": "direct_statement",
            "registration_state": "canonical",
            "provenance_verification": "verified",
            "semantic_fidelity": "direct",
            "truth_status": "attributed_opinion",
            "applicability_status": "applicable",
            "confidence": "medium",
        }],
    }


def _principal() -> dict[str, Any]:
    return {
        "subject_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "authentication_event_id": "auth:fixture",
        "authenticated_at": _tp("2026-09-16T23:00:00Z"),
        "expires_at": _tp("2026-09-17T01:00:00Z"),
    }


def _grant() -> dict[str, Any]:
    return {
        "grant_id": "grant:knowledge-fixture",
        "instance_id": "fixture-full-reference",
        "subject_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "permissions": ["knowledge.commit", "operation.approve"],
        "portfolio_scope": [],
        "tool_allowlist": [],
        "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
        "policy_version": "knowledge-fixture-v1",
        "issued_at": _tp("2026-09-16T23:00:00Z"),
        "expires_at": _tp("2026-09-17T01:00:00Z"),
    }


def _commit(store: NativeKnowledgeStore, approval_root: Path, request: dict[str, Any]) -> dict[str, Any]:
    preview = build_knowledge_preview(
        request,
        base_generation_id=store.latest_generation_id(),
        now=NOW,
        ttl_seconds=300,
    )
    _validate(preview, "mutation-preview.schema.json")
    approvals = TrustedApprovalStore(approval_root)
    approval = approvals.issue(
        preview,
        principal=_principal(),
        grant=_grant(),
        interaction_ref="local-tty:test",
        approval_method="local_tty",
        now=NOW,
        ttl_seconds=300,
    )
    return apply_knowledge_commit(
        preview,
        approval,
        principal=_principal(),
        grant=_grant(),
        approval_verifier=approvals.verify,
        store=store,
        now=NOW,
    )


def test_approved_native_knowledge_commit_is_append_only_and_schema_valid(tmp_path: Path) -> None:
    request = _request()
    _validate(request, "knowledge-write-request.schema.json")
    store = NativeKnowledgeStore(tmp_path / "native")

    result = _commit(store, tmp_path / "approvals", request)

    assert result["replayed"] is False
    assert result["claim_count"] == 1
    assert store.latest_generation_id() == request["generation_id"]
    assert len(store.read_journal()) == 1
    overlay = store.overlay()
    assert overlay is not None
    projection = {
        "protocol_version": "1.0-draft",
        "generated_at": overlay["generated_at"],
        "knowledge_generation": overlay["knowledge_generation"],
        "evidence": overlay["evidence"],
        "claims": overlay["claims"],
        "migration_gaps": [],
    }
    _validate(projection, "evidence-claim.schema.json")


def test_truth_status_provenance_and_temporal_errors_fail_before_persistence(tmp_path: Path) -> None:
    store = NativeKnowledgeStore(tmp_path / "native")
    cases: list[tuple[str, dict[str, Any]]] = []

    bad_truth = _request()
    bad_truth["claims"][0]["truth_status"] = "verified_truth"
    cases.append(("claim_truth_status_invalid", bad_truth))

    bad_ref = _request()
    bad_ref["claims"][0]["evidence_refs"] = ["evidence:missing"]
    cases.append(("claim_evidence_refs_invalid", bad_ref))

    bad_time = _request()
    bad_time["claims"][0]["valid_until"] = _tp("2026-09-16T23:00:00Z")
    cases.append(("claim_valid_until_before_effective_at", bad_time))

    for code, request in cases:
        with pytest.raises(KnowledgeRejected, match=code):
            build_knowledge_preview(request, base_generation_id=None, now=NOW)
    assert store.read_journal() == []


def test_native_overlay_overrides_baseline_current_and_claim_by_id(tmp_path: Path) -> None:
    baseline = PersonalDataStore(_synthetic_bundle(tmp_path / "personal"))
    native = NativeKnowledgeStore(tmp_path / "native")
    _commit(native, tmp_path / "approvals", _request())

    current = get_current_knowledge(baseline, max_outlook=20, native_knowledge=native)
    search = search_knowledge(baseline, "native advisory", limit=10, native_knowledge=native)

    _validate(current, "capability-result.schema.json")
    _validate(search, "capability-result.schema.json")
    assert current["producer"]["id"] == "investkitchen.native.knowledge"
    assert current["freshness"] == "current"
    assert current["source_mode"] == "local_store"
    assert current["data"]["summary"] == "Native advisory context is current."
    assert current["data"]["generation"]["generation_id"] == "knowledge-generation:2026-09-17-1"
    assert search["data"]["results"][0]["claim_id"] == "claim-alpha"
    assert search["data"]["results"][0]["statement"] == "Asset Alpha now has native advisory context."
    assert search["data"]["results"][0]["truth_status"] == "attributed_opinion"
    assert search["data"]["results"][0]["applicability_status"] == "applicable"


def test_reference_gateway_can_read_native_knowledge_without_legacy_or_personal_bundle(tmp_path: Path) -> None:
    root = tmp_path / "native"
    native = NativeKnowledgeStore(root)
    _commit(native, tmp_path / "approvals", _request())

    gateway = build_reference_gateway(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures/full-reference.instance.json",
        legacy_workspace=None,
        personal_data_root=None,
        native_store_root=root,
    )
    current = gateway.get_current_knowledge(max_outlook=10)
    search = gateway.search_knowledge("native advisory", limit=10)

    assert current["status"] == "ok"
    assert current["data"]["summary"] == "Native advisory context is current."
    assert search["data"]["result_count"] == 1
    assert search["data"]["results"][0]["claim_id"] == "claim-alpha"


def test_later_generation_requires_current_base_and_overlays_previous_state(tmp_path: Path) -> None:
    native = NativeKnowledgeStore(tmp_path / "native")
    approvals = tmp_path / "approvals"
    _commit(native, approvals, _request())
    second = _request(generation_id="knowledge-generation:2026-09-17-2")
    second["current_state"] = {"summary": "Second native state."}
    second["claims"][0]["claim_id"] = "claim-beta"
    _commit(native, approvals, second)

    overlay = native.overlay()
    assert overlay is not None
    assert overlay["current_state"]["summary"] == "Second native state."
    assert overlay["current_state"]["action_bias"] == ["확인 전 추격 금지"]
    assert {row["claim_id"] for row in overlay["claims"]} == {"claim-alpha", "claim-beta"}
    assert len(native.read_journal()) == 2
