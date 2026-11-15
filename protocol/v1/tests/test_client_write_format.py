from __future__ import annotations

import copy
import json

import pytest
from starlette.testclient import TestClient

from protocol.v1.adapters.common import digest
from protocol.v1.security.oauth_resource import JWTTokenVerifier
from protocol.v1.tests.test_advisory_plan_update import plan_request
from protocol.v1.tests.test_mcp_confirmed_writes import (
    _approve, _call, setup, signing_key,
)
from protocol.v1.tests.test_mcp_http import HEADERS, _gateway, _jwt, _jwks, _post, _verifier
from protocol.v1.tests.test_mcp_plan_http import runtime
from protocol.v1.tests.test_native_knowledge_store import _request
from protocol.v1.tests.test_portfolio_update_service import _narrow_request
from protocol.v1.transport.client_write_format import decode_write_arguments
from protocol.v1.transport.mcp_http import create_http_app


def _arguments(update, **overrides):
    return {"update_json": json.dumps(update, ensure_ascii=False),
            "expected_payload_digest": digest(update), **overrides}


@pytest.mark.parametrize("setup", ["json-string"], indirect=True)
def test_flat_discovery_contract_and_capabilities_follow_existing_grant(setup, signing_key):
    client, headers, _, _, _ = setup
    tools = {tool["name"]: tool for tool in _post(client, "tools/list", headers=headers).json()["result"]["tools"]}
    contract = tools["get_write_contract"]
    assert contract["inputSchema"]["properties"]["preview_tool"]["enum"] == [
        "preview_knowledge_update", "preview_portfolio_update"]
    assert contract["annotations"]["readOnlyHint"] is True
    schema = tools["preview_knowledge_update"]["inputSchema"]
    assert set(schema["properties"]) == {"update_json", "expected_payload_digest"}
    assert all(field["type"] == "string" for field in schema["properties"].values())
    assert schema["required"] == ["update_json"] and schema["additionalProperties"] is False
    result = _call(client, headers, "get_write_contract", {"preview_tool": "preview_knowledge_update"})["structuredContent"]
    original = copy.deepcopy(result)
    assert result["canonical_input_schema"]["properties"]["update"]["type"] == "object"
    result["canonical_input_schema"]["properties"].clear()
    assert _call(client, headers, "get_write_contract", {"preview_tool": "preview_knowledge_update"})["structuredContent"] == original
    assert _call(client, headers, "get_write_contract", {"preview_tool": "preview_plan_update"})["isError"]
    assert _call(client, headers, "get_write_contract", {"preview_tool": "preview_knowledge_update", "extra": True})["isError"]
    caps = _call(client, headers, "get_capabilities", {})["structuredContent"]
    assert caps["remote_write_input_format"] == "json-string"
    assert caps["remote_write_contract_tool"] == "get_write_contract"
    read = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key)}
    listed = _post(client, "tools/list", headers=read).json()["result"]["tools"]
    assert "get_write_contract" not in {tool["name"] for tool in listed}
    assert _call(client, read, "get_write_contract", {"preview_tool": "preview_knowledge_update"})["isError"]
    assert _call(client, read, "preview_knowledge_update", _arguments(_request()))["isError"]
    assert _call(client, headers, "list_portfolios", {})["structuredContent"]["portfolio_ids"] == ["portfolio-alpha"]


@pytest.mark.parametrize("setup", ["json-string"], indirect=True)
@pytest.mark.parametrize("kind", ["knowledge", "portfolio"])
def test_json_preview_preserves_content_and_still_requires_independent_approval(setup, kind):
    client, headers, writes, _, backends = setup
    update = _request() if kind == "knowledge" else _narrow_request()
    if kind == "knowledge":
        update["current_state"] = {}
        update["evidence"][0]["content_ref_or_value"] = {
            "analysis_note": '전체 원문을 유지합니다. "따옴표"와 줄바꿈\n생략 없음.',
            "empty": {}, "nested": [None, False, {"optional": []}],
        }
    result = _call(client, headers, "preview_" + kind + "_update", _arguments(update))
    assert not result.get("isError"), result
    review = result["structuredContent"]
    assert review["status"] == "review_required"
    if kind == "knowledge":
        assert review["payload_digest"] == digest(update) and review["current_state_fields"] == []
        exact = backends["fixture-reader"].connector_review(review["preview_id"])
        assert exact["canonical_payload"] == update
    args = {key: review[key] for key in ["preview_id", "confirmation"]}
    assert _call(client, headers, "apply_" + kind + "_update", args)["isError"]
    _approve(client, review)
    applied = _call(client, headers, "apply_" + kind + "_update", args)["structuredContent"]
    assert applied["status"] == "applied" and applied["read_back"]["matches"]
    assert _call(client, headers, "apply_" + kind + "_update", args)["isError"]


@pytest.mark.parametrize("setup", ["json-string"], indirect=True)
def test_changed_digest_bad_schema_and_out_of_scope_never_create_a_candidate(setup):
    client, headers, writes, _, _ = setup
    update = _request()
    args = _arguments(update)
    update["current_state"] = {}
    args["update_json"] = json.dumps(update)
    error = _call(client, headers, "preview_knowledge_update", args)
    assert error["isError"] and "digest mismatch" in error["content"][0]["text"]
    assert not writes.pending
    update["request_type"] = "incorrect"
    assert _call(client, headers, "preview_knowledge_update", _arguments(update))["isError"]
    portfolio = _narrow_request(); portfolio["portfolio_id"] = "portfolio-beta"
    assert _call(client, headers, "preview_portfolio_update", _arguments(portfolio))["isError"]
    assert not writes.pending
    plain = _request(); plain["current_state"] = {}
    without_digest = _call(client, headers, "preview_knowledge_update", {"update_json": json.dumps(plain)})
    assert without_digest["structuredContent"]["current_state_fields"] == []
    cached = _call(client, headers, "preview_knowledge_update", {"update": _request()})
    assert cached["isError"] and "refresh tool definitions" in cached["content"][0]["text"]


@pytest.mark.parametrize("setup", ["json-string"], indirect=True)
@pytest.mark.parametrize("raw", [
    '{"secret":"do-not-leak",', '{"a":1,"a":2}', '{"a":{"b":1,"b":2}}',
    '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', '[1]', 'null',
    '"do-not-leak"', '{"a":"\\ud800"}',
    '[' * 1100 + '0' + ']' * 1100,
], ids=["syntax", "duplicate", "nested-duplicate", "nan", "infinity", "overflow",
        "array", "null", "string", "surrogate", "depth"])
def test_invalid_json_is_bounded_and_does_not_echo_input_or_stage(setup, raw):
    client, headers, writes, _, _ = setup
    result = _call(client, headers, "preview_knowledge_update", {"update_json": raw})
    assert result["isError"] and "do-not-leak" not in json.dumps(result)
    assert not writes.pending


@pytest.mark.parametrize("setup", ["json-string"], indirect=True)
def test_json_update_byte_bound_and_http_body_limit_are_preserved(setup):
    from protocol.v1.transport.mcp_read_surface import ReadSurfaceError
    raw = json.dumps({"note": "가" * 25000}, ensure_ascii=False)
    assert len(raw) < 65536 and len(raw.encode()) > 65536
    with pytest.raises(ReadSurfaceError, match="Invalid tool arguments"):
        decode_write_arguments({"update_json": raw})
    client, headers, writes, _, _ = setup
    response = _post(client, "tools/call", {"name": "preview_knowledge_update", "arguments": {"update_json": raw}}, headers=headers)
    assert response.status_code == 413 and not writes.pending


@pytest.mark.parametrize("arguments", [
    {"update_json": "{}", "update": {}}, {"update_json": {}},
    {"update_json": "{}", "expected_payload_digest": None},
    {"update_json": "{}", "expected_payload_digest": "0" * 63},
    {"update_json": "{}", "expected_payload_digest": "A" * 64},
])
def test_json_envelope_cannot_mix_formats_or_silently_ignore_fields(arguments):
    from protocol.v1.transport.mcp_read_surface import ReadSurfaceError
    with pytest.raises(ReadSurfaceError, match="Invalid tool arguments"):
        decode_write_arguments(arguments)


def test_object_default_does_not_advertise_or_accept_json_envelope(setup):
    client, headers, writes, _, _ = setup
    tools = {tool["name"]: tool for tool in _post(client, "tools/list", headers=headers).json()["result"]["tools"]}
    assert "get_write_contract" not in tools
    assert "update" in tools["preview_knowledge_update"]["inputSchema"]["properties"]
    assert _call(client, headers, "preview_knowledge_update", _arguments(_request()))["isError"]
    assert not writes.pending


@pytest.mark.parametrize("format", ["json-string", "unknown"])
def test_json_profile_requires_existing_write_configuration(signing_key, format):
    with pytest.raises(ValueError, match="Invalid HTTP write input format"):
        create_http_app(_gateway(), _verifier(signing_key), write_input_format=format)


def test_json_plan_profile_retains_portfolio_scope_and_trusted_approval(runtime):
    _, headers, writes, _, backends, key, _ = runtime
    app = create_http_app(backends["fixture-reader"], JWTTokenVerifier(writes.resource, _jwks(key)),
                          writes=writes, write_input_format="json-string")
    with TestClient(app, base_url="https://mcp.example.test") as client:
        tools = _post(client, "tools/list", headers=headers).json()["result"]["tools"]
        contract = next(tool for tool in tools if tool["name"] == "get_write_contract")
        assert contract["inputSchema"]["properties"]["preview_tool"]["enum"] == ["preview_plan_update"]
        assert _call(client, headers, "preview_plan_update", _arguments(plan_request(portfolio_id="portfolio-beta")))["isError"]
        preview = _call(client, headers, "preview_plan_update", _arguments(plan_request()))["structuredContent"]
        args = {key: preview[key] for key in ["preview_id", "confirmation"]}
        assert _call(client, headers, "apply_plan_update", args)["isError"]
        _approve(client, preview)
        result = _call(client, headers, "apply_plan_update", args)["structuredContent"]
        assert result["status"] == "applied" and result["read_back"]["matches"]
