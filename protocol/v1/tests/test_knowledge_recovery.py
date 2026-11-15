"""Synthetic transport/store acceptance; these are not web-model behavior runs."""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from protocol.v1.adapters.common import digest
from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore, MAX_GENERATION_CONTENT_BYTES
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.tests.test_native_knowledge_store import _commit, _request as note
from protocol.v1.tests.test_mcp_confirmed_writes import setup, _call, _approve
from protocol.v1.tests.test_mcp_http import HEADERS, ROOT, _gateway, _jwt, _post, _verifier, signing_key
from protocol.v1.tests.test_mcp_stdio import _request as rpc
from protocol.v1.tests.test_mcp_write_surface import _server as write_server, _call as stdio_call
from protocol.v1.transport.mcp_http import create_http_app
from protocol.v1.transport.mcp_stdio import TradeMindMCPServer
from protocol.v1.transport.stdio_rpc import LocalToolRouter


def reader(store):
    gateway = _gateway()
    gateway.knowledge_generation_reader = store.get_generation
    return gateway


def test_exact_generation_survives_newer_updates_and_restart(tmp_path):
    store = NativeKnowledgeStore(tmp_path / "native")
    first = note()
    first["current_state"] = {}
    first["evidence"][0]["content_ref_or_value"] = "원문\n빈 객체 {}와 따옴표 \"를 보존한다."
    _commit(store, tmp_path / "approval", first)
    _commit(store, tmp_path / "approval", note(generation_id="generation:newer"))
    reopened = NativeKnowledgeStore(store.root, read_only=True)
    result = reader(reopened).get_knowledge_generation(first["generation_id"], expected_payload_digest=digest(first))
    assert result["commit_status"] == "committed" and result["is_latest"] is False
    assert result["expected_payload_matches"] is True
    assert result["content"] == first and result["content_status"] == "complete"
    assert result["content_matches_committed_payload"] is True
    assert result["verification"] == {"commit_and_payload_digest": True,
                                      "source_truth_verified": False, "semantic_fidelity_verified": False}
    assert result["retry_apply_safe"] is False
    assert not {"request", "approval_verification_ref", "journal_version", "event_type"} & result.keys()


def test_missing_mismatched_and_corrupt_records_never_authorize_reapply(tmp_path):
    store = NativeKnowledgeStore(tmp_path / "native", read_only=True)
    gateway = reader(store)
    assert gateway.get_knowledge_generation("generation:absent")["commit_status"] == "unknown"
    assert not store.root.exists()
    # A missing read-only lock is unavailable, not proof of an empty journal.
    NativeKnowledgeStore(store.root).read_journal()
    missing = gateway.get_knowledge_generation("generation:absent")
    assert missing["commit_status"] == "not_observed" and missing["retry_apply_safe"] is False
    assert missing["expected_payload_matches"] is None and not store.journal_path.exists()
    store = NativeKnowledgeStore(store.root)
    request = note()
    _commit(store, tmp_path / "approval", request)
    mismatch = gateway.get_knowledge_generation(request["generation_id"], expected_payload_digest="0" * 64)
    assert mismatch["commit_status"] == "committed" and mismatch["expected_payload_matches"] is False
    assert mismatch["next_action"] == "resolve_payload_conflict_do_not_reapply"
    store.journal_path.write_text('corrupt\n')
    broken = gateway.get_knowledge_generation(request["generation_id"])
    assert broken["status"] == "error" and broken["commit_status"] == "unknown"
    assert broken["retry_apply_safe"] is False
    assert str(store.root) not in str(broken) and "line" not in str(broken)


def test_content_is_bounded_and_explicitly_omitted(tmp_path):
    store = NativeKnowledgeStore(tmp_path / "native")
    request = note()
    request["evidence"][0]["content_ref_or_value"] = "가" * (MAX_GENERATION_CONTENT_BYTES // 3)
    _commit(store, tmp_path / "approval", request)
    result = reader(store).get_knowledge_generation(request["generation_id"], expected_payload_digest=digest(request))
    assert result["commit_status"] == "committed" and result["expected_payload_matches"] is True
    assert result["content"] is None and result["content_status"] == "omitted_size_limit"
    assert result["content_matches_committed_payload"] is None
    metadata = reader(store).get_knowledge_generation(request["generation_id"], include_content=False)
    assert metadata["content_status"] == "not_requested"
    assert len(json.dumps(result).encode()) < 2048


@pytest.mark.parametrize("locator", ["/volume1/private/note", "file:///private/note", "C:\\private\\note",
                                    "source at `/private/secret`", {"access_token": "synthetic-private-value"}])
def test_private_content_is_not_returned_as_raw_record(tmp_path, locator):
    store = NativeKnowledgeStore(tmp_path / "native")
    request = note()
    request["evidence"][0]["content_ref_or_value"] = locator
    _commit(store, tmp_path / "approval", request)
    result = reader(store).get_knowledge_generation(request["generation_id"])
    assert result["content"] is None and result["content_status"] == "withheld_private_locator"
    assert result["content_matches_committed_payload"] is None


def test_public_projection_is_not_claimed_to_match_original_digest(tmp_path):
    store = NativeKnowledgeStore(tmp_path / "native")
    request = note()
    request["evidence"][0]["source_locator"] = "knowledge/private/note"
    _commit(store, tmp_path / "approval", request)
    result = reader(store).get_knowledge_generation(request["generation_id"], expected_payload_digest=digest(request))
    assert result["content_status"] == "public_projection"
    assert result["expected_payload_matches"] is True
    assert result["content_matches_committed_payload"] is False
    assert "knowledge/private/note" not in str(result)


@pytest.mark.parametrize("args", [{"generation_id": "x" * 257}, {"generation_id": " "},
                                 {"generation_id": "x", "expected_payload_digest": "not-a-hash"},
                                 {"generation_id": "x", "include_content": "yes"}])
def test_invalid_lookup_arguments_are_bounded(tmp_path, args):
    gateway = reader(NativeKnowledgeStore(tmp_path / "native"))
    assert gateway.get_knowledge_generation(**args)["status"] == "error"
    assert not (tmp_path / "native").exists()


def test_read_only_composition_installs_lookup_even_for_empty_native_store(tmp_path):
    NativeWriteStore(tmp_path / "native").read_journal()
    NativeKnowledgeStore(tmp_path / "native").read_journal()
    before = sorted(p.name for p in (tmp_path / "native").iterdir())
    gateway = build_reference_gateway(runtime_root=ROOT,
        manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
        native_store_root=tmp_path / "native", read_only_stores=True)
    assert "get_knowledge_generation" in LocalToolRouter(gateway).available_tool_names()
    assert gateway.get_knowledge_generation("generation:missing")["commit_status"] == "not_observed"
    assert sorted(p.name for p in (tmp_path / "native").iterdir()) == before
    assert "get_knowledge_generation" not in LocalToolRouter(_gateway()).available_tool_names()


@pytest.mark.parametrize("setup", ["object", "json-string"], indirect=True)
def test_http_approval_lost_apply_response_and_stdio_readback(setup, signing_key, monkeypatch):
    client, headers, writes, _, backends = setup
    request = note()
    request["current_state"] = {}
    request["evidence"][0]["content_ref_or_value"] = "합성 원문\n한 줄도 누락하지 않는다."
    declarations = _post(client, "tools/list", headers=headers).json()["result"]["tools"]
    schema = next(t["inputSchema"] for t in declarations if t["name"] == "preview_knowledge_update")
    args = ({"update_json": json.dumps(request, ensure_ascii=False), "expected_payload_digest": digest(request)}
            if "update_json" in schema["properties"] else {"update": request})
    review = _call(client, headers, "preview_knowledge_update", args)["structuredContent"]
    assert review["approval_requirement"] == {"method": "separate_authenticated_browser_review",
                                               "approval_url_required": True, "model_may_approve": False,
                                               "on_approval": "await_client_apply"}
    assert review["operation_id"] and review["expires_at"]
    apply_args = {k: review[k] for k in ("preview_id", "confirmation")}
    assert _call(client, headers, "apply_knowledge_update", apply_args)["isError"]
    _approve(client, review)
    _, router = writes.backends[("fixture-reader", "gemini-fixture")]
    original = router.call_tool
    def lose_response(name, arguments):
        result = original(name, arguments)
        if name == "apply_knowledge_update":
            assert result["status"] == "applied"
            raise ConnectionError("synthetic response loss after commit")
        return result
    monkeypatch.setattr(router, "call_tool", lose_response)
    assert _call(client, headers, "apply_knowledge_update", apply_args)["isError"]
    readonly = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key)}
    recovery = review["recovery"]
    for _ in range(10):
        value = _call(client, readonly, recovery["tool"], recovery["arguments"])["structuredContent"]
        assert value["commit_status"] == "committed" and value["expected_payload_matches"] is True
    assert _call(client, headers, "apply_knowledge_update", apply_args)["isError"]
    # A new facade/process-style reader uses only the native domain store and read permission.
    native_root = backends["fixture-reader"].knowledge_generation_reader.__self__.root
    restarted = reader(NativeKnowledgeStore(native_root, read_only=True))
    server = TradeMindMCPServer(restarted)
    result = server.handle_request(rpc("tools/call", {"name": recovery["tool"],
        "arguments": {**recovery["arguments"], "include_content": True}}))["result"]
    assert not result["isError"] and result["structuredContent"]["content"] == request
    assert result["structuredContent"]["content_matches_committed_payload"] is True
    assert len(NativeKnowledgeStore(native_root, read_only=True).read_journal()) == 1


def test_generation_lookup_requires_read_permission_but_no_write_permission(tmp_path, signing_key):
    store = NativeKnowledgeStore(tmp_path / "native")
    _commit(store, tmp_path / "approval", note())
    gateway = reader(store)
    headers = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key)}
    for permissions, allowed in [(["portfolio.read"], False), (["knowledge.read"], True)]:
        with TestClient(create_http_app(gateway, _verifier(signing_key, permissions=permissions)),
                        base_url="https://mcp.example.test") as client:
            tools = _post(client, "tools/list", headers=headers).json()["result"]["tools"]
            assert ("get_knowledge_generation" in {t["name"] for t in tools}) is allowed
            result = _call(client, headers, "get_knowledge_generation", {"generation_id": note()["generation_id"]})
            assert bool(result.get("isError")) is not allowed
            assert _post(client, "tools/list", headers=HEADERS).status_code == 401


def test_stdio_confirmation_and_read_failure_do_not_erase_successful_commit(tmp_path, monkeypatch):
    server = write_server(tmp_path)
    review = stdio_call(server, "preview_knowledge_update", {"update": note()}, request_id="preview")
    assert review["approval_requirement"] == {"method": "trusted_connector_confirmation",
                                               "approval_url_required": False, "model_may_approve": False}
    assert "approval_url" not in review
    original = NativeKnowledgeStore.get_generation
    def fail_read(*args, **kwargs):
        raise OSError("synthetic read failure")
    monkeypatch.setattr(NativeKnowledgeStore, "get_generation", fail_read)
    args = {k: review[k] for k in ("preview_id", "confirmation")}
    applied = stdio_call(server, "apply_knowledge_update", args, request_id="apply")
    assert applied["status"] == "applied"
    assert applied["read_back"] == {"generation_id": note()["generation_id"], "matches": None, "status": "unavailable"}
    monkeypatch.setattr(NativeKnowledgeStore, "get_generation", original)
    observed = stdio_call(server, "get_knowledge_generation", applied["recovery"]["arguments"], request_id="read")
    assert observed["expected_payload_matches"] is True
    assert stdio_call(server, "apply_knowledge_update", args, request_id="retry")["status"] == "blocked"


def test_newer_generation_during_readback_does_not_turn_commit_into_failure(tmp_path, monkeypatch):
    server = write_server(tmp_path)
    review = stdio_call(server, "preview_knowledge_update", {"update": note()}, request_id="preview")
    original = NativeKnowledgeStore.get_generation
    interleaved = []
    def concurrent_read(store, *args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            _commit(store, tmp_path / "other-approval", note(generation_id="generation:concurrent"))
        return original(store, *args, **kwargs)
    monkeypatch.setattr(NativeKnowledgeStore, "get_generation", concurrent_read)
    applied = stdio_call(server, "apply_knowledge_update",
                         {k: review[k] for k in ("preview_id", "confirmation")}, request_id="apply")
    assert applied["status"] == "applied" and applied["read_back"]["matches"] is True
    observed = stdio_call(server, "get_knowledge_generation", applied["recovery"]["arguments"], request_id="read")
    assert observed["is_latest"] is False and observed["expected_payload_matches"] is True


def test_expired_write_grant_preserves_authorized_generation_reads(setup):
    from datetime import datetime, timezone
    client, headers, writes, _, _ = setup
    request = note()
    preview = _call(client, headers, "preview_knowledge_update", {"update": request})["structuredContent"]
    _approve(client, preview)
    assert _call(client, headers, "apply_knowledge_update",
                 {k: preview[k] for k in ("preview_id", "confirmation")})["structuredContent"]["status"] == "applied"
    expired = writes.config.grants[0].model_copy(update={"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)})
    writes.config = writes.config.model_copy(update={"grants": (expired, *writes.config.grants[1:])})
    listed = _post(client, "tools/list", headers=headers).json()["result"]["tools"]
    names = {t["name"] for t in listed}
    assert "get_knowledge_generation" in names and "apply_knowledge_update" not in names
    result = _call(client, headers, "get_knowledge_generation", preview["recovery"]["arguments"])["structuredContent"]
    assert result["expected_payload_matches"] is True
    assert _call(client, headers, "get_capabilities", {})["structuredContent"]["remote_write_authorization"]["status"] == "grant_expired"
    assert _call(client, headers, "preview_knowledge_update", {"update": request})["isError"]
