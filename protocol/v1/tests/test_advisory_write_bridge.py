from __future__ import annotations

from pathlib import Path

import pytest

from protocol.v1.deployment.advisory_write_bridge import (
    BridgeRejected,
    PendingStore,
    apply_knowledge,
    apply_portfolio,
    preview_knowledge,
    preview_portfolio,
)
from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore
from protocol.v1.runtime.portfolio_checkpoint import project_from_latest_checkpoint
from protocol.v1.tests.test_native_knowledge_store import _request as knowledge_request
from protocol.v1.tests.test_portfolio_update_service import _narrow_request, _stores


def _past_portfolio_request(*, quantity: int = 12, cash: int | None = 800, minute: int = 10) -> dict:
    request = _narrow_request()
    request["snapshot_effective_at"] = {
        "value": f"2026-09-17T00:{minute:02d}:00Z",
        "precision": "source_exact",
    }
    request["created_at"] = {
        "value": f"2026-09-17T00:{minute + 1:02d}:00Z",
        "precision": "source_exact",
    }
    request["source_ref"] = f"user-observation:chat:{minute}"
    request["changes"] = {
        "position_quantities": [
            {"account_id": "account-alpha", "symbol": "005930", "quantity": quantity},
        ],
        "cash_amounts": [] if cash is None else [{"cash_id": "cash-alpha", "amount": cash}],
    }
    return request


def test_knowledge_bridge_preview_apply_readback_and_one_shot(tmp_path: Path) -> None:
    state = tmp_path / "state"
    pending = PendingStore(state / "advisory-pending")

    review = preview_knowledge(
        knowledge_request(),
        native_root=state / "native-write",
        pending_store=pending,
    )
    assert review["status"] == "review_required"
    assert review["claim_count"] == 1
    assert review["confirmation"].startswith("APPROVE KNOWLEDGE UPDATE ")

    result = apply_knowledge(
        review["pending_id"],
        review["confirmation"],
        native_root=state / "native-write",
        approval_root=state / "approvals",
        pending_store=pending,
    )
    assert result["status"] == "applied"
    assert result["read_back"]["matches"] is True
    assert NativeKnowledgeStore(state / "native-write").latest_generation_id() == review["generation_id"]

    with pytest.raises(BridgeRejected, match="pending_already_applied"):
        apply_knowledge(
            review["pending_id"],
            review["confirmation"],
            native_root=state / "native-write",
            approval_root=state / "approvals",
            pending_store=pending,
        )


def test_knowledge_bridge_rejects_wrong_confirmation_without_write(tmp_path: Path) -> None:
    state = tmp_path / "state"
    pending = PendingStore(state / "advisory-pending")
    review = preview_knowledge(
        knowledge_request(),
        native_root=state / "native-write",
        pending_store=pending,
    )
    with pytest.raises(BridgeRejected, match="confirmation_mismatch"):
        apply_knowledge(
            review["pending_id"],
            "APPROVE SOMETHING ELSE",
            native_root=state / "native-write",
            approval_root=state / "approvals",
            pending_store=pending,
        )
    assert NativeKnowledgeStore(state / "native-write").read_journal() == []


def test_portfolio_bridge_preview_apply_readback_and_one_shot(tmp_path: Path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    pending = PendingStore(tmp_path / "advisory-pending")
    review = preview_portfolio(
        _past_portfolio_request(),
        native_root=write_store.root,
        checkpoint_root=checkpoint_store.root,
        pending_store=pending,
    )
    assert review["status"] == "review_required"
    assert review["summary"]["position_changes"] == 1
    assert review["summary"]["cash_changes"] == 1

    result = apply_portfolio(
        review["pending_id"],
        review["confirmation"],
        native_root=write_store.root,
        checkpoint_root=checkpoint_store.root,
        pending_store=pending,
    )
    assert result["status"] == "applied"
    assert result["read_back"]["matches"] is True
    projection = project_from_latest_checkpoint(
        checkpoint_store,
        write_store,
        portfolio_id="portfolio-alpha",
    )
    quantities = {
        row["asset"]["symbol"]: row["quantity"]
        for row in projection["materialization"]["portfolio"]["positions"]
    }
    assert quantities["005930"] == 12

    with pytest.raises(BridgeRejected, match="pending_already_applied"):
        apply_portfolio(
            review["pending_id"],
            review["confirmation"],
            native_root=write_store.root,
            checkpoint_root=checkpoint_store.root,
            pending_store=pending,
        )


def test_portfolio_bridge_rejects_stale_preview_after_new_checkpoint(tmp_path: Path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    pending = PendingStore(tmp_path / "advisory-pending")
    first = preview_portfolio(
        _past_portfolio_request(quantity=12, cash=800, minute=10),
        native_root=write_store.root,
        checkpoint_root=checkpoint_store.root,
        pending_store=pending,
    )
    second = preview_portfolio(
        _past_portfolio_request(quantity=13, cash=None, minute=20),
        native_root=write_store.root,
        checkpoint_root=checkpoint_store.root,
        pending_store=pending,
    )
    apply_portfolio(
        second["pending_id"],
        second["confirmation"],
        native_root=write_store.root,
        checkpoint_root=checkpoint_store.root,
        pending_store=pending,
    )
    with pytest.raises(BridgeRejected, match="preview_stale"):
        apply_portfolio(
            first["pending_id"],
            first["confirmation"],
            native_root=write_store.root,
            checkpoint_root=checkpoint_store.root,
            pending_store=pending,
        )
