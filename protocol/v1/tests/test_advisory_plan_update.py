from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from protocol.v1.adapters.advisory_state import get_active_plans, get_plan_history
from protocol.v1.runtime.advisory_plan_store import AdvisoryPlanStore
from protocol.v1.runtime.advisory_plan_update import (
    PlanUpdateCoordinator, PlanUpdateRejected, apply_plan_update, approval_envelope,
)
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.security.trusted_approval import ApprovalRejected, TrustedApprovalStore
from protocol.v1.tests.test_mcp_write_surface import _call, _grant, _principal, _request
from protocol.v1.transport.mcp_stdio import TradeMindMCPServer


ROOT = Path(__file__).resolve().parents[3]


def plan_request(operation="create", plan_id=None, portfolio_id="portfolio-alpha"):
    request = {
        "operation": operation, "portfolio_id": portfolio_id, "source_ref": "user-statement:synthetic-plan",
        "reason": "Replace the synthetic advisory board" if operation == "replace" else "Synthetic lifecycle test",
    }
    if plan_id is not None:
        request["plan_id"] = plan_id
    if operation != "close":
        request["plan"] = {
            "title": "Synthetic conditional plan", "objective": "Review conditions before any separate decision",
            "stages": [{"stage_id": "review", "conditions": ["Refresh all account observations"],
                        "actions": [{"account_id": "account-alpha", "symbol": "005930",
                                     "description": "Evaluate the hypothetical allocation only after revalidation",
                                     "conditions": ["A separate current review is required"]}]}],
        }
    return request


def coordinator(tmp_path, *, grant=None, principal=None):
    grant = copy.deepcopy(grant or _grant())
    if "plan.update" not in grant["permissions"]:
        grant["permissions"].append("plan.update")
    return PlanUpdateCoordinator(AdvisoryPlanStore(tmp_path / "plans"), TrustedApprovalStore(tmp_path / "approvals"),
                                 principal or _principal(), grant)


def apply_review(service, preview):
    return service.apply({"preview_id": preview["preview_id"], "confirmation": preview["confirmation"]})


def receipt(service, preview):
    return service.approval_store.issue(approval_envelope(preview), principal=service.principal, grant=service.grant,
                                       interaction_ref="interaction:synthetic", approval_method="fixture", ttl_seconds=300)


def apply_receipt(service, preview, approval):
    return apply_plan_update(service.store, preview, approval, principal=service.principal, grant=service.grant,
                             approval_verifier=service.approval_store.verify)


def test_create_replace_close_preserves_full_versions_and_curated_history(tmp_path):
    service = coordinator(tmp_path)
    preview = service.preview({"update": plan_request()})
    assert not (tmp_path / "plans").exists()  # preview is non-materializing
    first = apply_review(service, preview)
    original = service.store.list_plans()[0]
    assert first["read_back"]["active_plan_ids"] == [first["plan_id"]]
    assert original["order_authorized"] is original["automatic_execution"] is False
    preview = service.preview({"update": plan_request("replace", first["plan_id"])})
    second = apply_review(service, preview)
    assert second["prior_plan_id"] == first["plan_id"]
    closed = apply_review(service, service.preview({"update": plan_request("close", second["plan_id"])}))
    assert closed["read_back"] == {"matches": True, "active_plan_ids": []}
    reopened = AdvisoryPlanStore(service.store.root)
    rows = reopened.list_plans()
    assert [row["status"] for row in rows] == ["superseded", "closed"]
    assert rows[0]["stages"] == original["stages"]
    assert rows[1]["supersedes"] == first["plan_id"]
    state = reopened.snapshot()
    assert len(state["history"]) == 3
    assert state["history"][1]["before"] == original
    history = get_plan_history(reopened, portfolio_id="portfolio-alpha", limit=2)
    assert history["total_events"] == 3 and history["truncated"] is True
    assert [row["operation"] for row in history["events"]] == ["close", "replace"]
    assert not {"approval_id", "verification_ref", "before", "after", "preview_id"} & set(history["events"][0])
    assert get_plan_history(reopened, portfolio_id="portfolio-beta")["events"] == []
    assert len(get_plan_history(reopened, portfolio_id="portfolio-alpha", plan_id=first["plan_id"])["events"]) == 2
    assert (service.store.native_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(plan_id="arbitrary"), lambda r: r.pop("plan"),
    lambda r: r["plan"].update(order_authorized=True), lambda r: r["plan"].update(status="active"),
    lambda r: r["plan"]["stages"].clear(), lambda r: r.update(principal={"subject_user_id": "forged"}),
    lambda r: r["plan"]["stages"][0]["actions"][0].update(parameters={"quantity": float("nan")}),
])
def test_invalid_or_authority_injecting_requests_never_materialize(tmp_path, mutation):
    service = coordinator(tmp_path)
    request = plan_request()
    mutation(request)
    with pytest.raises(PlanUpdateRejected):
        service.preview({"update": request})
    assert not service.store.native_path.exists()


def test_active_target_and_scope_are_explicit(tmp_path):
    service = coordinator(tmp_path)
    with pytest.raises(PlanUpdateRejected, match="scope_denied"):
        service.preview({"update": plan_request(portfolio_id="portfolio-beta")})
    with pytest.raises(PlanUpdateRejected, match="target_not_active"):
        service.preview({"update": plan_request("close", "unknown")})
    first = apply_review(service, service.preview({"update": plan_request()}))
    with pytest.raises(PlanUpdateRejected, match="active_exists"):
        service.preview({"update": plan_request()})
    with pytest.raises(PlanUpdateRejected, match="target_not_active"):
        service.preview({"update": plan_request("replace", "wrong-id")})
    assert get_active_plans(service.store, portfolio_id="portfolio-alpha")["data"]["plans"][0]["plan_id"] == first["plan_id"]


def test_confirmation_replay_restart_and_client_isolation(tmp_path):
    service = coordinator(tmp_path)
    preview = service.preview({"update": plan_request()})
    with pytest.raises(PlanUpdateRejected, match="not_found"):
        apply_review(coordinator(tmp_path), preview)
    with pytest.raises(PlanUpdateRejected, match="confirmation_mismatch"):
        service.apply({"preview_id": preview["preview_id"], "confirmation": "yes"})
    with pytest.raises(PlanUpdateRejected, match="consumed"):
        apply_review(service, preview)
    preview = service.preview({"update": plan_request()})
    apply_review(service, preview)
    with pytest.raises(PlanUpdateRejected, match="consumed"):
        apply_review(service, preview)
    assert len(service.store.snapshot()["history"]) == 1


@pytest.mark.parametrize("field,value", [("permissions", []), ("portfolio_scope", []),
                                          ("client_id", "client:other"), ("expires_at", {"value": "2000-01-01T00:00:00Z"})])
def test_authority_is_rechecked_at_apply(tmp_path, field, value):
    service = coordinator(tmp_path)
    preview = service.preview({"update": plan_request()})
    service.grant[field] = value
    with pytest.raises(PlanUpdateRejected):
        apply_review(service, preview)
    assert not service.store.native_path.exists()


def test_approval_must_be_issued_bound_unexpired_and_not_revoked(tmp_path):
    service = coordinator(tmp_path)
    preview = service.preview({"update": plan_request()})
    approval = receipt(service, preview)
    forged = {**approval, "approval_id": "approval:forged"}
    with pytest.raises(PlanUpdateRejected, match="trusted_approval"):
        apply_receipt(service, preview, forged)
    changed = copy.deepcopy(preview)
    changed["candidate"]["next_plan"]["title"] = "Unapproved edit"
    with pytest.raises(PlanUpdateRejected, match="preview_invalid"):
        apply_receipt(service, changed, approval)
    from protocol.v1.adapters.common import digest
    changed["candidate_digest"] = digest(changed["candidate"])
    with pytest.raises(PlanUpdateRejected, match="binding_mismatch"):
        apply_receipt(service, changed, approval)
    expired = {**approval, "expires_at": {"value": "2000-01-01T00:00:00Z"}}
    with pytest.raises(PlanUpdateRejected, match="expired"):
        apply_receipt(service, preview, expired)
    service.approval_store.revoke(approval["approval_id"], principal=service.principal, grant=service.grant, reason="synthetic revocation")
    with pytest.raises(PlanUpdateRejected, match="trusted_approval"):
        apply_receipt(service, preview, approval)
    assert not service.store.native_path.exists()


def test_expired_preview_and_stale_snapshot_fail_closed(tmp_path):
    service = coordinator(tmp_path)
    old = service.preview({"update": plan_request()})
    service.pending[old["preview_id"]]["candidate"]["expires_at"] = {"value": "2000-01-01T00:00:00Z"}
    with pytest.raises((ApprovalRejected, PlanUpdateRejected)):
        apply_review(service, old)
    first = service.preview({"update": plan_request()})
    stale = coordinator(tmp_path)
    second = stale.preview({"update": plan_request()})
    apply_review(service, first)
    with pytest.raises(PlanUpdateRejected, match="stale"):
        apply_review(stale, second)
    assert len(service.store.list_plans()) == 1


def test_even_an_issued_receipt_cannot_cross_candidate_scope(tmp_path):
    from protocol.v1.adapters.common import digest
    service = coordinator(tmp_path)
    preview = service.preview({"update": plan_request()})
    preview["candidate"]["next_plan"]["portfolio_id"] = "portfolio-beta"
    preview["candidate_digest"] = digest(preview["candidate"])
    with pytest.raises(PlanUpdateRejected, match="scope_or_lineage"):
        apply_receipt(service, preview, receipt(service, preview))
    assert not service.store.native_path.exists()


def test_concurrent_writers_publish_exactly_one_revision(tmp_path):
    services = [coordinator(tmp_path), coordinator(tmp_path)]
    previews = [service.preview({"update": plan_request()}) for service in services]
    approvals = [receipt(service, preview) for service, preview in zip(services, previews)]
    barrier = Barrier(2)
    def run(index):
        barrier.wait()
        try:
            return apply_receipt(services[index], previews[index], approvals[index])["status"]
        except PlanUpdateRejected as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, [0, 1]))
    assert sorted(results) == ["applied", "plan_preview_stale"]
    assert len(services[0].store.snapshot()["history"]) == 1


def test_failed_publish_preserves_imported_baseline(tmp_path, monkeypatch):
    import hashlib
    import protocol.v1.runtime.advisory_plan_store as store_module
    root = tmp_path / "plans"
    root.mkdir()
    rows = [{"plan_id": "imported-plan:" + suffix, "portfolio_id": "portfolio-" + suffix,
             "status": "active", "stages": [], "order_authorized": False, "automatic_execution": False,
             "requires_current_portfolio_and_market_revalidation": True,
             "trigger_is_not_execution_authority": True} for suffix in ["alpha", "beta"]]
    raw = json.dumps(rows).encode()
    (root / "plans.json").write_bytes(raw)
    (root / "manifest.json").write_text(json.dumps({"schema_version": 1, "plans_sha256": hashlib.sha256(raw).hexdigest()}))
    originals = {p: p.read_bytes() for p in [root / "plans.json", root / "manifest.json"]}
    grant = _grant()
    grant["portfolio_scope"] = ["portfolio-alpha", "portfolio-beta"]
    service = coordinator(tmp_path, grant=grant)
    old = next(row for row in service.store.list_plans() if row["portfolio_id"] == "portfolio-alpha")
    preview = service.preview({"update": plan_request("replace", old["plan_id"], "portfolio-alpha")})
    def fail(*args):
        raise OSError("synthetic disk failure")
    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", fail)
        with pytest.raises(OSError):
            apply_review(service, preview)
    assert not service.store.native_path.exists()
    assert all(p.read_bytes() == value for p, value in originals.items())
    result = apply_review(service, service.preview({"update": plan_request("replace", old["plan_id"], "portfolio-alpha")}))
    assert result["read_back"]["matches"]
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert next(row for row in service.store.list_plans() if row["portfolio_id"] == "portfolio-beta")["status"] == "active"


def test_readback_mismatch_cannot_report_success(tmp_path, monkeypatch):
    service = coordinator(tmp_path)
    preview = service.preview({"update": plan_request()})
    original = service.store.replace_plans
    def commit_then_break_readback(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(AdvisoryPlanStore, "snapshot", lambda self: {"state_digest": "wrong"})
        return result
    monkeypatch.setattr(service.store, "replace_plans", commit_then_break_readback)
    with pytest.raises(PlanUpdateRejected, match="readback_mismatch"):
        apply_review(service, preview)


def test_gpt_native_tools_grants_and_full_lifecycle(tmp_path):
    grant = _grant()
    grant["permissions"].append("plan.update")
    gateway = build_reference_gateway(runtime_root=ROOT, manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
                                      native_store_root=tmp_path / "native", advisory_state_root=tmp_path / "advisory",
                                      approval_store_root=tmp_path / "approvals", write_principal=_principal(), write_grant=grant)
    server = TradeMindMCPServer(gateway)
    listed = server.handle_request(_request("tools/list"))["result"]["tools"]
    tools = {tool["name"]: tool for tool in listed}
    assert tools["preview_plan_update"]["annotations"]["readOnlyHint"] is True
    assert tools["apply_plan_update"]["annotations"]["readOnlyHint"] is False
    assert tools["apply_plan_update"]["_meta"]["io.trademind/requiresExplicitConfirmation"] is True
    first = None
    for index, operation in enumerate(["create", "replace", "close"]):
        preview = _call(server, "preview_plan_update", {"update": plan_request(operation, first)}, request_id=f"preview-{index}")
        result = _call(server, "apply_plan_update", {"preview_id": preview["preview_id"], "confirmation": preview["confirmation"]}, request_id=f"apply-{index}")
        assert result["status"] == "applied" and result["read_back"]["matches"]
        first = result["plan_id"]
    history = _call(server, "get_plan_history", {"portfolio_id": "portfolio-alpha"}, request_id="history")
    assert history["total_events"] == 3
    grant["tool_allowlist"] = ["preview_plan_update"]
    restricted = build_reference_gateway(runtime_root=ROOT, manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
                                         native_store_root=tmp_path / "native", advisory_state_root=tmp_path / "advisory",
                                         approval_store_root=tmp_path / "approvals", write_principal=_principal(), write_grant=grant)
    assert restricted.connector_tool_ready("preview_plan_update")
    assert not restricted.connector_tool_ready("apply_plan_update")


def test_read_only_store_and_corruption_fail_closed(tmp_path):
    service = coordinator(tmp_path)
    apply_review(service, service.preview({"update": plan_request()}))
    store = AdvisoryPlanStore(service.store.root, read_only=True)
    with pytest.raises(ValueError, match="read_only"):
        store.replace_plans([], expected_digest="x", event={})
    data = json.loads(service.store.native_path.read_text())
    data["history"][0]["reason"] = "corrupt"
    service.store.native_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        store.snapshot()
