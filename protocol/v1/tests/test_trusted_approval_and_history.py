from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.native_history import get_decision_history, get_transaction_history  # noqa: E402
from protocol.v1.deployment.local_approval_cli import approval_summary  # noqa: E402
from protocol.v1.runtime.native_write_store import NativeWriteStore, apply_mutation  # noqa: E402
from protocol.v1.runtime.reference_composition import build_reference_gateway  # noqa: E402
from protocol.v1.runtime.write_preview import build_mutation_preview  # noqa: E402
from protocol.v1.security.trusted_approval import ApprovalRejected, TrustedApprovalStore  # noqa: E402
from protocol.v1.transport.mcp_stdio import TradeMindMCPServer  # noqa: E402
from protocol.v1.transport.stdio_rpc import load_tool_catalog  # noqa: E402


NOW = datetime(2026, 9, 16, 0, 30, 0, tzinfo=timezone.utc)


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


def _validate_def(value: Any, schema_name: str, definition: str) -> None:
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://trademind.local/protocol/v1/test-trusted-approval-wrapper.json",
        "$ref": f"{schema_name}#/$defs/{definition}",
    }
    jsonschema.Draft202012Validator(
        wrapper,
        registry=_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(value)


def _principal(user: str = "user:alpha") -> dict[str, Any]:
    return {
        "subject_user_id": user,
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "authentication_event_id": "auth:fixture",
        "authenticated_at": _tp("2026-09-16T00:00:00Z"),
        "expires_at": _tp("2026-09-16T02:00:00Z"),
    }


def _grant(*, approve: bool = True) -> dict[str, Any]:
    permissions = [
        "decision.create",
        "transaction.record",
        "decision.history.read",
        "transaction.history.read",
    ]
    if approve:
        permissions.append("operation.approve")
    return {
        "grant_id": "grant:write-fixture",
        "instance_id": "fixture-full-reference",
        "subject_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "permissions": permissions,
        "portfolio_scope": ["portfolio-alpha"],
        "tool_allowlist": [],
        "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
        "policy_version": "write-fixture-v2",
        "issued_at": _tp("2026-09-16T00:00:00Z"),
        "expires_at": _tp("2026-09-16T02:00:00Z"),
    }


def _decision_request(statement: str, *, decided_at: str) -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "request_type": "decision.create",
        "portfolio_id": "portfolio-alpha",
        "account_ids": ["account-alpha"],
        "subject_refs": ["000660"],
        "statement": statement,
        "action_intent": {"action": "wait", "asset_ref": "000660", "quantity": None, "notes": None},
        "conditions": ["회복 확인"],
        "invalidation_conditions": ["전제 훼손"],
        "rationale_summary": None,
        "source_context_ref": None,
        "decided_at": _tp(decided_at),
        "authority_basis": "explicit_user_decision",
    }


def _transaction_request(*, account_id: str = "account-alpha", quantity: int = 1) -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "request_type": "transaction.record",
        "portfolio_id": "portfolio-alpha",
        "account_id": account_id,
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
        "quantity": quantity,
        "price": None,
        "amount": None,
        "effective_at": _tp("2026-09-16T00:10:00Z"),
        "detail_status": "partial",
        "provider_execution_id": None,
        "transfer_group_id": None,
        "correction_of": None,
        "occurrence_attestation": {
            "attestation_type": "explicit_user_statement",
            "source_ref": f"chat-message:{account_id}:{quantity}",
            "statement_digest": ("a" if quantity == 1 else "b") * 64,
            "attested_at": _tp("2026-09-16T00:20:00Z"),
        },
    }


def _issue_and_apply(
    request: dict[str, Any],
    *,
    write_store: NativeWriteStore,
    approval_store: TrustedApprovalStore,
    now: datetime,
) -> dict[str, Any]:
    preview = build_mutation_preview(request, now=now)
    approval = approval_store.issue(
        preview,
        principal=_principal(),
        grant=_grant(),
        interaction_ref=f"interaction:{preview['preview']['preview_id']}",
        approval_method="local_tty",
        now=now,
    )
    return apply_mutation(
        preview,
        approval,
        principal=_principal(),
        grant=_grant(),
        approval_verifier=approval_store.verify,
        store=write_store,
        now=now,
    )


def _mcp_meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "history-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def test_trusted_approval_is_server_recorded_schema_valid_and_verifiable(tmp_path: Path) -> None:
    preview = build_mutation_preview(_decision_request("기록할 결정", decided_at="2026-09-16T00:15:00Z"), now=NOW)
    store = TrustedApprovalStore(tmp_path / "approvals")
    approval = store.issue(
        preview,
        principal=_principal(),
        grant=_grant(),
        interaction_ref="interaction:tty-1",
        approval_method="local_tty",
        now=NOW,
    )
    _validate_def(approval, "operation.schema.json", "approval")
    rows = store.read_journal()
    assert len(rows) == 1
    _validate(rows[0], "trusted-approval-record.schema.json")
    verified = store.verify(approval, _principal(), _grant())
    assert verified["verified"] is True
    assert verified["verification_ref"].startswith("approval-verification:")


def test_fabricated_tampered_and_revoked_approvals_fail_verification(tmp_path: Path) -> None:
    preview = build_mutation_preview(_decision_request("결정", decided_at="2026-09-16T00:15:00Z"), now=NOW)
    store = TrustedApprovalStore(tmp_path / "approvals")
    approval = store.issue(
        preview,
        principal=_principal(),
        grant=_grant(),
        interaction_ref="interaction:tty-2",
        approval_method="local_tty",
        now=NOW,
    )
    fabricated = copy.deepcopy(approval)
    fabricated["approval_id"] = "approval:fabricated"
    assert store.verify(fabricated, _principal(), _grant())["verified"] is False
    tampered = copy.deepcopy(approval)
    tampered["approved_payload_digest"] = "f" * 64
    assert store.verify(tampered, _principal(), _grant())["verified"] is False
    store.revoke(approval["approval_id"], principal=_principal(), grant=_grant(), reason="operator_cancelled", now=NOW)
    assert store.verify(approval, _principal(), _grant())["verified"] is False
    assert [row["event_type"] for row in store.read_journal()] == ["approval_issued", "approval_revoked"]


def test_approval_issue_requires_distinct_approval_permission_and_scope(tmp_path: Path) -> None:
    preview = build_mutation_preview(_decision_request("결정", decided_at="2026-09-16T00:15:00Z"), now=NOW)
    store = TrustedApprovalStore(tmp_path / "approvals")
    with pytest.raises(ApprovalRejected, match="approval_permission_denied"):
        store.issue(
            preview,
            principal=_principal(),
            grant=_grant(approve=False),
            interaction_ref="interaction:no-permission",
            approval_method="local_tty",
            now=NOW,
        )
    other_scope = _grant()
    other_scope["portfolio_scope"] = ["portfolio-beta"]
    with pytest.raises(ApprovalRejected, match="portfolio_scope_denied"):
        store.issue(
            preview,
            principal=_principal(),
            grant=other_scope,
            interaction_ref="interaction:wrong-scope",
            approval_method="local_tty",
            now=NOW,
        )


def test_trusted_approval_integrates_with_native_apply_without_stub_verifier(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "writes")
    approval_store = TrustedApprovalStore(tmp_path / "approvals")
    result = _issue_and_apply(
        _decision_request("실제 승인 저장", decided_at="2026-09-16T00:15:00Z"),
        write_store=write_store,
        approval_store=approval_store,
        now=NOW,
    )
    assert result["resource_type"] == "decision"
    assert result["receipt"]["state"] == "completed"
    assert len(write_store.list_resources("decision")) == 1


def test_history_projection_is_bounded_filtered_sorted_and_schema_valid(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "writes")
    approval_store = TrustedApprovalStore(tmp_path / "approvals")
    first_time = datetime(2026, 9, 16, 0, 25, 0, tzinfo=timezone.utc)
    second_time = datetime(2026, 9, 16, 0, 26, 0, tzinfo=timezone.utc)
    _issue_and_apply(
        _decision_request("첫 결정", decided_at="2026-09-16T00:12:00Z"),
        write_store=write_store,
        approval_store=approval_store,
        now=first_time,
    )
    _issue_and_apply(
        _decision_request("둘째 결정", decided_at="2026-09-16T00:13:00Z"),
        write_store=write_store,
        approval_store=approval_store,
        now=second_time,
    )
    _issue_and_apply(
        _transaction_request(account_id="account-alpha", quantity=1),
        write_store=write_store,
        approval_store=approval_store,
        now=second_time,
    )
    _issue_and_apply(
        _transaction_request(account_id="account-beta", quantity=2),
        write_store=write_store,
        approval_store=approval_store,
        now=second_time,
    )

    decisions = get_decision_history(write_store, portfolio_id="portfolio-alpha", limit=1)
    transactions = get_transaction_history(write_store, portfolio_id="portfolio-alpha", account_id="account-alpha", limit=50)
    _validate(decisions, "capability-result.schema.json")
    _validate(decisions["data"], "history.schema.json")
    _validate(transactions, "capability-result.schema.json")
    _validate(transactions["data"], "history.schema.json")
    assert decisions["data"]["items"][0]["statement"] == "둘째 결정"
    assert decisions["data"]["count"] == 1
    assert decisions["data"]["total_matching"] == 2
    assert decisions["data"]["truncated"] is True
    assert len(transactions["data"]["items"]) == 1
    assert transactions["data"]["items"][0]["account_id"] == "account-alpha"


def test_history_tools_are_hidden_without_store_and_visible_with_explicit_store_binding(tmp_path: Path) -> None:
    manifest = PROTOCOL / "fixtures" / "full-reference.instance.json"
    no_store_gateway = build_reference_gateway(runtime_root=ROOT, manifest=manifest)
    no_store_server = TradeMindMCPServer(no_store_gateway, catalog=load_tool_catalog())
    request = {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {"_meta": _mcp_meta()}}
    hidden = no_store_server.handle_request(request)
    assert hidden is not None
    hidden_names = {tool["name"] for tool in hidden["result"]["tools"]}
    assert "get_decision_history" not in hidden_names
    assert "get_transactions" not in hidden_names

    bound_gateway = build_reference_gateway(
        runtime_root=ROOT,
        manifest=manifest,
        native_store_root=tmp_path / "writes",
    )
    bound_server = TradeMindMCPServer(bound_gateway, catalog=load_tool_catalog())
    visible = bound_server.handle_request(request)
    assert visible is not None
    visible_names = {tool["name"] for tool in visible["result"]["tools"]}
    assert {"get_decision_history", "get_transactions"}.issubset(visible_names)


def test_local_approval_summary_contains_digest_and_effect_but_no_raw_identity_secret() -> None:
    preview = build_mutation_preview(_decision_request("요약 테스트", decided_at="2026-09-16T00:15:00Z"), now=NOW)
    summary = approval_summary(preview)
    encoded = json.dumps(summary, ensure_ascii=False)
    assert summary["payload_digest"] == preview["preview"]["payload_digest"]
    assert summary["expected_effect"]
    assert "credential_binding" not in encoded
    assert "grant_id" not in encoded


def test_local_approval_cli_refuses_noninteractive_stdin(tmp_path: Path) -> None:
    cli = PROTOCOL / "deployment" / "local_approval_cli.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(cli),
            "--preview",
            str(tmp_path / "preview.json"),
            "--principal",
            str(tmp_path / "principal.json"),
            "--grant",
            str(tmp_path / "grant.json"),
            "--approval-store-root",
            str(tmp_path / "approvals"),
        ],
        input="APPROVE anything\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "TradeMind approval requires an interactive TTY\n"
    assert not (tmp_path / "approvals" / "approval-journal.jsonl").exists()
