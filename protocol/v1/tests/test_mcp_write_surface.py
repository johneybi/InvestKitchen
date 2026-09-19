from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"

from protocol.v1.runtime.reference_composition import build_reference_gateway  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import build_checkpoint, persist_checkpoint  # noqa: E402
from protocol.v1.security.remote_gateway import (  # noqa: E402
    ReplayGuard,
    authorize_authenticated_request,
    load_tool_catalog,
)
from protocol.v1.tests.test_native_knowledge_store import _request as knowledge_request  # noqa: E402
from protocol.v1.tests.test_portfolio_update_service import _narrow_request, _stores  # noqa: E402
from protocol.v1.transport.mcp_stdio import MCP_PROTOCOL_VERSION, TradeMindMCPServer  # noqa: E402


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


def _principal() -> dict[str, Any]:
    return {
        "subject_user_id": "user:connector",
        "client_id": "client:chatgpt-mcp",
        "credential_binding_id": "credential:connector-test",
        "authentication_event_id": "auth:connector-test",
        "authenticated_at": _tp("2026-09-17T00:00:00Z"),
        "expires_at": _tp("2099-01-01T00:00:00Z"),
    }


def _grant() -> dict[str, Any]:
    return {
        "grant_id": "grant:connector-test",
        "instance_id": "fixture-full-reference",
        "subject_user_id": "user:connector",
        "client_id": "client:chatgpt-mcp",
        "credential_binding_id": "credential:connector-test",
        "permissions": [
            "knowledge.commit",
            "portfolio.update",
            "operation.approve",
            "knowledge.read",
            "portfolio.read",
        ],
        "portfolio_scope": ["portfolio-alpha"],
        "tool_allowlist": [],
        "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
        "policy_version": "connector-test-v1",
        "issued_at": _tp("2026-09-17T00:00:00Z"),
        "expires_at": _tp("2099-01-01T00:00:00Z"),
    }


def _meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "connector-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _request(method: str, params: dict[str, Any] | None = None, *, request_id: str = "r") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**(params or {}), "_meta": _meta()},
    }


def _call(server: TradeMindMCPServer, name: str, arguments: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    response = server.handle_request(_request(
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id=request_id,
    ))
    assert response is not None
    assert "error" not in response
    return response["result"]["structuredContent"]


def _account_snapshot() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "provider_id": "provider-synthetic",
        "provider_account_ref": "provider-account-ref:synthetic",
        "snapshot_effective_at": _tp("2026-09-17T02:00:00Z"),
        "retrieved_at": _tp("2026-09-17T02:00:01Z"),
        "source_ref": "provider-observation:mcp-synthetic",
        "holdings": [
            {
                "asset": {
                    "asset_type": "stock", "symbol": "005930", "venue": "KRX", "currency": "KRW",
                    "display_name": "Synthetic A", "provider_refs": {},
                },
                "quantity": 12,
            },
            {
                "asset": {
                    "asset_type": "stock", "symbol": "000660", "venue": "KRX", "currency": "KRW",
                    "display_name": "Synthetic B", "provider_refs": {},
                },
                "quantity": 3,
            },
        ],
        "cash": [{"currency": "KRW", "cash_kind": "nominal_balance", "amount": 900}],
        "completeness": {"holdings": "complete", "cash": "complete"},
    }


def _server(
    tmp_path: Path,
    *,
    account_sync: bool = False,
    unrelated_unresolved: bool = False,
) -> TradeMindMCPServer:
    write_store, checkpoint_store = _stores(tmp_path)
    if unrelated_unresolved:
        latest = checkpoint_store.latest("portfolio-alpha")
        base = copy.deepcopy(latest["base_portfolio"])
        base["accounts"].append({
            "account_id": "account-beta",
            "portfolio_id": "portfolio-alpha",
            "display_name": "Unrelated Account",
            "provider_id": None,
            "account_type": "irp",
            "base_currency": "KRW",
            "role": "core",
            "status": "active",
            "constraints": [],
        })
        base["positions"].append({
            "position_id": "position-beta-unresolved",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-beta",
            "asset": {
                "asset_type": "unknown",
                "symbol": None,
                "venue": None,
                "currency": "KRW",
                "display_name": "Legacy Unresolved Product",
                "provider_refs": {},
            },
            "quantity": 1,
            "quantity_status": "confirmed",
            "quantity_basis": "direct_observation",
            "authority": "portfolio_fact",
            "source_evidence": ["legacy:beta"],
            "state": "open",
        })
        base["completeness"]["accounts"].append({
            "account_id": "account-beta",
            "holdings": "unknown",
            "cash": "unknown",
            "observed_at": _tp("2026-09-17T01:30:00Z"),
        })
        checkpoint = build_checkpoint(
            base,
            write_store,
            snapshot_effective_at=_tp("2026-09-17T01:30:00Z"),
            through_commit_id=None,
            source_kind="manual_snapshot",
            source_ref="fixture:unrelated-unresolved",
            cash_basis_refs=latest.get("cash_basis_refs") or [],
            short_allowed_account_ids=latest.get("short_allowed_account_ids") or [],
            created_at=_tp("2026-09-17T01:31:00Z"),
        )
        persist_checkpoint(checkpoint_store, write_store, checkpoint)
    grant = _grant()
    if account_sync:
        grant["permissions"].append("account.read")
    gateway = build_reference_gateway(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        native_store_root=write_store.root,
        portfolio_checkpoint_root=checkpoint_store.root,
        approval_store_root=tmp_path / "approvals",
        write_principal=_principal(),
        write_grant=grant,
        account_bindings=([{
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "provider_id": "provider-synthetic",
            "provider_account_ref": "provider-account-ref:synthetic",
        }] if account_sync else None),
        account_snapshot_providers=({"provider-synthetic": lambda _binding: _account_snapshot()} if account_sync else None),
        account_sync_max_age_seconds=315360000,
    )
    return TradeMindMCPServer(gateway)


def test_mcp_connector_lists_preview_read_only_and_apply_mutating_tools(tmp_path: Path) -> None:
    response = _server(tmp_path).handle_request(_request("tools/list", request_id="list"))
    assert response is not None
    by_name = {tool["name"]: tool for tool in response["result"]["tools"]}
    for name in (
        "preview_knowledge_update",
        "apply_knowledge_update",
        "preview_portfolio_update",
        "apply_portfolio_update",
    ):
        assert name in by_name
    assert by_name["preview_knowledge_update"]["annotations"]["readOnlyHint"] is True
    assert by_name["preview_portfolio_update"]["annotations"]["readOnlyHint"] is True
    assert by_name["apply_knowledge_update"]["annotations"]["readOnlyHint"] is False
    assert by_name["apply_portfolio_update"]["annotations"]["readOnlyHint"] is False
    assert by_name["apply_knowledge_update"]["annotations"]["destructiveHint"] is False
    assert by_name["apply_portfolio_update"]["annotations"]["destructiveHint"] is False
    assert by_name["apply_knowledge_update"]["_meta"]["io.trademind/requiresExplicitConfirmation"] is True
    assert by_name["apply_portfolio_update"]["_meta"]["io.trademind/requiresExplicitConfirmation"] is True

    knowledge_schema = by_name["preview_knowledge_update"]["inputSchema"]
    knowledge_update = knowledge_schema["properties"]["update"]
    assert knowledge_update["required"] == [
        "protocol_version",
        "request_type",
        "generation_id",
        "generated_at",
        "current_state",
        "evidence",
        "claims",
    ]
    assert knowledge_update["properties"]["request_type"] == {"const": "knowledge.commit"}
    assert knowledge_schema["$defs"]["evidence"]["required"][0] == "evidence_id"
    assert knowledge_schema["$defs"]["claim"]["required"][0] == "claim_id"
    assert knowledge_schema["$defs"]["timePoint"]["required"] == ["value", "precision"]
    assert knowledge_schema["$defs"]["timePoint"]["properties"]["value"]["anyOf"] == [
        {"format": "date-time"},
        {"format": "date"},
    ]
    assert knowledge_schema["$defs"]["timePoint"]["allOf"][0]["then"] == {
        "required": ["inference_basis"]
    }

    portfolio_schema = by_name["preview_portfolio_update"]["inputSchema"]
    portfolio_update = portfolio_schema["properties"]["update"]
    assert portfolio_update["properties"]["request_version"] == {"const": 1}
    assert set(portfolio_update["properties"]["changes"]["properties"]) == {
        "position_quantities",
        "cash_amounts",
    }
    assert portfolio_schema["$defs"]["positionChange"]["required"] == ["account_id", "quantity"]
    assert "position_id" in portfolio_schema["$defs"]["positionChange"]["properties"]
    assert portfolio_schema["$defs"]["cashChange"]["required"] == ["cash_id", "amount"]
    assert portfolio_schema["$defs"]["timePoint"] == knowledge_schema["$defs"]["timePoint"]


def test_remote_authorization_uses_write_permissions_and_portfolio_scope() -> None:
    grant = _grant()
    denied_scope_request = {
        "request_id": "write-scope",
        "instance_id": "fixture-full-reference",
        "method": "tools.call",
        "params": {
            "name": "preview_portfolio_update",
            "arguments": {"update": {"portfolio_id": "portfolio-beta"}},
        },
        "issued_at": _tp("2026-09-17T00:00:00Z"),
        "expires_at": _tp("2026-09-17T00:01:00Z"),
        "nonce": "write-scope-nonce",
    }
    decision, _ = authorize_authenticated_request(
        denied_scope_request,
        principal=_principal(),
        grant=grant,
        replay_guard=ReplayGuard(),
        now=datetime(2026, 9, 17, 0, 0, 30, tzinfo=timezone.utc),
        catalog=load_tool_catalog(),
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "portfolio_scope_denied"
    assert decision["permissions_evaluated"] == ["portfolio.update"]

    no_approval = _grant()
    no_approval["permissions"].remove("operation.approve")
    apply_request = {
        **denied_scope_request,
        "request_id": "write-permission",
        "params": {
            "name": "apply_knowledge_update",
            "arguments": {"preview_id": "preview:test", "confirmation": "confirm"},
        },
        "nonce": "write-permission-nonce",
    }
    decision, _ = authorize_authenticated_request(
        apply_request,
        principal=_principal(),
        grant=no_approval,
        replay_guard=ReplayGuard(),
        now=datetime(2026, 9, 17, 0, 0, 30, tzinfo=timezone.utc),
        catalog=load_tool_catalog(),
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "permission_denied"
    assert decision["permissions_evaluated"] == ["knowledge.commit", "operation.approve"]


def test_knowledge_connector_requires_exact_prior_preview_confirmation_and_rejects_replay(tmp_path: Path) -> None:
    server = _server(tmp_path)
    update = knowledge_request()
    preview = _call(server, "preview_knowledge_update", {"update": update}, request_id="kp")
    encoded = json.dumps(preview, ensure_ascii=False)
    assert preview["status"] == "review_required"
    assert preview["claim_count"] == 1
    assert update["claims"][0]["statement"] not in encoded
    assert "evidence:native:alpha" not in encoded

    mismatch = _call(server, "apply_knowledge_update", {
        "preview_id": preview["preview_id"],
        "confirmation": "APPROVE SOMETHING ELSE",
    }, request_id="km")
    assert mismatch == {"status": "blocked", "error_code": "confirmation_mismatch"}

    replay_after_mismatch = _call(server, "apply_knowledge_update", {
        "preview_id": preview["preview_id"],
        "confirmation": preview["confirmation"],
    }, request_id="kmr")
    assert replay_after_mismatch == {"status": "blocked", "error_code": "preview_replayed"}

    preview = _call(server, "preview_knowledge_update", {
        "update": knowledge_request(generation_id="knowledge-generation:2026-09-17-2"),
    }, request_id="kp2")

    applied = _call(server, "apply_knowledge_update", {
        "preview_id": preview["preview_id"],
        "confirmation": preview["confirmation"],
    }, request_id="ka")
    assert applied["status"] == "applied"
    assert applied["generation_id"] == "knowledge-generation:2026-09-17-2"
    assert applied["read_back"] == {"generation_id": "knowledge-generation:2026-09-17-2", "matches": True}

    replay = _call(server, "apply_knowledge_update", {
        "preview_id": preview["preview_id"],
        "confirmation": preview["confirmation"],
    }, request_id="kr")
    assert replay == {"status": "blocked", "error_code": "preview_replayed"}


def test_knowledge_connector_stale_preview_fails_closed(tmp_path: Path) -> None:
    server = _server(tmp_path)
    first = knowledge_request(generation_id="knowledge-generation:connector-first")
    second = knowledge_request(generation_id="knowledge-generation:connector-second")
    second["claims"][0]["claim_id"] = "claim-second"
    first_preview = _call(server, "preview_knowledge_update", {"update": first}, request_id="s1")
    second_preview = _call(server, "preview_knowledge_update", {"update": second}, request_id="s2")

    _call(server, "apply_knowledge_update", {
        "preview_id": second_preview["preview_id"],
        "confirmation": second_preview["confirmation"],
    }, request_id="s2a")
    stale = _call(server, "apply_knowledge_update", {
        "preview_id": first_preview["preview_id"],
        "confirmation": first_preview["confirmation"],
    }, request_id="s1a")
    assert stale == {"status": "blocked", "error_code": "preview_stale"}


def test_portfolio_connector_binds_candidate_confirmation_and_returns_bounded_readback(tmp_path: Path) -> None:
    server = _server(tmp_path)
    preview = _call(server, "preview_portfolio_update", {"update": _narrow_request()}, request_id="pp")
    assert preview["status"] == "review_required"
    assert preview["summary"]["position_changes"] == 1
    assert preview["summary"]["cash_changes"] == 1
    encoded = json.dumps(preview, ensure_ascii=False)
    assert "reconciled_portfolio" not in encoded
    assert "source_ref" not in encoded

    mismatch = _call(server, "apply_portfolio_update", {
        "preview_id": preview["preview_id"],
        "confirmation": "APPROVE PORTFOLIO UPDATE wrong",
    }, request_id="pm")
    assert mismatch == {"status": "blocked", "error_code": "confirmation_mismatch"}

    replay_after_mismatch = _call(server, "apply_portfolio_update", {
        "preview_id": preview["preview_id"],
        "confirmation": preview["confirmation"],
    }, request_id="pmr")
    assert replay_after_mismatch == {"status": "blocked", "error_code": "preview_replayed"}

    retry_update = _narrow_request()
    retry_update["created_at"] = _tp("2026-09-17T01:02:00Z")
    preview = _call(server, "preview_portfolio_update", {"update": retry_update}, request_id="pp2")

    applied = _call(server, "apply_portfolio_update", {
        "preview_id": preview["preview_id"],
        "confirmation": preview["confirmation"],
    }, request_id="pa")
    assert applied["status"] == "applied"
    assert applied["portfolio_id"] == "portfolio-alpha"
    assert applied["read_back"]["matches"] is True
    assert applied["read_back"]["candidate_ref"] == preview["preview_id"]

    replay = _call(server, "apply_portfolio_update", {
        "preview_id": preview["preview_id"],
        "confirmation": preview["confirmation"],
    }, request_id="pr")
    assert replay == {"status": "blocked", "error_code": "preview_replayed"}


def test_portfolio_connector_stale_candidate_is_rejected_after_other_update_applies(tmp_path: Path) -> None:
    server = _server(tmp_path)
    first_update = _narrow_request()
    first_update["changes"]["position_quantities"][0]["quantity"] = 11
    second_update = _narrow_request()
    second_update["changes"]["position_quantities"][0]["quantity"] = 12
    first = _call(server, "preview_portfolio_update", {"update": first_update}, request_id="p1")
    second = _call(server, "preview_portfolio_update", {"update": second_update}, request_id="p2")

    _call(server, "apply_portfolio_update", {
        "preview_id": second["preview_id"],
        "confirmation": second["confirmation"],
    }, request_id="p2a")
    stale = _call(server, "apply_portfolio_update", {
        "preview_id": first["preview_id"],
        "confirmation": first["confirmation"],
    }, request_id="p1a")
    assert stale == {"status": "blocked", "error_code": "preview_stale"}


def test_account_sync_connector_is_server_bound_preview_confirm_apply_with_readback(tmp_path: Path) -> None:
    server = _server(tmp_path, account_sync=True)
    listed = server.handle_request(_request("tools/list", request_id="account-list"))
    assert listed is not None
    by_name = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert by_name["preview_account_sync"]["annotations"]["readOnlyHint"] is True
    assert by_name["apply_account_sync"]["annotations"]["readOnlyHint"] is False
    assert by_name["apply_account_sync"]["_meta"]["io.trademind/requiresExplicitConfirmation"] is True

    preview = _call(
        server,
        "preview_account_sync",
        {"portfolio_id": "portfolio-alpha", "account_id": "account-alpha"},
        request_id="account-preview",
    )
    assert preview["status"] == "review_required"
    assert preview["summary"]["position_changes"] == 1
    assert preview["summary"]["cash_changes"] == 1
    encoded = json.dumps(preview, ensure_ascii=False)
    assert "provider-account-ref:synthetic" not in encoded
    assert "reconciled_portfolio" not in encoded

    applied = _call(
        server,
        "apply_account_sync",
        {"preview_id": preview["preview_id"], "confirmation": preview["confirmation"]},
        request_id="account-apply",
    )
    assert applied["status"] == "applied"
    assert applied["portfolio_id"] == "portfolio-alpha"
    assert applied["account_id"] == "account-alpha"
    assert applied["read_back"]["matches"] is True


def test_account_sync_apply_ignores_unrelated_unresolved_position_identity(tmp_path: Path) -> None:
    server = _server(tmp_path, account_sync=True, unrelated_unresolved=True)
    preview = _call(
        server,
        "preview_account_sync",
        {"portfolio_id": "portfolio-alpha", "account_id": "account-alpha"},
        request_id="account-preview-scoped",
    )
    assert preview["status"] == "review_required"
    applied = _call(
        server,
        "apply_account_sync",
        {"preview_id": preview["preview_id"], "confirmation": preview["confirmation"]},
        request_id="account-apply-scoped",
    )
    assert applied["status"] == "applied"
    assert applied["read_back"]["matches"] is True
