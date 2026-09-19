from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from protocol.v1.adapters.common import PROTOCOL_VERSION, digest, timepoint
from protocol.v1.runtime.asset_identity import asset_identity_conflict, asset_identity_issue, canonical_asset_identity


ORDERING = "effective_at_then_recorded_at_then_transaction_id"
SUPPORTED_TRANSACTION_TYPES = {
    "trade",
    "cash_transfer",
    "deposit",
    "withdrawal",
    "dividend",
    "fee",
    "tax",
    "corporate_action",
    "correction",
    "other",
}


class MaterializationBlocked(ValueError):
    """Raised when applying a transaction would require guessing canonical state."""

    def __init__(
        self,
        code: str,
        *,
        transaction_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.transaction_id = transaction_id
        self.details = copy.deepcopy(details) if details else None


@dataclass(frozen=True)
class CashBasisRef:
    account_id: str
    currency: str
    cash_id: str


def _decimal(value: Any, *, code: str, transaction_id: str | None = None) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MaterializationBlocked(code, transaction_id=transaction_id) from exc
    if not result.is_finite():
        raise MaterializationBlocked(code, transaction_id=transaction_id)
    return result


def _number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _time_value(point: Any, *, transaction_id: str, field: str) -> tuple[datetime, int]:
    if not isinstance(point, dict) or not isinstance(point.get("value"), str):
        raise MaterializationBlocked(f"{field}_missing", transaction_id=transaction_id)
    raw = point["value"]
    precision = str(point.get("precision") or "unknown")
    try:
        if "T" in raw:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            return parsed.astimezone(timezone.utc), 1
        parsed_date = date.fromisoformat(raw)
        return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc), 0
    except ValueError as exc:
        raise MaterializationBlocked(f"{field}_invalid", transaction_id=transaction_id) from exc


def _transaction_sort_key(transaction: dict[str, Any]) -> tuple[datetime, int, datetime, int, str]:
    transaction_id = str(transaction.get("transaction_id") or "")
    if not transaction_id:
        raise MaterializationBlocked("transaction_id_missing")
    effective, effective_precision = _time_value(
        transaction.get("effective_at"), transaction_id=transaction_id, field="effective_at"
    )
    recorded_point = transaction.get("recorded_at") or transaction.get("effective_at")
    recorded, recorded_precision = _time_value(
        recorded_point, transaction_id=transaction_id, field="recorded_at"
    )
    return effective, effective_precision, recorded, recorded_precision, transaction_id


def _merge_evidence(*values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in values:
        for item in group:
            text = str(item)
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def _gap(
    code: str,
    reason: str,
    impact: str,
    *,
    scope: dict[str, Any] | str | None = None,
    recoverable: bool = True,
) -> dict[str, Any]:
    return {
        "gap_code": code,
        "scope": scope,
        "reason": reason,
        "impact": impact,
        "recoverable": recoverable,
    }


def _position_key(account_id: str, asset: Any) -> tuple[str, tuple[str, ...]] | None:
    identity = canonical_asset_identity(asset)
    if identity is None:
        return None
    return account_id, identity


def _cash_effect(transaction: dict[str, Any]) -> tuple[str, Decimal] | None:
    transaction_id = str(transaction["transaction_id"])
    transaction_type = str(transaction.get("transaction_type") or "")
    side = transaction.get("side")
    quantity = transaction.get("quantity")
    amount = transaction.get("amount")
    price = transaction.get("price")

    if transaction_type in {"correction", "corporate_action", "other"}:
        raise MaterializationBlocked(
            f"unsupported_{transaction_type}_materialization",
            transaction_id=transaction_id,
        )

    money: dict[str, Any] | None = amount if isinstance(amount, dict) else None
    if transaction_type == "trade" and money is None and isinstance(price, dict) and quantity is not None:
        price_amount = _decimal(price.get("amount"), code="trade_price_invalid", transaction_id=transaction_id)
        quantity_value = _decimal(quantity, code="trade_quantity_invalid", transaction_id=transaction_id)
        if price_amount <= 0 or quantity_value <= 0:
            raise MaterializationBlocked("trade_price_or_quantity_nonpositive", transaction_id=transaction_id)
        money = {"amount": _number(price_amount * quantity_value), "currency": price.get("currency")}

    if money is None:
        return None
    currency = money.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise MaterializationBlocked("cash_effect_currency_invalid", transaction_id=transaction_id)
    value = _decimal(money.get("amount"), code="cash_effect_amount_invalid", transaction_id=transaction_id)
    if value <= 0:
        raise MaterializationBlocked("cash_effect_amount_nonpositive", transaction_id=transaction_id)

    if transaction_type == "trade":
        if side == "buy":
            return currency, -value
        if side == "sell":
            return currency, value
        raise MaterializationBlocked("trade_side_invalid", transaction_id=transaction_id)
    if transaction_type in {"deposit", "dividend"}:
        return currency, value
    if transaction_type in {"withdrawal", "fee", "tax"}:
        return currency, -value
    if transaction_type == "cash_transfer":
        if side == "transfer_in":
            return currency, value
        if side == "transfer_out":
            return currency, -value
        raise MaterializationBlocked("cash_transfer_side_invalid", transaction_id=transaction_id)
    return None


def _normalize_cash_basis(values: Iterable[dict[str, Any]] | None) -> list[CashBasisRef]:
    refs: list[CashBasisRef] = []
    seen: set[tuple[str, str]] = set()
    for value in values or []:
        account_id = str(value.get("account_id") or "")
        currency = str(value.get("currency") or "")
        cash_id = str(value.get("cash_id") or "")
        if not account_id or len(currency) != 3 or not cash_id:
            raise MaterializationBlocked("cash_basis_invalid")
        key = (account_id, currency)
        if key in seen:
            raise MaterializationBlocked("cash_basis_duplicate")
        seen.add(key)
        refs.append(CashBasisRef(account_id=account_id, currency=currency, cash_id=cash_id))
    return refs


def materialize_portfolio(
    base_portfolio: dict[str, Any],
    transactions: Iterable[dict[str, Any]],
    *,
    checkpoint_ref: str,
    cash_basis: Iterable[dict[str, Any]] | None = None,
    short_allowed_account_ids: Iterable[str] | None = None,
    generated_at: dict[str, Any] | None = None,
    identity_scope_account_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Project a current Portfolio from an explicit checkpoint plus transaction delta.

    This function is intentionally pure: it does not read or mutate PersonalDataStore,
    NativeWriteStore, legacy files, or deployment state.  The caller is responsible for
    selecting the explicit base checkpoint and the transaction delta that follows it.
    """

    if not isinstance(base_portfolio, dict) or base_portfolio.get("protocol_version") != PROTOCOL_VERSION:
        raise MaterializationBlocked("base_portfolio_invalid")
    portfolio_id = str(base_portfolio.get("portfolio_id") or "")
    if not portfolio_id or not checkpoint_ref:
        raise MaterializationBlocked("checkpoint_invalid")

    projected = copy.deepcopy(base_portfolio)
    delta = [copy.deepcopy(row) for row in transactions]
    ordered = sorted(delta, key=_transaction_sort_key)
    transaction_ids = [str(row.get("transaction_id") or "") for row in ordered]
    if len(set(transaction_ids)) != len(transaction_ids):
        raise MaterializationBlocked("duplicate_transaction_id")

    base_transaction_ids = {
        str(row.get("transaction_id") or "")
        for row in projected.get("transactions") or []
        if isinstance(row, dict) and row.get("transaction_id")
    }
    overlap = base_transaction_ids.intersection(transaction_ids)
    if overlap:
        raise MaterializationBlocked("transaction_already_in_base", transaction_id=sorted(overlap)[0])

    accounts: set[str] = set()
    for row in projected.get("accounts") or []:
        if not isinstance(row, dict):
            raise MaterializationBlocked("base_account_invalid")
        account_id = str(row.get("account_id") or "")
        if not account_id or account_id in accounts:
            raise MaterializationBlocked("base_account_identity_ambiguous")
        if row.get("portfolio_id") != portfolio_id:
            raise MaterializationBlocked("base_account_portfolio_mismatch")
        accounts.add(account_id)

    positions = projected.setdefault("positions", [])
    position_index: dict[tuple[str, tuple[str, ...]], int] = {}
    identity_scope = None if identity_scope_account_ids is None else {
        str(value) for value in identity_scope_account_ids if str(value)
    }
    unresolved_position_accounts: set[str] = set()
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise MaterializationBlocked("base_position_invalid")
        account_id = str(position.get("account_id") or "")
        if position.get("portfolio_id") != portfolio_id:
            raise MaterializationBlocked("base_position_portfolio_mismatch")
        if account_id not in accounts:
            raise MaterializationBlocked("base_position_account_unknown")
        key = _position_key(account_id, position.get("asset"))
        if key is None:
            if identity_scope is not None and account_id not in identity_scope:
                unresolved_position_accounts.add(account_id)
                continue
            raise MaterializationBlocked(
                "base_position_identity_incomplete",
                details={
                    "portfolio_id": portfolio_id,
                    "position_id": str(position.get("position_id") or "") or None,
                    "account_id": account_id or None,
                    **asset_identity_issue(position.get("asset")),
                },
            )
        if key in position_index:
            raise MaterializationBlocked("base_position_identity_ambiguous")
        position_index[key] = index

    cash_rows = projected.setdefault("cash", [])
    cash_by_id: dict[str, int] = {}
    for index, row in enumerate(cash_rows):
        if not isinstance(row, dict):
            raise MaterializationBlocked("base_cash_invalid")
        if row.get("portfolio_id") != portfolio_id:
            raise MaterializationBlocked("base_cash_portfolio_mismatch")
        if str(row.get("account_id") or "") not in accounts:
            raise MaterializationBlocked("base_cash_account_unknown")
        cash_id = str(row.get("cash_id") or "")
        if not cash_id or cash_id in cash_by_id:
            raise MaterializationBlocked("base_cash_identity_ambiguous")
        cash_by_id[cash_id] = index

    basis_refs = _normalize_cash_basis(cash_basis)
    basis_map: dict[tuple[str, str], int] = {}
    for ref in basis_refs:
        if ref.cash_id not in cash_by_id:
            raise MaterializationBlocked("cash_basis_not_found")
        row = cash_rows[cash_by_id[ref.cash_id]]
        if row.get("account_id") != ref.account_id or row.get("currency") != ref.currency:
            raise MaterializationBlocked("cash_basis_scope_mismatch")
        value = row.get("value")
        if not isinstance(value, dict) or value.get("currency") != ref.currency:
            raise MaterializationBlocked("cash_basis_value_currency_mismatch")
        basis_map[(ref.account_id, ref.currency)] = cash_by_id[ref.cash_id]

    short_allowed = {str(value) for value in short_allowed_account_ids or []}
    if not short_allowed.issubset(accounts):
        raise MaterializationBlocked("short_allowed_account_unknown")
    gaps: list[dict[str, Any]] = []
    applied: list[str] = []
    skipped: list[str] = []
    changed_positions: set[int] = set()
    changed_cash: set[int] = set()
    cash_incomplete_accounts: set[str] = set()
    last_effective_by_account: dict[str, dict[str, Any]] = {}

    for transaction in ordered:
        transaction_id = str(transaction["transaction_id"])
        if transaction.get("portfolio_id") != portfolio_id:
            raise MaterializationBlocked("transaction_portfolio_mismatch", transaction_id=transaction_id)
        account_id = str(transaction.get("account_id") or "")
        if account_id not in accounts:
            raise MaterializationBlocked("transaction_account_unknown", transaction_id=transaction_id)
        if account_id in unresolved_position_accounts:
            raise MaterializationBlocked(
                "base_position_identity_incomplete_for_transaction_account",
                transaction_id=transaction_id,
                details={
                    "portfolio_id": portfolio_id,
                    "account_id": account_id,
                    "reason": "unresolved_base_position_and_later_transaction_share_account_scope",
                },
            )
        if transaction.get("occurrence_status") != "confirmed":
            skipped.append(transaction_id)
            continue

        transaction_type = str(transaction.get("transaction_type") or "")
        if transaction_type not in SUPPORTED_TRANSACTION_TYPES:
            raise MaterializationBlocked("transaction_type_unsupported", transaction_id=transaction_id)
        if transaction_type in {"correction", "corporate_action", "other"}:
            raise MaterializationBlocked(
                f"unsupported_{transaction_type}_materialization",
                transaction_id=transaction_id,
            )

        if transaction_type == "trade":
            side = transaction.get("side")
            if side not in {"buy", "sell"}:
                raise MaterializationBlocked("trade_side_invalid", transaction_id=transaction_id)
            quantity = _decimal(
                transaction.get("quantity"), code="trade_quantity_invalid", transaction_id=transaction_id
            )
            if quantity <= 0:
                raise MaterializationBlocked("trade_quantity_nonpositive", transaction_id=transaction_id)
            asset = transaction.get("asset")
            key = _position_key(account_id, asset)
            if key is None:
                raise MaterializationBlocked("trade_asset_identity_incomplete", transaction_id=transaction_id)

            if key in position_index:
                position_index_value = position_index[key]
                position = positions[position_index_value]
                if not isinstance(position.get("asset"), dict) or not isinstance(asset, dict):
                    raise MaterializationBlocked("trade_asset_identity_incomplete", transaction_id=transaction_id)
                if asset_identity_conflict(position["asset"], asset):
                    raise MaterializationBlocked("asset_identity_conflict", transaction_id=transaction_id)
                if position.get("quantity_status") != "confirmed":
                    raise MaterializationBlocked("base_quantity_not_confirmed", transaction_id=transaction_id)
                current_quantity = _decimal(
                    position.get("quantity"), code="base_quantity_invalid", transaction_id=transaction_id
                )
            else:
                position_index_value = len(positions)
                current_quantity = Decimal(0)
                position = {
                    "position_id": "materialized-position:" + digest([portfolio_id, account_id, list(key)])[:24],
                    "portfolio_id": portfolio_id,
                    "account_id": account_id,
                    "asset": copy.deepcopy(asset),
                    "quantity": 0,
                    "quantity_status": "confirmed",
                    "quantity_basis": "execution_adjusted",
                    "authority": "estimated_portfolio_state",
                    "source_evidence": [],
                    "state": "closed",
                }
                positions.append(position)
                position_index[key] = position_index_value

            next_quantity = current_quantity + quantity if side == "buy" else current_quantity - quantity
            if next_quantity < 0 and account_id not in short_allowed:
                raise MaterializationBlocked("oversell_not_allowed", transaction_id=transaction_id)

            position["quantity"] = _number(next_quantity)
            position["quantity_status"] = "confirmed"
            position["quantity_basis"] = "execution_adjusted"
            position["authority"] = "estimated_portfolio_state"
            position["state"] = "closed" if next_quantity == 0 else "open"
            position["effective_at"] = copy.deepcopy(transaction["effective_at"])
            if transaction.get("recorded_at") is not None:
                position["recorded_at"] = copy.deepcopy(transaction["recorded_at"])
            position["source_evidence"] = _merge_evidence(
                position.get("source_evidence") or [], [transaction_id]
            )
            position.pop("cost_basis_values", None)
            position.pop("avg_cost_values", None)
            changed_positions.add(position_index_value)
            gaps.append(
                _gap(
                    "cost_basis_not_materialized",
                    "Quantity was updated from canonical transactions, but lot/cost-basis accounting is not implemented in materialization v1.",
                    "The projected quantity is usable; cost basis and average cost are intentionally omitted for the changed position.",
                    scope={"transaction_id": transaction_id, "account_id": account_id},
                    recoverable=True,
                )
            )

        effect = _cash_effect(transaction)
        if effect is not None:
            currency, cash_delta = effect
            cash_key = (account_id, currency)
            if cash_key not in basis_map:
                cash_incomplete_accounts.add(account_id)
                gaps.append(
                    _gap(
                        "cash_basis_not_declared",
                        "The transaction has a deterministic cash effect, but no explicit base cash row was declared for this account/currency.",
                        "Position effects can be projected, but cash remains at the checkpoint value for this account/currency.",
                        scope={"transaction_id": transaction_id, "account_id": account_id, "currency": currency},
                        recoverable=True,
                    )
                )
            else:
                cash_index = basis_map[cash_key]
                cash_row = cash_rows[cash_index]
                value = cash_row.get("value")
                if not isinstance(value, dict):
                    raise MaterializationBlocked("cash_basis_value_invalid", transaction_id=transaction_id)
                current_cash = _decimal(
                    value.get("amount"), code="cash_basis_value_invalid", transaction_id=transaction_id
                )
                next_cash = current_cash + cash_delta
                value["amount"] = _number(next_cash)
                value["currency"] = currency
                value["value_basis"] = "derived"
                value["as_of"] = copy.deepcopy(transaction["effective_at"])
                value["source_evidence"] = _merge_evidence(
                    value.get("source_evidence") or [], [transaction_id]
                )
                cash_row["authority"] = "estimated_portfolio_state"
                cash_row["observed_at"] = copy.deepcopy(transaction["effective_at"])
                if transaction.get("recorded_at") is not None:
                    cash_row["recorded_at"] = copy.deepcopy(transaction["recorded_at"])
                cash_row["source_evidence"] = _merge_evidence(
                    cash_row.get("source_evidence") or [], [transaction_id]
                )
                changed_cash.add(cash_index)
        elif transaction_type == "trade":
            cash_incomplete_accounts.add(account_id)
            gaps.append(
                _gap(
                    "trade_cash_effect_unknown",
                    "The trade has neither an explicit amount nor a usable price×quantity cash amount.",
                    "Position quantity can be projected, but cash is intentionally left at the checkpoint value.",
                    scope={"transaction_id": transaction_id, "account_id": account_id},
                    recoverable=True,
                )
            )
        elif transaction_type in {"deposit", "withdrawal", "dividend", "fee", "tax", "cash_transfer"}:
            cash_incomplete_accounts.add(account_id)
            gaps.append(
                _gap(
                    "cash_effect_amount_missing",
                    "The cash-only transaction does not contain an explicit amount.",
                    "Cash cannot be changed without guessing an amount.",
                    scope={"transaction_id": transaction_id, "account_id": account_id},
                    recoverable=True,
                )
            )

        applied.append(transaction_id)
        last_effective_by_account[account_id] = copy.deepcopy(transaction["effective_at"])

    projected["transactions"] = list(projected.get("transactions") or []) + ordered
    projected["generated_at"] = copy.deepcopy(generated_at or timepoint())
    projected["migration_gaps"] = list(projected.get("migration_gaps") or []) + copy.deepcopy(gaps)

    completeness = projected.setdefault("completeness", {})
    if changed_positions:
        completeness["valuation"] = "unknown"
    if cash_incomplete_accounts:
        completeness["cash"] = "partial"

    account_completeness = {
        str(row.get("account_id") or ""): row
        for row in completeness.get("accounts") or []
        if isinstance(row, dict)
    }
    for account_id in accounts:
        row = account_completeness.get(account_id)
        if row is None:
            row = {"account_id": account_id, "holdings": "unknown", "cash": "unknown"}
            completeness.setdefault("accounts", []).append(row)
            account_completeness[account_id] = row
        if account_id in cash_incomplete_accounts:
            row["cash"] = "partial"
        if account_id in last_effective_by_account:
            row["observed_at"] = copy.deepcopy(last_effective_by_account[account_id])

    result_gaps = copy.deepcopy(gaps)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "materialization_version": 1,
        "checkpoint_ref": checkpoint_ref,
        "base_portfolio_digest": digest(base_portfolio),
        "transaction_set_digest": digest(ordered),
        "generated_at": copy.deepcopy(projected["generated_at"]),
        "status": "partial" if result_gaps else "complete",
        "ordering": ORDERING,
        "portfolio": projected,
        "applied_transaction_ids": applied,
        "skipped_transaction_ids": skipped,
        "cash_basis_refs": [
            {"account_id": ref.account_id, "currency": ref.currency, "cash_id": ref.cash_id}
            for ref in basis_refs
        ],
        "gaps": result_gaps,
    }
