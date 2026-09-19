from __future__ import annotations

import hashlib
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
from protocol.v1.runtime.portfolio_shadow import build_shadow_report  # noqa: E402


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


def _portfolio() -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "portfolio_id": "portfolio-secret",
        "display_name": "Sensitive Fixture",
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
            "value": {"amount": 123456, "currency": "KRW", "value_basis": "observed"},
            "authority": "portfolio_fact",
            "source_evidence": ["observation:cash-secret"],
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


def _write_bundle(root: Path) -> None:
    portfolio = _portfolio()
    generated = _tp("2026-09-16T10:00:00Z")
    current = {
        "capability": "knowledge.current",
        "contract_version": "1.0-draft",
        "producer": {"id": "fixture", "version": "1"},
        "status": "ok",
        "data": {"generation": {"generation_id": "generation-fixture", "commit_status": "committed", "committed_at": generated}, "summary": "fixture", "outlook": []},
        "generated_at": generated,
        "data_times": {},
        "freshness": "local",
        "source_mode": "local_store",
        "provenance": [],
        "authority": "knowledge_claim",
        "warnings": [],
        "gaps": [],
        "conflicts": [],
        "permissions_used": ["knowledge.read"],
    }
    projection = {"protocol_version": "1.0-draft", "claims": [], "evidence": []}
    files = []
    for logical, relative, value, content_type, pid in (
        ("portfolio:secret", Path("portfolios/portfolio-secret.json"), portfolio, "portfolio", "portfolio-secret"),
        ("knowledge:current", Path("knowledge/current.json"), current, "knowledge_current", None),
        ("knowledge:projection", Path("knowledge/evidence-claims.json"), projection, "knowledge_projection", None),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (canonical_json(value) + "\n").encode("utf-8")
        path.write_bytes(raw)
        files.append({
            "logical_name": logical,
            "relative_path": relative.as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "content_type": content_type,
            "portfolio_id": pid,
        })
    manifest = {
        "protocol_version": "1.0-draft",
        "bundle_format_version": 1,
        "bundle_id": "personal-data:secret-fixture",
        "created_at": generated,
        "source_kind": "native_export",
        "portfolio_ids": ["portfolio-secret"],
        "files": files,
    }
    (root / "personal-data-manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")


def _transaction() -> dict[str, Any]:
    return {
        "transaction_id": "transaction-secret",
        "portfolio_id": "portfolio-secret",
        "account_id": "account-secret",
        "transaction_type": "trade",
        "asset": _portfolio()["positions"][0]["asset"],
        "side": "buy",
        "quantity": 2,
        "price": {"amount": 1000, "currency": "KRW", "value_basis": "observed"},
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


def _write_native_journal(root: Path, transaction: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    entry = {
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
    (root / "write-journal.jsonl").write_text(canonical_json(entry) + "\n", encoding="utf-8")


def test_empty_native_state_reproduces_personal_snapshot_without_mutating_inputs(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    native = tmp_path / "native-input-does-not-exist"
    _write_bundle(personal)
    before = hashlib.sha256((personal / "personal-data-manifest.json").read_bytes()).hexdigest()
    report = build_shadow_report(
        personal_data_root=personal,
        native_store_root=native,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    _validate(report, "portfolio-shadow-report.schema.json")
    assert report["summary"] == {"portfolio_count": 1, "exact_count": 1, "divergent_count": 0, "blocked_count": 0}
    assert report["portfolios"][0]["differences"] == {"account_changes": 0, "position_changes": 0, "cash_changes": 0}
    assert native.exists() is False
    assert hashlib.sha256((personal / "personal-data-manifest.json").read_bytes()).hexdigest() == before


def test_post_snapshot_native_transaction_is_reported_as_shadow_divergence_without_raw_private_values(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    native = tmp_path / "native"
    _write_bundle(personal)
    _write_native_journal(native, _transaction())
    source_before = (native / "write-journal.jsonl").read_bytes()
    report = build_shadow_report(
        personal_data_root=personal,
        native_store_root=native,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    _validate(report, "portfolio-shadow-report.schema.json")
    row = report["portfolios"][0]
    assert row["status"] == "divergent"
    assert row["native_transaction_delta_count"] == 1
    assert row["differences"]["position_changes"] == 1
    assert "cash_basis_not_declared" in row["gap_codes"]
    encoded = json.dumps(report, ensure_ascii=False)
    for private_value in ("portfolio-secret", "account-secret", "SECRET-SYMBOL", "123456", "transaction-secret"):
        assert private_value not in encoded
    assert (native / "write-journal.jsonl").read_bytes() == source_before
