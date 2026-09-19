from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import canonical_json, digest  # noqa: E402
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import PortfolioCheckpointStore, build_checkpoint, persist_checkpoint  # noqa: E402
from protocol.v1.runtime.portfolio_replay_verify import build_replay_verification_report  # noqa: E402


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
        "portfolio_id": "portfolio-secret",
        "display_name": "Sensitive Portfolio",
        "generated_at": _tp("2026-09-16T10:00:00Z"),
        "accounts": [{
            "account_id": "account-secret",
            "portfolio_id": "portfolio-secret",
            "display_name": "Sensitive Account",
            "provider_id": None,
            "account_type": "general",
            "base_currency": "KRW",
            "role": "core",
            "status": "active",
            "constraints": [],
        }],
        "positions": [{
            "position_id": "position-secret",
            "portfolio_id": "portfolio-secret",
            "account_id": "account-secret",
            "asset": {
                "asset_type": "stock",
                "symbol": "SECRET-SYMBOL",
                "venue": "KRX",
                "currency": "KRW",
                "display_name": "Sensitive Asset",
                "provider_refs": {},
            },
            "quantity": 10,
            "quantity_status": "confirmed",
            "quantity_basis": "direct_observation",
            "authority": "portfolio_fact",
            "source_evidence": ["observation:secret"],
            "state": "open",
        }],
        "cash": [{
            "cash_id": "cash-secret",
            "portfolio_id": "portfolio-secret",
            "account_id": "account-secret",
            "currency": "KRW",
            "cash_kind": "nominal_balance",
            "provider_label": None,
            "value": {"amount": 1000000, "currency": "KRW", "value_basis": "observed"},
            "authority": "portfolio_fact",
            "source_evidence": ["observation:cash"],
        }],
        "transactions": [],
        "policies": [],
        "completeness": {
            "holdings": "complete",
            "cash": "complete",
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "complete",
            "accounts": [{"account_id": "account-secret", "holdings": "complete", "cash": "complete"}],
        },
        "migration_gaps": [],
    }


def _transaction() -> dict[str, Any]:
    return {
        "transaction_id": "transaction-secret",
        "portfolio_id": "portfolio-secret",
        "account_id": "account-secret",
        "transaction_type": "trade",
        "asset": _base()["positions"][0]["asset"],
        "side": "buy",
        "quantity": 2,
        "price": {"amount": 10000, "currency": "KRW", "value_basis": "observed"},
        "amount": None,
        "effective_at": _tp("2026-09-16T11:00:00Z"),
        "recorded_at": _tp("2026-09-16T11:00:01Z"),
        "occurrence_status": "confirmed",
        "detail_status": "complete",
        "execution_basis": "provider_fill",
        "source_type": "provider_execution",
        "source_evidence": ["evidence:secret"],
        "provider_execution_id": "provider-secret",
        "transfer_group_id": None,
        "lineage": None,
    }


def _entry(transaction: dict[str, Any]) -> dict[str, Any]:
    return {
        "journal_version": 1,
        "event_type": "resource_commit",
        "commit_id": "commit-secret",
        "idempotency_key": "idem-secret",
        "operation_id": "operation-secret",
        "action": "transaction.record",
        "payload_digest": "a" * 64,
        "resource_type": "transaction",
        "resource_ref": transaction["transaction_id"],
        "resource_digest": digest(transaction),
        "receipt": {},
        "audit": {},
        "transaction_identity": None,
        "committed_at": _tp("2026-09-16T11:00:02Z"),
        "resource": transaction,
    }


def _seed(tmp_path: Path, *, cash_basis: bool = True) -> tuple[Path, Path]:
    native = tmp_path / "native-write"
    checkpoints = tmp_path / "portfolio-checkpoints"
    write_store = NativeWriteStore(native)
    checkpoint = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:00:00Z"),
        through_commit_id=None,
        source_kind="migration_import",
        source_ref="fixture:seed",
        cash_basis_refs=[{"account_id": "account-secret", "currency": "KRW", "cash_id": "cash-secret"}] if cash_basis else [],
        created_at=_tp("2026-09-16T10:01:00Z"),
    )
    persist_checkpoint(PortfolioCheckpointStore(checkpoints), write_store, checkpoint)
    return native, checkpoints


def test_replay_report_waits_when_native_journal_has_no_transaction(tmp_path: Path) -> None:
    native, checkpoints = _seed(tmp_path)
    report = build_replay_verification_report(
        native_store_root=native,
        checkpoint_root=checkpoints,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    _validate(report, "portfolio-replay-report.schema.json")
    assert report["status"] == "waiting_for_native_transaction"
    assert report["summary"]["native_transaction_delta_count"] == 0
    assert report["portfolios"][0]["differences"] == {"account_changes": 0, "position_changes": 0, "cash_changes": 0}


def test_replay_report_replays_native_transaction_and_hides_private_values(tmp_path: Path) -> None:
    native, checkpoints = _seed(tmp_path)
    transaction = _transaction()
    native.mkdir(parents=True, exist_ok=True)
    (native / "write-journal.jsonl").write_text(canonical_json(_entry(transaction)) + "\n", encoding="utf-8")
    report = build_replay_verification_report(
        native_store_root=native,
        checkpoint_root=checkpoints,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    _validate(report, "portfolio-replay-report.schema.json")
    row = report["portfolios"][0]
    assert report["status"] == "replayed_with_gaps"
    assert row["native_transaction_delta_count"] == 1
    assert row["differences"]["position_changes"] == 1
    assert row["differences"]["cash_changes"] == 1
    assert "cost_basis_not_materialized" in row["gap_codes"]
    encoded = json.dumps(report, ensure_ascii=False)
    for private_value in ("portfolio-secret", "account-secret", "SECRET-SYMBOL", "transaction-secret", "1000000"):
        assert private_value not in encoded


def test_replay_report_surfaces_cash_gap_without_guessing(tmp_path: Path) -> None:
    native, checkpoints = _seed(tmp_path, cash_basis=False)
    transaction = _transaction()
    native.mkdir(parents=True, exist_ok=True)
    (native / "write-journal.jsonl").write_text(canonical_json(_entry(transaction)) + "\n", encoding="utf-8")
    report = build_replay_verification_report(
        native_store_root=native,
        checkpoint_root=checkpoints,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    _validate(report, "portfolio-replay-report.schema.json")
    row = report["portfolios"][0]
    assert report["status"] == "replayed_with_gaps"
    assert row["differences"]["position_changes"] == 1
    assert row["differences"]["cash_changes"] == 0
    assert "cash_basis_not_declared" in row["gap_codes"]


def test_replay_verifier_does_not_create_files_under_source_roots(tmp_path: Path) -> None:
    native, checkpoints = _seed(tmp_path)
    native_lock = native / ".write-journal.lock"
    checkpoint_lock = checkpoints / ".checkpoints.lock"
    native_lock.unlink(missing_ok=True)
    checkpoint_lock.unlink(missing_ok=True)
    before_native = sorted(path.name for path in native.iterdir()) if native.exists() else []
    before_checkpoint = sorted(path.name for path in checkpoints.iterdir())
    build_replay_verification_report(native_store_root=native, checkpoint_root=checkpoints)
    after_native = sorted(path.name for path in native.iterdir()) if native.exists() else []
    after_checkpoint = sorted(path.name for path in checkpoints.iterdir())
    assert after_native == before_native
    assert after_checkpoint == before_checkpoint
    assert not native_lock.exists()
    assert not checkpoint_lock.exists()
