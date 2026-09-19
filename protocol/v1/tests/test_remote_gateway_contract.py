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

from protocol.v1.adapters.gateway_facade import ReadOnlyGatewayFacade  # noqa: E402
from protocol.v1.security.remote_gateway import ReplayGuard, authorize_authenticated_request, filter_tool_catalog_for_grant, load_tool_catalog  # noqa: E402
from protocol.v1.tests.fixture_handlers import synthetic_handlers  # noqa: E402
from protocol.v1.transport.authenticated_remote_contract import AuthenticatedRemoteContractHarness  # noqa: E402
from protocol.v1.transport.stdio_rpc import LocalToolRouter  # noqa: E402


NOW = datetime(2026, 9, 15, 8, 10, 30, tzinfo=timezone.utc)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        schema = _load(path)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def _validate_def(value: Any, definition: str) -> None:
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://trademind.local/protocol/v1/test-remote-wrapper.json",
        "$ref": f"remote-gateway.schema.json#/$defs/{definition}",
    }
    jsonschema.Draft202012Validator(
        wrapper,
        registry=_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(value)


def _fixture() -> dict[str, Any]:
    return _load(PROTOCOL / "fixtures" / "remote-gateway.contract.json")


def _router() -> LocalToolRouter:
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=synthetic_handlers(),
    )
    return LocalToolRouter(gateway)


def _request(
    *,
    request_id: str,
    name: str,
    arguments: dict[str, Any],
    nonce: str,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "instance_id": "fixture-full-reference",
        "method": "tools.call",
        "params": {"name": name, "arguments": arguments},
        "issued_at": {"value": "2026-09-15T08:10:00Z", "precision": "source_exact"},
        "expires_at": {"value": "2026-09-15T08:11:00Z", "precision": "source_exact"},
        "nonce": nonce,
    }


def test_remote_contract_fixture_parts_validate() -> None:
    fixture = _fixture()
    _validate_def(fixture["principal"], "authenticatedPrincipal")
    _validate_def(fixture["grant"], "accessGrant")
    _validate_def(fixture["request"], "wireRequest")
    _validate_def(fixture["rate_limit"], "rateLimitSignal")


def test_wire_request_cannot_supply_authentication_identity_or_permissions() -> None:
    fixture = _fixture()
    request = copy.deepcopy(fixture["request"])
    request["subject_user_id"] = "user:spoofed"
    try:
        _validate_def(request, "wireRequest")
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("wire request accepted caller-supplied authenticated identity")

    decision, _ = authorize_authenticated_request(
        request,
        principal=fixture["principal"],
        grant=fixture["grant"],
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "malformed_request"


def test_allowed_portfolio_read_binds_server_principal_and_emits_minimal_audit() -> None:
    fixture = _fixture()
    harness = AuthenticatedRemoteContractHarness(
        _router(),
        principal=fixture["principal"],
        grant=fixture["grant"],
    )
    response = harness.handle_authenticated_request(fixture["request"], now=NOW)
    assert response["ok"] is True
    assert response["result"]["capability"] == "portfolio.state"
    assert response["security"]["authorization"] == "allowed"
    _validate_def(response, "remoteResponse")

    audit = harness.audit_events[-1]
    _validate_def(audit, "auditEvent")
    assert audit["subject_user_id"] == fixture["principal"]["subject_user_id"]
    assert audit["client_id"] == fixture["principal"]["client_id"]
    assert audit["portfolio_ids"] == ["portfolio-alpha"]
    encoded = json.dumps(audit, ensure_ascii=False)
    assert "arguments" not in encoded
    assert "positions" not in encoded
    assert "SK하이닉스" not in encoded


def test_nonce_replay_is_rejected_after_first_authenticated_attempt() -> None:
    fixture = _fixture()
    guard = ReplayGuard()
    first, _ = authorize_authenticated_request(
        fixture["request"],
        principal=fixture["principal"],
        grant=fixture["grant"],
        replay_guard=guard,
        now=NOW,
    )
    second, _ = authorize_authenticated_request(
        fixture["request"],
        principal=fixture["principal"],
        grant=fixture["grant"],
        replay_guard=guard,
        now=NOW,
    )
    assert first["allowed"] is True
    assert second["allowed"] is False
    assert second["reason_code"] == "replay_detected"


def test_request_ttl_cannot_exceed_server_grant_policy() -> None:
    fixture = _fixture()
    request = copy.deepcopy(fixture["request"])
    request["expires_at"] = {"value": "2026-09-15T08:20:00Z", "precision": "source_exact"}
    decision, _ = authorize_authenticated_request(
        request,
        principal=fixture["principal"],
        grant=fixture["grant"],
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "request_ttl_exceeded"


def test_request_instance_must_match_server_grant_instance() -> None:
    fixture = _fixture()
    request = copy.deepcopy(fixture["request"])
    request["instance_id"] = "different-instance"
    decision, _ = authorize_authenticated_request(
        request,
        principal=fixture["principal"],
        grant=fixture["grant"],
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "instance_mismatch"


def test_portfolio_scope_is_enforced_independently_of_tool_argument() -> None:
    fixture = _fixture()
    request = _request(
        request_id="remote-other-portfolio-denied",
        name="get_portfolio_state",
        arguments={"portfolio_id": "portfolio-beta"},
        nonce="fixture-nonce-00000002",
    )
    decision, audit = authorize_authenticated_request(
        request,
        principal=fixture["principal"],
        grant=fixture["grant"],
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "portfolio_scope_denied"
    assert audit["portfolio_ids"] == ["portfolio-beta"]


def test_decision_context_cannot_mix_portfolio_scopes_even_when_nested() -> None:
    fixture = _fixture()
    request = _request(
        request_id="remote-mixed-portfolio",
        name="build_decision_context",
        nonce="fixture-nonce-00000003",
        arguments={
            "request": {
                "decision_request_id": "mixed-context",
                "actor": "web-gpt",
                "objective": "scope mixing must fail",
                "subject_refs": [],
                "portfolio_id": "portfolio-alpha",
                "account_ids": [],
                "horizon": "short_term",
                "as_of_request": {"value": "2026-09-15T08:10:00Z", "precision": "source_exact"},
                "constraints": None,
                "requested_context": ["portfolio.state"]
            },
            "capability_inputs": {"portfolio.state": {"portfolio_id": "portfolio-beta"}},
            "required_capabilities": ["portfolio.state"]
        },
    )
    decision, _ = authorize_authenticated_request(
        request,
        principal=fixture["principal"],
        grant=fixture["grant"],
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "portfolio_scope_conflict"


def test_decision_context_requires_permissions_for_nested_capabilities() -> None:
    fixture = _fixture()
    grant = copy.deepcopy(fixture["grant"])
    grant["permissions"].remove("market.read")
    request = _request(
        request_id="remote-nested-permission",
        name="build_decision_context",
        nonce="fixture-nonce-00000004",
        arguments={
            "request": {
                "decision_request_id": "market-context",
                "actor": "claimed-admin-name-does-not-grant-authority",
                "objective": "nested capability permission",
                "subject_refs": ["005930"],
                "portfolio_id": None,
                "account_ids": [],
                "horizon": "intraday",
                "as_of_request": {"value": "2026-09-15T08:10:00Z", "precision": "source_exact"},
                "constraints": None,
                "requested_context": ["market.quote"]
            },
            "capability_inputs": {"market.quote": {"symbols": ["005930"]}},
            "required_capabilities": ["market.quote"]
        },
    )
    decision, audit = authorize_authenticated_request(
        request,
        principal=fixture["principal"],
        grant=grant,
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "permission_denied"
    assert "market.read" in decision["permissions_evaluated"]
    assert audit["subject_user_id"] == fixture["principal"]["subject_user_id"]


def test_rate_limit_is_a_bounded_security_signal_without_capacity_disclosure() -> None:
    fixture = _fixture()
    harness = AuthenticatedRemoteContractHarness(
        _router(),
        principal=fixture["principal"],
        grant=fixture["grant"],
    )
    rate_limit = {
        "status": "limited",
        "policy_id": "remote-read-burst",
        "scope": "client",
        "retry_after_seconds": 30,
    }
    response = harness.handle_authenticated_request(fixture["request"], now=NOW, rate_limit=rate_limit)
    assert response["ok"] is False
    assert response["error"]["code"] == "rate_limited"
    assert response["security"]["rate_limit"] == rate_limit
    assert "capacity" not in json.dumps(response)
    _validate_def(response, "remoteResponse")


def test_principal_client_and_credential_binding_must_match_grant() -> None:
    fixture = _fixture()
    for field, reason in (
        ("client_id", "client_mismatch"),
        ("credential_binding_id", "credential_binding_mismatch"),
    ):
        principal = copy.deepcopy(fixture["principal"])
        principal[field] = principal[field] + ":wrong"
        decision, _ = authorize_authenticated_request(
            copy.deepcopy(fixture["request"]),
            principal=principal,
            grant=fixture["grant"],
            replay_guard=ReplayGuard(),
            now=NOW,
        )
        assert decision["allowed"] is False
        assert decision["reason_code"] == reason


def test_tools_list_is_filtered_by_granted_permissions_and_allowlist() -> None:
    fixture = _fixture()
    grant = copy.deepcopy(fixture["grant"])
    grant["permissions"] = ["system.tools.list", "knowledge.read"]
    grant["tool_allowlist"] = ["get_current_knowledge", "search_knowledge", "get_market_quote"]
    catalog = filter_tool_catalog_for_grant(load_tool_catalog(), grant)
    assert {tool["name"] for tool in catalog["tools"]} == {"get_current_knowledge", "search_knowledge"}


def test_tools_list_itself_requires_system_tools_list_permission() -> None:
    fixture = _fixture()
    grant = copy.deepcopy(fixture["grant"])
    grant["permissions"].remove("system.tools.list")
    request = {
        "request_id": "remote-tools-list-denied",
        "instance_id": "fixture-full-reference",
        "method": "tools.list",
        "params": {},
        "issued_at": {"value": "2026-09-15T08:10:00Z", "precision": "source_exact"},
        "expires_at": {"value": "2026-09-15T08:11:00Z", "precision": "source_exact"},
        "nonce": "fixture-nonce-00000005",
    }
    decision, _ = authorize_authenticated_request(
        request,
        principal=fixture["principal"],
        grant=grant,
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "permission_denied"
