from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.runtime.native_write_store import (  # noqa: E402
    ApplyRejected,
    NativeWriteStore,
    StoreConflict,
    apply_mutation,
    validate_apply,
)
from protocol.v1.runtime.write_preview import build_mutation_preview  # noqa: E402


NOW = datetime(2026, 9, 15, 13, 0, 0, tzinfo=timezone.utc)


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


def _decision_request() -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "request_type": "decision.create",
        "portfolio_id": "portfolio-alpha",
        "account_ids": ["account-alpha"],
        "subject_refs": ["000660"],
        "statement": "SK하이닉스가 185 구간을 회복하면 3주 재매수를 검토한다.",
        "action_intent": {
            "action": "buy",
            "asset_ref": "000660",
            "quantity": {"value": 3, "unit": "shares"},
            "notes": None,
        },
        "conditions": ["185 가격 구간 회복"],
        "invalidation_conditions": ["회복 실패 후 지지선 재이탈"],
        "rationale_summary": "가격 회복 확인 후 재진입",
        "source_context_ref": "context:fixture",
        "decided_at": _tp("2026-09-15T12:20:00Z"),
        "authority_basis": "explicit_user_decision",
    }


def _transaction_request(*, provider_execution_id: str | None = None) -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "request_type": "transaction.record",
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "transaction_type": "trade",
        "asset": {
            "asset_type": "stock",
            "symbol": "005930",
            "venue": "KRX",
            "currency": "KRW",
            "display_name": "삼성전자",
            "provider_refs": {},
        },
        "side": "buy",
        "quantity": 10,
        "price": None,
        "amount": None,
        "effective_at": _tp("2026-09-15T03:30:00Z"),
        "detail_status": "partial",
        "provider_execution_id": provider_execution_id,
        "transfer_group_id": None,
        "correction_of": None,
        "occurrence_attestation": {
            "attestation_type": "explicit_user_statement" if provider_execution_id is None else "provider_execution",
            "source_ref": "chat-message:fixture" if provider_execution_id is None else "provider-fill:fixture",
            "statement_digest": "a" * 64 if provider_execution_id is None else None,
            "attested_at": _tp("2026-09-15T12:25:00Z"),
        },
    }


def _principal() -> dict[str, Any]:
    return {
        "subject_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "authentication_event_id": "auth:fixture",
        "authenticated_at": _tp("2026-09-15T12:00:00Z"),
        "expires_at": _tp("2026-09-15T14:00:00Z"),
    }


def _grant() -> dict[str, Any]:
    return {
        "grant_id": "grant:write-fixture",
        "instance_id": "fixture-full-reference",
        "subject_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "permissions": ["decision.create", "transaction.record"],
        "portfolio_scope": ["portfolio-alpha"],
        "tool_allowlist": [],
        "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
        "policy_version": "write-fixture-v1",
        "issued_at": _tp("2026-09-15T12:00:00Z"),
        "expires_at": _tp("2026-09-15T14:00:00Z"),
    }


def _approval(preview: dict[str, Any]) -> dict[str, Any]:
    mutation = preview["preview"]
    return {
        "approval_id": "approval:fixture",
        "preview_id": mutation["preview_id"],
        "authenticated_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "approved_payload_digest": mutation["payload_digest"],
        "target": mutation["target"],
        "base_version": mutation["base_version"],
        "approved_at": _tp("2026-09-15T12:59:00Z"),
        "expires_at": _tp("2026-09-15T13:05:00Z"),
        "approval_method": "trusted-test-surface",
    }


def _verified(_approval: dict[str, Any], _principal: dict[str, Any], _grant: dict[str, Any]) -> dict[str, Any]:
    return {"verified": True, "verification_ref": "approval-verification:fixture"}


def test_decision_apply_persists_one_schema_valid_canonical_record(tmp_path: Path) -> None:
    preview = build_mutation_preview(_decision_request(), now=NOW)
    store = NativeWriteStore(tmp_path / "native-write")
    result = apply_mutation(
        preview,
        _approval(preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        store=store,
        now=NOW,
    )
    _validate(result, "write-apply-result.schema.json")
    _validate(result["resource"], "decision-record.schema.json")
    assert result["resource"]["authority"] == "explicit_user_decision"
    assert result["receipt"]["effect_scope"] == "decision_record_appended"
    assert result["replayed"] is False
    assert store.list_resources("decision") == [result["resource"]]


def test_transaction_apply_preserves_partial_detail_and_user_attestation_basis(tmp_path: Path) -> None:
    preview = build_mutation_preview(_transaction_request(), now=NOW)
    store = NativeWriteStore(tmp_path / "native-write")
    result = apply_mutation(
        preview,
        _approval(preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        store=store,
        now=NOW,
    )
    _validate(result, "write-apply-result.schema.json")
    transaction = result["resource"]
    assert transaction["occurrence_status"] == "confirmed"
    assert transaction["detail_status"] == "partial"
    assert transaction["price"] is None
    assert transaction["execution_basis"] == "explicit_user_confirmation"
    assert transaction["source_evidence"][0].startswith("attestation:")


def test_apply_requires_server_verified_approval_and_writes_nothing_when_unverified(tmp_path: Path) -> None:
    preview = build_mutation_preview(_decision_request(), now=NOW)
    store = NativeWriteStore(tmp_path / "native-write")
    validation = validate_apply(
        preview,
        _approval(preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=lambda *_: {"verified": False, "verification_ref": "untrusted"},
        now=NOW,
    )
    assert validation.allowed is False
    assert validation.reason_code == "approval_unverified"
    try:
        apply_mutation(
            preview,
            _approval(preview),
            principal=_principal(),
            grant=_grant(),
            approval_verifier=None,
            store=store,
            now=NOW,
        )
    except ApplyRejected as exc:
        assert exc.code == "approval_unverified"
    else:
        raise AssertionError("unverified approval was applied")
    assert store.read_journal() == []


def test_payload_target_identity_permission_scope_and_expiry_fail_closed(tmp_path: Path) -> None:
    base = build_mutation_preview(_decision_request(), now=NOW)
    cases: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], datetime]] = []

    payload_mismatch = _approval(base)
    payload_mismatch["approved_payload_digest"] = "b" * 64
    cases.append(("approval_payload_mismatch", base, payload_mismatch, _grant(), NOW))

    target_mismatch = _approval(base)
    target_mismatch["target"] = {"portfolio_id": "portfolio-beta"}
    cases.append(("approval_target_mismatch", base, target_mismatch, _grant(), NOW))

    identity_mismatch = _approval(base)
    identity_mismatch["authenticated_user_id"] = "user:other"
    cases.append(("approval_identity_mismatch", base, identity_mismatch, _grant(), NOW))

    permission_grant = _grant()
    permission_grant["permissions"].remove("decision.create")
    cases.append(("permission_denied", base, _approval(base), permission_grant, NOW))

    scope_grant = _grant()
    scope_grant["portfolio_scope"] = ["portfolio-beta"]
    cases.append(("portfolio_scope_denied", base, _approval(base), scope_grant, NOW))

    expired = datetime(2026, 9, 15, 13, 6, 0, tzinfo=timezone.utc)
    cases.append(("approval_expired", base, _approval(base), _grant(), expired))

    for expected, preview, approval, grant, when in cases:
        validation = validate_apply(
            preview,
            approval,
            principal=_principal(),
            grant=grant,
            approval_verifier=_verified,
            now=when,
        )
        assert validation.allowed is False
        assert validation.reason_code == expected

    assert NativeWriteStore(tmp_path / "native-write").read_journal() == []


def test_idempotent_retry_reuses_existing_commit_and_conflicting_payload_is_blocked(tmp_path: Path) -> None:
    preview = build_mutation_preview(_decision_request(), now=NOW)
    store = NativeWriteStore(tmp_path / "native-write")
    first = apply_mutation(
        preview,
        _approval(preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        store=store,
        now=NOW,
    )
    second = apply_mutation(
        copy.deepcopy(preview),
        _approval(preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        store=store,
        now=NOW,
    )
    assert second["replayed"] is True
    assert second["dedupe_basis"] == "idempotency"
    assert second["resource"] == first["resource"]
    assert len(store.read_journal()) == 1

    changed_request = _decision_request()
    changed_request["action_intent"]["quantity"]["value"] = 4
    changed = build_mutation_preview(changed_request, now=NOW)
    changed["operation"]["idempotency_key"] = preview["operation"]["idempotency_key"]
    try:
        apply_mutation(
            changed,
            _approval(changed),
            principal=_principal(),
            grant=_grant(),
            approval_verifier=_verified,
            store=store,
            now=NOW,
        )
    except StoreConflict as exc:
        assert exc.code == "idempotency_conflict"
    else:
        raise AssertionError("same idempotency key accepted a different payload")


def test_same_provider_execution_with_new_operation_does_not_create_duplicate_transaction(tmp_path: Path) -> None:
    request = _transaction_request(provider_execution_id="fill-123")
    first_preview = build_mutation_preview(request, now=NOW)
    second_preview = build_mutation_preview(copy.deepcopy(request), now=NOW)
    store = NativeWriteStore(tmp_path / "native-write")
    first = apply_mutation(
        first_preview,
        _approval(first_preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        store=store,
        now=NOW,
    )
    second = apply_mutation(
        second_preview,
        _approval(second_preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        store=store,
        now=NOW,
    )
    assert second["replayed"] is True
    assert second["dedupe_basis"] == "transaction_identity"
    assert second["resource"]["transaction_id"] == first["resource"]["transaction_id"]
    assert second["receipt"]["effect_scope"] == "existing_transaction_reused"
    assert len(store.list_resources("transaction")) == 1
    assert [entry["event_type"] for entry in store.read_journal()] == ["resource_commit", "operation_deduplicated"]


def test_transaction_identity_collision_with_different_payload_conflicts_instead_of_overwriting(tmp_path: Path) -> None:
    first_request = _transaction_request(provider_execution_id="fill-123")
    first_preview = build_mutation_preview(first_request, now=NOW)
    store = NativeWriteStore(tmp_path / "native-write")
    apply_mutation(
        first_preview,
        _approval(first_preview),
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        store=store,
        now=NOW,
    )

    changed_request = _transaction_request(provider_execution_id="fill-123")
    changed_request["quantity"] = 11
    changed_preview = build_mutation_preview(changed_request, now=NOW)
    try:
        apply_mutation(
            changed_preview,
            _approval(changed_preview),
            principal=_principal(),
            grant=_grant(),
            approval_verifier=_verified,
            store=store,
            now=NOW,
        )
    except StoreConflict as exc:
        assert exc.code == "transaction_identity_conflict"
    else:
        raise AssertionError("same provider execution identity overwrote different transaction detail")
    assert len(store.list_resources("transaction")) == 1


def test_base_version_conflict_fails_before_persistence(tmp_path: Path) -> None:
    preview = build_mutation_preview(_decision_request(), now=NOW)
    preview["preview"]["base_version"] = "decision-set:v1"
    approval = _approval(preview)
    validation = validate_apply(
        preview,
        approval,
        principal=_principal(),
        grant=_grant(),
        approval_verifier=_verified,
        now=NOW,
        current_base_version="decision-set:v2",
    )
    assert validation.allowed is False
    assert validation.reason_code == "base_version_conflict"
    assert NativeWriteStore(tmp_path / "native-write").read_journal() == []


def test_write_apply_surface_still_excludes_decision_and_transaction_mutations() -> None:
    catalog = json.loads((PROTOCOL / "transport" / "tool_catalog.json").read_text(encoding="utf-8"))
    by_name = {tool["name"]: tool for tool in catalog["tools"]}
    assert {
        name for name, tool in by_name.items() if tool["read_only"] is False
    } == {
        "apply_knowledge_update",
        "apply_portfolio_update",
        "apply_account_sync",
        "apply_policy_update",
        "apply_opinion_weighting_update",
    }
    assert "record_decision" not in by_name
    assert "record_transaction" not in by_name
