from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from protocol.v1.runtime.account_state import AccountStateReader
from protocol.v1.runtime.portfolio_account_sync import AccountBindingRegistry
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.security.remote_gateway import load_tool_catalog, portfolio_ids_for_request, required_permissions
from protocol.v1.tests.test_gateway_facade import _validate
from protocol.v1.tests.test_mcp_http import _config, _context, _gateway
from protocol.v1.tests.test_personal_data_migration import _synthetic_bundle
from protocol.v1.tests.test_portfolio_account_sync import _binding
from protocol.v1.tests.test_portfolio_update_service import _stores
from protocol.v1.tests.test_toss_holding_values import _snapshot
from protocol.v1.transport.mcp_read_surface import ReadSurfaceError, RemoteReadSurface
from protocol.v1.transport.mcp_stdio import TradeMindMCPServer
from protocol.v1.transport.stdio_rpc import LocalToolRouter, RequestError


class Clock:
    def __init__(self):
        self.seconds = 0

    def now(self):
        return datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)


def _fresh(clock, binding=None):
    value = _snapshot()
    if binding:
        value.update({key: binding[key] for key in ("portfolio_id", "account_id", "provider_id", "provider_account_ref")})
    point = {"value": clock.now().isoformat(), "precision": "source_exact"}
    value["snapshot_effective_at"] = copy.deepcopy(point)
    value["retrieved_at"] = copy.deepcopy(point)
    for holding in value["holdings"]:
        for field in ("avg_cost_values", "cost_basis_values"):
            for money in holding.get(field, []):
                money["as_of"] = copy.deepcopy(point)
        if "valuation" in holding:
            holding["valuation"]["observed_at"] = copy.deepcopy(point)
    return value


def _reader(provider=None, **options):
    clock, calls = Clock(), []
    def fetch(binding):
        calls.append(copy.deepcopy(binding))
        return provider(clock, binding) if provider else _fresh(clock, binding)
    reader = AccountStateReader(AccountBindingRegistry([_binding()]), {"toss_securities": fetch},
                                portfolio_ids=["portfolio-alpha"], clock=clock.now,
                                monotonic=lambda: clock.seconds, **options)
    return reader, clock, calls


def test_read_cache_expiry_and_forced_refresh_need_no_approval_or_disk_write(tmp_path):
    reader, clock, calls = _reader()
    assert list(tmp_path.iterdir()) == []
    first = reader.read("portfolio-alpha", "account-alpha")
    _validate(first, "capability-result.schema.json")
    _validate(first["data"], "account-state.schema.json")
    assert first["status"] == "ok" and first["source_mode"] == "live_fetch"
    assert first["data"]["approval_required"] is False
    assert first["data"]["canonical_state_updated"] is False
    assert first["data"]["holdings"][2]["avg_cost_values"][0]["amount"] == 80.12
    first["data"]["holdings"][0]["quantity"] = -1
    clock.seconds = 29
    cached = reader.read("portfolio-alpha", "account-alpha")
    assert cached["source_mode"] == "cache" and cached["freshness"] == "cached"
    assert cached["data"]["holdings"][0]["quantity"] == 10
    assert len(calls) == 1
    clock.seconds = 30
    current = reader.read("portfolio-alpha", "account-alpha")
    assert current["source_mode"] == "live_fetch" and len(calls) == 2
    assert current["data"]["snapshot_effective_at"] != cached["data"]["snapshot_effective_at"]
    reader.read("portfolio-alpha", "account-alpha", refresh=True)
    assert len(calls) == 3 and list(tmp_path.iterdir()) == []


def test_outage_returns_dated_stale_data_then_unavailable_without_renewing_cache():
    fail = [False]
    def provider(clock, binding):
        if fail[0]:
            raise RuntimeError("secret-token from /private/provider/config.json")
        return _fresh(clock, binding)
    reader, clock, calls = _reader(provider)
    first = reader.read("portfolio-alpha", "account-alpha")
    fail[0] = True
    clock.seconds = 31
    stale = reader.read("portfolio-alpha", "account-alpha")
    assert stale["status"] == stale["freshness"] == "stale"
    assert stale["data"]["snapshot_effective_at"] == first["data"]["snapshot_effective_at"]
    assert "secret-token" not in repr(stale) and "/private/" not in repr(stale)
    clock.seconds = 901
    unavailable = reader.read("portfolio-alpha", "account-alpha")
    assert unavailable["status"] == "unavailable" and unavailable["data"] is None
    assert len(calls) == 3


@pytest.mark.parametrize("mutation", ["binding", "time", "quantity", "currency", "contradictory", "all_unavailable", "cash_kind", "asset_label"])
def test_unusable_broker_response_fails_closed(mutation):
    def provider(clock, binding):
        value = _fresh(clock, binding)
        if mutation == "binding": value["account_id"] = "other-account"
        elif mutation == "time": value["snapshot_effective_at"]["value"] = "2000-01-01T00:00:00Z"
        elif mutation == "quantity": value["holdings"][0]["quantity"] = float("inf")
        elif mutation == "currency": value["holdings"][0]["valuation"]["currency"] = "USD"
        elif mutation == "contradictory": value["completeness"]["holdings"] = "unavailable"
        elif mutation == "cash_kind": value["cash"][0]["cash_kind"] = "unsupported-cash"
        elif mutation == "asset_label": value["holdings"][0]["asset"]["display_name"] = {"invalid": "object"}
        else:
            value["holdings"], value["cash"] = [], []
            value["completeness"] = {"holdings": "unavailable", "cash": "unavailable"}
        return value
    reader, _, _ = _reader(provider)
    result = reader.read("portfolio-alpha", "account-alpha")
    assert result["status"] == "unavailable" and result["data"] is None


def test_partial_holdings_are_returned_explicitly_and_do_not_replace_cache():
    def provider(clock, binding):
        value = _fresh(clock, binding)
        value["holdings"] = value["holdings"][:1]
        value["completeness"]["holdings"] = "partial"
        return value
    reader, _, calls = _reader(provider)
    result = reader.read("portfolio-alpha", "account-alpha")
    assert result["status"] == "partial" and len(result["data"]["holdings"]) == 1
    assert result["data"]["completeness"]["holdings"] == "partial"
    reader.read("portfolio-alpha", "account-alpha")
    assert len(calls) == 2


def test_private_provider_fields_evidence_paths_and_time_metadata_do_not_escape():
    def provider(clock, binding):
        value = _fresh(clock, binding)
        value["source_ref"] = "/private/provider/secret-token"
        value["holdings"][0]["asset"]["provider_secret"] = "secret-token"
        value["holdings"][0]["asset"]["provider_refs"] = {"provider_asset_id": "secret-token"}
        value["cash"][0]["provider_label"] = "/Users/private/provider.json"
        value["retrieved_at"]["secret"] = "secret-token"
        return value
    reader, _, _ = _reader(provider)
    result = reader.read("portfolio-alpha", "account-alpha")
    _validate(result["data"], "account-state.schema.json")
    serialized = json.dumps(result)
    assert all(text not in serialized for text in ("secret-token", "/private/", "/Users/", "provider_account_ref", "provider_credential_ref", "provider_secret"))


def test_scope_is_checked_even_with_cached_data_and_independent_accounts_do_not_mix():
    clock = Clock()
    calls = []
    bindings = [_binding(), {**_binding(), "portfolio_id": "portfolio-beta", "provider_account_ref": "provider-account-ref:other"}]
    def provider(binding):
        calls.append(binding)
        value = _fresh(clock, binding)
        value["holdings"][0]["quantity"] = 1 if binding["portfolio_id"] == "portfolio-alpha" else 2
        return value
    reader = AccountStateReader(AccountBindingRegistry(bindings), {"toss_securities": provider},
                                portfolio_ids=["portfolio-alpha", "portfolio-beta"], clock=clock.now, monotonic=lambda: 0)
    for portfolio_id, quantity in [("portfolio-alpha", 1), ("portfolio-beta", 2), ("portfolio-alpha", 1)]:
        assert reader.read(portfolio_id, "account-alpha")["data"]["holdings"][0]["quantity"] == quantity
    assert reader.read("not-authorized", "account-alpha")["status"] == "blocked"
    assert reader.read("portfolio-alpha", "unbound-account")["status"] == "unavailable"
    assert len(calls) == 2


def test_concurrent_expired_cache_reads_share_one_provider_request():
    def provider(clock, binding):
        time.sleep(0.03)
        return _fresh(clock, binding)
    reader, _, calls = _reader(provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: reader.read("portfolio-alpha", "account-alpha"), range(4)))
    assert len(calls) == 1 and all(value["status"] == "ok" for value in values)


def test_read_tool_discovery_and_context_work_without_write_grants_or_preview_stores():
    reader, _, calls = _reader()
    gateway = _gateway()
    gateway.account_state_reader = reader.read
    router = LocalToolRouter(gateway)
    assert router.tools["get_account_state"]["read_only"] is True
    assert router.tools["get_account_state"]["required_permissions"] == ["portfolio.read"]
    result = router.call_tool("get_account_state", {"portfolio_id": "portfolio-alpha", "account_id": "account-alpha"})
    assert result["status"] == "ok"
    request = _context(requested_context=["account.state"])
    request.update(capability_inputs={"account.state": {"account_id": "account-alpha"}}, required_capabilities=["account.state"])
    context = router.call_tool("build_decision_context", request)
    assert context["context"]["authority_summary"]["decision_ready"] is True
    assert context["context"]["component_results"][0]["data"]["account_id"] == "account-alpha"
    assert len(calls) == 1
    for field in ("confirmation", "auto_approve", "client_secret", "max_age_seconds"):
        with pytest.raises(RequestError):
            router.call_tool("get_account_state", {"portfolio_id": "portfolio-alpha", "account_id": "account-alpha", field: "forbidden"})
    gateway.account_state_reader = None
    assert "get_account_state" not in router.available_tool_names()


def test_remote_read_grant_enforces_scope_for_direct_and_composed_account_reads():
    reader, _, calls = _reader()
    gateway = _gateway()
    gateway.account_state_reader = reader.read
    surface = RemoteReadSurface(gateway, _config())
    grant = _config().grants[0]
    surface.grant = lambda token: grant
    assert "get_account_state" in surface.tool_names(grant)
    assert surface.call_tool(None, "get_account_state", {"portfolio_id": "portfolio-alpha", "account_id": "account-alpha"})["status"] == "ok"
    with pytest.raises(ReadSurfaceError, match="not authorized"):
        surface.call_tool(None, "get_account_state", {"portfolio_id": "portfolio-beta", "account_id": "account-alpha"})
    context = _context(requested_context=["account.state"])
    context["capability_inputs"] = {"account.state": {"portfolio_id": "portfolio-beta", "account_id": "account-alpha"}}
    with pytest.raises(ReadSurfaceError, match="not authorized"):
        surface.call_tool(None, "build_decision_context", context)
    assert len(calls) == 1
    catalog = load_tool_catalog()
    arguments = {"request": {}, "capability_inputs": {"account.state": {"portfolio_id": "portfolio-beta", "account_id": "account-alpha"}}}
    assert "portfolio.read" in required_permissions("build_decision_context", arguments, catalog)
    assert portfolio_ids_for_request("build_decision_context", arguments) == ["portfolio-beta"]


@pytest.mark.parametrize("automatic", [False, True])
def test_reference_runtime_reads_account_without_enabling_connector_writes(tmp_path, automatic):
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    native, checkpoints = _stores(tmp_path)
    from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore
    NativeKnowledgeStore(native.root).read_journal()
    personal = _synthetic_bundle(tmp_path / "personal")
    clock = Clock()
    def provider(binding):
        value = _fresh(clock, binding)
        # Runtime uses wall time, so update the observation metadata coherently.
        current = {"value": datetime.now(timezone.utc).isoformat(), "precision": "source_exact"}
        value["snapshot_effective_at"] = value["retrieved_at"] = current
        for row in value["holdings"]:
            row["valuation"]["observed_at"] = current
            for field in ("avg_cost_values", "cost_basis_values"):
                row[field][0]["as_of"] = current
        return value
    before = copy.deepcopy(checkpoints.read_all())
    options = {}
    if automatic:
        from protocol.v1.tests.test_account_observations import _approved
        private = tmp_path / "observation-config"
        private.mkdir()
        reader, _, _ = _reader()
        store = _approved(private, reader)
        options.update(account_observation_root=store.root, account_observation_policy_file=store.policy_file,
                       account_observation_key_file=store.key_file)
    gateway = build_reference_gateway(runtime_root=root, manifest=root / "protocol/v1/fixtures/full-reference.instance.json",
        personal_data_root=personal, native_store_root=native.root, portfolio_checkpoint_root=checkpoints.root,
        read_only_stores=True, account_bindings=[_binding()], account_snapshot_providers={"toss_securities": provider}, **options)
    assert gateway.connector_handlers == {}
    assert gateway.get_account_state("portfolio-alpha", "account-alpha")["status"] == "ok"
    current = gateway.get_portfolio_state("portfolio-alpha")
    assert bool(current["data"].get("account_observation_overlays")) is automatic
    _validate(current["data"], "portfolio.schema.json")
    assert checkpoints.read_all() == before
