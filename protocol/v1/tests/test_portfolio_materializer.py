from __future__ import annotations

import copy
import json
import sys
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

from protocol.v1.runtime.portfolio_materializer import (  # noqa: E402
    MaterializationBlocked,
    materialize_portfolio,
)


def _tp(value: str, precision: str = "source_exact") -> dict[str, str]:
    return {"value": value, "precision": precision}


def _money(amount: float, currency: str) -> dict[str, Any]:
    return {"amount": amount, "currency": currency, "value_basis": "observed", "source_evidence": []}


def _asset(symbol: str = "ALPHA", currency: str = "KRW") -> dict[str, Any]:
    return {
        "asset_type": "stock",
        "symbol": symbol,
        "venue": "KRX",
        "currency": currency,
        "display_name": f"Asset {symbol}",
        "provider_refs": {},
    }


def _base() -> dict[str, Any]:
    generated = _tp("2026-09-16T00:00:00Z")
    return {
        "protocol_version": "1.0-draft",
        "portfolio_id": "portfolio-alpha",
        "display_name": "Synthetic Portfolio",
        "generated_at": generated,
        "accounts": [{
            "account_id": "account-alpha",
            "portfolio_id": "portfolio-alpha",
            "display_name": "Synthetic Account",
            "provider_id": None,
            "account_type": "general",
            "base_currency": "KRW",
            "role": "tactical",
            "status": "active",
            "constraints": [],
        }],
        "positions": [{
            "position_id": "position-alpha",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "asset": _asset(),
            "quantity": 10,
            "quantity_status": "confirmed",
            "quantity_basis": "direct_observation",
            "cost_basis_values": [_money(1000, "KRW")],
            "avg_cost_values": [_money(100, "KRW")],
            "source_evidence": ["snapshot:alpha"],
            "authority": "portfolio_fact",
            "state": "open",
        }],
        "cash": [{
            "cash_id": "cash-alpha-krw",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "currency": "KRW",
            "cash_kind": "provider_specific",
            "provider_label": "explicit-test-ledger-basis",
            "value": _money(10000, "KRW"),
            "source_evidence": ["snapshot:cash"],
            "authority": "portfolio_fact",
        }],
        "transactions": [],
        "policies": [],
        "completeness": {
            "holdings": "complete",
            "cash": "complete",
            "valuation": "partial",
            "fx": "unknown",
            "transactions": "complete",
            "accounts": [{
                "account_id": "account-alpha",
                "holdings": "complete",
                "cash": "complete",
                "observed_at": generated,
            }],
        },
        "migration_gaps": [],
    }


def _trade(
    transaction_id: str,
    *,
    side: str,
    quantity: float,
    amount: float | None,
    effective_at: str,
    symbol: str = "ALPHA",
) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "asset": _asset(symbol),
        "transaction_type": "trade",
        "side": side,
        "quantity": quantity,
        "price": None,
        "amount": _money(amount, "KRW") if amount is not None else None,
        "effective_at": _tp(effective_at),
        "recorded_at": _tp("2026-09-16T12:00:00Z"),
        "occurrence_status": "confirmed",
        "detail_status": "complete" if amount is not None else "partial",
        "execution_basis": "explicit_user_confirmation",
        "source_type": "explicit_user_statement",
        "source_evidence": [f"evidence:{transaction_id}"],
        "provider_execution_id": None,
        "transfer_group_id": None,
        "lineage": None,
    }


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


def _basis() -> list[dict[str, str]]:
    return [{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha-krw"}]


def test_materializes_ordered_buys_and_sells_into_quantity_and_explicit_cash_basis() -> None:
    base = _base()
    transactions = [
        _trade("transaction:sell", side="sell", quantity=3, amount=450, effective_at="2026-09-16T10:00:00Z"),
        _trade("transaction:buy", side="buy", quantity=2, amount=200, effective_at="2026-09-16T09:00:00Z"),
    ]
    result = materialize_portfolio(
        base,
        transactions,
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    _validate(result, "portfolio-materialization.schema.json")
    _validate(result["portfolio"], "portfolio.schema.json")
    assert result["applied_transaction_ids"] == ["transaction:buy", "transaction:sell"]
    position = result["portfolio"]["positions"][0]
    assert position["quantity"] == 9
    assert position["quantity_basis"] == "execution_adjusted"
    assert position["authority"] == "estimated_portfolio_state"
    assert "cost_basis_values" not in position
    assert "avg_cost_values" not in position
    assert result["portfolio"]["cash"][0]["value"]["amount"] == 10250
    assert result["portfolio"]["cash"][0]["value"]["value_basis"] == "derived"
    assert result["status"] == "partial"
    assert {gap["gap_code"] for gap in result["gaps"]} == {"cost_basis_not_materialized"}
    assert base["positions"][0]["quantity"] == 10
    assert base["cash"][0]["value"]["amount"] == 10000


def test_input_order_does_not_change_projection_or_transaction_digest() -> None:
    buy = _trade("transaction:buy", side="buy", quantity=2, amount=200, effective_at="2026-09-16T09:00:00Z")
    sell = _trade("transaction:sell", side="sell", quantity=3, amount=450, effective_at="2026-09-16T10:00:00Z")
    kwargs = {
        "checkpoint_ref": "checkpoint:fixture",
        "cash_basis": _basis(),
        "generated_at": _tp("2026-09-16T13:00:00Z"),
    }
    first = materialize_portfolio(_base(), [sell, buy], **kwargs)
    second = materialize_portfolio(_base(), [buy, sell], **kwargs)
    assert first["transaction_set_digest"] == second["transaction_set_digest"]
    assert first["portfolio"] == second["portfolio"]
    assert first["applied_transaction_ids"] == second["applied_transaction_ids"]


def test_trade_without_cash_basis_updates_quantity_but_leaves_cash_and_marks_gap() -> None:
    result = materialize_portfolio(
        _base(),
        [_trade("transaction:buy", side="buy", quantity=2, amount=200, effective_at="2026-09-16T09:00:00Z")],
        checkpoint_ref="checkpoint:fixture",
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    assert result["portfolio"]["positions"][0]["quantity"] == 12
    assert result["portfolio"]["cash"][0]["value"]["amount"] == 10000
    codes = {gap["gap_code"] for gap in result["gaps"]}
    assert codes == {"cost_basis_not_materialized", "cash_basis_not_declared"}
    assert result["portfolio"]["completeness"]["cash"] == "partial"
    assert result["portfolio"]["completeness"]["accounts"][0]["cash"] == "partial"


def test_scoped_materialization_preserves_unrelated_unresolved_position_without_guessing_identity() -> None:
    base = _base()
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
    unresolved = {
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
        "source_evidence": ["snapshot:legacy"],
        "authority": "portfolio_fact",
        "state": "open",
    }
    base["positions"].append(unresolved)
    result = materialize_portfolio(
        base,
        [],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        identity_scope_account_ids={"account-alpha"},
    )
    preserved = next(row for row in result["portfolio"]["positions"] if row["position_id"] == "position-beta-unresolved")
    assert preserved == unresolved


def test_scoped_materialization_fails_if_unresolved_account_has_later_transaction() -> None:
    base = _base()
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
        "source_evidence": ["snapshot:legacy"],
        "authority": "portfolio_fact",
        "state": "open",
    })
    later = _trade("transaction:beta", side="buy", quantity=1, amount=100, effective_at="2026-09-16T09:00:00Z")
    later["account_id"] = "account-beta"
    with pytest.raises(MaterializationBlocked, match="base_position_identity_incomplete_for_transaction_account"):
        materialize_portfolio(
            base,
            [later],
            checkpoint_ref="checkpoint:fixture",
            cash_basis=_basis(),
            identity_scope_account_ids={"account-alpha"},
        )


def test_trade_with_missing_amount_and_price_never_guesses_cash_effect() -> None:
    result = materialize_portfolio(
        _base(),
        [_trade("transaction:buy", side="buy", quantity=1, amount=None, effective_at="2026-09-16T09:00:00Z")],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    assert result["portfolio"]["positions"][0]["quantity"] == 11
    assert result["portfolio"]["cash"][0]["value"]["amount"] == 10000
    assert {gap["gap_code"] for gap in result["gaps"]} == {
        "cost_basis_not_materialized",
        "trade_cash_effect_unknown",
    }
    assert result["portfolio"]["completeness"]["cash"] == "partial"


def test_trade_price_times_quantity_can_derive_cash_without_inventing_fx() -> None:
    transaction = _trade(
        "transaction:price-only",
        side="buy",
        quantity=2,
        amount=None,
        effective_at="2026-09-16T09:00:00Z",
    )
    transaction["price"] = _money(125, "KRW")
    result = materialize_portfolio(
        _base(),
        [transaction],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    assert result["portfolio"]["cash"][0]["value"]["amount"] == 9750
    assert "trade_cash_effect_unknown" not in {gap["gap_code"] for gap in result["gaps"]}


def test_foreign_currency_trade_is_not_converted_into_base_currency_cash() -> None:
    transaction = _trade(
        "transaction:usd",
        side="buy",
        quantity=1,
        amount=None,
        effective_at="2026-09-16T09:00:00Z",
        symbol="US-ALPHA",
    )
    transaction["asset"] = _asset("US-ALPHA", "USD")
    transaction["amount"] = _money(50, "USD")
    result = materialize_portfolio(
        _base(),
        [transaction],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    assert result["portfolio"]["cash"][0]["value"]["amount"] == 10000
    cash_gap = next(gap for gap in result["gaps"] if gap["gap_code"] == "cash_basis_not_declared")
    assert cash_gap["scope"]["currency"] == "USD"


def test_cash_only_transactions_apply_only_explicit_amounts() -> None:
    deposit = _trade(
        "transaction:deposit",
        side="buy",
        quantity=1,
        amount=500,
        effective_at="2026-09-16T09:00:00Z",
    )
    deposit["transaction_type"] = "deposit"
    deposit["asset"] = None
    deposit["side"] = None
    deposit["quantity"] = None

    fee = _trade(
        "transaction:fee",
        side="buy",
        quantity=1,
        amount=20,
        effective_at="2026-09-16T10:00:00Z",
    )
    fee["transaction_type"] = "fee"
    fee["asset"] = None
    fee["side"] = None
    fee["quantity"] = None

    result = materialize_portfolio(
        _base(),
        [fee, deposit],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    assert result["portfolio"]["cash"][0]["value"]["amount"] == 10480
    assert result["applied_transaction_ids"] == ["transaction:deposit", "transaction:fee"]


def test_new_position_is_created_deterministically_from_exact_asset_identity() -> None:
    result = materialize_portfolio(
        _base(),
        [_trade("transaction:new", side="buy", quantity=4, amount=800, effective_at="2026-09-16T09:00:00Z", symbol="BETA")],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    beta = next(row for row in result["portfolio"]["positions"] if row["asset"]["symbol"] == "BETA")
    assert beta["quantity"] == 4
    assert beta["position_id"].startswith("materialized-position:")
    assert beta["quantity_status"] == "confirmed"


def test_oversell_fails_closed_unless_short_is_explicitly_allowed() -> None:
    sell = _trade("transaction:oversell", side="sell", quantity=11, amount=1100, effective_at="2026-09-16T09:00:00Z")
    with pytest.raises(MaterializationBlocked, match="oversell_not_allowed") as exc:
        materialize_portfolio(_base(), [sell], checkpoint_ref="checkpoint:fixture", cash_basis=_basis())
    assert exc.value.transaction_id == "transaction:oversell"

    result = materialize_portfolio(
        _base(),
        [sell],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        short_allowed_account_ids=["account-alpha"],
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    assert result["portfolio"]["positions"][0]["quantity"] == -1

    with pytest.raises(MaterializationBlocked, match="short_allowed_account_unknown"):
        materialize_portfolio(
            _base(),
            [sell],
            checkpoint_ref="checkpoint:fixture",
            short_allowed_account_ids=["account-does-not-exist"],
        )


def test_cash_basis_requires_value_currency_to_match_declared_basis() -> None:
    base = _base()
    base["cash"][0]["value"]["currency"] = "USD"
    with pytest.raises(MaterializationBlocked, match="cash_basis_value_currency_mismatch"):
        materialize_portfolio(
            base,
            [_trade("transaction:buy", side="buy", quantity=1, amount=100, effective_at="2026-09-16T09:00:00Z")],
            checkpoint_ref="checkpoint:fixture",
            cash_basis=_basis(),
        )


def test_uncertain_base_quantity_and_ambiguous_asset_identity_fail_closed() -> None:
    uncertain = _base()
    uncertain["positions"][0]["quantity_status"] = "estimated_current"
    with pytest.raises(MaterializationBlocked, match="base_quantity_not_confirmed"):
        materialize_portfolio(
            uncertain,
            [_trade("transaction:buy", side="buy", quantity=1, amount=100, effective_at="2026-09-16T09:00:00Z")],
            checkpoint_ref="checkpoint:fixture",
        )

    bad_asset = _trade("transaction:bad-asset", side="buy", quantity=1, amount=100, effective_at="2026-09-16T09:00:00Z")
    bad_asset["asset"]["symbol"] = None
    bad_asset["asset"]["currency"] = None
    with pytest.raises(MaterializationBlocked, match="trade_asset_identity_incomplete"):
        materialize_portfolio(_base(), [bad_asset], checkpoint_ref="checkpoint:fixture")


def test_correction_and_corporate_action_block_instead_of_silent_projection() -> None:
    for transaction_type in ("correction", "corporate_action"):
        transaction = _trade(
            f"transaction:{transaction_type}",
            side="buy",
            quantity=1,
            amount=100,
            effective_at="2026-09-16T09:00:00Z",
        )
        transaction["transaction_type"] = transaction_type
        with pytest.raises(MaterializationBlocked, match=f"unsupported_{transaction_type}_materialization"):
            materialize_portfolio(_base(), [transaction], checkpoint_ref="checkpoint:fixture")

    unknown = _trade(
        "transaction:unknown-type",
        side="buy",
        quantity=1,
        amount=100,
        effective_at="2026-09-16T09:00:00Z",
    )
    unknown["transaction_type"] = "mystery"
    with pytest.raises(MaterializationBlocked, match="transaction_type_unsupported"):
        materialize_portfolio(_base(), [unknown], checkpoint_ref="checkpoint:fixture")


def test_existing_base_transaction_cannot_be_applied_again() -> None:
    base = _base()
    transaction = _trade("transaction:already", side="buy", quantity=1, amount=100, effective_at="2026-09-16T09:00:00Z")
    base["transactions"].append(copy.deepcopy(transaction))
    with pytest.raises(MaterializationBlocked, match="transaction_already_in_base"):
        materialize_portfolio(base, [transaction], checkpoint_ref="checkpoint:fixture")


def test_nonconfirmed_transaction_is_retained_but_not_applied() -> None:
    candidate = _trade("transaction:candidate", side="buy", quantity=1, amount=100, effective_at="2026-09-16T09:00:00Z")
    candidate["occurrence_status"] = "candidate"
    result = materialize_portfolio(
        _base(),
        [candidate],
        checkpoint_ref="checkpoint:fixture",
        cash_basis=_basis(),
        generated_at=_tp("2026-09-16T13:00:00Z"),
    )
    assert result["applied_transaction_ids"] == []
    assert result["skipped_transaction_ids"] == ["transaction:candidate"]
    assert result["portfolio"]["positions"][0]["quantity"] == 10
    assert result["portfolio"]["transactions"][-1]["transaction_id"] == "transaction:candidate"
    assert result["status"] == "complete"
