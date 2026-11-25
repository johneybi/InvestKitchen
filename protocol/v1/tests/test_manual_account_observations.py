"""Synthetic manual-screen observations; never import a private account fixture."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import jsonschema
import pytest

from protocol.v1.adapters.common import public_reference
from protocol.v1.runtime import portfolio_reconciliation, portfolio_update_service, reference_composition
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError, PortfolioCheckpointStore, build_checkpoint, persist_checkpoint, validate_checkpoint_journal_rows,
)
from protocol.v1.runtime.portfolio_update_service import (
    PortfolioUpdateError, accept_portfolio_update, build_portfolio_update_preview, new_approval,
)
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.tests.test_mcp_write_surface import PROTOCOL, ROOT, _call, _grant, _principal, _server
from protocol.v1.tests.test_portfolio_checkpoint import _validate
from protocol.v1.tests.test_portfolio_update_service import _narrow_request, _portfolio, _position, _stores, _tp
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


def test_reported_quantity_only_previews_removals_and_preserves_other_observations(tmp_path):
    base = _portfolio()
    original_time = _tp("2026-09-17T00:00:00Z")
    for position in base["positions"]:
        position["observed_at"] = original_time
        position["cost_basis_values"] = [_money(1000)]
        position["avg_cost_values"] = [_money(100)]
        position["valuation"] = {"currency": "KRW", "value_basis": "observed", "market_value": 900}
    base["positions"][0]["asset"]["display_name"] = "A"
    _seed(tmp_path, base)
    server = _existing_server(tmp_path)
    scope = {"portfolio_id": "portfolio-alpha"}
    current = _call(server, "get_portfolio_state", scope, request_id="quantity-current")["data"]
    a = next(row for row in current["positions"] if row["asset"]["display_name"] == "A")
    update = _manual_request()
    update["source_ref"] = "user-observation:chat:quantity-fixture"
    update["changes"] = {"position_observations": [
        {"account_id": a["account_id"], "position_id": a["position_id"], "quantity": 12},
    ]}
    preview = _call(server, "preview_portfolio_update", {"update": update}, request_id="quantity-preview")
    assert preview["snapshot_effective_at"] == update["snapshot_effective_at"]
    difference, = preview["differences"]
    fields = {change["field"]: change for change in difference["field_changes"]}
    assert fields["quantity"] == {"field": "quantity", "current": 10, "observed": 12}
    assert all(fields[field]["observed"] is None for field in ("cost_basis_values", "avg_cost_values", "valuation"))

    approval = {key: preview[key] for key in ("preview_id", "confirmation")}
    result = _call(server, "apply_portfolio_update", approval, request_id="quantity-approved")
    assert result["status"] == "applied" and result["read_back"]["matches"]
    stored = _call(_existing_server(tmp_path), "get_portfolio_state", scope, request_id="quantity-read-back")["data"]
    by_id = {row["position_id"]: row for row in stored["positions"]}
    recorded = by_id[a["position_id"]]
    assert recorded["quantity"] == 12 and recorded["observed_at"] == preview["snapshot_effective_at"]
    assert not {"cost_basis_values", "avg_cost_values", "valuation"} & recorded.keys()
    assert all(by_id[row["position_id"]] == row for row in current["positions"] if row != a)
    assert stored["cash"] == current["cash"] and stored["transactions"] == current["transactions"]


def _comparison_portfolio():
    base = _portfolio()
    other_account = {**base["accounts"][0], "account_id": "account-beta", "display_name": "Synthetic Other"}
    base["accounts"].append(other_account)
    base["positions"].extend([
        _position("MISSING", 7),
        {**_position("005930", 99), "account_id": "account-beta", "position_id": "position:other-005930"},
    ])
    base["cash"].append({**base["cash"][0], "account_id": "account-beta", "cash_id": "cash-beta"})
    for position in base["positions"]:
        position["observed_at"] = _tp("2026-09-17T00:00:00Z")
    return base


def test_account_comparison_merges_extracted_rows_and_reports_conflicts_without_double_apply(tmp_path):
    _seed(tmp_path, _comparison_portfolio())
    server = _existing_server(tmp_path)
    scope = {"portfolio_id": "portfolio-alpha"}
    current = _call(server, "get_portfolio_state", scope, request_id="comparison-current")["data"]
    update = _manual_request()
    update.update(reconcile_account_id="account-alpha", source_ref="user-observation:screens:synthetic")
    # Two extracted fragments. The second screen ends before MISSING becomes visible.
    first, second = [
        {"account_id": "account-alpha", "symbol": "005930", "quantity": 12,
         "valuation": {"value_basis": "observed", "currency": "KRW", "last_price": 90}},
        {"account_id": "account-alpha", "symbol": "000660", "quantity": 4,
         "valuation": {"currency": "KRW", "value_basis": "observed", "market_value": 500}},
    ], [
        {"account_id": "account-alpha", "position_id": "position:005930", "quantity": 12,
         "valuation": {"value_basis": "observed", "currency": "KRW", "market_value": 1080}},
        {"account_id": "account-alpha", "symbol": "000660", "quantity": 14},
    ]
    update["changes"] = {
        "position_observations": first + second,
        "position_additions": [_addition(), _addition()],
        "cash_amounts": [{"cash_id": "cash-alpha", "amount": 820}] * 2,
    }
    preview = _call(server, "preview_portfolio_update", {"update": update}, request_id="comparison-preview")
    assert preview["summary"]["position_changes"] == 2
    assert preview["summary"]["cash_changes"] == 1
    assert len(preview["differences"]) == 3
    unconfirmed = preview["reconciliation"]["unconfirmed"]
    assert unconfirmed == [
        {"position_id": "position:000660", "reason": "conflicting_values", "fields": ["quantity"],
         "rows": [{"collection": "position_observations", "index": 2},
                  {"collection": "position_observations", "index": 4}]},
        {"position_id": "position:MISSING", "reason": "not_observed"},
    ]
    assert not any(diff["change_type"] == "removed" for diff in preview["differences"])
    approval = {key: preview[key] for key in ("preview_id", "confirmation")}
    result = _call(server, "apply_portfolio_update", approval, request_id="comparison-apply")
    assert result["status"] == "applied" and result["read_back"]["matches"]
    fresh = _existing_server(tmp_path)
    stored = _call(fresh, "get_portfolio_state", scope, request_id="comparison-read-back")["data"]
    by_id = {row["position_id"]: row for row in stored["positions"]}
    updated = by_id["position:005930"]
    assert updated["quantity"] == 12
    assert updated["valuation"]["market_value"] == 1080 and updated["valuation"]["last_price"] == 90
    assert sum(row["asset"]["symbol"] == "0123X0" for row in stored["positions"]) == 1
    assert all(by_id[row["position_id"]] == row for row in current["positions"] if row["position_id"] != "position:005930")
    assert {row["cash_id"]: row["value"]["amount"] for row in stored["cash"]} == {"cash-alpha": 820, "cash-beta": 1000}
    assert stored["transactions"] == current["transactions"]

    replay = _call(fresh, "preview_portfolio_update", {"update": update}, request_id="comparison-resubmit")
    assert replay["status"] == "no_changes" and replay["differences"] == []
    assert replay["reconciliation"]["unconfirmed"] == unconfirmed and "preview_id" not in replay
    stores = NativeWriteStore(tmp_path / "native-write"), PortfolioCheckpointStore(tmp_path / "checkpoints")
    stale = copy.deepcopy(update)
    for index in (0, 2):
        stale["changes"]["position_observations"][index]["quantity"] = 13
    with pytest.raises(PortfolioUpdateError, match="checkpoint_effective_at_not_increasing"):
        _preview(stale, stores)
    assert len(stores[1].read_all()) == 2

    # Correct just the disputed row after reconnecting, using the actual time
    # on the original statement. A missing holding is not a disputed row.
    corrected = {**update, "changes": {"position_observations": [
        {"account_id": "account-alpha", "symbol": "000660", "quantity": 14},
    ]}}
    for other in (
        {**corrected, "source_ref": "user-observation:other"},
        {**corrected, "changes": {"position_observations": [
            {"account_id": "account-alpha", "symbol": "MISSING", "quantity": 9}]}},
        {**corrected, "reconcile_account_id": "account-beta", "changes": {"position_observations": [
            {"account_id": "account-beta", "symbol": "005930", "quantity": 14}]}},
    ):
        with pytest.raises(PortfolioUpdateError, match="checkpoint_effective_at_not_increasing"):
            _preview(other, stores)
    extra = copy.deepcopy(corrected)
    extra["changes"]["position_observations"][0]["valuation"] = {
        "last_price": 90, "currency": "KRW", "value_basis": "observed"}
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_held_row_fields_mismatch"):
        _preview(extra, stores)
    competing = _preview(corrected, stores)["candidate"]["checkpoint_draft"]
    later = _manual_request()
    later["snapshot_effective_at"] = _tp("2026-09-17T02:00:00Z")
    later["changes"] = {"position_observations": [
        {"account_id": "account-alpha", "symbol": "005930", "quantity": 13},
    ]}
    stale_later = _preview(later, stores)["candidate"]
    correction = _call(fresh, "preview_portfolio_update", {"update": corrected}, request_id="resolve-preview")
    assert correction["reconciliation"]["resolved"] == [{"position_id": "position:000660", "fields": ["quantity"]}]
    assert len(stores[1].read_all()) == 2
    applied = _call(fresh, "apply_portfolio_update", {key: correction[key] for key in ("preview_id", "confirmation")},
                    request_id="resolve-approved")
    assert applied["status"] == "applied" and applied["read_back"]["matches"]
    reader = _existing_server(tmp_path)
    after = _call(reader, "get_portfolio_state", scope, request_id="resolve-read")["data"]
    for row in after["positions"]:
        if row["position_id"] == "position:000660":
            assert row["quantity"] == 14 and row["observed_at"] == update["snapshot_effective_at"]
            assert update["source_ref"] in row["source_evidence"]
        else:
            assert row == by_id[row["position_id"]]
    assert after["cash"] == stored["cash"] and after["transactions"] == stored["transactions"]
    assert _call(reader, "preview_portfolio_update", {"update": corrected}, request_id="resolve-replay")["status"] == "no_changes"
    history = stores[1].read_all()
    assert len(history) == validate_checkpoint_journal_rows(history, []) == 3
    for checkpoint in history[1:]:
        _validate(checkpoint, "portfolio-checkpoint.schema.json")
    assert history[1]["snapshot_effective_at"] == history[2]["snapshot_effective_at"]
    assert history[2]["account_batch"] == {"parent_checkpoint_id": history[1]["checkpoint_id"],
                                           "account_id": "account-alpha", "held_rows": {}}
    assert history[1]["acceptance_verification_ref"] != history[2]["acceptance_verification_ref"]
    assert history[1]["base_portfolio"]["completeness"] == history[2]["base_portfolio"]["completeness"]
    assert history[1]["base_portfolio"]["migration_gaps"] == history[2]["base_portfolio"]["migration_gaps"]
    with pytest.raises(PortfolioUpdateError, match="preview_stale"):
        accept_portfolio_update(
            stale_later, new_approval(stale_later, approval_ref="synthetic:stale-later"),
            checkpoint_store=stores[1], write_store=stores[0],
            approval_verifier=lambda *_: {"verified": True, "verification_ref": "synthetic:test-approval"},
        )
    forged = copy.deepcopy(history[2]["base_portfolio"])
    next(row for row in forged["positions"] if row["position_id"] == "position:005930")["quantity"] = 99
    forged_checkpoint = build_checkpoint(
        forged, stores[0], snapshot_effective_at=history[2]["snapshot_effective_at"],
        through_commit_id=None, source_kind="reconciliation", source_ref=history[2]["source_ref"],
        reconciliation_candidate_ref="synthetic:forged-preview", acceptance_verification_ref="synthetic:forged-approval",
        account_batch=history[2]["account_batch"],
    )
    with pytest.raises(CheckpointError, match="checkpoint_batch_scope_invalid"):
        validate_checkpoint_journal_rows([*history[:2], forged_checkpoint], [])
    with pytest.raises(CheckpointError, match="checkpoint_batch_parent_invalid"):
        persist_checkpoint(stores[1], stores[0], portfolio_reconciliation.bind_reconciliation_acceptance(
            competing, candidate_ref="synthetic:other-preview", verification_ref="synthetic:other-approval"))


def test_same_day_revisions_read_back_by_checkpoint_even_after_another_correction(tmp_path, monkeypatch):
    base = _comparison_portfolio()
    original = next(row for row in base["positions"] if row["asset"]["symbol"] == "000660")
    original["quantity_basis"] = "user_confirmation"
    _, checkpoints = _seed(tmp_path, base)
    update = _manual_request()
    observed_at = {"value": "2026-09-18", "precision": "date_only"}
    update.update(reconcile_account_id="account-alpha", source_ref="user-observation:screens:date-only",
                  snapshot_effective_at=observed_at)
    held = {"account_id": "account-alpha", "position_id": original["position_id"]}
    initial = {**update, "changes": {
        "position_observations": [
            {"account_id": "account-alpha", "symbol": "005930", "quantity": 12},
            {**held, "quantity": 3}, {**held, "quantity": 4},
        ],
        "cash_amounts": [{"cash_id": "cash-alpha", "amount": amount} for amount in (1000, 1100)],
    }}

    def preview(server, request, label):
        return _call(server, "preview_portfolio_update", {"update": request}, request_id=label)

    def apply(server, review, label):
        payload = {key: review[key] for key in ("preview_id", "confirmation")}
        return _call(server, "apply_portfolio_update", payload, request_id=label)

    server = _existing_server(tmp_path)
    assert apply(server, preview(server, initial, "initial-preview"), "initial-apply")["read_back"]["matches"]
    server = _existing_server(tmp_path)
    revised = {**update, "changes": {"position_observations": [{**held, "quantity": 3}]}}
    review = preview(server, revised, "position-preview")
    assert review["reconciliation"]["unconfirmed"] == [
        {"cash_id": "cash-alpha", "reason": "conflicting_values", "fields": ["amount"]},
    ]
    assert {field["field"] for diff in review["differences"] for field in diff["field_changes"]} == {
        "observed_at", "source_evidence",
    }

    original_accept = reference_composition.accept_portfolio_update

    def accept_while_other_session_advances(candidate, *args, **kwargs):
        result = original_accept(candidate, *args, **kwargs)
        if candidate["candidate_id"] == review["preview_id"]:
            second_session = _existing_server(tmp_path)
            cash_update = {**update, "changes": {"cash_amounts": [{"cash_id": "cash-alpha", "amount": 1100}]}}
            cash_review = preview(second_session, cash_update, "cash-preview")
            assert apply(second_session, cash_review, "cash-apply")["read_back"]["matches"]
        return result

    monkeypatch.setattr(reference_composition, "accept_portfolio_update", accept_while_other_session_advances)
    saved = apply(server, review, "position-apply")
    assert saved["status"] == "applied" and saved["read_back"]["matches"]
    history = checkpoints.read_all()
    assert len(history) == 4
    assert saved["read_back"]["checkpoint_id"] == history[2]["checkpoint_id"] != history[3]["checkpoint_id"]
    assert all(row["snapshot_effective_at"] == observed_at and row["source_ref"] == update["source_ref"]
               for row in history[1:])
    current = _call(_existing_server(tmp_path), "get_portfolio_state", {"portfolio_id": "portfolio-alpha"},
                    request_id="fresh-session-read")["data"]
    position = next(row for row in current["positions"] if row["position_id"] == original["position_id"])
    assert position["quantity"] == 3 and position["observed_at"] == observed_at
    assert update["source_ref"] in position["source_evidence"]
    assert next(row for row in current["cash"] if row["cash_id"] == "cash-alpha")["value"]["amount"] == 1100


def test_account_comparison_keeps_unchanged_values_at_their_original_times(tmp_path):
    base = _portfolio()
    old_time = _tp("2026-09-17T00:00:00Z")
    position = base["positions"][0]
    position["avg_cost_values"] = [{**_money(100.25), "as_of": old_time}]
    position["valuation"] = {"currency": "KRW", "value_basis": "observed", "market_value": 900, "observed_at": old_time}
    stores = _seed(tmp_path, base)
    update = _manual_request()
    update.update(reconcile_account_id="account-alpha")
    update["changes"]["position_observations"][0]["quantity"] = 10
    update["changes"]["position_observations"][0]["valuation"] = {
        "currency": "KRW", "value_basis": "observed", "market_value": 900,
    }
    result = _preview(update, stores)
    difference, = result["review"]["differences"]
    assert [change["field"] for change in difference["field_changes"]] == ["cost_basis_values"]
    candidate = result["candidate"]["reconciled_portfolio"]
    recorded = next(row for row in candidate["positions"] if row["position_id"] == "position:005930")
    assert recorded["avg_cost_values"][0]["as_of"] == old_time
    assert recorded["valuation"]["observed_at"] == old_time


@pytest.mark.parametrize("edits", [
    {"position_observations": [{"account_id": "account-alpha", "position_id": "position:other-005930", "quantity": 12}]},
    {"position_observations": [{"account_id": "account-beta", "symbol": "005930", "quantity": 12}]},
    {"cash_amounts": [{"cash_id": "cash-beta", "amount": 0}]},
])
def test_account_comparison_rejects_rows_from_another_account(tmp_path, edits):
    stores = _seed(tmp_path, _comparison_portfolio())
    update = _manual_request()
    update.update(reconcile_account_id="account-alpha", changes=edits)
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_(reconcile_account_mismatch|position_match_not_unique)"):
        _preview(update, stores)
    assert len(stores[1].read_all()) == 1


def _addition():
    return {
        "account_id": "account-alpha",
        "asset": {"asset_type": "etf", "symbol": "0123X0", "venue": "KRX",
                  "currency": "KRW", "display_name": "Synthetic mixed ETF"},
        "cost_basis_values": [_money(300)],
        "valuation": {"currency": "KRW", "value_basis": "observed", "market_value": 280},
    }


def test_add_listed_holding_with_unreported_quantity_in_combined_mcp_update(tmp_path):
    server = _server(tmp_path)
    update = _manual_request()
    update["changes"].update(position_additions=[_addition()],
                             cash_amounts=[{"cash_id": "cash-alpha", "amount": 820}])
    preview = _call(server, "preview_portfolio_update", {"update": update}, request_id="add-preview")
    assert preview["status"] == "review_required"
    assert preview["summary"]["position_changes"] == 2
    added, = [diff for diff in preview["differences"] if diff["change_type"] == "added"]
    fields = {row["field"]: row["observed"] for row in added["field_changes"]}
    assert fields["quantity_status"] == "unknown"
    assert fields["valuation"]["market_value"] == 280

    before = _call(server, "get_portfolio_state", {"portfolio_id": "portfolio-alpha"}, request_id="add-before")["data"]
    assert not any(row["asset"]["symbol"] == "0123X0" for row in before["positions"])
    applied = _call(server, "apply_portfolio_update", {
        "preview_id": preview["preview_id"], "confirmation": preview["confirmation"],
    }, request_id="add-apply")
    assert applied["status"] == "applied"
    assert applied["read_back"]["matches"] is True
    fresh_server = _existing_server(tmp_path)
    portfolio = _call(fresh_server, "get_portfolio_state", {"portfolio_id": "portfolio-alpha"}, request_id="add-read")["data"]
    positions = {row["asset"]["symbol"]: row for row in portfolio["positions"]}
    row = positions["0123X0"]
    assert row["quantity"] is None
    assert row["quantity_status"] == row["quantity_basis"] == "unknown"
    assert "avg_cost_values" not in row
    assert row["cost_basis_values"][0]["amount"] == 300
    assert row["valuation"]["market_value"] == 280
    assert positions["005930"]["cost_basis_values"][0]["amount"] == 1003
    assert positions["000660"]["quantity"] == 3
    assert portfolio["cash"][0]["value"]["amount"] == 820
    assert portfolio["cash"][0]["cash_kind"] == before["cash"][0]["cash_kind"]
    assert portfolio["transactions"] == before["transactions"]

    # The quantity can be filled from a later observation using the saved ID.
    update["changes"] = {"position_quantities": [
        {"account_id": "account-alpha", "position_id": row["position_id"], "quantity": 7},
    ]}
    update["snapshot_effective_at"] = _tp("2026-09-22T00:00:00Z")
    later = _call(fresh_server, "preview_portfolio_update", {"update": update}, request_id="add-fill-quantity")
    assert later["status"] == "review_required"
    assert any(change["field"] == "quantity" and change["observed"] == 7
               for diff in later["differences"] for change in diff["field_changes"])


@pytest.mark.parametrize(("case", "code"), [
    ("existing_symbol", "portfolio_update_position_addition_already_exists"),
    ("repeated_symbol", "portfolio_update_position_addition_already_exists"),
    ("unknown_account", "portfolio_update_position_addition_account_unknown"),
    ("missing_venue", "portfolio_update_position_addition_asset_invalid"),
    ("invalid_quantity", "portfolio_update_position_quantity_invalid"),
    ("currency_mismatch", "portfolio_update_position_values_invalid"),
    ("no_observed_values", "portfolio_update_position_addition_invalid"),
])
def test_invalid_additions_reject_combined_preview(tmp_path, case, code):
    stores = _stores(tmp_path)
    update = _manual_request()
    addition = _addition()
    additions = [addition]
    if case == "existing_symbol":
        addition["asset"].update(symbol="005930", venue="XKRX")
    elif case == "repeated_symbol":
        additions.append(copy.deepcopy(addition))
    elif case == "unknown_account":
        addition["account_id"] = "account-missing"
    elif case == "missing_venue":
        addition["asset"].pop("venue")
    elif case == "invalid_quantity":
        addition["quantity"] = 0
    elif case == "currency_mismatch":
        addition["valuation"]["currency"] = "USD"
    else:
        addition.pop("valuation")
        addition.pop("cost_basis_values")
    update["changes"]["position_additions"] = additions
    with pytest.raises(PortfolioUpdateError, match=code):
        _preview(update, stores)


def test_new_holding_with_observed_quantity_needs_no_inferred_values(tmp_path):
    update = _manual_request()
    addition = _addition()
    addition.pop("cost_basis_values")
    addition.pop("valuation")
    addition["quantity"] = 7
    update["changes"] = {"position_additions": [addition]}
    preview = _preview(update, _stores(tmp_path))
    added = next(diff for diff in preview["review"]["differences"] if diff["change_type"] == "added")
    fields = {row["field"]: row["observed"] for row in added["field_changes"]}
    assert fields["quantity"] == 7
    assert fields["quantity_basis"] == "direct_observation"
    assert not {"cost_basis_values", "avg_cost_values", "valuation"} & fields.keys()


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
