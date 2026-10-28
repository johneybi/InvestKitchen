"""Synthetic manual-screen observations; never import a private account fixture."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import jsonschema
import pytest

from protocol.v1.adapters.common import public_reference
from protocol.v1.runtime import portfolio_reconciliation, portfolio_update_service
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import (
    PortfolioCheckpointStore, build_checkpoint, persist_checkpoint,
)
from protocol.v1.runtime.portfolio_update_service import PortfolioUpdateError, build_portfolio_update_preview
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.tests.test_mcp_write_surface import PROTOCOL, ROOT, _call, _grant, _principal, _server
from protocol.v1.tests.test_portfolio_update_service import _narrow_request, _portfolio, _stores, _tp
from protocol.v1.transport.stdio_rpc import load_tool_catalog
from protocol.v1.transport.mcp_stdio import TradeMindMCPServer


def _existing_server(tmp_path):
    return TradeMindMCPServer(build_reference_gateway(
        runtime_root=ROOT, manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        native_store_root=tmp_path / "native-write", portfolio_checkpoint_root=tmp_path / "checkpoints",
        approval_store_root=tmp_path / "approvals", write_principal=_principal(), write_grant=_grant(),
    ))


def _money(amount):
    return {"amount": amount, "currency": "KRW", "value_basis": "observed"}


def _observation():
    # Rounded screen average is intentionally independent of principal / quantity.
    return {
        "account_id": "account-alpha", "symbol": "005930",
        "cost_basis_values": [_money(1003)], "avg_cost_values": [_money(100.25)],
        "valuation": {"currency": "KRW", "value_basis": "observed", "last_price": 90,
                      "market_value": 900, "profit_loss": -103, "profit_loss_rate": -0.1027},
    }


def _manual_request():
    request = _narrow_request()
    request.pop("created_at")
    request["changes"] = {"position_observations": [_observation()]}
    return request


def _seed(tmp_path, base):
    writes = NativeWriteStore(tmp_path / "native-write")
    checkpoints = PortfolioCheckpointStore(tmp_path / "checkpoints")
    checkpoint = build_checkpoint(
        base, writes, snapshot_effective_at=_tp("2026-09-17T00:00:00Z"),
        through_commit_id=None, source_kind="manual_snapshot", source_ref="fixture:manual",
        cash_basis_refs=[], created_at=_tp("2026-09-17T00:01:00Z"),
    )
    persist_checkpoint(checkpoints, writes, checkpoint)
    return writes, checkpoints


def _preview(request, stores):
    writes, checkpoints = stores
    return build_portfolio_update_preview(request, write_store=writes, checkpoint_store=checkpoints)


def test_manual_observations_without_created_at_apply_through_mcp_and_persist(tmp_path, monkeypatch):
    server = _server(tmp_path)  # No broker binding or account.read permission.
    counter = itertools.count()
    def ticking_clock():
        return _tp(f"2026-10-28T00:00:{next(counter):02d}Z")
    monkeypatch.setattr(portfolio_update_service, "timepoint", ticking_clock)
    monkeypatch.setattr(portfolio_reconciliation, "timepoint", ticking_clock)
    update = _manual_request()
    update["changes"]["cash_amounts"] = [{"cash_id": "cash-alpha", "amount": 820}]
    preview = _call(server, "preview_portfolio_update", {"update": update}, request_id="manual-preview")
    assert preview["status"] == "review_required"
    assert preview["summary"]["position_changes"] == 1
    fields = {row["field"] for diff in preview["differences"] for row in diff["field_changes"]}
    assert {"cost_basis_values", "avg_cost_values", "valuation"} <= fields
    applied = _call(server, "apply_portfolio_update", {
        "preview_id": preview["preview_id"], "confirmation": preview["confirmation"],
    }, request_id="manual-apply")
    assert applied["status"] == "applied"
    assert applied["read_back"]["matches"] is True
    # A fresh server reads persisted canonical state, not an in-memory candidate.
    read = _call(_existing_server(tmp_path), "get_portfolio_state", {"portfolio_id": "portfolio-alpha"}, request_id="manual-read")
    portfolio = read["data"]
    row = next(row for row in portfolio["positions"] if row["asset"]["symbol"] == "005930")
    assert row["quantity"] == 10
    assert row["cost_basis_values"][0]["amount"] == 1003
    assert row["avg_cost_values"][0]["amount"] == 100.25
    assert row["avg_cost_values"][0]["value_basis"] == "observed"
    assert row["valuation"]["profit_loss"] == -103
    assert portfolio["cash"][0]["value"]["amount"] == 820
    assert next(row for row in portfolio["positions"] if row["asset"]["symbol"] == "000660")["quantity"] == 3


def test_public_ids_roundtrip_without_exposing_internal_ids(tmp_path):
    base = _portfolio()
    base["positions"][0]["position_id"] = "legacy-position:synthetic-private-name"
    base["positions"][0]["asset"].update(asset_type="fund", symbol=None, venue=None,
                                            provider_refs={"fund_code": "SYNTHETIC"})
    base["cash"][0].update(cash_id="legacy-cash:synthetic-private-name", cash_kind="provider_specific",
                           provider_label="Synthetic cash-like valuation")
    _seed(tmp_path, base)
    server = _existing_server(tmp_path)
    read = _call(server, "get_portfolio_state", {"portfolio_id": "portfolio-alpha"}, request_id="alias-read")
    public = read["data"]
    position_id = next(row["position_id"] for row in public["positions"] if row["asset"]["symbol"] is None)
    cash_id = public["cash"][0]["cash_id"]
    assert position_id.startswith("position-ref:") and cash_id.startswith("cash-ref:")
    update = _manual_request()
    change = update["changes"]["position_observations"][0]
    change.pop("symbol")
    change["position_id"] = position_id
    update["changes"]["cash_amounts"] = [{"cash_id": cash_id, "amount": 900}]
    preview = _call(server, "preview_portfolio_update", {"update": update}, request_id="alias-preview")
    assert preview["status"] == "review_required"
    assert "synthetic-private-name" not in json.dumps(preview)
    applied = _call(server, "apply_portfolio_update", {
        "preview_id": preview["preview_id"], "confirmation": preview["confirmation"],
    }, request_id="alias-apply")
    assert applied["status"] == "applied"
    read = _call(server, "get_portfolio_state", {"portfolio_id": "portfolio-alpha"}, request_id="alias-readback")
    assert read["data"]["cash"][0]["cash_kind"] == "provider_specific"
    assert read["data"]["cash"][0]["provider_label"] == "Synthetic cash-like valuation"
    assert read["data"]["cash"][0]["value"]["as_of"] == update["snapshot_effective_at"]


@pytest.mark.parametrize("field,selector", [("position_id", "position-ref:"), ("cash_id", "cash-ref:")])
def test_raw_public_alias_collision_rejects_before_mutation(tmp_path, field, selector):
    base = _portfolio()
    key = "positions" if field == "position_id" else "cash"
    raw = "legacy-position:synthetic" if field == "position_id" else "legacy-cash:synthetic"
    base[key][0][field] = raw
    collision = copy.deepcopy(base[key][0])
    collision[field] = public_reference(raw)
    assert collision[field].startswith(selector)
    if key == "positions":
        collision["asset"]["symbol"] = "999999"
    base[key].append(collision)
    stores = _seed(tmp_path, base)
    update = _manual_request()
    update["changes"] = ({"position_observations": [{"account_id": "account-alpha",
        "position_id": collision[field], "avg_cost_values": [_money(1)]}]} if key == "positions" else
        {"cash_amounts": [{"cash_id": collision[field], "amount": 1}]})
    before = stores[1].read_all()
    with pytest.raises(PortfolioUpdateError, match="match_(not_unique|ambiguous)"):
        _preview(update, stores)
    assert stores[1].read_all() == before


@pytest.mark.parametrize("mode", ["cross-account", "unknown", "duplicate", "mixed-duplicate"])
def test_manual_selectors_cannot_escape_scope_or_update_twice(tmp_path, mode):
    stores = _stores(tmp_path)
    update = _manual_request()
    change = update["changes"]["position_observations"][0]
    if mode == "cross-account":
        change["account_id"] = "account-other"
    elif mode == "unknown":
        change["symbol"] = "999999"
    elif mode == "duplicate":
        alias = copy.deepcopy(change)
        alias.pop("symbol")
        alias["position_id"] = "position:005930"
        update["changes"]["position_observations"].append(alias)
    else:
        update["changes"]["position_quantities"] = [{"account_id": "account-alpha", "symbol": "005930", "quantity": 12}]
    before = stores[1].read_all()
    with pytest.raises(PortfolioUpdateError):
        _preview(update, stores)
    assert stores[1].read_all() == before


@pytest.mark.parametrize("bad", [
    {"avg_cost_values": [_money(True)]}, {"cost_basis_values": [_money(-1)]},
    {"avg_cost_values": [_money(float("inf"))]},
    {"avg_cost_values": [{**_money(1), "currency": "USD"}]},
    {"avg_cost_values": [{**_money(1), "value_basis": "derived"}]},
    {"avg_cost_values": [{**_money(1), "as_of": _tp("2026-09-16T00:00:00Z")}]},
    {"valuation": {"currency": "KRW", "value_basis": "observed", "last_price": -1}},
    {"valuation": {"currency": "KRW", "value_basis": "observed", "profit_loss": float("nan")}},
])
def test_invalid_manual_values_fail_closed(tmp_path, bad):
    stores = _stores(tmp_path)
    update = _manual_request()
    update["changes"]["position_observations"][0].update(bad)
    before = stores[1].read_all()
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_position_values_invalid"):
        _preview(update, stores)
    assert stores[1].read_all() == before


def test_public_quantity_selector_and_duplicate_cash_aliases(tmp_path):
    base = _portfolio()
    base["positions"][0]["position_id"] = "legacy-position:synthetic"
    raw_cash = "legacy-cash:synthetic"
    base["cash"][0]["cash_id"] = raw_cash
    stores = _seed(tmp_path, base)
    update = _manual_request()
    update["changes"] = {"position_quantities": [{"account_id": "account-alpha",
        "position_id": public_reference(base["positions"][0]["position_id"]), "quantity": 11}]}
    assert _preview(update, stores)["candidate"]["reconciled_portfolio"]["positions"][0]["quantity"] == 11
    update["changes"]["cash_amounts"] = [
        {"cash_id": public_reference(raw_cash), "amount": 900}, {"cash_id": raw_cash, "amount": 800}]
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_cash_change_duplicate"):
        _preview(update, stores)
    update["changes"]["cash_amounts"].reverse()
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_cash_change_duplicate"):
        _preview(update, stores)


def test_position_id_from_another_existing_account_is_rejected(tmp_path):
    base = _portfolio()
    other_account = copy.deepcopy(base["accounts"][0])
    other_account["account_id"] = "account-beta"
    base["accounts"].append(other_account)
    stores = _seed(tmp_path, base)
    update = _manual_request()
    update["changes"] = {"position_observations": [{"account_id": "account-beta",
        "position_id": "position:005930", "avg_cost_values": [_money(123)]}]}
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_position_match_not_unique"):
        _preview(update, stores)


@pytest.mark.parametrize("quantity_changed", [False, True])
def test_partial_values_keep_old_timestamps_or_invalidate_when_quantity_changes(tmp_path, quantity_changed):
    base = _portfolio()
    row = base["positions"][0]
    old_time = _tp("2026-09-17T00:00:00Z")
    row.update(cost_basis_values=[{**_money(1000), "as_of": old_time}],
               avg_cost_values=[{**_money(100), "as_of": old_time}],
               valuation={"currency": "KRW", "value_basis": "observed", "observed_at": old_time,
                          "last_price": 100, "market_value": 1000})
    stores = _seed(tmp_path, base)
    update = _manual_request()
    change = {"account_id": "account-alpha", "symbol": "005930",
              "valuation": {"currency": "KRW", "value_basis": "observed", "market_value": 900}}
    if quantity_changed:
        change["quantity"] = 9
    update["changes"] = {"position_observations": [change]}
    result = _preview(update, stores)["candidate"]["reconciled_portfolio"]["positions"][0]
    assert "last_price" not in result["valuation"]
    assert result["valuation"]["observed_at"] == update["snapshot_effective_at"]
    if quantity_changed:
        assert "cost_basis_values" not in result and "avg_cost_values" not in result
    else:
        assert result["avg_cost_values"][0]["as_of"] == old_time
        assert result["cost_basis_values"][0]["as_of"] == old_time


def test_manual_input_schema_accepts_observed_values_and_rejects_wrong_basis_or_selector():
    tool = next(row for row in load_tool_catalog()["tools"] if row["name"] == "preview_portfolio_update")
    validator = jsonschema.Draft202012Validator(tool["input_schema"], format_checker=jsonschema.FormatChecker())
    update = _manual_request()
    validator.validate({"update": update})
    change = update["changes"]["position_observations"][0]
    change["avg_cost_values"][0]["value_basis"] = "derived"
    assert not validator.is_valid({"update": update})
    change["avg_cost_values"][0]["value_basis"] = "observed"
    change["position_id"] = "position:005930"
    assert not validator.is_valid({"update": update})
