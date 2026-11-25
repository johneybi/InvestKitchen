from __future__ import annotations

import copy
import math
import re
from collections import defaultdict
from typing import Any, Callable, Mapping

from protocol.v1.adapters.common import PROTOCOL_VERSION, digest, public_reference, timepoint
from protocol.v1.runtime.asset_identity import canonical_asset_identity
from protocol.v1.runtime.holding_observations import HOLDING_VALUE_FIELDS, validate_holding_values
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    checkpoint_time_is_later,
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
HeldRows = dict[str, dict[str, list[str]]]


class PortfolioUpdateError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = copy.deepcopy(details) if details else None


def _number(value: Any, *, code: str, nonnegative: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioUpdateError(code)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
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
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    return current, projection["checkpoint"]


def _new_listed_position(
    change: Any,
    *,
    portfolio_id: str,
    accounts: Mapping[str, Any],
    snapshot_effective_at: dict[str, Any],
    source_ref: str,
) -> dict[str, Any]:
    fields = {"quantity", *HOLDING_VALUE_FIELDS}
    if (not isinstance(change, Mapping) or set(change) - {"account_id", "asset", *fields}
            or not (set(change) & fields)):
        raise PortfolioUpdateError("portfolio_update_position_addition_invalid")
    account_id = change.get("account_id")
    if not isinstance(account_id, str) or account_id not in accounts:
        raise PortfolioUpdateError("portfolio_update_position_addition_account_unknown")
    asset = change.get("asset")
    if (not isinstance(asset, dict)
            or set(asset) != {"asset_type", "symbol", "venue", "currency", "display_name"}
            or asset["asset_type"] not in ("stock", "etf")
            or any(not isinstance(value, str) for value in asset.values())
            or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,31}", asset["symbol"])
            or not re.fullmatch(r"[A-Z0-9][A-Z0-9._\-]{0,31}", asset["venue"])
            or not re.fullmatch(r"[A-Z]{3}", asset["currency"])
            or not asset["display_name"].strip()
            or len(asset["display_name"]) > 200):
        raise PortfolioUpdateError("portfolio_update_position_addition_asset_invalid")
    identity = canonical_asset_identity(asset)
    quantity = None
    if "quantity" in change:
        quantity = _number(change["quantity"], code="portfolio_update_position_quantity_invalid", nonnegative=True)
        if quantity == 0:
            raise PortfolioUpdateError("portfolio_update_position_quantity_invalid")
    try:
        values = validate_holding_values(change, currency=asset["currency"],
                                         observed_at=snapshot_effective_at, source_ref=source_ref)
    except ValueError as exc:
        raise PortfolioUpdateError("portfolio_update_position_values_invalid") from exc
    return {
        "position_id": f"position:{digest([portfolio_id, account_id, identity])[:24]}",
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "asset": copy.deepcopy(asset),
        "quantity": quantity,
        "quantity_status": "confirmed" if quantity is not None else "unknown",
        "quantity_basis": "direct_observation" if quantity is not None else "unknown",
        "authority": "portfolio_fact",
        "state": "open",
        "observed_at": copy.deepcopy(snapshot_effective_at),
        "source_evidence": [source_ref],
        **values,
    }


def _position_for_observation(
    change: Mapping[str, Any], current_positions: list[dict[str, Any]], account_id: str,
) -> dict[str, Any]:
    keys = set(change)
    fields = {"quantity", *HOLDING_VALUE_FIELDS}
    if (keys - {"account_id", "symbol", "position_id", *fields}
            or not (keys & fields) or ("symbol" in keys) == ("position_id" in keys)):
        raise PortfolioUpdateError("portfolio_update_position_observation_invalid")
    selector = change.get("position_id", change.get("symbol"))
    if not isinstance(selector, str) or not selector:
        raise PortfolioUpdateError("portfolio_update_position_identity_invalid")
    code = "portfolio_update_position_match_not_unique"
    if "position_id" in change:
        row = _resolve_reference(current_positions, "position_id", selector, code=code)
        if row.get("account_id") == account_id:
            return row
    else:
        matches = [row for row in current_positions if row.get("account_id") == account_id
                   and row.get("asset", {}).get("symbol") == selector]
        if len(matches) == 1:
            return matches[0]
    raise PortfolioUpdateError(code)


def _holding_values(
    change: Mapping[str, Any], currency: str, observed_at: dict[str, Any], source_ref: str,
) -> dict[str, Any]:
    try:
        return validate_holding_values(change, currency=currency, observed_at=observed_at, source_ref=source_ref)
    except ValueError as exc:
        raise PortfolioUpdateError("portfolio_update_position_values_invalid") from exc


def _reported_values(row: Mapping[str, Any]) -> dict[str, Any]:
    """Compare economic fields independently of provenance and observation time."""
    values = {field: row[field] for field in ("quantity", "amount") if field in row}
    for field in HOLDING_VALUE_FIELDS:
        if field not in row:
            continue
        if field == "valuation":
            values[field] = {key: value for key, value in row[field].items()
                             if key not in {"source_evidence", "observed_at"}}
        else:
            values[field] = [{key: value for key, value in item.items()
                              if key not in {"as_of", "source_evidence"}} for item in row[field]]
    return values


def _merge_reported_values(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    merged: dict[str, Any] = {}
    conflicts: set[str] = set()
    for row in rows:
        for field, value in row.items():
            if field == "valuation":
                valuation = merged.setdefault(field, {})
                for key, item in value.items():
                    if valuation.setdefault(key, item) != item:
                        conflicts.add(f"valuation.{key}")
            elif field == "asset":
                if canonical_asset_identity(merged.setdefault(field, value)) != canonical_asset_identity(value):
                    conflicts.add(field)
            elif merged.setdefault(field, value) != value:
                conflicts.add(field)
    return merged, sorted(conflicts)


def _batch_row_selector(domain: str, identity: str) -> dict[str, str]:
    if domain == "addition":
        return {"symbol": identity}
    return {f"{domain}_id": public_reference(identity)}


def _batch_changes(
    current: dict[str, Any], changes: Mapping[str, Any], account_id: str,
    *, snapshot_effective_at: dict[str, Any], source_ref: str, prior_held: HeldRows | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], HeldRows, list[dict[str, Any]]]:
    """Reduce one account's extracted rows to the existing narrow update format."""
    accounts = {row["account_id"]: row for row in current["accounts"]}
    if not isinstance(account_id, str) or account_id not in accounts:
        raise PortfolioUpdateError("portfolio_update_reconcile_account_invalid")
    collections = ("position_observations", "position_additions", "cash_amounts")
    if set(changes) - set(collections) or not changes:
        raise PortfolioUpdateError("portfolio_update_changes_fields_invalid")
    positions = current["positions"]
    cash = current["cash"]
    target_positions = {row["position_id"]: row for row in positions if row["account_id"] == account_id}
    target_cash = {row["cash_id"]: row for row in cash if row["account_id"] == account_id}
    groups: dict[tuple[str, str], list[tuple[str, int, dict[str, Any]]]] = defaultdict(list)
    for collection in collections:
        rows = changes.get(collection, [])
        if not isinstance(rows, list) or len(rows) > 1000:
            raise PortfolioUpdateError("portfolio_update_changes_invalid")
        for index, row in enumerate(rows, 1):
            if not isinstance(row, Mapping):
                raise PortfolioUpdateError("portfolio_update_changes_invalid")
            if collection == "cash_amounts":
                if set(row) != {"cash_id", "amount"} or not isinstance(row["cash_id"], str):
                    raise PortfolioUpdateError("portfolio_update_cash_change_invalid")
                saved = _resolve_reference(cash, "cash_id", row["cash_id"], code="portfolio_update_cash_match_missing")
                if saved["account_id"] != account_id:
                    raise PortfolioUpdateError("portfolio_update_reconcile_account_mismatch")
                key = ("cash", saved["cash_id"])
                values = {"amount": _number(row["amount"], code="portfolio_update_cash_amount_invalid")}
            elif row.get("account_id") != account_id:
                raise PortfolioUpdateError("portfolio_update_reconcile_account_mismatch")
            elif collection == "position_additions":
                new = _new_listed_position(row, portfolio_id=current["portfolio_id"], accounts=accounts,
                                           snapshot_effective_at=snapshot_effective_at, source_ref=source_ref)
                values = _reported_values(new)
                if "quantity" not in row:
                    values.pop("quantity")
                symbol = new["asset"]["symbol"]
                matches = [saved for saved in target_positions.values()
                           if (saved["asset"].get("symbol") or "").strip().upper() == symbol]
                if matches:
                    if len(matches) != 1 or canonical_asset_identity(matches[0]["asset"]) != canonical_asset_identity(new["asset"]):
                        raise PortfolioUpdateError("portfolio_update_position_addition_already_exists")
                    key = ("position", matches[0]["position_id"])
                else:
                    key = ("addition", symbol)
                    values["asset"] = new["asset"]
            else:
                saved = _position_for_observation(row, positions, account_id)
                key = ("position", saved["position_id"])
                values = _reported_values(_holding_values(row, saved["asset"]["currency"], snapshot_effective_at, source_ref))
                if "quantity" in row:
                    values["quantity"] = _number(row["quantity"], code="portfolio_update_position_quantity_invalid",
                                                 nonnegative=True)
            groups[key].append((collection, index, values))
    if not groups:
        raise PortfolioUpdateError("portfolio_update_changes_empty")

    normalized: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unconfirmed = []
    held = dict(prior_held or {})
    resolved = []
    shown_conflicts = set()
    for (domain, identity), rows in groups.items():
        values, conflicts = _merge_reported_values([values for _, _, values in rows])
        row_key = f"{domain}:{identity}"
        fields = sorted((set(values) - {"valuation"}) | {
            f"valuation.{field}" for field in values.get("valuation", {})
        })
        selector = _batch_row_selector(domain, identity)
        if domain == "position":
            saved_values = _reported_values(target_positions[identity])
            saved_valuation = saved_values.get("valuation", {})
            if "valuation" in values:
                saved_values["valuation"] = {key: saved_valuation.get(key) for key in values["valuation"]}
            collection = "position_observations"
        elif domain == "cash":
            saved_values = {"amount": target_cash[identity]["value"]["amount"]}
            collection = "cash_amounts"
        else:
            saved_values = {}
            collection = "position_additions"
        changed = {field: value for field, value in values.items() if saved_values.get(field) != value}
        if conflicts:
            shown_conflicts.add(row_key)
            if prior_held is None:
                held[row_key] = {"reported": fields, "disputed": conflicts}
            unconfirmed.append({**selector, "reason": "conflicting_values", "fields": conflicts,
                                "rows": [{"collection": name, "index": index} for name, index, _ in rows]})
            continue
        resolving = prior_held is not None and row_key in prior_held
        if resolving:
            scope = prior_held[row_key]
            if not (set(scope["disputed"]) <= set(fields) <= set(scope["reported"])):
                raise PortfolioUpdateError("portfolio_update_held_row_fields_mismatch",
                                           details={"disputed": scope["disputed"], "reported": scope["reported"]})
            del held[row_key]
            resolved.append({**selector, "fields": fields})
        elif prior_held is not None and changed:
            raise PortfolioUpdateError("checkpoint_effective_at_not_increasing")
        if changed or resolving:
            if domain == "addition":
                normalized[collection].append({"account_id": account_id, **values})
            elif domain == "cash":
                normalized[collection].append({**selector, **values})
            else:
                # A changed quantity invalidates old values. Carry all directly
                # reported replacements, even if a replacement amount stayed equal.
                replacements = values if resolving or "quantity" in changed else changed
                normalized[collection].append({"account_id": account_id, **selector, **replacements})

    if resolved:
        # The correction covers just the contested rows. Read the remaining
        # unresolved scope from the approved snapshot, not the short retry.
        for row_key in sorted(held.keys() - shown_conflicts):
            domain, identity = row_key.split(":", 1)
            unconfirmed.append({**_batch_row_selector(domain, identity), "reason": "conflicting_values",
                                "fields": held[row_key]["disputed"]})
    else:
        unconfirmed.extend({"position_id": public_reference(identity), "reason": "not_observed"}
                           for identity, row in target_positions.items()
                           if row.get("state") != "closed" and ("position", identity) not in groups)
        unconfirmed.extend({"cash_id": public_reference(identity), "reason": "not_observed"}
                           for identity in target_cash if ("cash", identity) not in groups)
    return dict(normalized), unconfirmed, held, resolved


def _narrow_observation(
    current: dict[str, Any],
    changes: Mapping[str, Any],
    *,
    snapshot_effective_at: dict[str, Any],
    source_ref: str,
    record_matching_cash: bool = False,
) -> dict[str, Any]:
    allowed = {"position_quantities", "position_observations", "position_additions", "cash_amounts"}
    if set(changes) - allowed:
        raise PortfolioUpdateError("portfolio_update_changes_fields_invalid")
    position_changes = changes.get("position_quantities", [])
    position_observations = changes.get("position_observations", [])
    position_additions = changes.get("position_additions", [])
    cash_changes = changes.get("cash_amounts", [])
    if not all(isinstance(rows, list) for rows in (position_changes, position_observations, position_additions, cash_changes)):
        raise PortfolioUpdateError("portfolio_update_changes_invalid")
    if len(position_observations) > 1000 or len(position_additions) > 1000:
        raise PortfolioUpdateError("portfolio_update_changes_invalid")
    if not position_changes and not position_observations and not position_additions and not cash_changes:
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
        current_quantity = matches[0].get("quantity")
        if current_quantity is not None:
            current_quantity = _number(
                current_quantity, code="portfolio_update_position_quantity_invalid", nonnegative=True,
            )
            if current_quantity == quantity:
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

    # Legacy records can disagree on venue or asset type. Reject a reused
    # account/symbol even when those fields would give it a new canonical key.
    occupied_symbols = {
        (row.get("account_id"), asset["symbol"].strip().upper())
        for row in current_positions
        if isinstance(asset := row.get("asset"), dict) and isinstance(asset.get("symbol"), str)
    } if position_additions else set()
    for change in position_additions:
        row = _new_listed_position(
            change, portfolio_id=current["portfolio_id"], accounts=accounts,
            snapshot_effective_at=snapshot_effective_at, source_ref=source_ref,
        )
        account_id = row["account_id"]
        key = (account_id, row["asset"]["symbol"])
        position_id = row["position_id"]
        if key in occupied_symbols or position_id in position_by_id or position_id in observed_positions_by_id:
            raise PortfolioUpdateError("portfolio_update_position_addition_already_exists")
        occupied_symbols.add(key)
        observed_positions_by_id[position_id] = row
        touched_holdings.add(account_id)

    for change in position_observations:
        if not isinstance(change, Mapping):
            raise PortfolioUpdateError("portfolio_update_position_observation_invalid")
        account_id = change.get("account_id")
        if not isinstance(account_id, str) or account_id not in accounts:
            raise PortfolioUpdateError("portfolio_update_position_identity_invalid")
        current_row = _position_for_observation(change, current_positions, account_id)
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
        values = _holding_values(change, str(current_row.get("asset", {}).get("currency") or ""),
                                 snapshot_effective_at, source_ref)
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
        if current_amount == amount and not record_matching_cash:
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
        "reconcile_account_id",
    }
    if set(request) - allowed or request.get("request_version") != 1:
        raise PortfolioUpdateError("portfolio_update_request_fields_invalid")
    portfolio_id = str(request.get("portfolio_id") or "")
    source_ref = request.get("source_ref")
    snapshot_effective_at = request.get("snapshot_effective_at")
    created_at = copy.deepcopy(request["created_at"]) if request.get("created_at") is not None else timepoint()
    if (not portfolio_id or not isinstance(source_ref, str) or not source_ref.strip()
            or not isinstance(snapshot_effective_at, dict)):
        raise PortfolioUpdateError("portfolio_update_request_identity_invalid")
    full = request.get("observed_portfolio")
    changes = request.get("changes")
    reconcile_account = "reconcile_account_id" in request
    batch_review: dict[str, Any] = {}
    account_batch = None
    expected_checkpoint_id = None
    record_matching_cash = False
    if (full is None) == (changes is None):
        raise PortfolioUpdateError("portfolio_update_request_mode_invalid")
    if full is not None:
        if reconcile_account:
            raise PortfolioUpdateError("portfolio_update_request_mode_invalid")
        if not isinstance(full, dict) or full.get("portfolio_id") != portfolio_id:
            raise PortfolioUpdateError("portfolio_update_observation_portfolio_mismatch")
        observed = copy.deepcopy(full)
    else:
        if not isinstance(changes, Mapping):
            raise PortfolioUpdateError("portfolio_update_changes_invalid")
        current, latest = _current_at_snapshot(
            checkpoint_store,
            write_store,
            portfolio_id=portfolio_id,
            snapshot_effective_at=snapshot_effective_at,
            generated_at=created_at,
        )
        expected_checkpoint_id = latest["checkpoint_id"]
        if reconcile_account:
            account_id = request["reconcile_account_id"]
            prior = latest.get("account_batch") or {}
            prior_held = None
            if (snapshot_effective_at == latest["snapshot_effective_at"] and source_ref == latest.get("source_ref")
                    and account_id == prior.get("account_id")):
                prior_held = prior.get("held_rows")
            changes, unconfirmed, held, resolved = _batch_changes(
                current, changes, account_id, snapshot_effective_at=snapshot_effective_at, source_ref=source_ref,
                prior_held=prior_held,
            )
            batch_review = {"reconciliation": {"account_id": account_id, "unconfirmed": unconfirmed}}
            if resolved:
                batch_review["reconciliation"]["resolved"] = resolved
                record_matching_cash = True
            if held or resolved:
                account_batch = {"account_id": account_id, "parent_checkpoint_id": latest["checkpoint_id"], "held_rows": held}
            if not changes:
                return {
                    "status": "no_changes", "portfolio_id": portfolio_id,
                    "snapshot_effective_at": copy.deepcopy(snapshot_effective_at),
                    "differences": [], **batch_review,
                }
            try:
                if not resolved and not checkpoint_time_is_later(snapshot_effective_at, latest["snapshot_effective_at"]):
                    raise PortfolioUpdateError("checkpoint_effective_at_not_increasing")
            except CheckpointError as exc:
                raise PortfolioUpdateError(exc.code) from exc
        observed = _narrow_observation(
            current,
            changes,
            snapshot_effective_at=snapshot_effective_at,
            source_ref=source_ref,
            record_matching_cash=record_matching_cash,
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
            account_batch=account_batch,
            expected_checkpoint_id=expected_checkpoint_id,
        )
        validate_reconciliation_candidate(candidate)
    except ReconciliationError as exc:
        raise PortfolioUpdateError(exc.code, details=exc.details) from exc
    if expected_checkpoint_id is not None and candidate["current_portfolio_digest"] != digest(current):
        raise PortfolioUpdateError("preview_stale")
    return {
        "status": "review_required",
        "candidate": candidate,
        "review": {**portfolio_update_review(candidate), **batch_review},
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
