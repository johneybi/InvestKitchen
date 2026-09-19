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

from protocol.v1.adapters.common import canonical_json, digest, timepoint  # noqa: E402
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import (  # noqa: E402
    CheckpointError,
    PortfolioCheckpointStore,
    build_checkpoint,
    persist_checkpoint,
    project_from_latest_checkpoint,
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


def _base() -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "portfolio_id": "portfolio-alpha",
        "display_name": "Synthetic Portfolio",
        "generated_at": _tp("2026-09-16T00:00:00Z"),
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
        "positions": [{
            "position_id": "position-alpha",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "asset": {
                "asset_type": "stock",
                "symbol": "005930",
                "venue": "KRX",
                "currency": "KRW",
                "display_name": "Synthetic Stock",
                "provider_refs": {},
            },
            "quantity": 10,
            "quantity_status": "confirmed",
            "quantity_basis": "direct_observation",
            "authority": "portfolio_fact",
            "source_evidence": ["snapshot:alpha"],
            "state": "open",
        }],
        "cash": [{
            "cash_id": "cash-alpha-krw",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "currency": "KRW",
            "cash_kind": "nominal_balance",
            "provider_label": None,
            "value": {"amount": 1000000, "currency": "KRW", "value_basis": "observed"},
            "authority": "portfolio_fact",
            "source_evidence": ["snapshot:cash"],
        }],
        "transactions": [],
        "policies": [],
        "completeness": {
            "holdings": "complete",
            "cash": "complete",
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "complete",
            "accounts": [{"account_id": "account-alpha", "holdings": "complete", "cash": "complete"}],
        },
        "migration_gaps": [],
    }


def _transaction(transaction_id: str, *, quantity: int, effective_at: str) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "transaction_type": "trade",
        "asset": {
            "asset_type": "stock",
            "symbol": "005930",
            "venue": "KRX",
            "currency": "KRW",
            "display_name": "Synthetic Stock",
            "provider_refs": {},
        },
        "side": "buy",
        "quantity": quantity,
        "price": {"amount": 10000, "currency": "KRW", "value_basis": "observed"},
        "amount": None,
        "effective_at": _tp(effective_at),
        "recorded_at": _tp(effective_at),
        "occurrence_status": "confirmed",
        "detail_status": "complete",
        "execution_basis": "provider_fill",
        "source_type": "provider_execution",
        "source_evidence": [f"evidence:{transaction_id}"],
        "provider_execution_id": f"exec-{transaction_id}",
        "transfer_group_id": None,
        "lineage": None,
    }


def _entry(commit_id: str, resource_type: str, resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "journal_version": 1,
        "event_type": "resource_commit",
        "commit_id": commit_id,
        "idempotency_key": f"idem-{commit_id}",
        "operation_id": f"op-{commit_id}",
        "action": "transaction.record" if resource_type == "transaction" else "decision.create",
        "payload_digest": "a" * 64,
        "resource_type": resource_type,
        "resource_ref": resource.get("transaction_id") or resource.get("decision_id"),
        "resource_digest": digest(resource),
        "receipt": {},
        "audit": {},
        "transaction_identity": None,
        "committed_at": _tp("2026-09-16T12:00:00Z"),
        "resource": resource,
    }


def _write_journal(store: NativeWriteStore, rows: list[dict[str, Any]]) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    store.journal_path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def test_checkpoint_persists_exact_cursor_and_projects_only_later_transactions(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    before = _transaction("transaction-before", quantity=1, effective_at="2026-09-16T09:00:00Z")
    decision = {
        "decision_id": "decision-middle",
        "portfolio_id": "portfolio-alpha",
        "account_ids": [],
        "subject_refs": [],
        "statement": "fixture",
        "action_intent": {"action": "wait"},
        "conditions": [],
        "invalidation_conditions": [],
        "authority": "explicit_user_decision",
        "status": "active",
        "decided_at": _tp("2026-09-16T10:00:00Z"),
        "recorded_at": _tp("2026-09-16T10:00:00Z"),
    }
    rows = [
        _entry("commit-before", "transaction", before),
        _entry("commit-decision", "decision", decision),
    ]
    _write_journal(write_store, rows)

    checkpoint = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:30:00Z"),
        through_commit_id="commit-decision",
        source_kind="reconciliation",
        source_ref="reconciliation:fixture",
        cash_basis_refs=[{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha-krw"}],
        created_at=_tp("2026-09-16T10:31:00Z"),
    )
    _validate(checkpoint, "portfolio-checkpoint.schema.json")

    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    persist_checkpoint(checkpoint_store, write_store, checkpoint)

    after = _transaction("transaction-after", quantity=2, effective_at="2026-09-16T11:00:00Z")
    rows.append(_entry("commit-after", "transaction", after))
    _write_journal(write_store, rows)

    result = project_from_latest_checkpoint(
        checkpoint_store,
        write_store,
        portfolio_id="portfolio-alpha",
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    _validate(result, "portfolio-authority-projection.schema.json")
    assert result["delta"]["transaction_ids"] == ["transaction-after"]
    assert result["delta"]["transaction_commit_ids"] == ["commit-after"]
    assert result["materialization"]["portfolio"]["positions"][0]["quantity"] == 12
    assert result["materialization"]["portfolio"]["cash"][0]["value"]["amount"] == 980000


def test_checkpoint_prefix_tamper_fails_closed(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    before = _transaction("transaction-before", quantity=1, effective_at="2026-09-16T09:00:00Z")
    rows = [_entry("commit-before", "transaction", before)]
    _write_journal(write_store, rows)
    checkpoint = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:00:00Z"),
        through_commit_id="commit-before",
        source_kind="reconciliation",
        created_at=_tp("2026-09-16T10:01:00Z"),
    )
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    persist_checkpoint(checkpoint_store, write_store, checkpoint)

    tampered = copy.deepcopy(rows)
    tampered[0]["payload_digest"] = "c" * 64
    _write_journal(write_store, tampered)
    with pytest.raises(CheckpointError, match="checkpoint_cursor_prefix_mismatch"):
        project_from_latest_checkpoint(checkpoint_store, write_store, portfolio_id="portfolio-alpha")


def test_missing_cursor_and_cursor_regression_fail_closed(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    tx1 = _transaction("transaction-1", quantity=1, effective_at="2026-09-16T09:00:00Z")
    tx2 = _transaction("transaction-2", quantity=1, effective_at="2026-09-16T11:00:00Z")
    rows = [_entry("commit-1", "transaction", tx1), _entry("commit-2", "transaction", tx2)]
    _write_journal(write_store, rows)
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    newer = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        through_commit_id="commit-2",
        source_kind="reconciliation",
        created_at=_tp("2026-09-16T12:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, newer)

    older = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:30:00Z"),
        through_commit_id="commit-1",
        source_kind="reconciliation",
        created_at=_tp("2026-09-16T12:31:00Z"),
    )
    with pytest.raises(CheckpointError, match="checkpoint_cursor_regression"):
        persist_checkpoint(checkpoint_store, write_store, older)

    with pytest.raises(CheckpointError, match="journal_cursor_not_found"):
        build_checkpoint(
            _base(),
            write_store,
            snapshot_effective_at=_tp("2026-09-16T13:00:00Z"),
            through_commit_id="missing-commit",
            source_kind="reconciliation",
            created_at=_tp("2026-09-16T13:01:00Z"),
        )


def test_backdated_or_same_day_ambiguous_transaction_after_cursor_is_blocked(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at={"value": "2026-09-16", "precision": "date_only"},
        through_commit_id=None,
        source_kind="migration_import",
        created_at=_tp("2026-09-16T12:00:00Z"),
    )
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    persist_checkpoint(checkpoint_store, write_store, checkpoint)

    tx = _transaction("transaction-same-day", quantity=1, effective_at="2026-09-16T13:00:00Z")
    _write_journal(write_store, [_entry("commit-same-day", "transaction", tx)])
    with pytest.raises(CheckpointError, match="transaction_effective_at_not_after_checkpoint"):
        project_from_latest_checkpoint(checkpoint_store, write_store, portfolio_id="portfolio-alpha")


def test_checkpoint_store_is_append_only_and_latest_is_last_persisted(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    first = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:00:00Z"),
        through_commit_id=None,
        source_kind="migration_import",
        created_at=_tp("2026-09-16T10:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, first)
    second = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T11:00:00Z"),
        through_commit_id=None,
        source_kind="manual_snapshot",
        created_at=_tp("2026-09-16T11:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, second)
    assert checkpoint_store.latest("portfolio-alpha")["checkpoint_id"] == second["checkpoint_id"]
    assert len(checkpoint_store.read_all()) == 2


def test_checkpoint_record_tamper_and_transaction_resource_mismatch_fail_closed(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    checkpoint = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:00:00Z"),
        through_commit_id=None,
        source_kind="migration_import",
        created_at=_tp("2026-09-16T10:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, checkpoint)
    tampered = copy.deepcopy(checkpoint)
    tampered["source_kind"] = "manual_snapshot"
    checkpoint_store.journal_path.write_text(canonical_json(tampered) + "\n", encoding="utf-8")
    with pytest.raises(CheckpointError, match="checkpoint_identity_digest_mismatch"):
        checkpoint_store.read_all()

    checkpoint_store.journal_path.write_text(canonical_json(checkpoint) + "\n", encoding="utf-8")
    tx = _transaction("transaction-after", quantity=1, effective_at="2026-09-16T11:00:00Z")
    bad_entry = _entry("commit-after", "transaction", tx)
    bad_entry["resource_ref"] = "transaction-other"
    _write_journal(write_store, [bad_entry])
    with pytest.raises(CheckpointError, match="journal_corrupt"):
        project_from_latest_checkpoint(checkpoint_store, write_store, portfolio_id="portfolio-alpha")
