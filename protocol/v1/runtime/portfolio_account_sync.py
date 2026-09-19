from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from protocol.v1.adapters.common import PROTOCOL_VERSION, digest, timepoint
from protocol.v1.runtime.asset_identity import canonical_asset_identity
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    project_from_latest_checkpoint_through_cursor,
)
from protocol.v1.runtime.portfolio_reconciliation import journal_cursor_for_snapshot
from protocol.v1.runtime.portfolio_update_service import (
    ApprovalVerifier,
    PortfolioUpdateError,
    accept_portfolio_update,
    build_portfolio_update_preview,
)


AccountSnapshotReader = Callable[[dict[str, Any]], dict[str, Any]]


class AccountSyncError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = copy.deepcopy(details) if details else None


@dataclass(frozen=True)
class AccountBinding:
    portfolio_id: str
    account_id: str
    provider_id: str
    provider_account_ref: str
    provider_credential_ref: str | None = None

    def private_value(self) -> dict[str, str]:
        return {
            "portfolio_id": self.portfolio_id,
            "account_id": self.account_id,
            "provider_id": self.provider_id,
            "provider_account_ref": self.provider_account_ref,
        }

    def provider_request_value(self) -> dict[str, str]:
        value = self.private_value()
        if self.provider_credential_ref:
            value["provider_credential_ref"] = self.provider_credential_ref
        return value


class AccountBindingRegistry:
    """Server-owned provider/account bindings scoped by Portfolio and account.

    The opaque provider account reference is deliberately never used as a public
    account identifier and display names are not accepted as lookup keys.
    """

    def __init__(self, bindings: Iterable[Mapping[str, Any]]) -> None:
        self._bindings: dict[tuple[str, str], AccountBinding] = {}
        provider_scopes: set[tuple[str, str, str]] = set()
        for raw in bindings:
            portfolio_id = str(raw.get("portfolio_id") or "").strip()
            account_id = str(raw.get("account_id") or "").strip()
            provider_id = str(raw.get("provider_id") or "").strip()
            provider_account_ref = str(raw.get("provider_account_ref") or "").strip()
            provider_credential_ref = str(raw.get("provider_credential_ref") or "").strip() or None
            if not all((portfolio_id, account_id, provider_id, provider_account_ref)):
                raise AccountSyncError("account_binding_invalid")
            key = (portfolio_id, account_id)
            if key in self._bindings:
                raise AccountSyncError("account_binding_duplicate_scope")
            provider_scope = (provider_id, provider_credential_ref or "<default>", provider_account_ref)
            if provider_scope in provider_scopes:
                raise AccountSyncError("account_binding_provider_scope_duplicate")
            provider_scopes.add(provider_scope)
            self._bindings[key] = AccountBinding(
                portfolio_id=portfolio_id,
                account_id=account_id,
                provider_id=provider_id,
                provider_account_ref=provider_account_ref,
                provider_credential_ref=provider_credential_ref,
            )

    def resolve(self, portfolio_id: str, account_id: str) -> AccountBinding:
        binding = self._bindings.get((str(portfolio_id), str(account_id)))
        if binding is None:
            raise AccountSyncError(
                "account_binding_not_found",
                details={"portfolio_id": str(portfolio_id), "account_id": str(account_id)},
            )
        return binding


def _parse_exact_timepoint(value: Any, *, code: str) -> datetime:
    if not isinstance(value, dict) or value.get("precision") != "source_exact":
        raise AccountSyncError(code)
    raw = value.get("value")
    if not isinstance(raw, str) or "T" not in raw:
        raise AccountSyncError(code)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AccountSyncError(code) from exc
    if parsed.tzinfo is None:
        raise AccountSyncError(code)
    return parsed.astimezone(timezone.utc)


def _number(value: Any, *, code: str, nonnegative: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise AccountSyncError(code)
    if nonnegative and value < 0:
        raise AccountSyncError(code)
    return value


def _evidence(values: Any, source_ref: str) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    if source_ref not in result:
        result.append(source_ref)
    return result


def validate_account_snapshot(
    snapshot: Mapping[str, Any],
    binding: AccountBinding,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> dict[str, Any]:
    """Validate one provider-neutral snapshot without persisting anything."""

    if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != "1.0":
        raise AccountSyncError("account_snapshot_version_invalid")
    allowed = {
        "schema_version",
        "portfolio_id",
        "account_id",
        "provider_id",
        "provider_account_ref",
        "snapshot_effective_at",
        "retrieved_at",
        "holdings",
        "cash",
        "completeness",
        "source_ref",
    }
    if set(snapshot) - allowed:
        raise AccountSyncError("account_snapshot_fields_invalid")

    expected = binding.private_value()
    actual = {key: str(snapshot.get(key) or "") for key in expected}
    if actual != expected:
        raise AccountSyncError(
            "account_snapshot_binding_mismatch",
            details={
                "portfolio_id": binding.portfolio_id,
                "account_id": binding.account_id,
                "reason": "provider_or_account_scope_mismatch",
                "repair_hint": "Verify the server-owned provider/account binding; do not map by account display name.",
            },
        )

    source_ref = str(snapshot.get("source_ref") or "").strip()
    if not source_ref:
        raise AccountSyncError("account_snapshot_source_ref_missing")
    observed_at = _parse_exact_timepoint(snapshot.get("snapshot_effective_at"), code="account_snapshot_observed_at_invalid")
    retrieved_at = _parse_exact_timepoint(snapshot.get("retrieved_at"), code="account_snapshot_retrieved_at_invalid")
    if observed_at > retrieved_at:
        raise AccountSyncError("account_snapshot_time_order_invalid")
    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if max_age_seconds < 0:
        raise AccountSyncError("account_snapshot_freshness_policy_invalid")
    if (reference_now - observed_at).total_seconds() > max_age_seconds:
        raise AccountSyncError(
            "account_snapshot_stale",
            details={
                "portfolio_id": binding.portfolio_id,
                "account_id": binding.account_id,
                "reason": "observation_older_than_freshness_policy",
            },
        )
    if observed_at > reference_now:
        raise AccountSyncError("account_snapshot_from_future")

    completeness = snapshot.get("completeness")
    if not isinstance(completeness, Mapping) or set(completeness) != {"holdings", "cash"}:
        raise AccountSyncError("account_snapshot_completeness_invalid")
    for component in ("holdings", "cash"):
        if completeness.get(component) not in {"complete", "partial", "unavailable", "stale"}:
            raise AccountSyncError("account_snapshot_completeness_invalid")

    holdings = snapshot.get("holdings")
    cash = snapshot.get("cash")
    if not isinstance(holdings, list) or not isinstance(cash, list):
        raise AccountSyncError("account_snapshot_components_invalid")

    seen_assets: set[tuple[str, ...]] = set()
    normalized_holdings: list[dict[str, Any]] = []
    for raw in holdings:
        if not isinstance(raw, Mapping):
            raise AccountSyncError("account_snapshot_holding_invalid")
        asset = copy.deepcopy(raw.get("asset"))
        identity = canonical_asset_identity(asset)
        if identity is None:
            raise AccountSyncError(
                "account_snapshot_asset_identity_incomplete",
                details={
                    "portfolio_id": binding.portfolio_id,
                    "account_id": binding.account_id,
                    "missing_fields": ["asset.identity"],
                    "reason": "provider_holding_identity_unresolved",
                    "repair_hint": "Provide a verified market identity or explicit non-market provider product/fund identifier.",
                },
            )
        if identity in seen_assets:
            raise AccountSyncError("account_snapshot_asset_identity_duplicate")
        seen_assets.add(identity)
        quantity = _number(raw.get("quantity"), code="account_snapshot_quantity_invalid", nonnegative=True)
        normalized_holdings.append({
            "asset": asset,
            "quantity": quantity,
            "source_evidence": _evidence(raw.get("source_evidence"), source_ref),
        })

    seen_cash: set[tuple[str, str]] = set()
    normalized_cash: list[dict[str, Any]] = []
    for raw in cash:
        if not isinstance(raw, Mapping):
            raise AccountSyncError("account_snapshot_cash_invalid")
        currency = str(raw.get("currency") or "").strip()
        cash_kind = str(raw.get("cash_kind") or "").strip()
        if len(currency) != 3 or not cash_kind:
            raise AccountSyncError("account_snapshot_cash_identity_invalid")
        key = (currency, cash_kind)
        if key in seen_cash:
            raise AccountSyncError("account_snapshot_cash_identity_duplicate")
        seen_cash.add(key)
        amount = _number(raw.get("amount"), code="account_snapshot_cash_amount_invalid")
        normalized_cash.append({
            "cash_id": str(raw.get("cash_id") or "").strip() or None,
            "currency": currency,
            "cash_kind": cash_kind,
            "provider_label": raw.get("provider_label") if isinstance(raw.get("provider_label"), str) else None,
            "amount": amount,
            "source_evidence": _evidence(raw.get("source_evidence"), source_ref),
        })

    return {
        **expected,
        "schema_version": "1.0",
        "snapshot_effective_at": copy.deepcopy(snapshot["snapshot_effective_at"]),
        "retrieved_at": copy.deepcopy(snapshot["retrieved_at"]),
        "source_ref": source_ref,
        "holdings": normalized_holdings,
        "cash": normalized_cash,
        "completeness": {"holdings": str(completeness["holdings"]), "cash": str(completeness["cash"])},
    }


def _current_as_of_snapshot(
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    *,
    portfolio_id: str,
    account_id: str,
    snapshot_effective_at: dict[str, Any],
) -> dict[str, Any]:
    try:
        latest = checkpoint_store.latest(portfolio_id)
        latest_at = _parse_exact_timepoint(latest.get("snapshot_effective_at"), code="account_sync_checkpoint_time_invalid")
        observed_at = _parse_exact_timepoint(snapshot_effective_at, code="account_snapshot_observed_at_invalid")
        if observed_at <= latest_at:
            raise AccountSyncError("account_snapshot_stale_checkpoint")
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
            generated_at=snapshot_effective_at,
            identity_scope_account_ids={account_id},
        )
    except AccountSyncError:
        raise
    except CheckpointError as exc:
        raise AccountSyncError(exc.code) from exc
    current = projection.get("materialization", {}).get("portfolio")
    if not isinstance(current, dict) or current.get("portfolio_id") != portfolio_id:
        raise AccountSyncError("account_sync_projection_invalid")
    return current


def account_snapshot_to_observed_portfolio(
    snapshot: Mapping[str, Any],
    current_portfolio: Mapping[str, Any],
) -> dict[str, Any]:
    """Map validated complete components into a scoped account observation.

    A provider may authoritatively observe holdings while not exposing an
    equivalent cash balance (or vice versa). Components marked unavailable are
    omitted and represented as unknown completeness so reconciliation preserves
    the existing canonical component instead of guessing or zeroing it.
    """

    portfolio_id = str(snapshot.get("portfolio_id") or "")
    account_id = str(snapshot.get("account_id") or "")
    if current_portfolio.get("portfolio_id") != portfolio_id:
        raise AccountSyncError("account_sync_portfolio_mismatch")
    accounts = [
        copy.deepcopy(row)
        for row in current_portfolio.get("accounts") or []
        if isinstance(row, dict) and row.get("account_id") == account_id
    ]
    if len(accounts) != 1:
        raise AccountSyncError("account_sync_account_match_not_unique")

    current_positions: dict[tuple[str, ...], dict[str, Any]] = {}
    legacy_market_aliases: dict[tuple[str, str], dict[str, Any]] = {}
    for row in current_portfolio.get("positions") or []:
        if not isinstance(row, dict) or row.get("account_id") != account_id:
            continue
        identity = canonical_asset_identity(row.get("asset"))
        if identity is None:
            raise AccountSyncError(
                "account_sync_current_position_repair_required",
                details={
                    "portfolio_id": portfolio_id,
                    "position_id": str(row.get("position_id") or "") or None,
                    "account_id": account_id,
                    "missing_fields": ["asset.identity"],
                    "reason": "current_position_identity_unresolved",
                    "repair_hint": "Repair the existing position with verified market or provider identity before account sync.",
                },
            )
        if identity in current_positions:
            raise AccountSyncError("account_sync_current_position_identity_duplicate")
        current_positions[identity] = row
        asset = row.get("asset") if isinstance(row.get("asset"), dict) else {}
        if str(asset.get("asset_type") or "") == "unknown":
            symbol = str(asset.get("symbol") or "").strip()
            currency = str(asset.get("currency") or "").strip()
            if symbol and currency:
                alias = (symbol, currency)
                if alias in legacy_market_aliases:
                    raise AccountSyncError("account_sync_current_position_legacy_alias_duplicate")
                legacy_market_aliases[alias] = row

    holdings_state = str((snapshot.get("completeness") or {}).get("holdings") or "unavailable")
    cash_state = str((snapshot.get("completeness") or {}).get("cash") or "unavailable")
    positions: list[dict[str, Any]] = []
    observed_at = copy.deepcopy(snapshot["snapshot_effective_at"])
    source_ref = str(snapshot["source_ref"])
    for holding in (snapshot.get("holdings") or []) if holdings_state == "complete" else []:
        asset = copy.deepcopy(holding["asset"])
        identity = canonical_asset_identity(asset)
        if identity is None:
            raise AccountSyncError("account_snapshot_asset_identity_incomplete")
        current = current_positions.get(identity)
        if current is None:
            alias = (str(asset.get("symbol") or "").strip(), str(asset.get("currency") or "").strip())
            legacy_current = legacy_market_aliases.get(alias)
            if legacy_current is not None:
                current = legacy_current
                # Preserve the existing canonical asset metadata so a legacy
                # ``asset_type=unknown`` market row is updated in place rather
                # than being removed/re-added solely because the provider can
                # classify it more specifically.
                asset = copy.deepcopy(current["asset"])
                identity = canonical_asset_identity(asset)
                if identity is None:
                    raise AccountSyncError("account_sync_current_position_repair_required")
        if current is None:
            row = {
                "position_id": "observed-position:" + digest([portfolio_id, account_id, list(identity)])[:24],
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "asset": asset,
                "quantity": holding["quantity"],
                "quantity_status": "confirmed",
                "quantity_basis": "direct_observation",
                "authority": "portfolio_fact",
                "source_evidence": list(holding["source_evidence"]),
                "observed_at": observed_at,
                "state": "closed" if holding["quantity"] == 0 else "open",
            }
        else:
            row = copy.deepcopy(current)
            row["asset"] = asset
            row["quantity"] = holding["quantity"]
            row["quantity_status"] = "confirmed"
            row["quantity_basis"] = "direct_observation"
            row["authority"] = "portfolio_fact"
            row["source_evidence"] = _evidence(row.get("source_evidence"), source_ref)
            row["observed_at"] = observed_at
            row["state"] = "closed" if holding["quantity"] == 0 else "open"
        positions.append(row)

    current_cash: dict[tuple[str, str], list[dict[str, Any]]] = {}
    cash_by_id: dict[str, dict[str, Any]] = {}
    for row in current_portfolio.get("cash") or []:
        if not isinstance(row, dict) or row.get("account_id") != account_id:
            continue
        key = (str(row.get("currency") or ""), str(row.get("cash_kind") or ""))
        current_cash.setdefault(key, []).append(row)
        cash_id = str(row.get("cash_id") or "")
        if cash_id:
            if cash_id in cash_by_id:
                raise AccountSyncError("account_sync_current_cash_identity_duplicate")
            cash_by_id[cash_id] = row

    cash_rows: list[dict[str, Any]] = []
    for observed_cash in (snapshot.get("cash") or []) if cash_state == "complete" else []:
        explicit_cash_id = observed_cash.get("cash_id")
        current: dict[str, Any] | None = None
        if explicit_cash_id:
            current = cash_by_id.get(str(explicit_cash_id))
            if current is None:
                raise AccountSyncError("account_sync_cash_id_not_found")
            if (
                current.get("currency") != observed_cash["currency"]
                or current.get("cash_kind") != observed_cash["cash_kind"]
            ):
                raise AccountSyncError("account_sync_cash_scope_mismatch")
        else:
            matches = current_cash.get((observed_cash["currency"], observed_cash["cash_kind"]), [])
            if len(matches) > 1:
                raise AccountSyncError("account_sync_cash_match_ambiguous")
            current = matches[0] if matches else None

        if current is None:
            row = {
                "cash_id": "observed-cash:" + digest([
                    portfolio_id,
                    account_id,
                    observed_cash["currency"],
                    observed_cash["cash_kind"],
                ])[:24],
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "currency": observed_cash["currency"],
                "cash_kind": observed_cash["cash_kind"],
                "provider_label": observed_cash["provider_label"],
                "value": {
                    "amount": observed_cash["amount"],
                    "currency": observed_cash["currency"],
                    "value_basis": "observed",
                },
                "authority": "portfolio_fact",
                "source_evidence": list(observed_cash["source_evidence"]),
                "observed_at": observed_at,
            }
        else:
            row = copy.deepcopy(current)
            row["provider_label"] = observed_cash["provider_label"]
            row["value"] = {
                "amount": observed_cash["amount"],
                "currency": observed_cash["currency"],
                "value_basis": "observed",
            }
            row["authority"] = "portfolio_fact"
            row["source_evidence"] = _evidence(row.get("source_evidence"), source_ref)
            row["observed_at"] = observed_at
        cash_rows.append(row)

    account = accounts[0]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "portfolio_id": portfolio_id,
        "display_name": str(current_portfolio.get("display_name") or portfolio_id),
        "generated_at": observed_at,
        "accounts": [account],
        "positions": positions,
        "cash": cash_rows,
        "transactions": [],
        "policies": copy.deepcopy(current_portfolio.get("policies") or []),
        "completeness": {
            "holdings": "partial" if holdings_state == "complete" else "unknown",
            "cash": "partial" if cash_state == "complete" else "unknown",
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "unknown",
            "accounts": [{
                "account_id": account_id,
                "holdings": "complete" if holdings_state == "complete" else "unknown",
                "cash": "complete" if cash_state == "complete" else "unknown",
                "observed_at": observed_at,
            }],
        },
        "migration_gaps": [],
    }


def _target_account_state(portfolio: Mapping[str, Any], account_id: str) -> dict[str, Any]:
    positions = []
    for row in portfolio.get("positions") or []:
        if isinstance(row, dict) and row.get("account_id") == account_id:
            identity = canonical_asset_identity(row.get("asset"))
            if identity is None:
                raise AccountSyncError("account_sync_readback_identity_incomplete")
            positions.append((list(identity), copy.deepcopy(row)))
    positions.sort(key=lambda item: repr(item[0]))
    cash = [
        copy.deepcopy(row)
        for row in portfolio.get("cash") or []
        if isinstance(row, dict) and row.get("account_id") == account_id
    ]
    cash.sort(key=lambda row: str(row.get("cash_id") or ""))
    accounts = [
        copy.deepcopy(row)
        for row in portfolio.get("accounts") or []
        if isinstance(row, dict) and row.get("account_id") == account_id
    ]
    if len(accounts) != 1:
        raise AccountSyncError("account_sync_readback_account_invalid")
    return {"account": accounts[0], "positions": [row for _, row in positions], "cash": cash}


def build_account_sync_preview(
    *,
    portfolio_id: str,
    account_id: str,
    registry: AccountBindingRegistry,
    providers: Mapping[str, AccountSnapshotReader],
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> dict[str, Any]:
    """Read, validate and preview one account snapshot without Portfolio mutation."""

    binding = registry.resolve(portfolio_id, account_id)
    reader = providers.get(binding.provider_id)
    if reader is None:
        raise AccountSyncError("account_sync_provider_unavailable")
    try:
        raw = reader(binding.provider_request_value())
    except Exception as exc:
        raise AccountSyncError("account_sync_provider_unavailable") from exc
    snapshot = validate_account_snapshot(raw, binding, now=now, max_age_seconds=max_age_seconds)
    component_states = snapshot["completeness"]
    if any(component_states[name] in {"partial", "stale"} for name in ("holdings", "cash")):
        raise AccountSyncError(
            "account_snapshot_incomplete",
            details={
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "reason": "provider_component_partial_or_stale",
                "repair_hint": "Retry the provider read; partial/stale components are never applied.",
            },
        )
    if all(component_states[name] == "unavailable" for name in ("holdings", "cash")):
        raise AccountSyncError(
            "account_snapshot_unavailable",
            details={
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "reason": "no_authoritative_component_available",
                "repair_hint": "Restore at least one authoritative provider component before syncing.",
            },
        )
    if component_states["holdings"] == "unavailable" and snapshot["holdings"]:
        raise AccountSyncError("account_snapshot_completeness_conflict")
    if component_states["cash"] == "unavailable" and snapshot["cash"]:
        raise AccountSyncError("account_snapshot_completeness_conflict")
    current = _current_as_of_snapshot(
        checkpoint_store,
        write_store,
        portfolio_id=portfolio_id,
        account_id=account_id,
        snapshot_effective_at=snapshot["snapshot_effective_at"],
    )
    observed = account_snapshot_to_observed_portfolio(snapshot, current)
    request = {
        "request_version": 1,
        "portfolio_id": portfolio_id,
        "snapshot_effective_at": copy.deepcopy(snapshot["snapshot_effective_at"]),
        "created_at": copy.deepcopy(snapshot["retrieved_at"]),
        "source_ref": snapshot["source_ref"],
        "observed_portfolio": observed,
    }
    try:
        preview = build_portfolio_update_preview(
            request,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            identity_scope_account_ids={account_id},
        )
    except PortfolioUpdateError as exc:
        raise AccountSyncError(exc.code, details=exc.details) from exc
    candidate = preview["candidate"]
    checkpoint = candidate.get("checkpoint_draft") if isinstance(candidate, dict) else None
    reconciled = candidate.get("reconciled_portfolio") if isinstance(candidate, dict) else None
    if not isinstance(checkpoint, dict) or not isinstance(reconciled, dict):
        raise AccountSyncError("account_sync_candidate_invalid")
    cash_by_id = {
        str(row.get("cash_id") or ""): row
        for row in reconciled.get("cash") or []
        if isinstance(row, dict) and row.get("cash_id")
    }
    for ref in checkpoint.get("cash_basis_refs") or []:
        if not isinstance(ref, dict) or ref.get("account_id") != account_id:
            continue
        cash_id = str(ref.get("cash_id") or "")
        row = cash_by_id.get(cash_id)
        if (
            row is None
            or row.get("account_id") != account_id
            or row.get("currency") != ref.get("currency")
        ):
            raise AccountSyncError(
                "account_sync_cash_basis_unobserved",
                details={
                    "portfolio_id": portfolio_id,
                    "account_id": account_id,
                    "reason": "provider_cash_snapshot_does_not_preserve_materialization_basis",
                    "repair_hint": "Define an explicit provider cash-kind mapping to the existing cash basis before applying this snapshot.",
                },
            )
    review = preview["review"]
    summary = review.get("summary") or {}
    change_count = sum(int(summary.get(field) or 0) for field in ("account_changes", "position_changes", "cash_changes"))
    common = {
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "provider_id": binding.provider_id,
        "snapshot_effective_at": copy.deepcopy(snapshot["snapshot_effective_at"]),
        "retrieved_at": copy.deepcopy(snapshot["retrieved_at"]),
        "source_ref": snapshot["source_ref"],
        "completeness": copy.deepcopy(snapshot["completeness"]),
    }
    if change_count == 0:
        return {
            "status": "no_change",
            **common,
            "summary": copy.deepcopy(review.get("summary") or {}),
        }
    return {"status": "review_required", **common, "candidate": candidate, "review": review}


def accept_account_sync(
    sync_preview: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    approval_verifier: ApprovalVerifier | None,
) -> dict[str, Any]:
    """Apply a previously reviewed sync and verify authoritative read-back."""

    if not isinstance(sync_preview, Mapping) or sync_preview.get("status") != "review_required":
        raise AccountSyncError("account_sync_preview_not_applicable")
    candidate = sync_preview.get("candidate")
    if not isinstance(candidate, dict):
        raise AccountSyncError("account_sync_candidate_invalid")
    portfolio_id = str(sync_preview.get("portfolio_id") or "")
    account_id = str(sync_preview.get("account_id") or "")
    if candidate.get("portfolio_id") != portfolio_id or not account_id:
        raise AccountSyncError("account_sync_candidate_scope_mismatch")
    try:
        result = accept_portfolio_update(
            candidate,
            approval,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            approval_verifier=approval_verifier,
        )
        cursor = candidate.get("journal_cursor")
        if not isinstance(cursor, dict):
            raise AccountSyncError("account_sync_candidate_cursor_invalid")
        projection = project_from_latest_checkpoint_through_cursor(
            checkpoint_store,
            write_store,
            portfolio_id=portfolio_id,
            through_commit_id=cursor.get("through_commit_id"),
            generated_at=timepoint(),
            identity_scope_account_ids={account_id},
        )
    except AccountSyncError:
        raise
    except (PortfolioUpdateError, CheckpointError) as exc:
        raise AccountSyncError(getattr(exc, "code", "account_sync_apply_failed"), details=getattr(exc, "details", None)) from exc
    current = projection.get("materialization", {}).get("portfolio")
    expected = candidate.get("reconciled_portfolio")
    if not isinstance(current, dict) or not isinstance(expected, dict):
        raise AccountSyncError("account_sync_readback_invalid")
    expected_state = _target_account_state(expected, account_id)
    actual_state = _target_account_state(current, account_id)
    matches = digest(actual_state) == digest(expected_state)
    if not matches:
        raise AccountSyncError("account_sync_readback_mismatch")
    return {
        "status": "applied",
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "candidate_digest": result["candidate_digest"],
        "checkpoint_id": result["checkpoint_id"],
        "accepted_at": result["accepted_at"],
        "read_back": {"matches": True, "account_state_digest": digest(actual_state)},
    }


__all__ = [
    "AccountBinding",
    "AccountBindingRegistry",
    "AccountSnapshotReader",
    "AccountSyncError",
    "accept_account_sync",
    "account_snapshot_to_observed_portfolio",
    "build_account_sync_preview",
    "validate_account_snapshot",
]
