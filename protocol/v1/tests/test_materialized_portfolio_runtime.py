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
from protocol.v1.runtime.portfolio_checkpoint import (  # noqa: E402
    PortfolioCheckpointStore,
    build_checkpoint,
    persist_checkpoint,
)
from protocol.v1.runtime.reference_composition import build_reference_gateway  # noqa: E402


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


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


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
        "positions": [],
        "cash": [{
            "cash_id": "cash-alpha",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "currency": "KRW",
            "cash_kind": "nominal_balance",
            "provider_label": None,
            "value": {"amount": 1000000, "currency": "KRW", "value_basis": "observed"},
            "authority": "portfolio_fact",
            "source_evidence": [],
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


def _transaction() -> dict[str, Any]:
    return {
        "transaction_id": "transaction-after",
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
        "quantity": 2,
        "price": {"amount": 10000, "currency": "KRW", "value_basis": "observed"},
        "amount": None,
        "effective_at": _tp("2026-09-16T11:00:00Z"),
        "recorded_at": _tp("2026-09-16T11:00:00Z"),
        "occurrence_status": "confirmed",
        "detail_status": "complete",
        "execution_basis": "provider_fill",
        "source_type": "provider_execution",
        "source_evidence": ["evidence:after"],
        "provider_execution_id": "exec-after",
        "transfer_group_id": None,
        "lineage": None,
    }


def _write_transaction(store: NativeWriteStore, transaction: dict[str, Any]) -> None:
    entry = {
        "journal_version": 1,
        "event_type": "resource_commit",
        "commit_id": "commit-after",
        "idempotency_key": "idem-after",
        "operation_id": "op-after",
        "action": "transaction.record",
        "payload_digest": "a" * 64,
        "resource_type": "transaction",
        "resource_ref": transaction["transaction_id"],
        "resource_digest": digest(transaction),
        "receipt": {},
        "audit": {},
        "transaction_identity": None,
        "committed_at": _tp("2026-09-16T11:01:00Z"),
        "resource": transaction,
    }
    store.root.mkdir(parents=True, exist_ok=True)
    store.journal_path.write_text(canonical_json(entry) + "\n", encoding="utf-8")


def test_checkpoint_root_option_overrides_personal_portfolio_handler_without_deployment_cutover(tmp_path: Path) -> None:
    native_root = tmp_path / "native-write"
    checkpoint_root = tmp_path / "checkpoints"
    write_store = NativeWriteStore(native_root)
    checkpoint = build_checkpoint(
        _base(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T10:00:00Z"),
        through_commit_id=None,
        source_kind="reconciliation",
        cash_basis_refs=[{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha"}],
        created_at=_tp("2026-09-16T10:01:00Z"),
    )
    persist_checkpoint(PortfolioCheckpointStore(checkpoint_root), write_store, checkpoint)
    _write_transaction(write_store, _transaction())

    gateway = build_reference_gateway(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures/full-reference.instance.json",
        native_store_root=native_root,
        portfolio_checkpoint_root=checkpoint_root,
    )
    result = gateway.get_portfolio_state("portfolio-alpha")
    _validate(result, "capability-result.schema.json")
    _validate(result["data"], "portfolio.schema.json")
    assert result["status"] == "partial"
    assert result["authority"] == "derived_calculation"
    assert result["data"]["positions"][0]["quantity"] == 2
    assert result["data"]["cash"][0]["value"]["amount"] == 980000
    assert result["provenance"][0]["source_type"] == "portfolio_checkpoint"


def test_checkpoint_root_requires_native_store_root() -> None:
    try:
        build_reference_gateway(
            runtime_root=ROOT,
            manifest=PROTOCOL / "fixtures/full-reference.instance.json",
            portfolio_checkpoint_root=ROOT / "does-not-matter",
        )
    except ValueError as exc:
        assert "requires native store root" in str(exc)
    else:
        raise AssertionError("checkpoint runtime binding succeeded without native store root")
