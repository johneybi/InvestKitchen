from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from protocol.v1.adapters.materialized_portfolio import get_materialized_portfolio_state
from protocol.v1.providers.account_readonly_adapters import AccountProviderError, normalize_toss_account_snapshot
from protocol.v1.runtime.portfolio_account_sync import (
    AccountBindingRegistry, AccountSyncError, accept_account_sync,
    build_account_sync_preview, validate_account_snapshot,
)
from protocol.v1.runtime.portfolio_update_service import build_portfolio_update_preview, new_approval
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import PortfolioCheckpointStore
from protocol.v1.tests.test_gateway_facade import _validate
from protocol.v1.tests.test_portfolio_account_sync import _binding, _tp
from protocol.v1.tests.test_portfolio_update_service import _narrow_request, _stores


NOW = datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc)


def _raw() -> dict:
    return {
        "observed_at": _tp("2026-09-17T01:00:00Z"),
        "retrieved_at": _tp("2026-09-17T01:00:01Z"),
        "source_ref": "provider-observation:toss-synthetic-values",
        "result": {"items": [
            {
                "symbol": "005930", "name": "Synthetic KR holding", "venue": "KRX",
                "currency": "KRW", "quantity": "10", "averagePurchasePrice": "1000",
                "lastPrice": "1100", "marketValue": {"purchaseAmount": "10000", "amount": "11000", "amountAfterCost": "10950"},
                "profitLoss": {"amount": "1000", "amountAfterCost": "950", "rate": "0.1", "rateAfterCost": "0.095"},
            },
            {
                "symbol": "000660", "name": "Synthetic loss holding", "venue": "KRX",
                "currency": "KRW", "quantity": "3", "averagePurchasePrice": "2000",
                "lastPrice": "1800", "marketValue": {"purchaseAmount": "6000", "amount": "5400", "amountAfterCost": "5394"},
                "profitLoss": {"amount": "-600", "amountAfterCost": "-606", "rate": "-0.1", "rateAfterCost": "-0.101"},
            },
            {
                "symbol": "AAPL", "name": "Synthetic US holding", "venue": "NASDAQ",
                "currency": "USD", "quantity": "0.25", "averagePurchasePrice": "80.12",
                "lastPrice": "80.12", "marketValue": {"purchaseAmount": "20.03", "amount": "20.03", "amountAfterCost": "20.01"},
                "profitLoss": {"amount": "0", "amountAfterCost": "-0.02", "rate": "0", "rateAfterCost": "-0.001"},
            },
        ]},
        "cash": [{"cash_id": "cash-alpha", "currency": "KRW", "cash_kind": "nominal_balance", "amount": 1000}],
        "completeness": {"holdings": "complete", "cash": "complete"},
    }


def _snapshot() -> dict:
    return normalize_toss_account_snapshot(_raw(), _binding())


def _advance(snapshot: dict) -> None:
    snapshot["snapshot_effective_at"] = _tp("2026-09-17T01:01:00Z")
    snapshot["retrieved_at"] = _tp("2026-09-17T01:01:01Z")
    for holding in snapshot["holdings"]:
        if "valuation" in holding:
            holding["valuation"]["observed_at"] = copy.deepcopy(snapshot["snapshot_effective_at"])


def _preview(stores, snapshot: dict) -> dict:
    write_store, checkpoint_store = stores
    return build_account_sync_preview(
        portfolio_id="portfolio-alpha", account_id="account-alpha",
        registry=AccountBindingRegistry([_binding()]),
        providers={"toss_securities": lambda _: copy.deepcopy(snapshot)},
        checkpoint_store=checkpoint_store, write_store=write_store, now=NOW,
    )


def _apply(stores, preview: dict) -> dict:
    write_store, checkpoint_store = stores
    approval = new_approval(preview["candidate"], approval_ref="approval:synthetic-values", approved_at=_tp("2026-09-17T01:02:00Z"))
    return accept_account_sync(
        preview, approval, checkpoint_store=checkpoint_store, write_store=write_store,
        approval_verifier=lambda *_: {"verified": True, "verification_ref": "verify:synthetic-values"},
    )


def test_toss_values_round_trip_preview_approval_checkpoint_and_public_read(tmp_path) -> None:
    stores = _stores(tmp_path)
    snapshot = _snapshot()
    _validate(snapshot, "account-snapshot.schema.json")
    before = copy.deepcopy(stores[1].read_all())
    preview = _preview(stores, snapshot)
    assert stores[1].read_all() == before
    assert preview["status"] == "review_required"
    _validate(preview["candidate"]["reconciled_portfolio"], "portfolio.schema.json")
    _validate(preview["candidate"], "portfolio-reconciliation.schema.json")
    assert _apply(stores, preview)["read_back"]["matches"] is True
    # Reopen the stores, so read-back cannot be satisfied by in-memory values.
    public = get_materialized_portfolio_state(
        PortfolioCheckpointStore(stores[1].root), NativeWriteStore(stores[0].root), "portfolio-alpha",
    )
    portfolio = public["data"]
    _validate(portfolio, "portfolio.schema.json")
    positions = {row["asset"]["symbol"]: row for row in portfolio["positions"]}
    for holding in snapshot["holdings"]:
        position = positions[holding["asset"]["symbol"]]
        assert position["quantity"] == holding["quantity"]
        for field in ("cost_basis_values", "avg_cost_values", "valuation"):
            assert position[field] == holding[field]
        assert position["valuation"]["observed_at"] == snapshot["snapshot_effective_at"]
        assert position["valuation"]["source_evidence"] == [snapshot["source_ref"]]
    assert positions["005930"]["avg_cost_values"][0]["amount"] == 1000
    assert positions["000660"]["valuation"]["profit_loss"] == -600
    assert positions["AAPL"]["cost_basis_values"][0] == {
        "amount": 20.03, "currency": "USD", "value_basis": "observed",
        "as_of": snapshot["snapshot_effective_at"], "source_evidence": [snapshot["source_ref"]],
    }
    assert positions["AAPL"]["valuation"]["profit_loss"] == 0
    assert positions["AAPL"]["valuation"]["profit_loss_rate"] == 0
    assert portfolio["transactions"] == []


def test_valuation_change_without_quantity_change_is_reviewed_and_persisted(tmp_path) -> None:
    stores = _stores(tmp_path)
    snapshot = _snapshot()
    _apply(stores, _preview(stores, snapshot))
    # A newer observation changes valuation while retaining quantities and prior costs.
    for holding in snapshot["holdings"]:
        holding.pop("cost_basis_values")
        holding.pop("avg_cost_values")
    _advance(snapshot)
    # Only the first holding's valuation is observed in this update.
    for holding in snapshot["holdings"][1:]:
        holding.pop("valuation")
    snapshot["holdings"][0]["valuation"]["last_price"] = 1200
    snapshot["holdings"][0]["valuation"]["market_value"] = 12000
    snapshot["holdings"][0]["valuation"]["profit_loss"] = 2000
    preview = _preview(stores, snapshot)
    changes = [row for row in preview["candidate"]["differences"] if row["domain"] == "position"]
    assert len(changes) == 1
    assert [row["field"] for row in changes[0]["field_changes"]] == ["valuation"]
    assert _apply(stores, preview)["read_back"]["matches"] is True
    portfolio = get_materialized_portfolio_state(stores[1], stores[0], "portfolio-alpha")["data"]
    position = next(row for row in portfolio["positions"] if row["asset"]["symbol"] == "005930")
    assert position["valuation"]["market_value"] == 12000
    with pytest.raises(AccountSyncError, match="account_snapshot_stale_checkpoint"):
        _preview(stores, snapshot)


@pytest.mark.parametrize(("path", "value"), [
    (("averagePurchasePrice",), True), (("averagePurchasePrice",), "NaN"),
    (("averagePurchasePrice",), "-1"), (("lastPrice",), "Infinity"),
    (("marketValue", "purchaseAmount"), "-1"), (("marketValue", "amount"), "-1"),
    (("marketValue",), None), (("profitLoss",), []),
    (("profitLoss", "amount"), None), (("profitLoss", "rate"), "bad"),
])
def test_malformed_toss_values_fail_closed(path, value) -> None:
    raw = _raw()
    row = raw["result"]["items"][0]
    for key in path[:-1]:
        row = row[key]
    row[path[-1]] = value
    with pytest.raises(AccountProviderError, match="toss_holding_value_invalid"):
        normalize_toss_account_snapshot(raw, _binding())


@pytest.mark.parametrize("mutation", ["currency", "nonfinite", "time", "basis", "unknown", "cost_currency"])
def test_normalized_values_are_validated_before_preview(tmp_path, mutation) -> None:
    stores = _stores(tmp_path)
    snapshot = _snapshot()
    holding = snapshot["holdings"][0]
    if mutation == "currency":
        holding["valuation"]["currency"] = "USD"
    elif mutation == "nonfinite":
        holding["valuation"]["profit_loss"] = float("inf")
    elif mutation == "time":
        holding["valuation"]["observed_at"] = _tp("2026-09-16T00:00:00Z")
    elif mutation == "basis":
        holding["valuation"]["value_basis"] = "derived"
    elif mutation == "unknown":
        holding["valuation"]["private_provider_field"] = "excluded"
    else:
        holding["cost_basis_values"][0]["currency"] = "USD"
    before = copy.deepcopy(stores[1].read_all())
    with pytest.raises(AccountSyncError, match="account_snapshot_holding_values_invalid"):
        _preview(stores, snapshot)
    assert stores[1].read_all() == before


def test_quantity_only_snapshot_cannot_reuse_values_for_changed_quantity(tmp_path) -> None:
    stores = _stores(tmp_path)
    _apply(stores, _preview(stores, _snapshot()))
    snapshot = _snapshot()
    for holding in snapshot["holdings"]:
        for field in ("avg_cost_values", "cost_basis_values", "valuation"):
            holding.pop(field)
    snapshot["holdings"][0]["quantity"] += 1
    _advance(snapshot)
    preview = _preview(stores, snapshot)
    changed = next(row for row in preview["candidate"]["reconciled_portfolio"]["positions"] if row["asset"]["symbol"] == "005930")
    assert all(field not in changed for field in ("avg_cost_values", "cost_basis_values", "valuation"))
    assert _apply(stores, preview)["read_back"]["matches"] is True


def test_manual_quantity_change_invalidates_stored_valuation(tmp_path) -> None:
    stores = _stores(tmp_path)
    _apply(stores, _preview(stores, _snapshot()))
    request = _narrow_request()
    request["snapshot_effective_at"] = _tp("2026-09-17T01:01:00Z")
    preview = build_portfolio_update_preview(request, checkpoint_store=stores[1], write_store=stores[0])
    position = next(row for row in preview["candidate"]["reconciled_portfolio"]["positions"] if row["asset"]["symbol"] == "005930")
    assert "valuation" not in position


def test_quantity_only_adapter_does_not_invent_cost_or_valuation() -> None:
    raw = _raw()
    for row in raw["result"]["items"]:
        for field in ("averagePurchasePrice", "lastPrice", "marketValue", "profitLoss"):
            row.pop(field)
    snapshot = normalize_toss_account_snapshot(raw, _binding())
    binding = AccountBindingRegistry([_binding()]).resolve("portfolio-alpha", "account-alpha")
    validated = validate_account_snapshot(snapshot, binding, now=NOW)
    assert all(not (set(row) & {"cost_basis_values", "avg_cost_values", "valuation"}) for row in validated["holdings"])


def test_zero_purchase_cost_and_price_are_preserved() -> None:
    raw = _raw()
    row = raw["result"]["items"][0]
    row["averagePurchasePrice"] = "0"
    row["lastPrice"] = "0"
    row["marketValue"] = {"purchaseAmount": "0", "amount": "0", "amountAfterCost": "0"}
    row["profitLoss"] = {"amount": "0", "amountAfterCost": "0", "rate": "0", "rateAfterCost": "0"}
    snapshot = normalize_toss_account_snapshot(raw, _binding())
    holding = snapshot["holdings"][0]
    assert holding["avg_cost_values"][0]["amount"] == 0
    assert holding["cost_basis_values"][0]["amount"] == 0
    assert holding["valuation"]["last_price"] == 0
    assert holding["valuation"]["market_value"] == 0


@pytest.mark.parametrize("tamper", [False, True])
def test_value_updates_require_trusted_approval_and_untampered_candidate(tmp_path, tamper) -> None:
    stores = _stores(tmp_path)
    preview = _preview(stores, _snapshot())
    approval = new_approval(preview["candidate"], approval_ref="approval:synthetic-values", approved_at=_tp("2026-09-17T01:02:00Z"))
    before = copy.deepcopy(stores[1].read_all())
    verifier = None
    if tamper:
        preview["candidate"]["reconciled_portfolio"]["positions"][0]["valuation"]["profit_loss"] = 99999
        verifier = lambda *_: {"verified": True, "verification_ref": "verify:must-not-authorize-tamper"}
    with pytest.raises(AccountSyncError):
        accept_account_sync(preview, approval, checkpoint_store=stores[1], write_store=stores[0], approval_verifier=verifier)
    assert stores[1].read_all() == before
