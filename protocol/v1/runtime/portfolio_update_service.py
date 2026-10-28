from __future__ import annotations

import copy
import math
from typing import Any, Callable, Mapping

from protocol.v1.adapters.common import PROTOCOL_VERSION, digest, public_reference, timepoint
from protocol.v1.runtime.holding_observations import HOLDING_VALUE_FIELDS, validate_holding_values
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    project_from_latest_checkpoint_through_cursor,
)
from protocol.v1.runtime.portfolio_reconciliation import (
    ReconciliationError,
    accept_reconciliation_candidate,
    build_reconciliation_candidate_from_latest_checkpoint,
    journal_cursor_for_snapshot,
    validate_reconciliation_candidate,
)


ApprovalVerifier = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class PortfolioUpdateError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = copy.deepcopy(details) if details else None


def _number(value: Any, *, code: str, nonnegative: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise PortfolioUpdateError(code)
    if nonnegative and value < 0:
        raise PortfolioUpdateError(code)
    return value


def _merge_evidence(existing: Any, source_ref: str) -> list[str]:
    values = [str(value) for value in existing or [] if str(value)]
    if source_ref not in values:
        values.append(source_ref)
    return values


def _resolve_reference(rows: list[dict[str, Any]], field: str, selector: str, *, code: str,
                       ambiguous_code: str | None = None) -> dict[str, Any]:
    # Resolve only IDs already present in this current Portfolio. Hashes cannot
    # authorize a new identity; collisions and raw/public alias ambiguity reject.
    matches = [row for row in rows if isinstance(row.get(field), str)
               and selector in {row[field], public_reference(row[field])}]
    if len(matches) != 1:
        raise PortfolioUpdateError(ambiguous_code if len(matches) > 1 and ambiguous_code else code)
    return matches[0]


def _current_at_snapshot(
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    *,
    portfolio_id: str,
    snapshot_effective_at: dict[str, Any],
    generated_at: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        cursor = journal_cursor_for_snapshot(
            write_store,
            portfolio_id=portfolio_id,
            snapshot_effective_at=snapshot_effective_at,
        )
        projection = project_from_latest_checkpoint_through_cursor(
            checkpoint_store,
            write_store,
            portfolio_id=portfolio_id,
            through_commit_id=cursor.get("through_commit_id"),
            generated_at=generated_at,
        )
    except (CheckpointError, ReconciliationError) as exc:
        raise PortfolioUpdateError(
            getattr(exc, "code", "portfolio_update_projection_failed"),
            details=getattr(exc, "details", None),
        ) from exc
    materialization = projection.get("materialization")
    current = materialization.get("portfolio") if isinstance(materialization, dict) else None
    if not isinstance(current, dict) or current.get("portfolio_id") != portfolio_id:
        raise PortfolioUpdateError("portfolio_update_projection_invalid")
    return current


def _narrow_observation(
    current: dict[str, Any],
    changes: Mapping[str, Any],
    *,
    snapshot_effective_at: dict[str, Any],
    source_ref: str,
) -> dict[str, Any]:
    allowed = {"position_quantities", "position_observations", "cash_amounts"}
    if set(changes) - allowed:
        raise PortfolioUpdateError("portfolio_update_changes_fields_invalid")
    position_changes = changes.get("position_quantities", [])
    position_observations = changes.get("position_observations", [])
    cash_changes = changes.get("cash_amounts", [])
    if not all(isinstance(rows, list) for rows in (position_changes, position_observations, cash_changes)):
        raise PortfolioUpdateError("portfolio_update_changes_invalid")
    if len(position_observations) > 1000:
        raise PortfolioUpdateError("portfolio_update_changes_invalid")
    if not position_changes and not position_observations and not cash_changes:
        raise PortfolioUpdateError("portfolio_update_changes_empty")

    accounts: dict[str, dict[str, Any]] = {}
    for row in current.get("accounts") or []:
        if not isinstance(row, dict):
            raise PortfolioUpdateError("portfolio_update_account_invalid")
        account_id = str(row.get("account_id") or "")
        if not account_id or account_id in accounts:
            raise PortfolioUpdateError("portfolio_update_account_identity_ambiguous")
        if row.get("portfolio_id") != current.get("portfolio_id"):
            raise PortfolioUpdateError("portfolio_update_account_portfolio_mismatch")
        accounts[account_id] = row
    current_positions = [row for row in current.get("positions") or [] if isinstance(row, dict)]
    current_cash = [row for row in current.get("cash") or [] if isinstance(row, dict)]
    observed_positions_by_id: dict[str, dict[str, Any]] = {}
    observed_cash: list[dict[str, Any]] = []
    touched_holdings: set[str] = set()
    touched_cash: set[str] = set()
    seen_positions: set[tuple[str, str]] = set()
    claimed_positions: set[str] = set()
    seen_cash: set[str] = set()

    position_by_id: dict[str, dict[str, Any]] = {}
    for row in current_positions:
        position_id = str(row.get("position_id") or "")
        if not position_id or position_id in position_by_id:
            raise PortfolioUpdateError("portfolio_update_position_identity_ambiguous")
        position_by_id[position_id] = row

    for change in position_changes:
        if not isinstance(change, Mapping):
            raise PortfolioUpdateError("portfolio_update_position_change_invalid")
        keys = set(change)
        symbol_mode = keys == {"account_id", "symbol", "quantity"}
        position_id_mode = keys == {"account_id", "position_id", "quantity"}
        if not symbol_mode and not position_id_mode:
            raise PortfolioUpdateError("portfolio_update_position_change_invalid")
        account_id = str(change.get("account_id") or "")
        selector = str(change.get("symbol") if symbol_mode else change.get("position_id") or "")
        if not account_id or not selector or account_id not in accounts:
            raise PortfolioUpdateError("portfolio_update_position_identity_invalid")
        key = (account_id, ("symbol:" if symbol_mode else "position_id:") + selector)
        if key in seen_positions:
            raise PortfolioUpdateError("portfolio_update_position_change_duplicate")
        seen_positions.add(key)
        if position_id_mode:
            matched = _resolve_reference(current_positions, "position_id", selector,
                                         code="portfolio_update_position_match_not_unique")
            matches = [matched] if matched.get("account_id") == account_id else []
        else:
            matches = [
                row for row in current_positions
                if row.get("account_id") == account_id
                and isinstance(row.get("asset"), dict)
                and row["asset"].get("symbol") == selector
            ]
        if len(matches) != 1:
            raise PortfolioUpdateError("portfolio_update_position_match_not_unique")
        position_id = str(matches[0]["position_id"])
        if position_id in claimed_positions:
            raise PortfolioUpdateError("portfolio_update_position_change_duplicate")
        claimed_positions.add(position_id)
        quantity = _number(
            change.get("quantity"),
            code="portfolio_update_position_quantity_invalid",
            nonnegative=True,
        )
        current_quantity = _number(
            matches[0].get("quantity"),
            code="portfolio_update_position_quantity_invalid",
            nonnegative=True,
        )
        if float(current_quantity) == float(quantity):
            continue
        row = copy.deepcopy(matches[0])
        row["quantity"] = quantity
        row.pop("valuation", None)
        row["quantity_status"] = "confirmed"
        row["quantity_basis"] = "user_confirmation"
        row["authority"] = "portfolio_fact"
        row["source_evidence"] = _merge_evidence(row.get("source_evidence"), source_ref)
        row["observed_at"] = copy.deepcopy(snapshot_effective_at)
        row["state"] = "closed" if quantity == 0 else "open"
        observed_positions_by_id[position_id] = row
        touched_holdings.add(account_id)

    for change in position_observations:
        if not isinstance(change, Mapping):
            raise PortfolioUpdateError("portfolio_update_position_observation_invalid")
        keys = set(change)
        fields = {"quantity", *HOLDING_VALUE_FIELDS}
        if (keys - {"account_id", "symbol", "position_id", *fields}
                or not (keys & fields) or ("symbol" in keys) == ("position_id" in keys)):
            raise PortfolioUpdateError("portfolio_update_position_observation_invalid")
        account_id = change.get("account_id")
        selector = change.get("position_id") if "position_id" in change else change.get("symbol")
        if not isinstance(account_id, str) or account_id not in accounts or not isinstance(selector, str) or not selector:
            raise PortfolioUpdateError("portfolio_update_position_identity_invalid")
        if "position_id" in change:
            current_row = _resolve_reference(current_positions, "position_id", selector,
                                             code="portfolio_update_position_match_not_unique")
            if current_row.get("account_id") != account_id:
                raise PortfolioUpdateError("portfolio_update_position_match_not_unique")
        else:
            matches = [row for row in current_positions if row.get("account_id") == account_id
                       and row.get("asset", {}).get("symbol") == change["symbol"] and change["symbol"]]
            if len(matches) != 1:
                raise PortfolioUpdateError("portfolio_update_position_match_not_unique")
            current_row = matches[0]
        position_id = str(current_row["position_id"])
        if position_id in claimed_positions:
            raise PortfolioUpdateError("portfolio_update_position_change_duplicate")
        claimed_positions.add(position_id)
        row = copy.deepcopy(current_row)
        if "quantity" in change:
            quantity = _number(change["quantity"], code="portfolio_update_position_quantity_invalid", nonnegative=True)
            if quantity != current_row["quantity"]:
                for field in HOLDING_VALUE_FIELDS:
                    row.pop(field, None)
            row.update(quantity=quantity, quantity_status="confirmed", quantity_basis="user_confirmation",
                       state="closed" if quantity == 0 else "open")
        try:
            values = validate_holding_values(change, currency=str(current_row.get("asset", {}).get("currency") or ""),
                observed_at=snapshot_effective_at, source_ref=source_ref)
        except ValueError as exc:
            raise PortfolioUpdateError("portfolio_update_position_values_invalid") from exc
        row.update(values)
        row.update(authority="portfolio_fact", observed_at=copy.deepcopy(snapshot_effective_at),
                   source_evidence=_merge_evidence(row.get("source_evidence"), source_ref))
        observed_positions_by_id[position_id] = row
        touched_holdings.add(account_id)

    cash_by_id = {
        str(row.get("cash_id") or ""): row
        for row in current_cash
        if row.get("cash_id")
    }
    if len(cash_by_id) != len([row for row in current_cash if row.get("cash_id")]):
        raise PortfolioUpdateError("portfolio_update_cash_identity_ambiguous")
    for change in cash_changes:
        if not isinstance(change, Mapping) or set(change) != {"cash_id", "amount"}:
            raise PortfolioUpdateError("portfolio_update_cash_change_invalid")
        cash_id = str(change.get("cash_id") or "")
        if not cash_id or cash_id in seen_cash:
            raise PortfolioUpdateError("portfolio_update_cash_change_duplicate")
        seen_cash.add(cash_id)
        current_row = _resolve_reference(current_cash, "cash_id", cash_id, code="portfolio_update_cash_match_missing",
                                         ambiguous_code="portfolio_update_cash_match_ambiguous")
        internal_id = str(current_row["cash_id"])
        if internal_id in seen_cash and internal_id != cash_id:
            raise PortfolioUpdateError("portfolio_update_cash_change_duplicate")
        seen_cash.add(internal_id)
        account_id = str(current_row.get("account_id") or "")
        if not account_id or account_id not in accounts:
            raise PortfolioUpdateError("portfolio_update_cash_account_invalid")
        amount = _number(change.get("amount"), code="portfolio_update_cash_amount_invalid")
        row = copy.deepcopy(current_row)
        value = row.get("value")
        if not isinstance(value, dict):
            raise PortfolioUpdateError("portfolio_update_cash_value_invalid")
        current_amount = _number(value.get("amount"), code="portfolio_update_cash_amount_invalid")
        if float(current_amount) == float(amount):
            continue
        value["amount"] = amount
        value["value_basis"] = "observed"
        value["as_of"] = copy.deepcopy(snapshot_effective_at)
        row["value"] = value
        row["authority"] = "portfolio_fact"
        row["source_evidence"] = _merge_evidence(row.get("source_evidence"), source_ref)
        row["observed_at"] = copy.deepcopy(snapshot_effective_at)
        observed_cash.append(row)
        touched_cash.add(account_id)

    if not observed_positions_by_id and not observed_cash:
        raise PortfolioUpdateError("portfolio_update_no_effect")

    touched_accounts = touched_holdings | touched_cash
    account_rows = [copy.deepcopy(accounts[account_id]) for account_id in sorted(touched_accounts)]
    completeness_accounts = [
        {
            "account_id": account_id,
            "holdings": "partial" if account_id in touched_holdings else "unknown",
            "cash": "partial" if account_id in touched_cash else "unknown",
            "observed_at": copy.deepcopy(snapshot_effective_at),
        }
        for account_id in sorted(touched_accounts)
    ]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "portfolio_id": current["portfolio_id"],
        "display_name": str(current.get("display_name") or current["portfolio_id"]),
        "generated_at": copy.deepcopy(snapshot_effective_at),
        "accounts": account_rows,
        "positions": list(observed_positions_by_id.values()),
        "cash": observed_cash,
        "transactions": [],
        "policies": copy.deepcopy(current.get("policies") or []),
        "completeness": {
            "holdings": "partial" if touched_holdings else "unknown",
            "cash": "partial" if touched_cash else "unknown",
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "unknown",
            "accounts": completeness_accounts,
        },
        "migration_gaps": [],
    }


def build_portfolio_update_preview(
    request: Mapping[str, Any],
    *,
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    identity_scope_account_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build a reconciliation candidate from a full observation or narrow updates.

    This function is read-only. It never persists a checkpoint or reconciliation.
    """

    if not isinstance(request, Mapping):
        raise PortfolioUpdateError("portfolio_update_request_invalid")
    allowed = {
        "request_version",
        "portfolio_id",
        "snapshot_effective_at",
        "source_ref",
        "observed_portfolio",
        "changes",
        "created_at",
    }
    if set(request) - allowed or request.get("request_version") != 1:
        raise PortfolioUpdateError("portfolio_update_request_fields_invalid")
    portfolio_id = str(request.get("portfolio_id") or "")
    source_ref = str(request.get("source_ref") or "")
    snapshot_effective_at = request.get("snapshot_effective_at")
    created_at = copy.deepcopy(request["created_at"]) if request.get("created_at") is not None else timepoint()
    if not portfolio_id or not source_ref or not isinstance(snapshot_effective_at, dict):
        raise PortfolioUpdateError("portfolio_update_request_identity_invalid")
    full = request.get("observed_portfolio")
    changes = request.get("changes")
    if (full is None) == (changes is None):
        raise PortfolioUpdateError("portfolio_update_request_mode_invalid")
    if full is not None:
        if not isinstance(full, dict) or full.get("portfolio_id") != portfolio_id:
            raise PortfolioUpdateError("portfolio_update_observation_portfolio_mismatch")
        observed = copy.deepcopy(full)
    else:
        if not isinstance(changes, Mapping):
            raise PortfolioUpdateError("portfolio_update_changes_invalid")
        current = _current_at_snapshot(
            checkpoint_store,
            write_store,
            portfolio_id=portfolio_id,
            snapshot_effective_at=snapshot_effective_at,
            generated_at=created_at,
        )
        observed = _narrow_observation(
            current,
            changes,
            snapshot_effective_at=snapshot_effective_at,
            source_ref=source_ref,
        )
    try:
        candidate = build_reconciliation_candidate_from_latest_checkpoint(
            checkpoint_store,
            write_store,
            observed,
            snapshot_effective_at=snapshot_effective_at,
            source_ref=source_ref,
            created_at=created_at,
            identity_scope_account_ids=identity_scope_account_ids,
        )
        validate_reconciliation_candidate(candidate)
    except ReconciliationError as exc:
        raise PortfolioUpdateError(exc.code, details=exc.details) from exc
    return {
        "status": "review_required",
        "candidate": candidate,
        "review": portfolio_update_review(candidate),
    }


def approval_phrase(candidate: Mapping[str, Any]) -> str:
    if not isinstance(candidate, dict):
        raise PortfolioUpdateError("portfolio_update_candidate_invalid")
    try:
        validate_reconciliation_candidate(candidate)
    except ReconciliationError as exc:
        raise PortfolioUpdateError(exc.code, details=exc.details) from exc
    return f"APPROVE PORTFOLIO UPDATE {str(candidate['candidate_digest'])[:12]}"


def portfolio_update_review(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise PortfolioUpdateError("portfolio_update_candidate_invalid")
    try:
        validate_reconciliation_candidate(candidate)
    except ReconciliationError as exc:
        raise PortfolioUpdateError(exc.code, details=exc.details) from exc
    return {
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "snapshot_effective_at": copy.deepcopy(candidate.get("snapshot_effective_at")),
        "summary": copy.deepcopy(candidate.get("summary")),
        "differences": copy.deepcopy(candidate.get("differences")),
        "gaps": copy.deepcopy(candidate.get("gaps")),
        "approval_phrase": approval_phrase(candidate),
    }


def accept_portfolio_update(
    candidate: dict[str, Any],
    approval: Mapping[str, Any],
    *,
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    approval_verifier: ApprovalVerifier | None,
) -> dict[str, Any]:
    """Persist an accepted reconciliation only after exact approval verification.

    A transport (local TTY, future MCP interaction, etc.) must inject the
    verifier. Caller-supplied confirmation text alone is never trusted.
    """

    try:
        validate_reconciliation_candidate(candidate)
    except ReconciliationError as exc:
        raise PortfolioUpdateError(exc.code, details=exc.details) from exc
    if not isinstance(approval, Mapping):
        raise PortfolioUpdateError("portfolio_update_approval_invalid")
    expected_fields = {
        "candidate_id",
        "candidate_digest",
        "confirmation",
        "approved_at",
        "approval_ref",
    }
    if set(approval) != expected_fields:
        raise PortfolioUpdateError("portfolio_update_approval_fields_invalid")
    if approval.get("candidate_id") != candidate["candidate_id"] or approval.get("candidate_digest") != candidate["candidate_digest"]:
        raise PortfolioUpdateError("portfolio_update_approval_candidate_mismatch")
    if approval.get("confirmation") != approval_phrase(candidate):
        raise PortfolioUpdateError("portfolio_update_approval_confirmation_mismatch")
    if not isinstance(approval.get("approved_at"), dict) or not str(approval.get("approval_ref") or ""):
        raise PortfolioUpdateError("portfolio_update_approval_invalid")
    if approval_verifier is None:
        raise PortfolioUpdateError("portfolio_update_approval_unverified")
    approval_value = copy.deepcopy(dict(approval))
    try:
        verification = approval_verifier(candidate, approval_value)
    except Exception as exc:
        raise PortfolioUpdateError("portfolio_update_approval_unverified") from exc
    if not isinstance(verification, dict) or verification.get("verified") is not True or not str(verification.get("verification_ref") or ""):
        raise PortfolioUpdateError("portfolio_update_approval_unverified")
    verification_ref = str(verification["verification_ref"])

    acceptance = {
        "candidate_id": candidate["candidate_id"],
        "accepted_candidate_digest": candidate["candidate_digest"],
        "decision": "accept",
        "accepted_at": copy.deepcopy(approval_value["approved_at"]),
    }

    def bound_verifier(candidate_value: dict[str, Any], acceptance_value: dict[str, Any]) -> dict[str, Any]:
        if digest(candidate_value) != digest(candidate):
            return {"verified": False, "verification_ref": None}
        if acceptance_value != acceptance:
            return {"verified": False, "verification_ref": None}
        return {"verified": True, "verification_ref": verification_ref}

    try:
        return accept_reconciliation_candidate(
            candidate,
            acceptance,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            acceptance_verifier=bound_verifier,
        )
    except ReconciliationError as exc:
        raise PortfolioUpdateError(exc.code, details=exc.details) from exc


def new_approval(
    candidate: dict[str, Any],
    *,
    approval_ref: str,
    approved_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct the exact approval object; this does not verify user approval."""

    if not approval_ref:
        raise PortfolioUpdateError("portfolio_update_approval_ref_missing")
    return {
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "confirmation": approval_phrase(candidate),
        "approved_at": copy.deepcopy(approved_at or timepoint()),
        "approval_ref": approval_ref,
    }


__all__ = [
    "ApprovalVerifier",
    "PortfolioUpdateError",
    "accept_portfolio_update",
    "approval_phrase",
    "build_portfolio_update_preview",
    "new_approval",
    "portfolio_update_review",
]
