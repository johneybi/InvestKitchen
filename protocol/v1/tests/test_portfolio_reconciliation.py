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

from protocol.v1.adapters.common import canonical_json, digest  # noqa: E402
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import PortfolioCheckpointStore, build_checkpoint, persist_checkpoint, project_from_latest_checkpoint  # noqa: E402
from protocol.v1.runtime.portfolio_reconciliation import (  # noqa: E402
    ReconciliationError,
    accept_reconciliation_candidate,
    build_reconciliation_candidate,
    build_reconciliation_candidate_from_latest_checkpoint,
)


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


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


def _asset(symbol: str) -> dict[str, Any]:
    return {
        "asset_type": "stock",
        "symbol": symbol,
        "venue": "KRX",
        "currency": "KRW",
        "display_name": f"Asset {symbol}",
        "provider_refs": {},
    }


def _position(symbol: str, quantity: int) -> dict[str, Any]:
    return {
        "position_id": f"position:{symbol}",
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "asset": _asset(symbol),
        "quantity": quantity,
        "quantity_status": "confirmed",
        "quantity_basis": "direct_observation",
        "authority": "portfolio_fact",
        "source_evidence": [f"observation:{symbol}"],
        "state": "open" if quantity else "closed",
    }


def _cash(amount: int) -> dict[str, Any]:
    return {
        "cash_id": "cash-alpha-krw",
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "currency": "KRW",
        "cash_kind": "nominal_balance",
        "provider_label": None,
        "value": {"amount": amount, "currency": "KRW", "value_basis": "observed"},
        "authority": "portfolio_fact",
        "source_evidence": ["observation:cash"],
    }


def _portfolio(*, positions: list[dict[str, Any]], cash: int, holdings_state: str = "complete", cash_state: str = "complete") -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "portfolio_id": "portfolio-alpha",
        "display_name": "Synthetic Portfolio",
        "generated_at": _tp("2026-09-16T10:00:00Z"),
        "accounts": [{
            "account_id": "account-alpha",
            "portfolio_id": "portfolio-alpha",
            "display_name": "Synthetic Account",
            "provider_id": None,
            "account_type": "general",
            "base_currency": "KRW",
            "role": "core",
            "status": "active",
            "constraints": [],
        }],
        "positions": copy.deepcopy(positions),
        "cash": [_cash(cash)],
        "transactions": [],
        "policies": [],
        "completeness": {
            "holdings": holdings_state,
            "cash": cash_state,
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "complete",
            "accounts": [{
                "account_id": "account-alpha",
                "holdings": holdings_state,
                "cash": cash_state,
                "observed_at": _tp("2026-09-16T10:00:00Z"),
            }],
        },
        "migration_gaps": [],
    }


def _transaction(transaction_id: str, quantity: int, effective_at: str) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "transaction_type": "trade",
        "asset": _asset("AAA"),
        "side": "buy",
        "quantity": quantity,
        "price": {"amount": 100, "currency": "KRW", "value_basis": "observed"},
        "amount": None,
        "effective_at": _tp(effective_at),
        "recorded_at": _tp(effective_at),
        "occurrence_status": "confirmed",
        "detail_status": "complete",
        "execution_basis": "provider_fill",
        "source_type": "provider_execution",
        "source_evidence": [f"evidence:{transaction_id}"],
        "provider_execution_id": f"exec:{transaction_id}",
        "transfer_group_id": None,
        "lineage": None,
    }


def _entry(commit_id: str, transaction: dict[str, Any]) -> dict[str, Any]:
    return {
        "journal_version": 1,
        "event_type": "resource_commit",
        "commit_id": commit_id,
        "idempotency_key": f"idem:{commit_id}",
        "operation_id": f"operation:{commit_id}",
        "action": "transaction.record",
        "payload_digest": "a" * 64,
        "resource_type": "transaction",
        "resource_ref": transaction["transaction_id"],
        "resource_digest": digest(transaction),
        "receipt": {},
        "audit": {},
        "transaction_identity": None,
        "committed_at": _tp(transaction["recorded_at"]["value"]),
        "resource": transaction,
    }


def _write_journal(store: NativeWriteStore, rows: list[dict[str, Any]]) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    store.journal_path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def test_reconciliation_preserves_unobserved_account_with_unresolved_position_identity(tmp_path: Path) -> None:
    current = _portfolio(positions=[_position("AAA", 10)], cash=1000)
    current["accounts"].append({
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
        "authority": "portfolio_fact",
        "source_evidence": ["legacy:beta"],
        "state": "open",
    }
    current["positions"].append(unresolved)
    current["completeness"]["accounts"].append({
        "account_id": "account-beta",
        "holdings": "unknown",
        "cash": "unknown",
        "observed_at": _tp("2026-09-16T10:00:00Z"),
    })

    observed = _portfolio(positions=[_position("AAA", 11)], cash=900)
    candidate = build_reconciliation_candidate(
        current,
        observed,
        NativeWriteStore(tmp_path / "native-write"),
        snapshot_effective_at=_tp("2026-09-16T11:00:00Z"),
        source_ref="provider-observation:scoped",
        created_at=_tp("2026-09-16T11:01:00Z"),
    )
    preserved = next(
        row for row in candidate["reconciled_portfolio"]["positions"]
        if row["position_id"] == "position-beta-unresolved"
    )
    assert preserved == unresolved


def _basis() -> list[dict[str, str]]:
    return [{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha-krw"}]


def test_complete_observation_builds_review_candidate_without_persisting(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    current = _portfolio(positions=[_position("AAA", 10), _position("BBB", 5)], cash=1000)
    observed = _portfolio(positions=[_position("AAA", 12)], cash=800)
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")

    candidate = build_reconciliation_candidate(
        current,
        observed,
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:provider:fixture",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    _validate(candidate, "portfolio-reconciliation.schema.json")
    assert checkpoint_store.read_all() == []
    assert candidate["status"] == "review_required"
    assert candidate["acceptance_required"] is True
    assert {row["change_type"] for row in candidate["differences"] if row["domain"] == "position"} == {"changed", "removed"}
    assert candidate["reconciled_portfolio"]["positions"][0]["quantity"] == 12
    assert all(row["asset"]["symbol"] != "BBB" for row in candidate["reconciled_portfolio"]["positions"])
    assert candidate["reconciled_portfolio"]["cash"][0]["value"]["amount"] == 800
    assert current["positions"][0]["quantity"] == 10


def test_partial_observation_never_interprets_unseen_position_or_cash_as_zero(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    current = _portfolio(positions=[_position("AAA", 10), _position("BBB", 5)], cash=1000)
    observed = _portfolio(positions=[_position("AAA", 11)], cash=900, holdings_state="partial", cash_state="partial")
    candidate = build_reconciliation_candidate(
        current,
        observed,
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:partial",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    symbols = {row["asset"]["symbol"]: row["quantity"] for row in candidate["reconciled_portfolio"]["positions"]}
    assert symbols == {"AAA": 11, "BBB": 5}
    assert candidate["reconciled_portfolio"]["cash"][0]["value"]["amount"] == 900
    codes = {gap["gap_code"] for gap in candidate["gaps"]}
    assert {"reconciliation_holdings_incomplete", "reconciliation_cash_incomplete"}.issubset(codes)


def test_observed_transactions_and_policies_are_not_silently_ingested(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    current = _portfolio(positions=[_position("AAA", 10)], cash=1000)
    observed = copy.deepcopy(current)
    observed["transactions"] = [_transaction("observed-only", 1, "2026-09-16T11:00:00Z")]
    observed["policies"] = [{
        "policy_id": "policy:observed",
        "portfolio_id": "portfolio-alpha",
        "scope": "portfolio",
        "version": "1",
        "status": "active",
        "rules": {},
        "authority": "fixture",
    }]
    candidate = build_reconciliation_candidate(
        current,
        observed,
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:with-transactions",
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    assert candidate["reconciled_portfolio"]["transactions"] == current["transactions"]
    assert candidate["reconciled_portfolio"]["policies"] == current["policies"]
    codes = {gap["gap_code"] for gap in candidate["gaps"]}
    assert {"reconciliation_transactions_not_ingested", "reconciliation_policies_not_ingested"}.issubset(codes)


def test_acceptance_requires_exact_digest_and_server_side_verifier(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    candidate = build_reconciliation_candidate(
        _portfolio(positions=[_position("AAA", 10)], cash=1000),
        _portfolio(positions=[_position("AAA", 12)], cash=800),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:acceptance",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    acceptance = {
        "candidate_id": candidate["candidate_id"],
        "accepted_candidate_digest": candidate["candidate_digest"],
        "decision": "accept",
        "accepted_at": _tp("2026-09-16T12:05:00Z"),
    }
    with pytest.raises(ReconciliationError, match="reconciliation_acceptance_unverified"):
        accept_reconciliation_candidate(
            candidate,
            acceptance,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            acceptance_verifier=None,
        )
    bad = copy.deepcopy(acceptance)
    bad["accepted_candidate_digest"] = "b" * 64
    with pytest.raises(ReconciliationError, match="reconciliation_acceptance_digest_mismatch"):
        accept_reconciliation_candidate(
            candidate,
            bad,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            acceptance_verifier=lambda *_: {"verified": True, "verification_ref": "verification:fixture"},
        )
    result = accept_reconciliation_candidate(
        candidate,
        acceptance,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        acceptance_verifier=lambda *_: {"verified": True, "verification_ref": "verification:fixture"},
    )
    _validate(result, "portfolio-reconciliation-acceptance.schema.json")
    stored = checkpoint_store.latest("portfolio-alpha")
    assert stored["checkpoint_id"] == result["checkpoint_id"]
    assert stored["reconciliation_candidate_ref"] == candidate["candidate_id"]
    assert stored["acceptance_verification_ref"] == "verification:fixture"


def test_candidate_tamper_is_rejected_before_checkpoint_write(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    candidate = build_reconciliation_candidate(
        _portfolio(positions=[_position("AAA", 10)], cash=1000),
        _portfolio(positions=[_position("AAA", 12)], cash=800),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:tamper",
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    candidate["reconciled_portfolio"]["positions"][0]["quantity"] = 999
    acceptance = {
        "candidate_id": candidate["candidate_id"],
        "accepted_candidate_digest": candidate["candidate_digest"],
        "decision": "accept",
        "accepted_at": _tp("2026-09-16T12:05:00Z"),
    }
    with pytest.raises(ReconciliationError, match="reconciliation_candidate_digest_mismatch"):
        accept_reconciliation_candidate(
            candidate,
            acceptance,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            acceptance_verifier=lambda *_: {"verified": True, "verification_ref": "verification:fixture"},
        )
    assert checkpoint_store.read_all() == []


def test_new_transaction_after_candidate_remains_delta_after_acceptance(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    candidate = build_reconciliation_candidate(
        _portfolio(positions=[_position("AAA", 10)], cash=1000),
        _portfolio(positions=[_position("AAA", 12)], cash=800),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:race",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    later = _transaction("transaction:later", 1, "2026-09-16T13:00:00Z")
    _write_journal(write_store, [_entry("commit:later", later)])
    acceptance = {
        "candidate_id": candidate["candidate_id"],
        "accepted_candidate_digest": candidate["candidate_digest"],
        "decision": "accept",
        "accepted_at": _tp("2026-09-16T13:05:00Z"),
    }
    accept_reconciliation_candidate(
        candidate,
        acceptance,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        acceptance_verifier=lambda *_: {"verified": True, "verification_ref": "verification:fixture"},
    )
    projection = project_from_latest_checkpoint(
        checkpoint_store,
        write_store,
        portfolio_id="portfolio-alpha",
        generated_at=_tp("2026-09-16T14:00:00Z"),
    )
    assert projection["delta"]["transaction_ids"] == ["transaction:later"]
    assert projection["materialization"]["portfolio"]["positions"][0]["quantity"] == 13


def test_observation_cursor_never_claims_later_journal_events_are_in_snapshot(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    later = _transaction("transaction:later", 1, "2026-09-16T13:00:00Z")
    _write_journal(write_store, [_entry("commit:later", later)])
    candidate = build_reconciliation_candidate(
        _portfolio(positions=[_position("AAA", 10)], cash=1000),
        _portfolio(positions=[_position("AAA", 10)], cash=1000),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:cursor-cutoff",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T14:00:00Z"),
    )
    assert candidate["journal_cursor"]["through_commit_id"] is None
    assert candidate["journal_cursor"]["event_count"] == 0


def test_date_only_observation_does_not_claim_same_day_journal_events(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    same_day = _transaction("transaction:same-day", 1, "2026-09-16T09:00:00Z")
    _write_journal(write_store, [_entry("commit:same-day", same_day)])
    candidate = build_reconciliation_candidate(
        _portfolio(positions=[_position("AAA", 10)], cash=1000),
        _portfolio(positions=[_position("AAA", 10)], cash=1000),
        write_store,
        snapshot_effective_at={"value": "2026-09-16", "precision": "date_only"},
        source_ref="observation:date-only",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-17T00:00:00Z"),
    )
    assert candidate["journal_cursor"]["through_commit_id"] is None
    assert candidate["journal_cursor"]["event_count"] == 0


def test_latest_checkpoint_helper_compares_against_state_as_of_observation_not_future_tail(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    base = _portfolio(positions=[_position("AAA", 10), _position("BBB", 5)], cash=1000)
    checkpoint = build_checkpoint(
        base,
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:00:00Z"),
        through_commit_id=None,
        source_kind="migration_import",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T10:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, checkpoint)

    before = _transaction("transaction:before-observation", 1, "2026-09-16T11:00:00Z")
    after = _transaction("transaction:after-observation", 10, "2026-09-16T13:00:00Z")
    _write_journal(write_store, [_entry("commit:before", before), _entry("commit:after", after)])
    observed = _portfolio(
        positions=[_position("AAA", 11)],
        cash=900,
        holdings_state="partial",
        cash_state="partial",
    )
    candidate = build_reconciliation_candidate_from_latest_checkpoint(
        checkpoint_store,
        write_store,
        observed,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:as-of",
        created_at=_tp("2026-09-16T14:00:00Z"),
    )
    # The 13:00 trade is after the observation and must not be folded into the
    # partial snapshot's retained state.
    aaa = next(row for row in candidate["reconciled_portfolio"]["positions"] if row["asset"]["symbol"] == "AAA")
    assert aaa["quantity"] == 11
    assert candidate["journal_cursor"]["through_commit_id"] == "commit:before"


def test_latest_checkpoint_helper_preserves_checkpoint_cash_and_short_policy(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    base = _portfolio(positions=[_position("AAA", 10)], cash=1000)
    checkpoint = build_checkpoint(
        base,
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:00:00Z"),
        through_commit_id=None,
        source_kind="migration_import",
        cash_basis_refs=_basis(),
        short_allowed_account_ids=["account-alpha"],
        created_at=_tp("2026-09-16T10:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, checkpoint)
    observed = _portfolio(positions=[_position("AAA", 11)], cash=900)
    candidate = build_reconciliation_candidate_from_latest_checkpoint(
        checkpoint_store,
        write_store,
        observed,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:latest",
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    assert candidate["checkpoint_draft"]["cash_basis_refs"] == _basis()
    assert candidate["checkpoint_draft"]["short_allowed_account_ids"] == ["account-alpha"]


def test_observation_rows_cannot_reference_unknown_accounts(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    current = _portfolio(positions=[_position("AAA", 10)], cash=1000)
    observed = _portfolio(positions=[_position("AAA", 11)], cash=900)
    observed["positions"][0]["account_id"] = "account-unknown"
    with pytest.raises(ReconciliationError, match="reconciliation_observed_position_account_unknown"):
        build_reconciliation_candidate(
            current,
            observed,
            write_store,
            snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
            source_ref="observation:unknown-account",
            created_at=_tp("2026-09-16T12:01:00Z"),
        )


def test_acceptance_time_and_checkpoint_effective_time_cannot_regress(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    current = _portfolio(positions=[_position("AAA", 10)], cash=1000)
    first = build_checkpoint(
        current,
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        through_commit_id=None,
        source_kind="migration_import",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, first)

    candidate = build_reconciliation_candidate(
        current,
        _portfolio(positions=[_position("AAA", 11)], cash=900),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T11:00:00Z"),
        source_ref="observation:older",
        cash_basis_refs=_basis(),
        created_at=_tp("2026-09-16T12:30:00Z"),
    )
    acceptance = {
        "candidate_id": candidate["candidate_id"],
        "accepted_candidate_digest": candidate["candidate_digest"],
        "decision": "accept",
        "accepted_at": _tp("2026-09-16T12:20:00Z"),
    }
    with pytest.raises(ReconciliationError, match="reconciliation_acceptance_before_candidate"):
        accept_reconciliation_candidate(
            candidate,
            acceptance,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            acceptance_verifier=lambda *_: {"verified": True, "verification_ref": "verification:fixture"},
        )

    acceptance["accepted_at"] = _tp("2026-09-16T12:40:00Z")
    with pytest.raises(ReconciliationError, match="checkpoint_effective_at_not_increasing"):
        accept_reconciliation_candidate(
            candidate,
            acceptance,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            acceptance_verifier=lambda *_: {"verified": True, "verification_ref": "verification:fixture"},
        )


def test_non_market_identity_rename_does_not_change_identity_or_silently_replace_asset(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    current_position = _position("AAA", 10)
    current_position["position_id"] = "position:fund"
    current_position["asset"] = {
        "asset_type": "fund",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Synthetic Fund A",
        "provider_refs": {"fund_code": "FUND-001"},
    }
    observed_position = copy.deepcopy(current_position)
    observed_position["asset"]["display_name"] = "Synthetic Fund B"
    candidate = build_reconciliation_candidate(
        _portfolio(positions=[current_position], cash=1000),
        _portfolio(positions=[observed_position], cash=1000),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:fund-rename",
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    assert candidate["summary"]["position_changes"] == 0
    assert candidate["reconciled_portfolio"]["positions"][0]["asset"]["display_name"] == "Synthetic Fund A"


def test_symbol_less_unknown_position_returns_repair_details(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    broken = _position("AAA", 10)
    broken["position_id"] = "position:repair-required"
    broken["asset"] = {
        "asset_type": "unknown",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Synthetic Unknown Product",
        "provider_refs": {},
    }
    with pytest.raises(ReconciliationError, match="reconciliation_position_identity_incomplete") as caught:
        build_reconciliation_candidate(
            _portfolio(positions=[broken], cash=1000),
            _portfolio(positions=[copy.deepcopy(broken)], cash=1000),
            write_store,
            snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
            source_ref="observation:repair-required",
            created_at=_tp("2026-09-16T12:01:00Z"),
        )
    assert caught.value.details["position_id"] == "position:repair-required"
    assert caught.value.details["account_id"] == "account-alpha"
    assert caught.value.details["reason"] == "asset_kind_and_identity_unresolved"
    assert "repair_hint" in caught.value.details


def test_duplicate_non_market_identity_and_cross_portfolio_rows_fail_closed(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    first = _position("AAA", 10)
    first["position_id"] = "position:fund-1"
    first["asset"] = {
        "asset_type": "fund", "symbol": None, "venue": None, "currency": "KRW",
        "display_name": "Synthetic Fund", "provider_refs": {"fund_code": "FUND-001"},
    }
    second = copy.deepcopy(first)
    second["position_id"] = "position:fund-2"
    with pytest.raises(ReconciliationError, match="reconciliation_position_identity_ambiguous"):
        build_reconciliation_candidate(
            _portfolio(positions=[first, second], cash=1000),
            _portfolio(positions=[copy.deepcopy(first)], cash=1000),
            write_store,
            snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
            source_ref="observation:duplicate",
            created_at=_tp("2026-09-16T12:01:00Z"),
        )

    current = _portfolio(positions=[_position("AAA", 10)], cash=1000)
    current["positions"][0]["portfolio_id"] = "portfolio-beta"
    with pytest.raises(ReconciliationError, match="reconciliation_current_position_portfolio_mismatch"):
        build_reconciliation_candidate(
            current,
            _portfolio(positions=[_position("AAA", 10)], cash=1000),
            write_store,
            snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
            source_ref="observation:scope-mismatch",
            created_at=_tp("2026-09-16T12:01:00Z"),
        )
