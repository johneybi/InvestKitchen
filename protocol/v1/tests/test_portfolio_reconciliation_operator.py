from __future__ import annotations

import json
import subprocess
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

from protocol.v1.deployment.portfolio_reconciliation_cli import (  # noqa: E402
    OperatorCancelled,
    accept_candidate_with_confirmation,
    acceptance_phrase,
    reconciliation_review_summary,
)
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import PortfolioCheckpointStore  # noqa: E402
from protocol.v1.runtime.portfolio_reconciliation import build_reconciliation_candidate  # noqa: E402


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


def _asset() -> dict[str, Any]:
    return {
        "asset_type": "stock",
        "symbol": "TEST",
        "venue": "KRX",
        "currency": "KRW",
        "display_name": "Synthetic Asset",
        "provider_refs": {},
    }


def _portfolio(quantity: int, cash: int) -> dict[str, Any]:
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
        "positions": [{
            "position_id": "position-alpha",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "asset": _asset(),
            "quantity": quantity,
            "quantity_status": "confirmed",
            "quantity_basis": "direct_observation",
            "authority": "portfolio_fact",
            "source_evidence": ["observation:position"],
            "state": "open",
        }],
        "cash": [{
            "cash_id": "cash-alpha",
            "portfolio_id": "portfolio-alpha",
            "account_id": "account-alpha",
            "currency": "KRW",
            "cash_kind": "nominal_balance",
            "provider_label": None,
            "value": {"amount": cash, "currency": "KRW", "value_basis": "observed"},
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
            "accounts": [{
                "account_id": "account-alpha",
                "holdings": "complete",
                "cash": "complete",
                "observed_at": _tp("2026-09-16T10:00:00Z"),
            }],
        },
        "migration_gaps": [],
    }


def _candidate(write_store: NativeWriteStore) -> dict[str, Any]:
    return build_reconciliation_candidate(
        _portfolio(10, 1000),
        _portfolio(12, 800),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T12:00:00Z"),
        source_ref="observation:operator-fixture",
        cash_basis_refs=[{"account_id": "account-alpha", "currency": "KRW", "cash_id": "cash-alpha"}],
        created_at=_tp("2026-09-16T12:01:00Z"),
    )


def test_review_summary_contains_changes_but_not_full_candidate_state(tmp_path: Path) -> None:
    candidate = _candidate(NativeWriteStore(tmp_path / "native-write"))
    summary = reconciliation_review_summary(candidate)
    encoded = json.dumps(summary, ensure_ascii=False)
    assert summary["summary"]["position_changes"] == 1
    assert summary["summary"]["cash_changes"] == 1
    assert "reconciled_portfolio" not in summary
    assert "checkpoint_draft" not in summary
    assert "current_portfolio_digest" not in summary
    assert '"quantity"' in encoded
    assert '"value"' in encoded


def test_wrong_confirmation_never_persists_checkpoint(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    candidate = _candidate(write_store)
    with pytest.raises(OperatorCancelled, match="reconciliation_operator_cancelled"):
        accept_candidate_with_confirmation(
            candidate,
            confirmation="yes",
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            accepted_at=_tp("2026-09-16T12:05:00Z"),
            interaction_ref="local-tty:test-cancel",
        )
    assert checkpoint_store.read_all() == []


def test_exact_confirmation_creates_verified_checkpoint_and_bounded_result(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_store = PortfolioCheckpointStore(tmp_path / "checkpoints")
    candidate = _candidate(write_store)
    result = accept_candidate_with_confirmation(
        candidate,
        confirmation=acceptance_phrase(candidate),
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        accepted_at=_tp("2026-09-16T12:05:00Z"),
        interaction_ref="local-tty:test-accept",
    )
    _validate(result, "portfolio-reconciliation-acceptance.schema.json")
    assert result["verification_ref"].startswith("reconciliation-verification:")
    stored = checkpoint_store.latest("portfolio-alpha")
    assert stored["reconciliation_candidate_ref"] == candidate["candidate_id"]
    assert stored["acceptance_verification_ref"] == result["verification_ref"]
    encoded = json.dumps(result, ensure_ascii=False)
    assert "Synthetic Asset" not in encoded
    assert "account-alpha" not in encoded
    assert '"quantity"' not in encoded
    assert '"cash"' not in encoded


def test_accept_subcommand_refuses_noninteractive_stdin_without_writing(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    checkpoint_root = tmp_path / "checkpoints"
    candidate = _candidate(write_store)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(PROTOCOL / "deployment" / "portfolio_reconciliation_cli.py"),
            "accept",
            "--candidate",
            str(candidate_path),
            "--native-store-root",
            str(write_store.root),
            "--checkpoint-root",
            str(checkpoint_root),
        ],
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "interactive TTY" in completed.stderr
    assert not (checkpoint_root / "checkpoints.jsonl").exists()


def test_review_subcommand_is_read_only_and_does_not_emit_full_portfolio(tmp_path: Path) -> None:
    write_store = NativeWriteStore(tmp_path / "native-write")
    candidate = _candidate(write_store)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(PROTOCOL / "deployment" / "portfolio_reconciliation_cli.py"),
            "review",
            "--candidate",
            str(candidate_path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    value = json.loads(completed.stdout)
    assert value["candidate_id"] == candidate["candidate_id"]
    assert "reconciled_portfolio" not in value
    assert "checkpoint_draft" not in value
    assert not (tmp_path / "checkpoints").exists()
