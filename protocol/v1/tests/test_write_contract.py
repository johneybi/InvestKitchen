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

from protocol.v1.runtime.write_preview import build_mutation_preview  # noqa: E402


NOW = datetime(2026, 9, 15, 12, 30, 0, tzinfo=timezone.utc)


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def _validate(value: Any, schema_name: str) -> None:
    jsonschema.Draft202012Validator(
        _load_schema(schema_name),
        registry=_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(value)


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


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


def _transaction_request() -> dict[str, Any]:
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
        "provider_execution_id": None,
        "transfer_group_id": None,
        "correction_of": None,
        "occurrence_attestation": {
            "attestation_type": "explicit_user_statement",
            "source_ref": "chat-message:fixture",
            "statement_digest": "a" * 64,
            "attested_at": _tp("2026-09-15T12:25:00Z"),
        },
    }


def test_decision_write_request_and_preview_validate_without_apply() -> None:
    request = _decision_request()
    _validate(request, "write-request.schema.json")
    preview = build_mutation_preview(request, now=NOW)
    _validate(preview, "mutation-preview.schema.json")
    assert preview["resource_type"] == "decision"
    assert preview["operation"]["state"] == "awaiting_approval"
    assert preview["approval"] is None
    assert preview["apply_state"] == "not_applied"


def test_transaction_occurrence_attestation_is_not_mutation_approval() -> None:
    request = _transaction_request()
    _validate(request, "write-request.schema.json")
    preview = build_mutation_preview(request, now=NOW)
    _validate(preview, "mutation-preview.schema.json")
    assert preview["preview"]["canonical_payload"]["occurrence_attestation"]["attestation_type"] == "explicit_user_statement"
    assert preview["approval"] is None
    assert preview["approval_required"] is True
    assert preview["apply_state"] == "not_applied"


def test_transaction_trade_requires_account_asset_side_and_quantity() -> None:
    for field in ("account_id", "asset", "side", "quantity"):
        request = _transaction_request()
        request.pop(field)
        try:
            _validate(request, "write-request.schema.json")
        except jsonschema.ValidationError:
            pass
        else:
            raise AssertionError(f"transaction request accepted without {field}")


def test_client_boolean_confirmation_is_not_part_of_write_request_contract() -> None:
    request = _decision_request()
    request["user_confirmed"] = True
    try:
        _validate(request, "write-request.schema.json")
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("write request accepted caller-supplied user_confirmed")


def test_payload_digest_binds_exact_preview_payload() -> None:
    request = _decision_request()
    first = build_mutation_preview(request, now=NOW)
    second = build_mutation_preview(copy.deepcopy(request), now=NOW)
    assert first["preview"]["payload_digest"] == second["preview"]["payload_digest"]

    changed = copy.deepcopy(request)
    changed["action_intent"]["quantity"]["value"] = 4
    third = build_mutation_preview(changed, now=NOW)
    assert third["preview"]["payload_digest"] != first["preview"]["payload_digest"]


def test_partial_transaction_preview_warns_without_inventing_fill_detail() -> None:
    request = _transaction_request()
    preview = build_mutation_preview(request, now=NOW)
    assert preview["preview"]["warnings"]
    assert preview["preview"]["canonical_payload"]["price"] is None
    assert preview["preview"]["canonical_payload"]["detail_status"] == "partial"
