from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.deployment.portfolio_update_cli import main as update_cli_main  # noqa: E402
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import (  # noqa: E402
    PortfolioCheckpointStore,
    build_checkpoint,
    persist_checkpoint,
    project_from_latest_checkpoint,
)
from protocol.v1.runtime.portfolio_update_service import (  # noqa: E402
    PortfolioUpdateError,
    accept_portfolio_update,
    build_portfolio_update_preview,
    new_approval,
)


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


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
        "source_evidence": [f"seed:{symbol}"],
        "state": "open" if quantity else "closed",
    }


def _cash(amount: int) -> dict[str, Any]:
    return {
        "cash_id": "cash-alpha",
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "currency": "KRW",
        "cash_kind": "nominal_balance",
        "provider_label": None,
        "value": {"amount": amount, "currency": "KRW", "value_basis": "observed"},
        "authority": "portfolio_fact",
        "source_evidence": ["seed:cash"],
    }


def _portfolio(quantity: int = 10, cash: int = 1000) -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "portfolio_id": "portfolio-alpha",
        "display_name": "Synthetic Portfolio",
        "generated_at": _tp("2026-09-17T00:00:00Z"),
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
        "positions": [_position("005930", quantity), _position("000660", 3)],
        "cash": [_cash(cash)],
        "transactions": [],
        "policies": [],
        "completeness": {
            "holdings": "complete",
            "cash": "complete",
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "complete",
            "accounts": [{
                "account_id": "account-alpha",
                "holdings": "complete",
                "cash": "complete",
                "observed_at": _tp("2026-09-17T00:00:00Z"),
            }],
        },
        "migration_gaps": [],
    }


def _stores(tmp_path: Path) -> tuple[NativeWriteStore, PortfolioCheckpointStore]:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    checkpoint = build_checkpoint(
        _portfolio(),
        write_store,
        snapshot_effective_at=_tp("2026-09-17T00:00:00Z"),
        through_commit_id=None,
        source_kind="manual_snapshot",
        source_ref="fixture:seed",
        cash_basis_refs=[{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha"}],
        created_at=_tp("2026-09-17T00:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, checkpoint)
    return write_store, checkpoint_store


def _narrow_request() -> dict[str, Any]:
    return {
        "request_version": 1,
        "portfolio_id": "portfolio-alpha",
        "snapshot_effective_at": _tp("2026-09-17T01:00:00Z"),
        "created_at": _tp("2026-09-17T01:01:00Z"),
        "source_ref": "user-observation:chat:fixture",
        "changes": {
            "position_quantities": [
                {"account_id": "account-alpha", "symbol": "005930", "quantity": 12},
            ],
            "cash_amounts": [{"cash_id": "cash-alpha", "amount": 800}],
        },
    }


def test_narrow_changes_build_review_only_preview_and_preserve_unmentioned_position(tmp_path: Path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    before = checkpoint_store.read_all()
    preview = build_portfolio_update_preview(
        _narrow_request(),
        checkpoint_store=checkpoint_store,
        write_store=write_store,
    )

    assert preview["status"] == "review_required"
    assert checkpoint_store.read_all() == before
    assert preview["review"]["summary"]["position_changes"] == 1
    assert preview["review"]["summary"]["cash_changes"] == 1
    assert "reconciled_portfolio" not in preview["review"]
    reconciled = preview["candidate"]["reconciled_portfolio"]
    quantities = {row["asset"]["symbol"]: row["quantity"] for row in reconciled["positions"]}
    assert quantities == {"005930": 12, "000660": 3}
    assert reconciled["cash"][0]["value"]["amount"] == 800
    assert reconciled["positions"][0]["quantity_basis"] == "user_confirmation"
    assert "user-observation:chat:fixture" in reconciled["positions"][0]["source_evidence"]


def test_full_observed_snapshot_uses_existing_reconciliation_path(tmp_path: Path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    observed = _portfolio(quantity=11, cash=900)
    observed["generated_at"] = _tp("2026-09-17T01:00:00Z")
    observed["completeness"]["accounts"][0]["observed_at"] = _tp("2026-09-17T01:00:00Z")
    request = {
        "request_version": 1,
        "portfolio_id": "portfolio-alpha",
        "snapshot_effective_at": _tp("2026-09-17T01:00:00Z"),
        "created_at": _tp("2026-09-17T01:01:00Z"),
        "source_ref": "user-observation:full:fixture",
        "observed_portfolio": observed,
    }
    preview = build_portfolio_update_preview(
        request,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
    )
    assert preview["candidate"]["reconciled_portfolio"]["positions"][0]["quantity"] == 11
    assert preview["candidate"]["reconciled_portfolio"]["cash"][0]["value"]["amount"] == 900


def test_narrow_change_cannot_invent_or_ambiguously_select_position(tmp_path: Path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    request = _narrow_request()
    request["changes"] = {
        "position_quantities": [{"account_id": "account-alpha", "symbol": "999999", "quantity": 1}],
        "cash_amounts": [],
    }
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_position_match_not_unique"):
        build_portfolio_update_preview(
            request,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
        )


def test_narrow_change_can_target_existing_non_market_position_by_position_id(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    base = _portfolio()
    fund = _position("FUND", 4)
    fund["position_id"] = "position:synthetic-fund"
    fund["asset"] = {
        "asset_type": "fund",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Synthetic Fund",
        "provider_refs": {"fund_code": "FUND-001"},
    }
    base["positions"].append(fund)
    checkpoint = build_checkpoint(
        base,
        write_store,
        snapshot_effective_at=_tp("2026-09-17T00:00:00Z"),
        through_commit_id=None,
        source_kind="manual_snapshot",
        source_ref="fixture:non-market-seed",
        cash_basis_refs=[{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha"}],
        created_at=_tp("2026-09-17T00:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, checkpoint)
    request = {
        "request_version": 1,
        "portfolio_id": "portfolio-alpha",
        "snapshot_effective_at": _tp("2026-09-17T01:00:00Z"),
        "created_at": _tp("2026-09-17T01:01:00Z"),
        "source_ref": "user-observation:fund:fixture",
        "changes": {
            "position_quantities": [{
                "account_id": "account-alpha",
                "position_id": "position:synthetic-fund",
                "quantity": 5,
            }],
            "cash_amounts": [],
        },
    }
    preview = build_portfolio_update_preview(
        request,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
    )
    fund_rows = [
        row for row in preview["candidate"]["reconciled_portfolio"]["positions"]
        if row.get("position_id") == "position:synthetic-fund"
    ]
    assert len(fund_rows) == 1
    assert fund_rows[0]["quantity"] == 5
    assert fund_rows[0]["asset"]["provider_refs"] == {"fund_code": "FUND-001"}


def test_malformed_existing_position_blocks_preview_with_repair_details(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    base = _portfolio()
    broken = base["positions"][0]
    broken["position_id"] = "position:repair-required"
    broken["asset"] = {
        "asset_type": "unknown",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Synthetic Unknown Product",
        "provider_refs": {},
    }
    checkpoint = build_checkpoint(
        base,
        write_store,
        snapshot_effective_at=_tp("2026-09-17T00:00:00Z"),
        through_commit_id=None,
        source_kind="migration_import",
        source_ref="fixture:malformed-seed",
        cash_basis_refs=[{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha"}],
        created_at=_tp("2026-09-17T00:01:00Z"),
    )
    persist_checkpoint(checkpoint_store, write_store, checkpoint)
    with pytest.raises(PortfolioUpdateError, match="base_position_identity_incomplete") as caught:
        build_portfolio_update_preview(
            _narrow_request(),
            checkpoint_store=checkpoint_store,
            write_store=write_store,
        )
    assert caught.value.details["position_id"] == "position:repair-required"
    assert caught.value.details["account_id"] == "account-alpha"
    assert caught.value.details["reason"] == "asset_kind_and_identity_unresolved"
    assert caught.value.details["repair_hint"]


def test_narrow_changes_reject_exact_noop_without_checkpoint_write(tmp_path: Path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    request = _narrow_request()
    request["changes"] = {
        "position_quantities": [
            {"account_id": "account-alpha", "symbol": "005930", "quantity": 10},
        ],
        "cash_amounts": [{"cash_id": "cash-alpha", "amount": 1000}],
    }
    before = checkpoint_store.read_all()
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_no_effect"):
        build_portfolio_update_preview(
            request,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
        )
    assert checkpoint_store.read_all() == before


def test_acceptance_requires_exact_bound_approval_and_trusted_verifier(tmp_path: Path) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    preview = build_portfolio_update_preview(
        _narrow_request(),
        checkpoint_store=checkpoint_store,
        write_store=write_store,
    )
    candidate = preview["candidate"]
    approval = new_approval(
        candidate,
        approval_ref="chat:user-explicit:fixture",
        approved_at=_tp("2026-09-17T01:02:00Z"),
    )
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_approval_unverified"):
        accept_portfolio_update(
            candidate,
            approval,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            approval_verifier=None,
        )
    assert len(checkpoint_store.read_all()) == 1

    mismatched = copy.deepcopy(approval)
    mismatched["candidate_digest"] = "b" * 64
    with pytest.raises(PortfolioUpdateError, match="portfolio_update_approval_candidate_mismatch"):
        accept_portfolio_update(
            candidate,
            mismatched,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            approval_verifier=lambda *_: {"verified": True, "verification_ref": "should-not-run"},
        )
    assert len(checkpoint_store.read_all()) == 1

    result = accept_portfolio_update(
        candidate,
        approval,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        approval_verifier=lambda candidate_value, approval_value: {
            "verified": (
                candidate_value["candidate_digest"] == approval_value["candidate_digest"]
                and approval_value["approval_ref"] == "chat:user-explicit:fixture"
            ),
            "verification_ref": "chat-approval-verification:fixture",
        },
    )
    assert result["status"] == "accepted"
    assert result["verification_ref"] == "chat-approval-verification:fixture"
    assert len(checkpoint_store.read_all()) == 2
    latest = checkpoint_store.latest("portfolio-alpha")
    assert latest["reconciliation_candidate_ref"] == candidate["candidate_id"]
    assert latest["acceptance_verification_ref"] == "chat-approval-verification:fixture"
    projection = project_from_latest_checkpoint(
        checkpoint_store,
        write_store,
        portfolio_id="portfolio-alpha",
        generated_at=_tp("2026-09-17T01:03:00Z"),
    )
    quantities = {
        row["asset"]["symbol"]: row["quantity"]
        for row in projection["materialization"]["portfolio"]["positions"]
    }
    assert quantities == {"005930": 12, "000660": 3}
    assert projection["materialization"]["portfolio"]["cash"][0]["value"]["amount"] == 800


def test_preview_cli_writes_private_candidate_and_prints_bounded_review(tmp_path: Path, capsys) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_narrow_request(), ensure_ascii=False), encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    candidate_path = private / "candidate.json"

    assert update_cli_main([
        "preview",
        "--request", str(request_path),
        "--native-store-root", str(write_store.root),
        "--checkpoint-root", str(checkpoint_store.root),
        "--candidate-out", str(candidate_path),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["position_changes"] == 1
    assert "reconciled_portfolio" not in output
    assert candidate_path.is_file()
    assert stat.S_IMODE(candidate_path.stat().st_mode) == 0o600
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["candidate_id"] == output["candidate_id"]
    assert len(checkpoint_store.read_all()) == 1


def test_accept_cli_refuses_noninteractive_use_without_checkpoint_write(tmp_path: Path, capsys) -> None:
    write_store, checkpoint_store = _stores(tmp_path)
    preview = build_portfolio_update_preview(
        _narrow_request(),
        checkpoint_store=checkpoint_store,
        write_store=write_store,
    )
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(preview["candidate"], ensure_ascii=False), encoding="utf-8")
    original_isatty = sys.stdin.isatty
    try:
        sys.stdin.isatty = lambda: False  # type: ignore[method-assign]
        assert update_cli_main([
            "accept",
            "--candidate", str(candidate_path),
            "--native-store-root", str(write_store.root),
            "--checkpoint-root", str(checkpoint_store.root),
        ]) == 2
    finally:
        sys.stdin.isatty = original_isatty  # type: ignore[method-assign]
    assert "interactive TTY" in capsys.readouterr().err
    assert len(checkpoint_store.read_all()) == 1
