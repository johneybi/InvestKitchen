from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from protocol.v1.providers.account_readonly_adapters import (
    ReadOnlyAccountAdapter,
    normalize_namuh_account_snapshot,
    normalize_toss_account_snapshot,
)
from protocol.v1.runtime.portfolio_account_sync import (
    AccountBindingRegistry,
    AccountSyncError,
    accept_account_sync,
    account_snapshot_to_observed_portfolio,
    build_account_sync_preview,
    validate_account_snapshot,
)
from protocol.v1.runtime.portfolio_update_service import new_approval
from protocol.v1.tests.test_portfolio_update_service import _stores


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


def _binding(provider_id: str = "toss_securities") -> dict[str, str]:
    return {
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "provider_id": provider_id,
        "provider_account_ref": "provider-account-ref:synthetic",
    }


def _asset(symbol: str) -> dict:
    return {
        "asset_type": "stock",
        "symbol": symbol,
        "venue": "KRX",
        "currency": "KRW",
        "display_name": f"Synthetic {symbol}",
        "provider_refs": {},
    }


def _snapshot(*, quantity: int = 12, cash: int = 800, completeness: str = "complete", add_fund: bool = False) -> dict:
    holdings = [
        {"asset": _asset("005930"), "quantity": quantity},
        {"asset": _asset("000660"), "quantity": 3},
    ]
    if add_fund:
        holdings.append({
            "asset": {
                "asset_type": "fund",
                "symbol": None,
                "venue": None,
                "currency": "KRW",
                "display_name": "Synthetic Retirement Fund",
                "provider_refs": {"fund_code": "FUND-SYNTHETIC-1"},
            },
            "quantity": 2,
        })
    return {
        "schema_version": "1.0",
        **_binding(),
        "snapshot_effective_at": _tp("2026-09-17T01:00:00Z"),
        "retrieved_at": _tp("2026-09-17T01:01:00Z"),
        "source_ref": "provider-observation:synthetic-1",
        "holdings": holdings,
        "cash": [{"currency": "KRW", "cash_kind": "nominal_balance", "amount": cash}],
        "completeness": {"holdings": completeness, "cash": completeness},
    }


def test_binding_registry_is_scoped_by_portfolio_and_account() -> None:
    registry = AccountBindingRegistry([
        _binding(),
        {**_binding(), "portfolio_id": "portfolio-beta", "provider_account_ref": "provider-account-ref:beta"},
    ])
    assert registry.resolve("portfolio-alpha", "account-alpha").provider_account_ref.endswith("synthetic")
    assert registry.resolve("portfolio-beta", "account-alpha").provider_account_ref.endswith("beta")
    with pytest.raises(AccountSyncError, match="account_binding_not_found"):
        registry.resolve("portfolio-gamma", "account-alpha")


def test_binding_registry_allows_same_provider_account_selector_for_distinct_credential_profiles() -> None:
    registry = AccountBindingRegistry([
        {
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "provider_id": "toss_securities",
            "provider_account_ref": "unique_brokerage",
            "provider_credential_ref": "portfolio-a",
        },
        {
            "portfolio_id": "portfolio-beta",
            "account_id": "account-beta",
            "provider_id": "toss_securities",
            "provider_account_ref": "unique_brokerage",
            "provider_credential_ref": "portfolio-b",
        },
    ])
    assert registry.resolve("portfolio-alpha", "account-alpha").provider_credential_ref == "portfolio-a"
    assert registry.resolve("portfolio-beta", "account-beta").provider_credential_ref == "portfolio-b"
    assert "provider_credential_ref" not in registry.resolve("portfolio-beta", "account-beta").private_value()

    with pytest.raises(AccountSyncError, match="account_binding_provider_scope_duplicate"):
        AccountBindingRegistry([
            {
                "portfolio_id": "portfolio-alpha",
                "account_id": "account-alpha",
                "provider_id": "toss_securities",
                "provider_account_ref": "unique_brokerage",
                "provider_credential_ref": "same",
            },
            {
                "portfolio_id": "portfolio-beta",
                "account_id": "account-beta",
                "provider_id": "toss_securities",
                "provider_account_ref": "unique_brokerage",
                "provider_credential_ref": "same",
            },
        ])


def test_snapshot_binding_duplicate_identity_stale_and_partial_fail_closed() -> None:
    binding = AccountBindingRegistry([_binding()]).resolve("portfolio-alpha", "account-alpha")
    now = datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc)
    assert validate_account_snapshot(_snapshot(), binding, now=now)["completeness"]["holdings"] == "complete"

    mismatch = _snapshot()
    mismatch["account_id"] = "other-account"
    with pytest.raises(AccountSyncError, match="account_snapshot_binding_mismatch"):
        validate_account_snapshot(mismatch, binding, now=now)

    duplicate = _snapshot()
    duplicate["holdings"].append(copy.deepcopy(duplicate["holdings"][0]))
    with pytest.raises(AccountSyncError, match="account_snapshot_asset_identity_duplicate"):
        validate_account_snapshot(duplicate, binding, now=now)

    with pytest.raises(AccountSyncError, match="account_snapshot_stale"):
        validate_account_snapshot(_snapshot(), binding, now=datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc))


def test_full_account_sync_adds_non_market_position_without_cost_basis_and_applies_with_readback(tmp_path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    registry = AccountBindingRegistry([_binding()])
    raw = _snapshot(add_fund=True)
    preview = build_account_sync_preview(
        portfolio_id="portfolio-alpha",
        account_id="account-alpha",
        registry=registry,
        providers={"toss_securities": lambda _: copy.deepcopy(raw)},
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        now=datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc),
    )
    assert preview["status"] == "review_required"
    candidate = preview["candidate"]
    added = [
        row for row in candidate["reconciled_portfolio"]["positions"]
        if row["asset"].get("provider_refs", {}).get("fund_code") == "FUND-SYNTHETIC-1"
    ]
    assert len(added) == 1
    assert added[0]["quantity_basis"] == "direct_observation"
    assert "cost_basis_values" not in added[0]
    assert "avg_cost_values" not in added[0]

    approval = new_approval(
        candidate,
        approval_ref="approval:synthetic",
        approved_at=_tp("2026-09-17T01:02:00Z"),
    )
    result = accept_account_sync(
        preview,
        approval,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        approval_verifier=lambda _candidate, _approval: {"verified": True, "verification_ref": "verify:synthetic"},
    )
    assert result["status"] == "applied"
    assert result["read_back"]["matches"] is True


def test_partial_snapshot_never_builds_applyable_preview(tmp_path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    with pytest.raises(AccountSyncError, match="account_snapshot_incomplete"):
        build_account_sync_preview(
            portfolio_id="portfolio-alpha",
            account_id="account-alpha",
            registry=AccountBindingRegistry([_binding()]),
            providers={"toss_securities": lambda _: _snapshot(completeness="partial")},
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            now=datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc),
        )


def test_complete_holdings_with_unavailable_cash_updates_holdings_and_preserves_cash(tmp_path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    raw = _snapshot(quantity=12, cash=1000)
    raw["cash"] = []
    raw["completeness"] = {"holdings": "complete", "cash": "unavailable"}
    preview = build_account_sync_preview(
        portfolio_id="portfolio-alpha",
        account_id="account-alpha",
        registry=AccountBindingRegistry([_binding()]),
        providers={"toss_securities": lambda _: copy.deepcopy(raw)},
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        now=datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc),
    )
    assert preview["status"] == "review_required"
    assert preview["review"]["summary"]["position_changes"] == 1
    assert preview["review"]["summary"]["cash_changes"] == 0
    reconciled = preview["candidate"]["reconciled_portfolio"]
    quantities = {row["asset"]["symbol"]: row["quantity"] for row in reconciled["positions"]}
    assert quantities["005930"] == 12
    assert reconciled["cash"][0]["value"]["amount"] == 1000
    assert any(gap["gap_code"] == "reconciliation_cash_incomplete" for gap in preview["review"]["gaps"])

    candidate = preview["candidate"]
    approval = new_approval(
        candidate,
        approval_ref="approval:holdings-only",
        approved_at=_tp("2026-09-17T01:02:00Z"),
    )
    result = accept_account_sync(
        preview,
        approval,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        approval_verifier=lambda *_: {"verified": True, "verification_ref": "verify:holdings-only"},
    )
    assert result["status"] == "applied"
    assert result["read_back"]["matches"] is True


def test_provider_stock_matches_legacy_unknown_market_position_in_place(tmp_path) -> None:
    _write_store, checkpoint_store = _stores(tmp_path)
    current = copy.deepcopy(checkpoint_store.latest("portfolio-alpha")["base_portfolio"])
    legacy = next(row for row in current["positions"] if row["asset"]["symbol"] == "005930")
    legacy["asset"]["asset_type"] = "unknown"
    binding = AccountBindingRegistry([_binding()]).resolve("portfolio-alpha", "account-alpha")
    snapshot = validate_account_snapshot(
        _snapshot(quantity=12, cash=1000),
        binding,
        now=datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc),
    )
    observed = account_snapshot_to_observed_portfolio(snapshot, current)
    matched = next(row for row in observed["positions"] if row["position_id"] == legacy["position_id"])
    assert matched["quantity"] == 12
    assert matched["asset"]["asset_type"] == "unknown"


def test_unchanged_snapshot_returns_no_change_without_candidate(tmp_path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    result = build_account_sync_preview(
        portfolio_id="portfolio-alpha",
        account_id="account-alpha",
        registry=AccountBindingRegistry([_binding()]),
        providers={"toss_securities": lambda _: _snapshot(quantity=10, cash=1000)},
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        now=datetime(2026, 9, 17, 1, 2, tzinfo=timezone.utc),
    )
    assert result["status"] == "no_change"
    assert "candidate" not in result


def test_toss_and_namuh_readonly_normalizers_produce_same_contract_without_orders() -> None:
    toss_binding = _binding()
    toss = normalize_toss_account_snapshot({
        "observed_at": _tp("2026-09-17T01:00:00Z"),
        "retrieved_at": _tp("2026-09-17T01:00:01Z"),
        "source_ref": "provider-observation:toss-synthetic",
        "result": {
            "items": [{"symbol": "005930", "quantity": "4", "currency": "KRW", "venue": "KRX", "name": "Synthetic"}],
        },
    }, toss_binding)
    assert toss["holdings"][0]["quantity"] == 4
    assert toss["cash"] == []
    assert toss["completeness"]["cash"] == "unavailable"

    nh_binding = _binding("nhplug")
    namuh = normalize_namuh_account_snapshot({
        "observed_at": _tp("2026-09-17T01:00:00Z"),
        "retrieved_at": _tp("2026-09-17T01:00:01Z"),
        "source_ref": "provider-observation:nh-synthetic",
        "value": {
            "cash": "54321",
            "currency": "KRW",
            "positions": [{"pdno": "005930", "hldg_qty": "5", "currency": "KRW", "venue": "KRX", "prdt_name": "Synthetic"}],
        },
    }, nh_binding)
    assert namuh["holdings"][0]["quantity"] == 5
    assert namuh["cash"][0]["amount"] == 54321

    called = []
    adapter = ReadOnlyAccountAdapter(
        "toss_securities",
        lambda binding: called.append(binding) or {
            "observed_at": _tp("2026-09-17T01:00:00Z"),
            "retrieved_at": _tp("2026-09-17T01:00:01Z"),
            "source_ref": "provider-observation:adapter-synthetic",
            "result": {"items": []},
        },
        normalize_toss_account_snapshot,
    )
    snapshot = adapter.read_snapshot(toss_binding)
    assert len(called) == 1
    assert set(snapshot) >= {"holdings", "cash", "completeness"}
