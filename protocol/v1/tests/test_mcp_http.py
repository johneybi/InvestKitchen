from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from starlette.testclient import TestClient

from protocol.v1.adapters.gateway_facade import ReadOnlyGatewayFacade
from protocol.v1.adapters.market_legacy import adapt_market_result
from protocol.v1.security.oauth_resource import JWTTokenVerifier, OAuthResourceConfig, READ_SCOPE
from protocol.v1.tests.fixture_handlers import synthetic_handlers
from protocol.v1.transport.mcp_http import create_http_app
from protocol.v1.transport.mcp_read_surface import ReadSurfaceError, RemoteReadSurface


ROOT = Path(__file__).resolve().parents[3]
RESOURCE = "https://mcp.example.test/mcp"
ISSUER = "https://auth.example.test/"
PERMISSIONS = ["system.capabilities.read", "portfolio.read", "knowledge.read", "market.read", "decision.context.build"]
HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28"}


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key):
    return {"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
                      "kid": "fixture-key", "alg": "RS256", "use": "sig"}]}


def _config(permissions=PERMISSIONS):
    return OAuthResourceConfig.model_validate({
        "issuer_url": ISSUER, "resource_url": RESOURCE,
        "grants": [{"subject": "fixture-reader", "client_id": "gemini-fixture",
                    "permissions": permissions, "portfolio_scope": ["portfolio-alpha"]},
                   {"subject": "fixture-other", "client_id": "gemini-fixture",
                    "permissions": PERMISSIONS, "portfolio_scope": ["portfolio-beta"]}],
    })


def _jwt(key, **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": RESOURCE, "iat": now, "exp": now + 300,
              "sub": "fixture-reader", "client_id": "gemini-fixture", "scope": READ_SCOPE}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture-key"})


def _verifier(key, permissions=PERMISSIONS):
    return JWTTokenVerifier(_config(permissions), _jwks(key))


def _access(key, **overrides):
    return asyncio.run(_verifier(key).verify_token(_jwt(key, **overrides)))


def _market(capability):
    def handler(payload):
        raw = {"retrieved_at": "2026-10-14T00:00:00Z", "source": "synthetic",
               "quotes": {symbol: {"symbol": symbol, "price": 123} for symbol in payload["symbols"]},
               "components": {"quotes": {"status": "available"}}}
        return adapt_market_result(raw, capability=capability, source_mode="fixture")
    return handler


def _gateway():
    return ReadOnlyGatewayFacade(
        workspace=ROOT, manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
        portfolio_ids=["portfolio-alpha", "portfolio-beta"],
        handlers=synthetic_handlers(market_handlers={"market.quote": _market("market.quote")}),
        connector_handlers={"apply_knowledge_update": lambda payload: pytest.fail("Write dispatched"),
                            "preview_knowledge_update": lambda payload: pytest.fail("Preview dispatched")},
    )


def _context(**overrides):
    request = {"decision_request_id": "request:fixture", "actor": "fixture-reader",
               "objective": "Compare synthetic facts", "subject_refs": [],
               "as_of_request": {"value": "2026-10-14T00:00:00Z", "precision": "source_exact"},
               "portfolio_id": "portfolio-alpha", "requested_context": ["portfolio.state", "knowledge.current"]}
    request.update(overrides)
    return {"request": request}


def _rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {
        **(params or {}), "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "gemini-fixture", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }}


def _post(client, method, params=None, *, headers):
    routing = {"Mcp-Method": method}
    if params and "name" in params:
        routing["Mcp-Name"] = params["name"]
    return client.post("/mcp", headers={**headers, **routing}, json=_rpc(method, params))


@pytest.mark.parametrize("claims", [
    {"iss": "https://wrong.example.test/"}, {"aud": "https://wrong.example.test/mcp"},
    {"exp": 1}, {"iat": int(time.time()) + 3600}, {"nbf": int(time.time()) + 3600},
    {"sub": "unknown"}, {"client_id": "unknown"}, {"sub": None},
    {"scope": [READ_SCOPE]}, {"exp": True}, {"iat": True},
])
def test_signed_token_must_match_issuer_resource_identity_and_time(signing_key, claims):
    assert _access(signing_key, **claims) is None


def test_tokens_require_rsa_signature_and_known_key(signing_key):
    verifier = _verifier(signing_key)
    valid = _jwt(signing_key)
    assert asyncio.run(verifier.verify_token(valid)) is not None
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert asyncio.run(verifier.verify_token(_jwt(other_key))) is None
    for token in ["not-a-jwt", jwt.encode({"sub": "fixture-reader"}, "synthetic-fixture-secret-32-bytes-long", algorithm="HS256"),
                  jwt.encode({"sub": "fixture-reader"}, signing_key, algorithm="RS256", headers={"kid": "unknown"})]:
        assert asyncio.run(verifier.verify_token(token)) is None


def test_provider_array_scope_claim_requires_explicit_configuration_and_valid_members(signing_key):
    config = OAuthResourceConfig.model_validate({**_config().model_dump(), "scope_claim": "scp"})
    verifier = JWTTokenVerifier(config, _jwks(signing_key))
    token = _jwt(signing_key, scope=None, scp=[READ_SCOPE, "openid", "offline_access"])
    access = asyncio.run(verifier.verify_token(token))
    assert access is not None and access.scopes == [READ_SCOPE, "openid", "offline_access"]
    assert asyncio.run(_verifier(signing_key).verify_token(token)) is None
    with TestClient(create_http_app(_gateway(), verifier), base_url="https://mcp.example.test") as client:
        headers = {**HEADERS, "Authorization": "Bearer " + token}
        assert _post(client, "tools/list", headers=headers).status_code == 200
        denied = _post(client, "tools/call", {"name": "get_portfolio_state", "arguments": {
            "portfolio_id": "portfolio-beta"}}, headers=headers)
        assert denied.json()["result"]["isError"] is True
        # An unselected string claim cannot add scopes to an array-scope token.
        other = _jwt(signing_key, scope=READ_SCOPE, scp=["openid"])
        assert _post(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer " + other}).status_code == 403
    for invalid in [None, READ_SCOPE, [1], [None], [""], ["investkitchen.read openid"]]:
        assert asyncio.run(verifier.verify_token(_jwt(signing_key, scp=invalid))) is None
    with pytest.raises(ValidationError):
        OAuthResourceConfig.model_validate({**_config().model_dump(), "scope_claim": "permissions"})


def test_configuration_rejects_write_grants_duplicate_identity_and_private_keys(signing_key):
    for update in [{"issuer_url": "http://auth.example.test"}, {"resource_url": "https://mcp.example.test/other"},
                   {"client_id_claim": "sub"}, {"resource_url": "https://owner:password@mcp.example.test/mcp"}]:
        with pytest.raises(ValidationError):
            OAuthResourceConfig.model_validate({**_config().model_dump(), **update})
    for grants in [[_config().grants[0].model_dump()] * 2,
                   [{**_config().grants[0].model_dump(), "permissions": ["knowledge.commit"]}]]:
        with pytest.raises(ValidationError):
            OAuthResourceConfig.model_validate({**_config().model_dump(), "grants": grants})
    private_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key))
    with pytest.raises(ValueError):
        JWTTokenVerifier(_config(), {"keys": [{**private_jwk, "kid": "private-fixture"}]})
    with pytest.raises(ValueError):
        JWTTokenVerifier(_config(), {"keys": _jwks(signing_key)["keys"] * 2})


@pytest.mark.parametrize("scopes", [[], ["openid"], [READ_SCOPE, READ_SCOPE],
                                  [READ_SCOPE, "investkitchen.write"]])
def test_provider_scopes_cannot_remove_read_scope_or_add_writes(scopes):
    with pytest.raises(ValidationError):
        OAuthResourceConfig.model_validate({**_config().model_dump(), "required_scopes": scopes})


def test_provider_refresh_scopes_are_advertised_and_enforced_without_more_data_access(signing_key):
    scopes = [READ_SCOPE, "openid", "offline_access"]
    config = OAuthResourceConfig.model_validate({**_config().model_dump(), "required_scopes": scopes})
    verifier = JWTTokenVerifier(config, _jwks(signing_key))
    with TestClient(create_http_app(_gateway(), verifier), base_url="https://mcp.example.test") as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["scopes_supported"] == scopes
        insufficient = _post(client, "server/discover", headers={
            **HEADERS, "Authorization": "Bearer " + _jwt(signing_key)})
        assert insufficient.status_code == 403
        headers = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key, scope=" ".join(scopes))}
        listed = _post(client, "tools/list", headers=headers)
        assert listed.status_code == 200
        assert all(tool["annotations"]["readOnlyHint"] for tool in listed.json()["result"]["tools"])
        denied = _post(client, "tools/call", {"name": "get_portfolio_state", "arguments": {
            "portfolio_id": "portfolio-beta"}}, headers=headers)
        assert denied.json()["result"]["isError"] is True


@pytest.mark.parametrize("issuer", ["https://auth.example.test", "https://auth.example.test/",
                                   "https://auth.example.test:8443/issuer"])
def test_discovery_preserves_exact_issuer_identifier_and_token_validation(signing_key, issuer):
    config = OAuthResourceConfig.model_validate({**_config().model_dump(), "issuer_url": issuer})
    verifier = JWTTokenVerifier(config, _jwks(signing_key))
    with TestClient(create_http_app(_gateway(), verifier), base_url="https://mcp.example.test") as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp", headers={"Origin": "https://client.example.test"})
        assert metadata.json()["authorization_servers"] == [issuer]
        assert metadata.headers["Cache-Control"] == "no-store"
        assert metadata.headers["Access-Control-Allow-Origin"] == "*"
        exact = _post(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer " + _jwt(signing_key, iss=issuer)})
        assert exact.status_code == 200
        changed_issuer = issuer.rstrip("/") if issuer.endswith("/") else issuer + "/"
        denied = _post(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer " + _jwt(signing_key, iss=changed_issuer)})
        assert denied.status_code == 401


def test_discovery_and_direct_reads_obey_server_owned_scope(signing_key):
    surface = RemoteReadSurface(_gateway(), _config())
    token = _access(signing_key)
    tools = surface.list_tools(token)
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
    assert not any(tool["name"].startswith(("preview_", "apply_")) for tool in tools)
    assert surface.call_tool(token, "list_portfolios", {})["portfolio_ids"] == ["portfolio-alpha"]
    assert surface.call_tool(token, "get_portfolio_state", {"portfolio_id": "portfolio-alpha"})["data"]["portfolio_id"] == "portfolio-alpha"
    assert surface.call_tool(token, "search_knowledge", {"query": "fixture"})["data"]["result_count"] == 1
    assert surface.call_tool(token, "get_market_quote", {"symbols": ["AAPL"]})["status"] == "ok"
    with pytest.raises(ReadSurfaceError, match="not authorized"):
        surface.call_tool(token, "get_portfolio_state", {"portfolio_id": "portfolio-beta"})
    with pytest.raises(ReadSurfaceError, match="Invalid tool arguments"):
        surface.call_tool(token, "list_portfolios", {"client_id": "spoofed", "permissions": PERMISSIONS})
    with pytest.raises(ReadSurfaceError, match="Authentication required"):
        surface.list_tools(None)


def test_all_preview_and_apply_tools_are_refused_even_when_runtime_supports_them(signing_key):
    surface = RemoteReadSurface(_gateway(), _config())
    for name in surface.router.tools:
        if name.startswith(("apply_", "preview_")):
            with pytest.raises(ReadSurfaceError, match="Unknown or unavailable"):
                surface.call_tool(_access(signing_key), name, {})


def test_composed_reads_check_every_capability_and_every_portfolio(signing_key):
    surface = RemoteReadSurface(_gateway(), _config())
    token = _access(signing_key)
    result = surface.call_tool(token, "build_decision_context", _context())
    assert result["context"]["portfolio"]["portfolio_id"] == "portfolio-alpha"
    assert result["context"]["knowledge"]["summary"]
    attacks = [
        _context(portfolio_id="portfolio-beta"),
        {**_context(), "capability_inputs": {"portfolio.state": {"portfolio_id": "portfolio-beta"}}},
        _context(requested_context=["policy.current"]),
        {**_context(), "required_capabilities": ["transaction.history"]},
        {**_context(), "capability_inputs": {"reflection.session": {}}},
        {**_context(), "capability_inputs": {"knowledge.search": {"query": "fixture", "limit": 100000}}},
    ]
    for arguments in attacks:
        with pytest.raises(ReadSurfaceError):
            surface.call_tool(token, "build_decision_context", arguments)
    narrow = RemoteReadSurface(_gateway(), _config(["decision.context.build", "system.capabilities.read"]))
    with pytest.raises(ReadSurfaceError, match="not authorized"):
        narrow.call_tool(token, "build_decision_context", _context())
    caps = narrow.call_tool(token, "get_capabilities", {})["capabilities"]
    assert not next(row for row in caps if row["capability"] == "portfolio.state")["client_usable"]


def test_composed_market_reads_preserve_bounds_and_normalization(signing_key):
    surface = RemoteReadSurface(_gateway(), _config())
    arguments = _context(requested_context=["market.quote"], subject_refs=["NASDAQ:AAPL"])
    result = surface.call_tool(_access(signing_key), "build_decision_context", arguments)
    assert result["context"]["component_results"][0]["status"] == "ok"
    arguments["capability_inputs"] = {"market.quote": {"symbols": "AAPL, MSFT"}}
    assert surface.call_tool(_access(signing_key), "build_decision_context", arguments)["context"]["market"]
    arguments["capability_inputs"] = {"market.quote": {"symbols": ["AAPL"] * 101}}
    with pytest.raises(ReadSurfaceError):
        surface.call_tool(_access(signing_key), "build_decision_context", arguments)


def test_composition_refuses_mixed_scope_even_when_both_portfolios_are_granted(signing_key):
    config = _config().model_dump()
    config["grants"][0]["portfolio_scope"] = ["portfolio-alpha", "portfolio-beta"]
    surface = RemoteReadSurface(_gateway(), OAuthResourceConfig.model_validate(config))
    arguments = {**_context(), "capability_inputs": {"portfolio.state": {"portfolio_id": "portfolio-beta"}}}
    with pytest.raises(ReadSurfaceError, match="Conflicting portfolio scope"):
        surface.call_tool(_access(signing_key), "build_decision_context", arguments)


def test_http_authentication_metadata_modern_reads_and_denied_writes(signing_key):
    with TestClient(create_http_app(_gateway(), _verifier(signing_key)), base_url="https://mcp.example.test") as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == RESOURCE
        assert metadata.json()["authorization_servers"] == [ISSUER]
        for authorization in [None, "Bearer invalid", "Bearer " + _jwt(signing_key, aud="https://wrong.example.test/mcp"),
                              "Bearer " + _jwt(signing_key, exp=1), "Bearer " + _jwt(signing_key, sub="unknown")]:
            headers = {**HEADERS, **({"Authorization": authorization} if authorization else {})}
            denied = _post(client, "server/discover", headers=headers)
            assert denied.status_code == 401
            assert "resource_metadata=" in denied.headers["WWW-Authenticate"]
            assert denied.headers["Cache-Control"] == "no-store"
        insufficient = _post(client, "server/discover", headers={**HEADERS, "Authorization": "Bearer " + _jwt(signing_key, scope="other")})
        assert insufficient.status_code == 403
        headers = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key)}
        discovery = _post(client, "server/discover", headers=headers)
        assert discovery.status_code == 200
        assert "2026-07-28" in discovery.json()["result"]["supportedVersions"]
        listing = _post(client, "tools/list", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["result"]["cacheScope"] == "private"
        read = _post(client, "tools/call", {"name": "list_portfolios", "arguments": {}}, headers=headers)
        assert read.json()["result"]["structuredContent"]["portfolio_ids"] == ["portfolio-alpha"]
        # Signed token claims and model-supplied MCP identity metadata do not add a grant.
        inflated = {**headers, "Authorization": "Bearer " + _jwt(signing_key, permissions=["portfolio.read", "knowledge.commit"], portfolio_scope=["portfolio-beta"])}
        body = _rpc("tools/call", {"name": "get_portfolio_state", "arguments": {"portfolio_id": "portfolio-beta"}})
        body["params"]["_meta"]["subject_user_id"] = "fixture-other"
        denied = client.post("/mcp", headers={**inflated, "Mcp-Method": "tools/call", "Mcp-Name": "get_portfolio_state"}, json=body)
        assert denied.json()["result"]["isError"] is True
        for name in ["apply_knowledge_update", "preview_knowledge_update"]:
            denied = _post(client, "tools/call", {"name": name, "arguments": {}}, headers=headers)
            assert denied.json()["result"]["isError"] is True


def test_legacy_initialize_and_session_identity_isolation(signing_key):
    with TestClient(create_http_app(_gateway(), _verifier(signing_key)), base_url="https://mcp.example.test") as client:
        headers = {"Accept": HEADERS["Accept"], "Authorization": "Bearer " + _jwt(signing_key)}
        initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"},
        }})
        assert initialized.status_code == 200
        assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"
        session_headers = {**headers, "MCP-Protocol-Version": "2025-11-25", "Mcp-Session-Id": initialized.headers["Mcp-Session-Id"]}
        notification = client.post("/mcp", headers=session_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert notification.status_code == 202
        listing = client.post("/mcp", headers=session_headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listing.status_code == 200
        assert listing.json()["result"]["tools"]
        other_headers = {**session_headers, "Authorization": "Bearer " + _jwt(signing_key, sub="fixture-other")}
        denied = client.post("/mcp", headers=other_headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert denied.status_code in {403, 404}


def test_http_errors_do_not_expose_local_paths_and_reject_host_origin_and_large_body(signing_key):
    gateway = _gateway()
    gateway.handlers["knowledge.current"] = lambda payload: (_ for _ in ()).throw(RuntimeError("/private/fixture/secret-file token=synthetic-secret"))
    with TestClient(create_http_app(gateway, _verifier(signing_key)), base_url="https://mcp.example.test") as client:
        headers = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key)}
        response = _post(client, "tools/call", {"name": "get_current_knowledge", "arguments": {}}, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is True
        assert "secret-file" not in response.text
        assert "synthetic-secret" not in response.text
        for extra in [{"Host": "untrusted.example.test"}, {"Origin": "https://untrusted.example.test"}]:
            denied = _post(client, "server/discover", headers={**headers, **extra})
            assert denied.status_code in {400, 403, 421}
        too_big = client.post("/mcp", headers={**headers, "Content-Type": "application/json"}, content="x" * 65537)
        assert too_big.status_code == 413


def test_cli_fails_closed_and_redacts_missing_configuration_paths(tmp_path):
    missing = tmp_path / "private-configuration.json"
    completed = subprocess.run([
        sys.executable, "-m", "protocol.v1.transport.mcp_http", "--oauth-config", str(missing),
        "--public-jwks", str(missing), "--manifest", str(missing), "--personal-data-root", str(tmp_path),
    ], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "InvestKitchen HTTP MCP configuration error\n"
