from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.security.oauth_resource import JWTTokenVerifier
from protocol.v1.tests.test_mcp_http import (
    HEADERS, ROOT, _config, _jwt, _jwks, _post, signing_key,
)
from protocol.v1.tests.test_native_knowledge_store import _request as knowledge_request
from protocol.v1.tests.test_personal_data_migration import _synthetic_bundle
from protocol.v1.tests.test_portfolio_update_service import _stores, _narrow_request
from protocol.v1.transport.mcp_confirmed_writes import (
    COOKIE, WRITE_SCOPE, AutheliaBrowserIdentity, ConfirmedWriteConfig, ConfirmedWriteSurface,
)
from protocol.v1.transport.mcp_http import create_http_app


def _write_config(**overrides):
    return ConfirmedWriteConfig.model_validate({
        "authelia_authz_url": "http://127.0.0.1:19091/api/authz/forward-auth",
        "grants": [{"subject": "fixture-reader", "client_id": "gemini-fixture",
                    "browser_username": "fixture-owner",
                    "permissions": ["knowledge.commit", "portfolio.update"],
                    "portfolio_scope": ["portfolio-alpha"], "expires_at": "2099-01-01T00:00:00Z"},
                   {"subject": "fixture-other", "client_id": "gemini-fixture",
                    "browser_username": "fixture-other",
                    "permissions": ["knowledge.commit", "portfolio.update"],
                    "portfolio_scope": ["portfolio-beta"], "expires_at": "2099-01-01T00:00:00Z"}],
        **overrides,
    })


@pytest.fixture
def setup(tmp_path, signing_key):
    native, checkpoints = _stores(tmp_path)
    personal = _synthetic_bundle(tmp_path / "personal")
    resource = _config()
    backends = {}
    def factory(grant):
        expiry = {"value": grant.expires_at.isoformat(), "precision": "source_exact"}
        identity = {"subject_user_id": grant.subject, "client_id": grant.client_id,
                    "credential_binding_id": "credential:" + grant.subject}
        principal = {**identity, "expires_at": expiry}
        authority = {**identity, "grant_id": "grant:" + grant.subject,
                     "permissions": [*grant.permissions, "operation.approve"],
                     "portfolio_scope": list(grant.portfolio_scope), "expires_at": expiry}
        gateway = build_reference_gateway(
            runtime_root=ROOT, manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
            personal_data_root=personal,
            native_store_root=native.root, portfolio_checkpoint_root=checkpoints.root,
            approval_store_root=tmp_path / "authority" / grant.subject,
            write_principal=principal, write_grant=authority)
        backends[grant.subject] = gateway
        return gateway
    now = [time.time()]
    writes = ConfirmedWriteSurface(resource, _write_config(), factory,
        audit_root=tmp_path / "audit", clock=lambda: now[0],
        browser_identity=lambda request: request.cookies.get("fixture-session"))
    read_gateway = backends["fixture-reader"]
    # Both IDs are configured in the read facade; beta has no checkpoint to mutate.
    read_gateway.portfolio_ids = ["portfolio-alpha", "portfolio-beta"]
    verifier = JWTTokenVerifier(resource, _jwks(signing_key))
    app = create_http_app(read_gateway, verifier, writes=writes)
    with TestClient(app, base_url="https://mcp.example.test") as client:
        headers = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key, scope="investkitchen.read " + WRITE_SCOPE)}
        yield client, headers, writes, now, backends


def _call(client, headers, name, args):
    response = _post(client, "tools/call", {"name": name, "arguments": args}, headers=headers)
    assert response.status_code == 200
    return response.json()["result"]


def _preview(client, headers, kind="knowledge"):
    args = {"update": knowledge_request() if kind == "knowledge" else _narrow_request()}
    result = _call(client, headers, "preview_" + kind + "_update", args)
    assert not result.get("isError")
    value = result["structuredContent"]
    assert value["status"] == "review_required"
    return value


def _browser_form(client, preview, username="fixture-owner"):
    client.cookies.set("fixture-session", username)
    path = urlsplit(preview["approval_url"]).path
    page = client.get(path)
    assert page.status_code == 200, page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return path, {"csrf": csrf, "decision": "approve"}


def _approve(client, preview):
    path, form = _browser_form(client, preview)
    response = client.post(path, data=form, headers={"Origin": "https://mcp.example.test"})
    assert response.status_code == 200
    assert "아직 저장하지 않았습니다" in response.text


@pytest.mark.parametrize("kind", ["knowledge", "portfolio"])
def test_signed_http_native_write_requires_human_review_and_reads_back(setup, kind):
    client, headers, writes, _, backends = setup
    gateway = backends["fixture-reader"]
    before = (gateway.get_current_knowledge() if kind == "knowledge" else gateway.get_portfolio_state("portfolio-alpha"))
    preview = _preview(client, headers, kind)
    args = {k: preview[k] for k in ["preview_id", "confirmation"]}
    blocked = _call(client, headers, "apply_" + kind + "_update", args)
    assert blocked["isError"] and "browser approval required" in blocked["content"][0]["text"]
    unchanged = gateway.get_current_knowledge() if kind == "knowledge" else gateway.get_portfolio_state("portfolio-alpha")
    def stable_data(result):
        return {k: v for k, v in result["data"].items() if k != "generated_at"}
    assert stable_data(unchanged) == stable_data(before)
    _approve(client, preview)
    wrong = _call(client, headers, "apply_" + kind + "_update", {**args, "confirmation": "forged"})
    assert wrong["isError"]
    applied = _call(client, headers, "apply_" + kind + "_update", args)["structuredContent"]
    assert applied["status"] == "applied" and applied["read_back"]["matches"]
    after = gateway.get_current_knowledge() if kind == "knowledge" else gateway.get_portfolio_state("portfolio-alpha")
    assert stable_data(after) != stable_data(before)
    assert _call(client, headers, "apply_" + kind + "_update", args)["isError"]
    journal = writes.audit_root / "browser-confirmations.jsonl"
    assert journal.stat().st_mode & 0o777 == 0o600
    evidence = json.loads(journal.read_text())
    assert evidence["browser_username"] == "fixture-owner" and evidence["event"].endswith("approve")
    assert "review" not in evidence and "Cookie" not in journal.read_text()


def test_write_discovery_requires_server_grant_and_write_scope(setup, signing_key):
    client, headers, _, _, _ = setup
    tools = _post(client, "tools/list", headers=headers).json()["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["preview_knowledge_update"]["annotations"]["readOnlyHint"]
    assert not by_name["apply_knowledge_update"]["annotations"]["readOnlyHint"]
    assert by_name["apply_portfolio_update"]["annotations"]["destructiveHint"]
    readonly = {**headers, "Authorization": "Bearer " + _jwt(signing_key)}
    assert not any(t["name"].startswith(("apply_", "preview_")) for t in
                   _post(client, "tools/list", headers=readonly).json()["result"]["tools"])
    assert _call(client, readonly, "preview_knowledge_update", {"update": knowledge_request()})["isError"]
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert WRITE_SCOPE in metadata["scopes_supported"]
    caps = _call(client, headers, "get_capabilities", {})["structuredContent"]
    assert "apply_knowledge_update" in caps["remote_write_tools"]


def test_identity_scope_and_apply_tool_are_bound_to_preview(setup, signing_key):
    client, headers, _, _, _ = setup
    bad = _narrow_request(); bad["portfolio_id"] = "portfolio-beta"
    assert _call(client, headers, "preview_portfolio_update", {"update": bad})["isError"]
    preview = _preview(client, headers)
    _approve(client, preview)
    args = {k: preview[k] for k in ["preview_id", "confirmation"]}
    other = {**headers, "Authorization": "Bearer " + _jwt(signing_key, sub="fixture-other", scope="investkitchen.read " + WRITE_SCOPE)}
    assert _call(client, other, "apply_knowledge_update", args)["isError"]
    assert _call(client, headers, "apply_portfolio_update", args)["isError"]
    assert _call(client, headers, "apply_knowledge_update", args)["structuredContent"]["status"] == "applied"


def test_browser_authentication_csrf_origin_and_rejection_are_fail_closed(setup):
    client, headers, writes, _, _ = setup
    preview = _preview(client, headers)
    path = urlsplit(preview["approval_url"]).path
    unauth = client.get(path, headers={"Remote-User": "fixture-owner"}, follow_redirects=False)
    assert unauth.status_code == 303 and unauth.headers["location"].startswith(writes.origin + "/?rd=")
    client.cookies.set("fixture-session", "different-user")
    assert client.get(path).status_code == 404
    path, form = _browser_form(client, preview)
    for form_value, origin in [(form, "https://evil.example.test"),
                               ({**form, "csrf": "forged"}, writes.origin),
                               ({"decision": "approve"}, writes.origin)]:
        assert client.post(path, data=form_value, headers={"Origin": origin}).status_code == 403
    no_session = client.cookies.get(COOKIE)
    client.cookies.delete(COOKIE)
    assert client.post(path, data=form, headers={"Origin": writes.origin}).status_code == 403
    client.cookies.set(COOKIE, no_session)
    assert client.post(path, data={**form, "decision": "reject"}, headers={"Origin": writes.origin}).status_code == 200
    assert _call(client, headers, "apply_knowledge_update", {k: preview[k] for k in ["preview_id", "confirmation"]})["isError"]


def test_expiry_and_bounded_pending_drop_authority(setup):
    client, headers, writes, now, backends = setup
    writes.max_pending = 1
    preview = _preview(client, headers)
    assert _call(client, headers, "preview_knowledge_update", {"update": knowledge_request(generation_id="next")})["isError"]
    now[0] += 301
    client.cookies.set("fixture-session", "fixture-owner")
    assert client.get(urlsplit(preview["approval_url"]).path).status_code == 404
    # Expired entries are removed from both the broker and native coordinator.
    coordinator = backends["fixture-reader"].connector_review.__self__
    assert not writes.pending and not coordinator.pending


def test_native_write_is_single_use_under_concurrent_http_apply(setup):
    client, headers, _, _, _ = setup
    preview = _preview(client, headers)
    _approve(client, preview)
    args = {k: preview[k] for k in ["preview_id", "confirmation"]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _call(client, headers, "apply_knowledge_update", args), range(2)))
    assert sum(r.get("structuredContent", {}).get("status") == "applied" for r in results) == 1
    assert sum(bool(r.get("isError")) for r in results) == 1


def test_expired_grant_and_expired_token_cannot_use_approved_preview(setup, signing_key):
    client, headers, writes, now, _ = setup
    preview = _preview(client, headers); _approve(client, preview)
    expired = {**headers, "Authorization": "Bearer " + _jwt(signing_key, exp=1, scope="investkitchen.read " + WRITE_SCOPE)}
    response = _post(client, "tools/call", {"name": "apply_knowledge_update", "arguments": {
        k: preview[k] for k in ["preview_id", "confirmation"]}}, headers=expired)
    assert response.status_code == 401
    original = writes.config.grants[0]
    writes.config = writes.config.model_copy(update={"grants": (
        original.model_copy(update={"expires_at": original.expires_at.replace(year=2020)}),
        writes.config.grants[1])})
    assert _call(client, headers, "apply_knowledge_update", {k: preview[k] for k in ["preview_id", "confirmation"]})["isError"]


def test_process_restart_requires_new_preview_and_browser_approval(setup):
    client, headers, writes, _, backends = setup
    preview = _preview(client, headers); _approve(client, preview)
    restarted = ConfirmedWriteSurface(writes.resource, writes.config,
        lambda grant: backends[grant.subject], audit_root=writes.audit_root,
        browser_identity=writes.browser_identity)
    # A valid token does not recover approval state from a model-owned preview ID.
    from mcp.server.auth.provider import AccessToken
    access = AccessToken(token="synthetic", subject="fixture-reader", client_id="gemini-fixture",
                         scopes=["investkitchen.read", WRITE_SCOPE], expires_at=int(time.time()) + 300,
                         resource=writes.resource.resource_url)
    from protocol.v1.transport.mcp_read_surface import ReadSurfaceError
    with pytest.raises(ReadSurfaceError, match="Preview not found"):
        restarted.call_tool(access, "apply_knowledge_update", {k: preview[k] for k in ["preview_id", "confirmation"]})


def test_browser_review_escapes_exact_payload_and_auth_failure_cannot_approve(setup, monkeypatch):
    client, headers, writes, _, _ = setup
    request = knowledge_request(); request["current_state"]["summary"] = '</pre><script>alert("x")</script>'
    preview = _call(client, headers, "preview_knowledge_update", {"update": request})["structuredContent"]
    path, form = _browser_form(client, preview)
    page = client.get(path)
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text
    assert page.headers["cache-control"] == "no-store" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "HttpOnly" in page.headers["set-cookie"] and "Secure" in page.headers["set-cookie"]
    def unavailable(*args): raise OSError("private service path and secret")
    monkeypatch.setattr(writes, "browser_identity", unavailable)
    response = client.post(path, data=form, headers={"Origin": writes.origin})
    assert response.status_code == 503 and "private service" not in response.text
    assert next(iter(writes.pending.values()))["state"] == "awaiting_approval"


def test_approval_audit_failure_does_not_authorize(setup, monkeypatch):
    client, headers, writes, _, _ = setup
    preview = _preview(client, headers); path, form = _browser_form(client, preview)
    def fail(*args): raise OSError("unavailable")
    monkeypatch.setattr(writes, "_audit", fail)
    assert client.post(path, data=form, headers={"Origin": writes.origin}).status_code == 503
    assert next(iter(writes.pending.values()))["state"] == "awaiting_approval"


def test_stale_native_preview_after_other_approved_write_is_rejected(setup):
    client, headers, _, _, _ = setup
    first = _preview(client, headers)
    second = _call(client, headers, "preview_knowledge_update", {
        "update": knowledge_request(generation_id="knowledge-generation:second")})["structuredContent"]
    _approve(client, first); _approve(client, second)
    for preview, expected in [(first, "applied"), (second, "blocked")]:
        result = _call(client, headers, "apply_knowledge_update", {k: preview[k] for k in ["preview_id", "confirmation"]})
        assert result["structuredContent"]["status"] == expected


def test_forward_auth_sends_only_session_cookie_and_pinned_metadata(monkeypatch):
    from starlette.requests import Request
    captured = {}
    class Conn:
        def __init__(self, *args, **kwargs): captured["target"] = args
        def request(self, method, path, headers): captured.update(method=method, path=path, headers=headers)
        def getresponse(self):
            class Reply:
                status = 200
                def getheader(self, name, default): return "fixture-owner" if name == "Remote-User" else default
            return Reply()
        def close(self): pass
    monkeypatch.setattr("http.client.HTTPConnection", Conn)
    request = Request({"type": "http", "method": "POST", "path": "/approve/ticket", "scheme": "https",
                       "headers": [(b"cookie", b"authelia_session=private-session; unwanted=do-not-forward"),
                                   (b"authorization", b"Bearer attacker"), (b"remote-user", b"attacker"),
                                   (b"x-forwarded-host", b"attacker.example.test")]})
    auth = AutheliaBrowserIdentity(_write_config(), "https://mcp.example.test")
    assert auth(request) == "fixture-owner"
    assert captured["headers"]["Cookie"] == "authelia_session=private-session"
    assert captured["headers"]["X-Forwarded-Host"] == "mcp.example.test"
    assert "Authorization" not in captured["headers"] and "Remote-User" not in captured["headers"]


def test_config_requires_loopback_auth_expiring_identity_and_read_scope():
    for endpoint in ["http://evil.example.test/api/authz/forward-auth", "http://127.0.0.1:1/other",
                     "http://user:secret@127.0.0.1:1/api/authz/forward-auth"]:
        with pytest.raises(ValidationError): _write_config(authelia_authz_url=endpoint)
    base = _write_config().model_dump(mode="json")
    for change in [{"expires_at": "2099-01-01"}, {"permissions": ["transaction.record"]},
                   {"portfolio_scope": []}]:
        with pytest.raises(ValidationError):
            _write_config(grants=[{**base["grants"][0], **change}])
    config = _write_config(grants=[{**base["grants"][0], "portfolio_scope": ["portfolio-beta"]}])
    with pytest.raises(ValueError): config.validate_read_grants(_config())


def test_cli_composition_uses_exact_native_stores_and_separate_approval_evidence(tmp_path, signing_key, monkeypatch):
    from protocol.v1.transport.mcp_http import main
    from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore
    native, checkpoints = _stores(tmp_path)
    NativeKnowledgeStore(native.root).read_journal()
    personal = _synthetic_bundle(tmp_path / "personal")
    resource = _config().model_dump(mode="json")
    resource["grants"] = resource["grants"][:1]
    writes = _write_config().model_dump(mode="json")
    writes["grants"] = writes["grants"][:1]
    for name, value in [("resource", resource), ("jwks", _jwks(signing_key)), ("writes", writes)]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    argv = ["mcp_http", "--oauth-config", str(tmp_path / "resource.json"),
            "--public-jwks", str(tmp_path / "jwks.json"),
            "--manifest", str(ROOT / "protocol/v1/fixtures/full-reference.instance.json"),
            "--personal-data-root", str(personal), "--native-store-root", str(native.root),
            "--portfolio-checkpoint-root", str(checkpoints.root),
            "--write-config", str(tmp_path / "writes.json"),
            "--approval-store-root", str(tmp_path / "approvals")]
    captured = {}
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app, options=kwargs))
    assert main() == 0
    assert captured["options"]["host"] == "127.0.0.1" and not captured["options"]["access_log"]
    headers = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key, scope="investkitchen.read " + WRITE_SCOPE)}
    with TestClient(captured["app"], base_url="https://mcp.example.test") as client:
        preview = _preview(client, headers)
        # The CLI uses the real cookie authenticator, never a model header.
        page = client.get(urlsplit(preview["approval_url"]).path,
                          headers={"Remote-User": "fixture-owner"}, follow_redirects=False)
        assert page.status_code == 303
        assert _call(client, headers, "apply_knowledge_update", {
            k: preview[k] for k in ["preview_id", "confirmation"]})["isError"]
    monkeypatch.setattr("sys.argv", argv[:-1] + [str(native.root / "approvals")])
    assert main() == 2
