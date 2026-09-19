from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import canonical_json, digest  # noqa: E402
from protocol.v1.runtime.portfolio_authority_seed import (  # noqa: E402
    PortfolioAuthoritySeedError,
    infer_initial_cash_basis_refs,
    seed_initial_portfolio_authority,
    verify_seeded_portfolio_authority,
)


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


def _portfolio(*, cash_kind: str = "nominal_balance") -> dict[str, Any]:
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
        "positions": [],
        "cash": [{
            "cash_id": "cash-alpha",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "currency": "KRW",
            "cash_kind": cash_kind,
            "provider_label": None,
            "value": {"amount": 1000, "currency": "KRW", "value_basis": "observed"},
            "authority": "portfolio_fact",
            "source_evidence": ["observation:cash"],
        }],
        "transactions": [],
        "policies": [],
        "completeness": {
            "holdings": "complete",
            "cash": "partial",
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "partial",
            "accounts": [{"account_id": "account-alpha", "holdings": "complete", "cash": "partial"}],
        },
        "migration_gaps": [],
    }


def _write_bundle(root: Path, portfolio: dict[str, Any]) -> None:
    generated = portfolio["generated_at"]
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
        ("portfolio:alpha", Path("portfolios/portfolio-alpha.json"), portfolio, "portfolio", "portfolio-alpha"),
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
        "bundle_id": "personal-data:fixture",
        "created_at": generated,
        "source_kind": "native_export",
        "portfolio_ids": ["portfolio-alpha"],
        "files": files,
    }
    (root / "personal-data-manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")


def _transaction() -> dict[str, Any]:
    return {
        "transaction_id": "transaction-after",
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "transaction_type": "trade",
        "asset": {
            "asset_type": "stock",
            "symbol": "TEST",
            "venue": "KRX",
            "currency": "KRW",
            "display_name": "Synthetic Asset",
            "provider_refs": {},
        },
        "side": "buy",
        "quantity": 1,
        "price": {"amount": 100, "currency": "KRW", "value_basis": "observed"},
        "amount": None,
        "effective_at": _tp("2026-09-16T11:00:00Z"),
        "recorded_at": _tp("2026-09-16T11:00:00Z"),
        "occurrence_status": "confirmed",
        "detail_status": "complete",
        "execution_basis": "provider_fill",
        "source_type": "provider_execution",
        "source_evidence": ["evidence:tx"],
        "provider_execution_id": "exec-after",
        "transfer_group_id": None,
        "lineage": None,
    }


def _write_native_transaction(root: Path) -> None:
    transaction = _transaction()
    row = {
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
        "committed_at": _tp("2026-09-16T11:00:01Z"),
        "resource": transaction,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "write-journal.jsonl").write_text(canonical_json(row) + "\n", encoding="utf-8")


def test_initial_seed_is_atomic_private_and_ready_for_cutover(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    native = tmp_path / "native"
    checkpoints = tmp_path / "checkpoints"
    _write_bundle(personal, _portfolio())
    result = seed_initial_portfolio_authority(
        personal_data_root=personal,
        native_store_root=native,
        checkpoint_root=checkpoints,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    assert result["status"] == "seeded"
    assert result["ready_for_cutover"] is True
    assert result["portfolio_count"] == 1
    assert result["checkpoint_count"] == 1
    assert result["cash_basis_ref_count"] == 1
    assert result["cash_basis_policy"] == "nominal_balance_only"
    verified = verify_seeded_portfolio_authority(
        personal_data_root=personal,
        native_store_root=native,
        checkpoint_root=checkpoints,
        generated_at=_tp("2026-09-16T12:00:01Z"),
    )
    assert verified["ready_for_cutover"] is True
    assert verified["native_transaction_delta_count"] == 0


def test_legacy_provider_specific_cash_is_not_promoted_to_ledger_basis() -> None:
    assert infer_initial_cash_basis_refs(_portfolio(cash_kind="provider_specific")) == []


def test_seed_refuses_existing_checkpoint_journal(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    native = tmp_path / "native"
    checkpoints = tmp_path / "checkpoints"
    _write_bundle(personal, _portfolio())
    seed_initial_portfolio_authority(
        personal_data_root=personal,
        native_store_root=native,
        checkpoint_root=checkpoints,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    with pytest.raises(PortfolioAuthoritySeedError, match="seed_checkpoint_journal_not_empty"):
        seed_initial_portfolio_authority(
            personal_data_root=personal,
            native_store_root=native,
            checkpoint_root=checkpoints,
            generated_at=_tp("2026-09-16T12:01:00Z"),
        )


def test_seed_blocks_when_native_post_snapshot_delta_exists(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    native = tmp_path / "native"
    _write_bundle(personal, _portfolio())
    _write_native_transaction(native)
    with pytest.raises(PortfolioAuthoritySeedError, match="seed_shadow_not_exact"):
        seed_initial_portfolio_authority(
            personal_data_root=personal,
            native_store_root=native,
            checkpoint_root=tmp_path / "checkpoints",
            generated_at=_tp("2026-09-16T12:00:00Z"),
        )


def test_verify_blocks_if_personal_snapshot_changes_after_seed(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    native = tmp_path / "native"
    checkpoints = tmp_path / "checkpoints"
    portfolio = _portfolio()
    _write_bundle(personal, portfolio)
    seed_initial_portfolio_authority(
        personal_data_root=personal,
        native_store_root=native,
        checkpoint_root=checkpoints,
        generated_at=_tp("2026-09-16T12:00:00Z"),
    )
    changed = _portfolio()
    changed["cash"][0]["value"]["amount"] = 999
    _write_bundle(personal, changed)
    with pytest.raises(PortfolioAuthoritySeedError, match="seed_personal_snapshot_changed"):
        verify_seeded_portfolio_authority(
            personal_data_root=personal,
            native_store_root=native,
            checkpoint_root=checkpoints,
            generated_at=_tp("2026-09-16T12:01:00Z"),
        )
