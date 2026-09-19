from __future__ import annotations

import copy
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Iterable

from protocol.v1.adapters.common import PROTOCOL_VERSION, digest, timepoint
from protocol.v1.runtime.asset_identity import (
    asset_identity_conflict,
    asset_identity_issue,
    canonical_asset_identity,
    public_asset_identity,
)
from protocol.v1.runtime.native_write_store import NativeWriteStore, StoreConflict, StoreCorrupt
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    bind_reconciliation_acceptance,
    build_checkpoint,
    persist_checkpoint,
    project_from_latest_checkpoint,
    project_from_latest_checkpoint_through_cursor,
)


AcceptanceVerifier = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class ReconciliationError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = copy.deepcopy(details) if details else None


def _point(value: Any, *, code: str) -> datetime:
    if not isinstance(value, dict) or not isinstance(value.get("value"), str):
        raise ReconciliationError(code)
    raw = value["value"]
    try:
        if "T" in raw:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            return parsed.astimezone(timezone.utc)
        return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=timezone.utc)
    except ValueError as exc:
        raise ReconciliationError(code) from exc


def _point_is_after(left: Any, right: Any, *, code: str) -> bool:
    left_point = _point(left, code=code)
    right_point = _point(right, code=code)
    left_raw = str(left["value"])
    right_raw = str(right["value"])
    if "T" not in left_raw or "T" not in right_raw:
        if left_point.date() == right_point.date():
            return False
    return left_point > right_point


def _point_is_unambiguously_at_or_before(left: Any, right: Any, *, code: str) -> bool:
    left_point = _point(left, code=code)
    right_point = _point(right, code=code)
    left_raw = str(left["value"])
    right_raw = str(right["value"])
    if "T" not in left_raw or "T" not in right_raw:
        if left_point.date() == right_point.date():
            return False
    return left_point <= right_point


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


def _validate_portfolio_shape(value: Any, *, expected_portfolio_id: str | None = None) -> str:
    if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
        raise ReconciliationError("reconciliation_portfolio_invalid")
    portfolio_id = str(value.get("portfolio_id") or "")
    if not portfolio_id or (expected_portfolio_id is not None and portfolio_id != expected_portfolio_id):
        raise ReconciliationError("reconciliation_portfolio_mismatch")
    for field in ("accounts", "positions", "cash", "transactions"):
        if not isinstance(value.get(field), list):
            raise ReconciliationError(f"reconciliation_{field}_invalid")
    if not isinstance(value.get("completeness"), dict):
        raise ReconciliationError("reconciliation_completeness_invalid")
    return portfolio_id


def _position_key(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    account_id = str(row.get("account_id") or "")
    identity = canonical_asset_identity(row.get("asset"))
    if not account_id or identity is None:
        issue = asset_identity_issue(row.get("asset"))
        if not account_id:
            issue = {
                "missing_fields": ["account_id", *issue.get("missing_fields", [])],
                "reason": "position_scope_or_asset_identity_incomplete",
                "repair_hint": issue.get("repair_hint"),
            }
        raise ReconciliationError(
            "reconciliation_position_identity_incomplete",
            details={
                "portfolio_id": str(row.get("portfolio_id") or "") or None,
                "position_id": str(row.get("position_id") or "") or None,
                "account_id": account_id or None,
                **issue,
            },
        )
    return account_id, identity


def _position_identity_public(key: tuple[str, tuple[str, ...]]) -> dict[str, Any]:
    account_id, identity = key
    return {"account_id": account_id, **public_asset_identity(identity)}


def _cash_key(row: dict[str, Any]) -> str:
    cash_id = str(row.get("cash_id") or "")
    if not cash_id:
        raise ReconciliationError("reconciliation_cash_identity_incomplete")
    return cash_id


def _account_index(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReconciliationError("reconciliation_account_invalid")
        account_id = str(row.get("account_id") or "")
        if not account_id or account_id in result:
            raise ReconciliationError("reconciliation_account_identity_ambiguous")
        result[account_id] = row
    return result


def _index_unique(rows: Iterable[dict[str, Any]], key_fn: Callable[[dict[str, Any]], Any], *, code: str) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReconciliationError(code)
        key = key_fn(row)
        if key in result:
            raise ReconciliationError(code)
        result[key] = row
    return result


def _account_completeness(portfolio: dict[str, Any]) -> dict[str, dict[str, Any]]:
    completeness = portfolio.get("completeness") if isinstance(portfolio.get("completeness"), dict) else {}
    rows = completeness.get("accounts") if isinstance(completeness.get("accounts"), list) else []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReconciliationError("reconciliation_account_completeness_invalid")
        account_id = str(row.get("account_id") or "")
        if not account_id or account_id in result:
            raise ReconciliationError("reconciliation_account_completeness_ambiguous")
        result[account_id] = row
    return result


def _field_changes(current: dict[str, Any], observed: dict[str, Any], fields: Iterable[str]) -> list[dict[str, Any]]:
    changes = []
    for field in fields:
        if current.get(field) != observed.get(field):
            changes.append({
                "field": field,
                "current": copy.deepcopy(current.get(field)),
                "observed": copy.deepcopy(observed.get(field)),
            })
    return changes


def _merge_position_observation(current: dict[str, Any], observed: dict[str, Any], *, changed: bool) -> dict[str, Any]:
    """Apply state-bearing observation fields without replacing canonical asset metadata."""

    if not changed:
        return copy.deepcopy(current)
    row = copy.deepcopy(current)
    for field in ("quantity", "quantity_status", "quantity_basis", "authority", "state"):
        if field in observed:
            row[field] = copy.deepcopy(observed[field])
    for field in ("cost_basis_values", "avg_cost_values", "observed_at", "effective_at", "recorded_at"):
        if field in observed:
            row[field] = copy.deepcopy(observed[field])
    evidence = [str(value) for value in row.get("source_evidence") or [] if str(value)]
    for value in observed.get("source_evidence") or []:
        text = str(value)
        if text and text not in evidence:
            evidence.append(text)
    row["source_evidence"] = evidence
    return row


def _merge_cash_observation(current: dict[str, Any], observed: dict[str, Any], *, changed: bool) -> dict[str, Any]:
    if not changed:
        return copy.deepcopy(current)
    row = copy.deepcopy(current)
    for field in ("provider_label", "value", "authority", "observed_at", "recorded_at"):
        if field in observed:
            row[field] = copy.deepcopy(observed[field])
    evidence = [str(value) for value in row.get("source_evidence") or [] if str(value)]
    for value in observed.get("source_evidence") or []:
        text = str(value)
        if text and text not in evidence:
            evidence.append(text)
    row["source_evidence"] = evidence
    return row


def _difference(
    domain: str,
    change_type: str,
    identity: dict[str, Any],
    field_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "domain": domain,
        "change_type": change_type,
        "identity": identity,
        "field_changes": field_changes or [],
    }


def _rollup_state(values: Iterable[str]) -> str:
    states = [str(value) for value in values]
    if not states:
        return "unknown"
    if "conflicted" in states:
        return "conflicted"
    if "stale" in states:
        return "stale"
    if all(value == "complete" for value in states):
        return "complete"
    if all(value == "unknown" for value in states):
        return "unknown"
    return "partial"


def _candidate_identity_material(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in candidate.items()
        if key not in {"candidate_id", "candidate_digest"}
    }


def _validate_candidate(candidate: dict[str, Any]) -> None:
    if candidate.get("protocol_version") != PROTOCOL_VERSION or candidate.get("reconciliation_version") != 1:
        raise ReconciliationError("reconciliation_candidate_version_invalid")
    if candidate.get("status") != "review_required" or candidate.get("acceptance_required") is not True:
        raise ReconciliationError("reconciliation_candidate_state_invalid")
    expected_digest = digest(_candidate_identity_material(candidate))
    if candidate.get("candidate_digest") != expected_digest:
        raise ReconciliationError("reconciliation_candidate_digest_mismatch")
    expected_id = "portfolio-reconciliation:" + expected_digest[:24]
    if candidate.get("candidate_id") != expected_id:
        raise ReconciliationError("reconciliation_candidate_identity_mismatch")


def validate_reconciliation_candidate(candidate: dict[str, Any]) -> None:
    """Validate one review candidate without mutating any persistent state."""

    _validate_candidate(candidate)


def _journal_cursor_for_snapshot(
    write_store: NativeWriteStore,
    *,
    portfolio_id: str,
    snapshot_effective_at: dict[str, Any],
) -> dict[str, Any]:
    try:
        entries = write_store.read_journal()
        if not entries:
            return write_store.journal_cursor(None)
        safe_commit_id: str | None = None
        previous_committed_at: dict[str, Any] | None = None
        for entry in entries:
            commit_id = str(entry.get("commit_id") or "")
            committed_at = entry.get("committed_at")
            if not commit_id:
                raise ReconciliationError("reconciliation_journal_corrupt")
            _point(committed_at, code="reconciliation_journal_commit_time_invalid")
            if previous_committed_at is not None and _point_is_after(
                previous_committed_at,
                committed_at,
                code="reconciliation_journal_commit_time_invalid",
            ):
                raise ReconciliationError("reconciliation_journal_commit_time_regression")
            previous_committed_at = committed_at
            if not _point_is_unambiguously_at_or_before(
                committed_at,
                snapshot_effective_at,
                code="reconciliation_journal_commit_time_invalid",
            ):
                break
            if entry.get("event_type") == "resource_commit" and entry.get("resource_type") == "transaction":
                resource = entry.get("resource")
                if not isinstance(resource, dict):
                    raise ReconciliationError("reconciliation_journal_corrupt")
                if resource.get("portfolio_id") == portfolio_id:
                    if not _point_is_unambiguously_at_or_before(
                        resource.get("effective_at"),
                        snapshot_effective_at,
                        code="reconciliation_transaction_effective_at_invalid",
                    ):
                        break
            safe_commit_id = commit_id
        return write_store.journal_cursor(safe_commit_id)
    except StoreConflict as exc:
        raise ReconciliationError("reconciliation_journal_changed_during_build") from exc
    except StoreCorrupt as exc:
        raise ReconciliationError("reconciliation_journal_corrupt") from exc


def journal_cursor_for_snapshot(
    write_store: NativeWriteStore,
    *,
    portfolio_id: str,
    snapshot_effective_at: dict[str, Any],
) -> dict[str, Any]:
    """Public read helper for selecting the safe journal prefix for an observation.

    Shadow/reconciliation callers use the same cursor semantics so they cannot
    accidentally include commits that happened after, or ambiguously overlap,
    the observed Portfolio snapshot.
    """

    return _journal_cursor_for_snapshot(
        write_store,
        portfolio_id=portfolio_id,
        snapshot_effective_at=snapshot_effective_at,
    )


def build_reconciliation_candidate(
    current_portfolio: dict[str, Any],
    observed_portfolio: dict[str, Any],
    write_store: NativeWriteStore,
    *,
    snapshot_effective_at: dict[str, Any],
    source_ref: str,
    cash_basis_refs: Iterable[dict[str, Any]] | None = None,
    short_allowed_account_ids: Iterable[str] | None = None,
    created_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    portfolio_id = _validate_portfolio_shape(current_portfolio)
    _validate_portfolio_shape(observed_portfolio, expected_portfolio_id=portfolio_id)
    _point(snapshot_effective_at, code="reconciliation_effective_at_invalid")
    if not source_ref:
        raise ReconciliationError("reconciliation_source_ref_missing")

    current = copy.deepcopy(current_portfolio)
    observed = copy.deepcopy(observed_portfolio)
    current_accounts = _account_index(current["accounts"])
    observed_accounts = _account_index(observed["accounts"])
    observed_completeness = _account_completeness(observed)
    observed_scope = set(observed_accounts) | set(observed_completeness)
    scoped_current_position_rows = [
        row
        for row in current["positions"]
        if isinstance(row, dict) and str(row.get("account_id") or "") in observed_scope
    ]
    current_positions = _index_unique(
        scoped_current_position_rows,
        _position_key,
        code="reconciliation_position_identity_ambiguous",
    )
    observed_positions = _index_unique(observed["positions"], _position_key, code="reconciliation_position_identity_ambiguous")
    scoped_current_cash_rows = [
        row
        for row in current["cash"]
        if isinstance(row, dict) and str(row.get("account_id") or "") in observed_scope
    ]
    current_cash = _index_unique(scoped_current_cash_rows, _cash_key, code="reconciliation_cash_identity_ambiguous")
    observed_cash = _index_unique(observed["cash"], _cash_key, code="reconciliation_cash_identity_ambiguous")

    for account_id, row in current_accounts.items():
        if row.get("portfolio_id") != portfolio_id:
            raise ReconciliationError(
                "reconciliation_current_account_portfolio_mismatch",
                details={"portfolio_id": portfolio_id, "account_id": account_id, "reason": "account_scope_mismatch"},
            )
    for row in current["positions"]:
        if not isinstance(row, dict):
            raise ReconciliationError("reconciliation_current_position_invalid")
        if row.get("portfolio_id") != portfolio_id:
            raise ReconciliationError("reconciliation_current_position_portfolio_mismatch")
        if str(row.get("account_id") or "") not in current_accounts:
            raise ReconciliationError("reconciliation_current_position_account_unknown")
    for row in current["cash"]:
        if not isinstance(row, dict):
            raise ReconciliationError("reconciliation_current_cash_invalid")
        if row.get("portfolio_id") != portfolio_id:
            raise ReconciliationError("reconciliation_current_cash_portfolio_mismatch")
        if str(row.get("account_id") or "") not in current_accounts:
            raise ReconciliationError("reconciliation_current_cash_account_unknown")
    for row in observed_positions.values():
        if row.get("portfolio_id") != portfolio_id:
            raise ReconciliationError("reconciliation_observed_position_portfolio_mismatch")
        if str(row.get("account_id") or "") not in observed_accounts:
            raise ReconciliationError("reconciliation_observed_position_account_unknown")
        source_evidence = row.get("source_evidence")
        if not isinstance(source_evidence, list) or not any(str(value) for value in source_evidence):
            raise ReconciliationError(
                "reconciliation_observed_position_source_evidence_missing",
                details={
                    "portfolio_id": portfolio_id,
                    "position_id": str(row.get("position_id") or "") or None,
                    "account_id": str(row.get("account_id") or "") or None,
                    "missing_fields": ["source_evidence"],
                    "reason": "observed_position_requires_source_evidence",
                    "repair_hint": "Attach the stable observation/source reference that directly supports this position.",
                },
            )
    for row in observed_cash.values():
        if row.get("portfolio_id") != portfolio_id:
            raise ReconciliationError("reconciliation_observed_cash_portfolio_mismatch")
        if str(row.get("account_id") or "") not in observed_accounts:
            raise ReconciliationError("reconciliation_observed_cash_account_unknown")
    for account_id in observed_completeness:
        if account_id not in observed_accounts and account_id not in current_accounts:
            raise ReconciliationError("reconciliation_observed_completeness_account_unknown")

    differences: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = [copy.deepcopy(gap) for gap in observed.get("migration_gaps") or [] if isinstance(gap, dict)]
    reconciled = copy.deepcopy(current)

    reconciled_accounts = _account_index(reconciled["accounts"])
    account_fields = ("display_name", "provider_id", "account_type", "base_currency", "role", "status", "constraints")
    for account_id, observed_account in observed_accounts.items():
        if observed_account.get("portfolio_id") != portfolio_id:
            raise ReconciliationError("reconciliation_account_portfolio_mismatch")
        current_account = current_accounts.get(account_id)
        if current_account is None:
            reconciled["accounts"].append(copy.deepcopy(observed_account))
            reconciled_accounts[account_id] = reconciled["accounts"][-1]
            differences.append(_difference("account", "added", {"account_id": account_id}))
        else:
            changes = _field_changes(current_account, observed_account, account_fields)
            if changes:
                reconciled_accounts[account_id].update(copy.deepcopy(observed_account))
                differences.append(_difference("account", "changed", {"account_id": account_id}, changes))

    all_account_ids = set(current_accounts) | set(observed_accounts)
    reconciled_position_rows: list[dict[str, Any]] = []
    for account_id in sorted(all_account_ids):
        if account_id not in observed_scope:
            reconciled_position_rows.extend(
                copy.deepcopy(row)
                for row in current["positions"]
                if isinstance(row, dict) and row.get("account_id") == account_id
            )
            continue
        status = observed_completeness.get(account_id, {})
        holdings_state = str(status.get("holdings") or "unknown")
        current_for_account = {key: row for key, row in current_positions.items() if key[0] == account_id}
        observed_for_account = {key: row for key, row in observed_positions.items() if key[0] == account_id}

        if holdings_state == "complete":
            for key, current_row in current_for_account.items():
                if key not in observed_for_account:
                    differences.append(_difference("position", "removed", _position_identity_public(key)))
            for key, observed_row in observed_for_account.items():
                current_row = current_for_account.get(key)
                if current_row is None:
                    differences.append(_difference("position", "added", _position_identity_public(key)))
                else:
                    if asset_identity_conflict(current_row.get("asset"), observed_row.get("asset")):
                        raise ReconciliationError("reconciliation_position_identity_conflict")
                    changes = _field_changes(
                        current_row,
                        observed_row,
                        ("quantity", "quantity_status", "quantity_basis", "cost_basis_values", "avg_cost_values", "authority", "state"),
                    )
                    if changes:
                        differences.append(_difference("position", "changed", _position_identity_public(key), changes))
                reconciled_position_rows.append(
                    copy.deepcopy(observed_row)
                    if current_row is None
                    else _merge_position_observation(current_row, observed_row, changed=bool(changes))
                )
        else:
            reconciled_for_account = {key: copy.deepcopy(row) for key, row in current_for_account.items()}
            for key, observed_row in observed_for_account.items():
                current_row = current_for_account.get(key)
                if current_row is None:
                    differences.append(_difference("position", "added", _position_identity_public(key)))
                else:
                    if asset_identity_conflict(current_row.get("asset"), observed_row.get("asset")):
                        raise ReconciliationError("reconciliation_position_identity_conflict")
                    changes = _field_changes(
                        current_row,
                        observed_row,
                        ("quantity", "quantity_status", "quantity_basis", "cost_basis_values", "avg_cost_values", "authority", "state"),
                    )
                    if changes:
                        differences.append(_difference("position", "changed", _position_identity_public(key), changes))
                reconciled_for_account[key] = (
                    copy.deepcopy(observed_row)
                    if current_row is None
                    else _merge_position_observation(current_row, observed_row, changed=bool(changes))
                )
            reconciled_position_rows.extend(reconciled_for_account.values())
            if account_id in observed_accounts or observed_for_account:
                gaps.append(_gap(
                    "reconciliation_holdings_incomplete",
                    "The observation does not declare holdings complete for this account.",
                    "Unobserved existing positions were retained instead of being interpreted as zero.",
                    scope={"account_id": account_id, "holdings": holdings_state},
                ))

    reconciled["positions"] = reconciled_position_rows

    reconciled_cash_rows: list[dict[str, Any]] = []
    for account_id in sorted(all_account_ids):
        if account_id not in observed_scope:
            reconciled_cash_rows.extend(
                copy.deepcopy(row)
                for row in current["cash"]
                if isinstance(row, dict) and row.get("account_id") == account_id
            )
            continue
        status = observed_completeness.get(account_id, {})
        cash_state = str(status.get("cash") or "unknown")
        current_for_account = {key: row for key, row in current_cash.items() if row.get("account_id") == account_id}
        observed_for_account = {key: row for key, row in observed_cash.items() if row.get("account_id") == account_id}
        for cash_id, observed_row in observed_for_account.items():
            current_row = current_cash.get(cash_id)
            if current_row is not None and (
                current_row.get("account_id") != observed_row.get("account_id")
                or current_row.get("currency") != observed_row.get("currency")
            ):
                raise ReconciliationError("reconciliation_cash_identity_conflict")

        if cash_state == "complete":
            for cash_id, current_row in current_for_account.items():
                if cash_id not in observed_for_account:
                    differences.append(_difference("cash", "removed", {"cash_id": cash_id, "account_id": account_id}))
            for cash_id, observed_row in observed_for_account.items():
                current_row = current_for_account.get(cash_id)
                if current_row is None:
                    differences.append(_difference("cash", "added", {"cash_id": cash_id, "account_id": account_id}))
                else:
                    changes = _field_changes(
                        current_row,
                        observed_row,
                        ("currency", "cash_kind", "provider_label", "value", "authority"),
                    )
                    if changes:
                        differences.append(_difference("cash", "changed", {"cash_id": cash_id, "account_id": account_id}, changes))
                reconciled_cash_rows.append(
                    copy.deepcopy(observed_row)
                    if current_row is None
                    else _merge_cash_observation(current_row, observed_row, changed=bool(changes))
                )
        else:
            reconciled_for_account = {key: copy.deepcopy(row) for key, row in current_for_account.items()}
            for cash_id, observed_row in observed_for_account.items():
                current_row = current_for_account.get(cash_id)
                if current_row is None:
                    differences.append(_difference("cash", "added", {"cash_id": cash_id, "account_id": account_id}))
                else:
                    changes = _field_changes(
                        current_row,
                        observed_row,
                        ("currency", "cash_kind", "provider_label", "value", "authority"),
                    )
                    if changes:
                        differences.append(_difference("cash", "changed", {"cash_id": cash_id, "account_id": account_id}, changes))
                reconciled_for_account[cash_id] = (
                    copy.deepcopy(observed_row)
                    if current_row is None
                    else _merge_cash_observation(current_row, observed_row, changed=bool(changes))
                )
            reconciled_cash_rows.extend(reconciled_for_account.values())
            if account_id in observed_accounts or observed_for_account:
                gaps.append(_gap(
                    "reconciliation_cash_incomplete",
                    "The observation does not declare cash complete for this account.",
                    "Unobserved existing cash rows were retained instead of being interpreted as zero.",
                    scope={"account_id": account_id, "cash": cash_state},
                ))

    reconciled["cash"] = reconciled_cash_rows

    if observed.get("transactions") != current.get("transactions") and observed.get("transactions"):
        differences.append(_difference("transactions", "ignored", {"portfolio_id": portfolio_id}))
        gaps.append(_gap(
            "reconciliation_transactions_not_ingested",
            "Observed transaction rows are not imported through Portfolio reconciliation.",
            "Canonical Transaction ingestion remains a separate path; current canonical transaction history was preserved.",
            scope={"portfolio_id": portfolio_id},
        ))
    reconciled["transactions"] = copy.deepcopy(current.get("transactions") or [])

    if observed.get("policies") is not None and observed.get("policies") != current.get("policies"):
        differences.append(_difference("policies", "ignored", {"portfolio_id": portfolio_id}))
        gaps.append(_gap(
            "reconciliation_policies_not_ingested",
            "Observed policy rows are not imported through Portfolio reconciliation.",
            "Current canonical policies were preserved.",
            scope={"portfolio_id": portfolio_id},
        ))
    reconciled["policies"] = copy.deepcopy(current.get("policies") or [])
    reconciled["generated_at"] = copy.deepcopy(snapshot_effective_at)

    completeness = copy.deepcopy(current.get("completeness") or {})
    account_rows = {
        str(row.get("account_id") or ""): copy.deepcopy(row)
        for row in completeness.get("accounts") or []
        if isinstance(row, dict) and row.get("account_id")
    }
    for account_id in sorted(all_account_ids):
        observed_row = observed_completeness.get(account_id)
        row = account_rows.get(account_id) or {
            "account_id": account_id,
            "holdings": "unknown",
            "cash": "unknown",
        }
        if observed_row is not None:
            row["holdings"] = str(observed_row.get("holdings") or "unknown")
            row["cash"] = str(observed_row.get("cash") or "unknown")
            row["observed_at"] = copy.deepcopy(snapshot_effective_at)
        account_rows[account_id] = row
    completeness["accounts"] = [account_rows[key] for key in sorted(account_rows)]
    completeness["holdings"] = _rollup_state(row["holdings"] for row in completeness["accounts"])
    completeness["cash"] = _rollup_state(row["cash"] for row in completeness["accounts"])
    reconciled["completeness"] = completeness
    reconciled["migration_gaps"] = list(current.get("migration_gaps") or []) + copy.deepcopy(gaps)

    created = copy.deepcopy(created_at or timepoint())
    if _point(created, code="reconciliation_created_at_invalid") < _point(
        snapshot_effective_at,
        code="reconciliation_effective_at_invalid",
    ):
        raise ReconciliationError("reconciliation_created_before_snapshot")
    cursor = _journal_cursor_for_snapshot(
        write_store,
        portfolio_id=portfolio_id,
        snapshot_effective_at=snapshot_effective_at,
    )
    try:
        checkpoint = build_checkpoint(
            reconciled,
            write_store,
            snapshot_effective_at=snapshot_effective_at,
            through_commit_id=cursor.get("through_commit_id"),
            source_kind="reconciliation",
            source_ref=source_ref,
            cash_basis_refs=cash_basis_refs,
            short_allowed_account_ids=short_allowed_account_ids,
            created_at=created,
        )
    except CheckpointError as exc:
        raise ReconciliationError(exc.code) from exc
    if checkpoint.get("write_cursor") != cursor:
        raise ReconciliationError("reconciliation_cursor_changed_during_build")

    candidate: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "reconciliation_version": 1,
        "candidate_id": "",
        "candidate_digest": "",
        "portfolio_id": portfolio_id,
        "created_at": created,
        "snapshot_effective_at": copy.deepcopy(snapshot_effective_at),
        "source_ref": source_ref,
        "status": "review_required",
        "current_portfolio_digest": digest(current_portfolio),
        "observed_portfolio_digest": digest(observed_portfolio),
        "journal_cursor": copy.deepcopy(cursor),
        "summary": {
            "account_changes": sum(1 for row in differences if row["domain"] == "account"),
            "position_changes": sum(1 for row in differences if row["domain"] == "position"),
            "cash_changes": sum(1 for row in differences if row["domain"] == "cash"),
            "gap_count": len(gaps),
        },
        "differences": differences,
        "gaps": gaps,
        "reconciled_portfolio": reconciled,
        "checkpoint_draft": checkpoint,
        "acceptance_required": True,
    }
    candidate_digest = digest(_candidate_identity_material(candidate))
    candidate["candidate_digest"] = candidate_digest
    candidate["candidate_id"] = "portfolio-reconciliation:" + candidate_digest[:24]
    return candidate


def build_reconciliation_candidate_from_latest_checkpoint(
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    observed_portfolio: dict[str, Any],
    *,
    snapshot_effective_at: dict[str, Any],
    source_ref: str,
    created_at: dict[str, Any] | None = None,
    identity_scope_account_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    portfolio_id = _validate_portfolio_shape(observed_portfolio)
    try:
        cursor = _journal_cursor_for_snapshot(
            write_store,
            portfolio_id=portfolio_id,
            snapshot_effective_at=snapshot_effective_at,
        )
        projection = project_from_latest_checkpoint_through_cursor(
            checkpoint_store,
            write_store,
            portfolio_id=portfolio_id,
            through_commit_id=cursor.get("through_commit_id"),
            generated_at=created_at,
            identity_scope_account_ids=identity_scope_account_ids,
        )
        current = projection["materialization"]["portfolio"]
        checkpoint = projection["checkpoint"]
    except CheckpointError as exc:
        raise ReconciliationError(exc.code) from exc
    candidate = build_reconciliation_candidate(
        current,
        observed_portfolio,
        write_store,
        snapshot_effective_at=snapshot_effective_at,
        source_ref=source_ref,
        cash_basis_refs=checkpoint.get("cash_basis_refs") or [],
        short_allowed_account_ids=checkpoint.get("short_allowed_account_ids") or [],
        created_at=created_at,
    )
    if candidate.get("journal_cursor") != cursor:
        raise ReconciliationError("reconciliation_cursor_changed_during_build")
    return candidate


def accept_reconciliation_candidate(
    candidate: dict[str, Any],
    acceptance: dict[str, Any],
    *,
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    acceptance_verifier: AcceptanceVerifier | None,
) -> dict[str, Any]:
    _validate_candidate(candidate)
    if not isinstance(acceptance, dict):
        raise ReconciliationError("reconciliation_acceptance_invalid")
    if acceptance.get("candidate_id") != candidate.get("candidate_id"):
        raise ReconciliationError("reconciliation_acceptance_candidate_mismatch")
    if acceptance.get("accepted_candidate_digest") != candidate.get("candidate_digest"):
        raise ReconciliationError("reconciliation_acceptance_digest_mismatch")
    accepted_at = acceptance.get("accepted_at")
    accepted_point = _point(accepted_at, code="reconciliation_acceptance_time_invalid")
    created_point = _point(candidate.get("created_at"), code="reconciliation_candidate_time_invalid")
    if accepted_point < created_point:
        raise ReconciliationError("reconciliation_acceptance_before_candidate")
    if acceptance.get("decision") != "accept":
        raise ReconciliationError("reconciliation_not_accepted")
    if acceptance_verifier is None:
        raise ReconciliationError("reconciliation_acceptance_unverified")
    try:
        verification = acceptance_verifier(candidate, acceptance)
    except Exception as exc:
        raise ReconciliationError("reconciliation_acceptance_unverified") from exc
    if not isinstance(verification, dict) or verification.get("verified") is not True:
        raise ReconciliationError("reconciliation_acceptance_unverified")
    verification_ref = str(verification.get("verification_ref") or "")
    if not verification_ref:
        raise ReconciliationError("reconciliation_acceptance_unverified")

    checkpoint = candidate.get("checkpoint_draft")
    if not isinstance(checkpoint, dict):
        raise ReconciliationError("reconciliation_checkpoint_invalid")
    try:
        final_checkpoint = bind_reconciliation_acceptance(
            checkpoint,
            candidate_ref=str(candidate["candidate_id"]),
            verification_ref=verification_ref,
        )
        persist_checkpoint(checkpoint_store, write_store, final_checkpoint)
    except CheckpointError as exc:
        raise ReconciliationError(exc.code) from exc
    return {
        "protocol_version": PROTOCOL_VERSION,
        "reconciliation_version": 1,
        "status": "accepted",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "checkpoint_id": final_checkpoint["checkpoint_id"],
        "accepted_at": copy.deepcopy(accepted_at),
        "verification_ref": verification_ref,
    }
