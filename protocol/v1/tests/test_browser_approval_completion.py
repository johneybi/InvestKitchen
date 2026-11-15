from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
import json

from protocol.v1.tests.test_mcp_confirmed_writes import (
    setup, signing_key, _preview, _browser_form, _call, _jwt, WRITE_SCOPE,
)
from protocol.v1.tests.test_mcp_plan_http import runtime, preview as plan_preview


def enable(setup):
    client, headers, writes, now, backends = setup
    writes.config = writes.config.model_copy(update={"approval_mode": "approve_and_apply"})
    return setup


def post(client, path, form):
    return client.post(path, data=form, headers={"Origin": "https://mcp.example.test"})


@pytest.mark.parametrize("kind", ["knowledge", "portfolio"])
def test_one_human_confirmation_applies_once_and_ai_only_retrieves_receipt(setup, monkeypatch, kind, signing_key):
    client, headers, writes, now, backends = enable(setup)
    preview = _preview(client, headers, kind)
    assert preview["approval_requirement"]["on_approval"] == "apply_exact_change"
    args = {key: preview[key] for key in ("preview_id", "confirmation")}
    tool = "apply_" + kind + "_update"
    assert _call(client, headers, tool, args)["isError"]
    path, form = _browser_form(client, preview)
    assert "이 변경 승인하고 저장" in client.get(path).text
    router = next(iter(writes.backends.values()))[1]
    original = router.call_tool
    calls = []
    def counted(name, arguments):
        if name.startswith("apply_"): calls.append(name)
        return original(name, arguments)
    monkeypatch.setattr(router, "call_tool", counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(client, path, form), range(2)))
    assert all(r.status_code == 200 and "저장했고 재조회로 확인" in r.text for r in responses)
    first = _call(client, headers, tool, args)["structuredContent"]
    assert first["status"] == "applied" and first["retrieved_from_browser_result"] is True
    assert first["read_back"]["matches"] is True
    assert _call(client, headers, tool, args)["structuredContent"] == first
    assert calls == [tool]
    assert len((writes.audit_root / "browser-confirmations.jsonl").read_text().splitlines()) == 1
    now[0] += 301
    # Refresh the credential, not the already consumed approval.
    headers = {**headers, "Authorization": "Bearer " + _jwt(signing_key, exp=int(now[0]) + 900,
                                                            scope="investkitchen.read " + WRITE_SCOPE)}
    # Expired approval authority does not erase an already-consumed result.
    assert _call(client, headers, tool, args)["structuredContent"] == first
    now[0] += 301
    assert _call(client, headers, tool, args)["isError"]


@pytest.mark.parametrize("mode", ["approve_then_apply", "approve_and_apply"])
def test_displayed_deadline_is_actual_browser_deadline(setup, mode):
    client, headers, writes, now, _ = setup
    writes.config = writes.config.model_copy(update={"approval_mode": mode})
    preview = _preview(client, headers)
    deadline = datetime.fromisoformat(preview["approval_expires_at"].replace("Z", "+00:00")).timestamp()
    assert deadline == pytest.approx(next(iter(writes.pending.values()))["expires"], abs=0.000001, rel=0)
    assert deadline <= now[0] + 300.000001
    assert preview["expires_at"]["value"] == preview["approval_expires_at"]
    assert preview["preview_expires_at"] != preview["expires_at"]
    path, form = _browser_form(client, preview)
    assert preview["approval_expires_at"] in client.get(path).text
    now[0] = deadline + 0.000001
    assert post(client, path, form).status_code == 404


def test_shorter_native_expiry_bounds_link_and_malformed_expiry_fails_closed(setup, monkeypatch):
    client, headers, writes, now, backends = enable(setup)
    gateway = backends["fixture-reader"]
    original = gateway.connector_review
    deadline = datetime.fromtimestamp(now[0] + 30, timezone.utc).isoformat()
    def shorter(identifier):
        result = original(identifier)
        result["expires_at"]["value"] = deadline
        return result
    monkeypatch.setattr(gateway, "connector_review", shorter)
    preview = _preview(client, headers)
    assert datetime.fromisoformat(preview["approval_expires_at"].replace("Z", "+00:00")).timestamp() == pytest.approx(now[0] + 30, abs=0.000001, rel=0)
    monkeypatch.setattr(gateway, "connector_review", lambda identifier: {"expires_at": {"value": "bad"}})
    from protocol.v1.tests.test_native_knowledge_store import _request
    failed = _call(client, headers, "preview_knowledge_update", {"update": _request(generation_id="invalid-expiry")})
    assert failed["isError"]
    assert len(writes.pending) == 1


def test_one_click_still_rejects_forged_origin_session_identity_and_audit_failure(setup, monkeypatch):
    client, headers, writes, _, backends = enable(setup)
    preview = _preview(client, headers)
    path, form = _browser_form(client, preview)
    before = backends["fixture-reader"].get_current_knowledge()["data"]
    assert client.post(path, data=form, headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert post(client, path, {**form, "csrf": "forged"}).status_code == 403
    client.cookies.set("fixture-session", "fixture-other")
    assert post(client, path, form).status_code == 404
    client.cookies.set("fixture-session", "fixture-owner")
    def broken_audit(*args): raise OSError("synthetic audit failure")
    monkeypatch.setattr(writes, "_audit", broken_audit)
    assert post(client, path, form).status_code == 503
    assert next(iter(writes.pending.values()))["state"] == "awaiting_approval"
    after = backends["fixture-reader"].get_current_knowledge()["data"]
    assert {k:v for k,v in after.items() if k != "generated_at"} == {k:v for k,v in before.items() if k != "generated_at"}


def test_receipt_requires_original_identity_and_exact_confirmation(setup, signing_key):
    client, headers, _, _, _ = enable(setup)
    preview = _preview(client, headers)
    path, form = _browser_form(client, preview)
    post(client, path, form)
    args = {key: preview[key] for key in ("preview_id", "confirmation")}
    other = {**headers, "Authorization": "Bearer " + _jwt(signing_key, sub="fixture-other", scope="investkitchen.read " + WRITE_SCOPE)}
    assert _call(client, other, "apply_knowledge_update", args)["isError"]
    assert _call(client, headers, "apply_knowledge_update", {**args, "confirmation": "forged"})["isError"]


def test_commit_then_lost_response_is_unknown_and_never_reapplied(setup, monkeypatch):
    client, headers, writes, _, backends = enable(setup)
    preview = _preview(client, headers)
    path, form = _browser_form(client, preview)
    router = next(iter(writes.backends.values()))[1]
    original = router.call_tool
    calls = []
    def lost(name, arguments):
        if name == "apply_knowledge_update":
            calls.append(name)
            original(name, arguments)
            raise OSError("response lost after commit")
        return original(name, arguments)
    monkeypatch.setattr(router, "call_tool", lost)
    response = post(client, path, form)
    assert response.status_code == 200 and "적용 여부를 확정할 수 없습니다" in response.text
    args = {key: preview[key] for key in ("preview_id", "confirmation")}
    for _ in range(3):
        result = _call(client, headers, "apply_knowledge_update", args)["structuredContent"]
        assert result["status"] == "outcome_unknown" and result["retry_apply_safe"] is False
    assert calls == ["apply_knowledge_update"]
    assert result["recovery"]["arguments"]["generation_id"] == preview["generation_id"]
    assert backends["fixture-reader"].connector_review.__self__.native_knowledge.latest_generation_id() == preview["generation_id"]


@pytest.mark.parametrize("state", ["readback_unavailable", "blocked"])
def test_browser_does_not_claim_verified_success_without_evidence(setup, monkeypatch, state):
    client, headers, writes, _, _ = enable(setup)
    preview = _preview(client, headers)
    path, form = _browser_form(client, preview)
    router = next(iter(writes.backends.values()))[1]
    original = router.call_tool
    def changed(name, arguments):
        if name.startswith("apply_"):
            if state == "blocked": return {"status": "blocked", "reason": "synthetic conflict"}
            result = original(name, arguments)
            result["read_back"] = {"status": "unavailable", "matches": None}
            return result
        return original(name, arguments)
    monkeypatch.setattr(router, "call_tool", changed)
    response = post(client, path, form)
    assert "저장했고 재조회로 확인" not in response.text
    assert ("재조회 검증은 아직" if state == "readback_unavailable" else "차단") in response.text


def test_plan_create_replace_close_with_browser_apply_and_receipt_only_retries(runtime):
    client, headers, writes, _, _, _, _ = runtime
    writes.config = writes.config.model_copy(update={"approval_mode": "approve_and_apply"})
    current = None
    for operation in ["create", "replace", "close"]:
        review = plan_preview(client, headers, operation, current)
        path, form = _browser_form(client, review)
        assert "저장했고 재조회로 확인" in post(client, path, form).text
        args = {k: review[k] for k in ["preview_id", "confirmation"]}
        first = _call(client, headers, "apply_plan_update", args)["structuredContent"]
        assert first["status"] == "applied" and first["retrieved_from_browser_result"]
        current = first["plan_id"]
        assert _call(client, headers, "apply_plan_update", args)["structuredContent"] == first
    history = _call(client, headers, "get_plan_history", {"portfolio_id": "portfolio-alpha"})["structuredContent"]
    assert history["total_events"] == 3


@pytest.mark.parametrize("setup", ["json-string"], indirect=True)
def test_json_string_preview_one_click_preserves_exact_payload(setup):
    from protocol.v1.tests.test_native_knowledge_store import _request
    from protocol.v1.adapters.common import digest
    client, headers, _, _, backends = enable(setup)
    update = _request(); update["current_state"] = {}
    update["evidence"][0]["content_ref_or_value"] = "원문\n{}와 공백 유지"
    review = _call(client, headers, "preview_knowledge_update", {
        "update_json": json.dumps(update), "expected_payload_digest": digest(update)})["structuredContent"]
    path, form = _browser_form(client, review)
    assert "저장했고 재조회로 확인" in post(client, path, form).text
    observed = backends["fixture-reader"].get_knowledge_generation(update["generation_id"], expected_payload_digest=digest(update))
    assert observed["expected_payload_matches"] and observed["content"] == update


def test_rejection_does_not_apply_and_expired_grant_cannot_approve(setup):
    client, headers, writes, now, backends = enable(setup)
    review = _preview(client, headers)
    path, form = _browser_form(client, review)
    post(client, path, {**form, "decision": "reject"})
    assert backends["fixture-reader"].get_knowledge_generation(review["generation_id"])["commit_status"] == "not_observed"
    review = _preview(client, headers)
    path, form = _browser_form(client, review)
    # Expired authorization cannot be prolonged by a live review ticket.
    expired = tuple(g.model_copy(update={"expires_at": datetime.fromtimestamp(now[0] - 1, timezone.utc)})
                    for g in writes.config.grants)
    writes.config = writes.config.model_copy(update={"grants": expired})
    assert post(client, path, form).status_code in {403, 404}
    assert backends["fixture-reader"].get_knowledge_generation(review["generation_id"])["commit_status"] == "not_observed"
