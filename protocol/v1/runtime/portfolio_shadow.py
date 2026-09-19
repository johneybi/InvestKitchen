from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint
from protocol.v1.runtime.asset_identity import canonical_asset_identity
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.personal_data_store import MANIFEST_NAME, PersonalDataStore
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    build_checkpoint,
    persist_checkpoint,
    project_from_latest_checkpoint,
)
from protocol.v1.runtime.portfolio_reconciliation import ReconciliationError, journal_cursor_for_snapshot


class PortfolioShadowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_optional_regular_file(path: Path) -> tuple[bool, bytes]:
    try:
        if path.is_symlink():
            raise PortfolioShadowError("shadow_source_symlink_not_allowed")
        if not path.exists():
            return False, b""
        if not path.is_file():
            raise PortfolioShadowError("shadow_source_not_regular_file")
        return True, path.read_bytes()
    except OSError as exc:
        raise PortfolioShadowError("shadow_source_read_failed") from exc


def _position_identity(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    account_id = str(row.get("account_id") or "")
    identity = canonical_asset_identity(row.get("asset"))
    if not account_id or identity is None:
        raise PortfolioShadowError("shadow_portfolio_position_identity_incomplete")
    return account_id, identity


def _index(rows: Iterable[dict[str, Any]], key_fn) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PortfolioShadowError("shadow_portfolio_row_invalid")
        key = key_fn(row)
        if key in result:
            raise PortfolioShadowError("shadow_portfolio_identity_ambiguous")
        result[key] = row
    return result


def _state_view(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Return only state-bearing Portfolio domains used for shadow equality.

    `generated_at`, transaction history, provenance gaps, and presentation metadata
    are deliberately excluded. The report asks whether current Account/Position/Cash
    state changes, while transaction deltas and materialization gaps are reported
    separately.
    """

    return {
        "accounts": portfolio.get("accounts") or [],
        "positions": portfolio.get("positions") or [],
        "cash": portfolio.get("cash") or [],
        "policies": portfolio.get("policies") or [],
        "completeness": portfolio.get("completeness") or {},
    }


def _difference_count(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    key_fn,
) -> int:
    left = _index(before, key_fn)
    right = _index(after, key_fn)
    changes = 0
    for key in set(left) | set(right):
        if key not in left or key not in right:
            changes += 1
            continue
        if canonical_json(left[key]) != canonical_json(right[key]):
            changes += 1
    return changes


def _state_differences(production: dict[str, Any], shadow: dict[str, Any]) -> dict[str, int]:
    return {
        "account_changes": _difference_count(
            list(production.get("accounts") or []),
            list(shadow.get("accounts") or []),
            key_fn=lambda row: str(row.get("account_id") or ""),
        ),
        "position_changes": _difference_count(
            list(production.get("positions") or []),
            list(shadow.get("positions") or []),
            key_fn=_position_identity,
        ),
        "cash_changes": _difference_count(
            list(production.get("cash") or []),
            list(shadow.get("cash") or []),
            key_fn=lambda row: str(row.get("cash_id") or ""),
        ),
    }


def _copy_native_write_journal(source_root: Path, target_root: Path) -> tuple[bool, str]:
    source = source_root.expanduser().resolve() / "write-journal.jsonl"
    present, value = _read_optional_regular_file(source)
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if present:
        (target_root / "write-journal.jsonl").write_bytes(value)
    return present, _sha256_bytes(value)


def build_shadow_report(
    *,
    personal_data_root: Path,
    native_store_root: Path,
    generated_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the native Portfolio authority path without mutating production inputs."""

    personal_store = PersonalDataStore(personal_data_root)
    verification = personal_store.verify()
    manifest_path = personal_store.resolved_root() / MANIFEST_NAME
    manifest_bytes = manifest_path.read_bytes()
    report_time = generated_at or timepoint()

    with tempfile.TemporaryDirectory(prefix="investkitchen-shadow-") as tmp:
        temp_root = Path(tmp)
        temp_native_root = temp_root / "native-write"
        journal_present, journal_sha = _copy_native_write_journal(native_store_root, temp_native_root)
        write_store = NativeWriteStore(temp_native_root)
        event_count = len(write_store.read_journal())
        checkpoint_store = PortfolioCheckpointStore(temp_root / "portfolio-checkpoints")

        rows: list[dict[str, Any]] = []
        for portfolio_id in sorted(str(value) for value in verification.get("portfolio_ids") or []):
            production = personal_store.portfolio(portfolio_id)
            portfolio_ref = "portfolio-shadow:" + hashlib.sha256(portfolio_id.encode("utf-8")).hexdigest()[:16]
            production_snapshot_digest = digest(production)
            empty_diff = {"account_changes": 0, "position_changes": 0, "cash_changes": 0}
            try:
                snapshot_at = production.get("generated_at")
                if not isinstance(snapshot_at, dict):
                    raise PortfolioShadowError("shadow_snapshot_time_missing")
                cursor = journal_cursor_for_snapshot(
                    write_store,
                    portfolio_id=portfolio_id,
                    snapshot_effective_at=snapshot_at,
                )
                checkpoint = build_checkpoint(
                    production,
                    write_store,
                    snapshot_effective_at=snapshot_at,
                    through_commit_id=cursor.get("through_commit_id"),
                    source_kind="migration_import",
                    source_ref="shadow:personal-data-bundle",
                    created_at=report_time,
                )
                if checkpoint.get("write_cursor") != cursor:
                    raise PortfolioShadowError("shadow_cursor_changed_during_seed")
                persist_checkpoint(checkpoint_store, write_store, checkpoint)
                projection = project_from_latest_checkpoint(
                    checkpoint_store,
                    write_store,
                    portfolio_id=portfolio_id,
                    generated_at=report_time,
                )
                materialization = projection["materialization"]
                shadow = materialization["portfolio"]
                differences = _state_differences(production, shadow)
                delta_count = len(projection["delta"]["transaction_ids"])
                gap_codes = sorted({
                    str(gap.get("gap_code"))
                    for gap in materialization.get("gaps") or []
                    if isinstance(gap, dict) and gap.get("gap_code")
                })
                status = "exact" if not any(differences.values()) and delta_count == 0 and not gap_codes else "divergent"
                rows.append({
                    "portfolio_ref": portfolio_ref,
                    "status": status,
                    "production_snapshot_digest": production_snapshot_digest,
                    "production_state_digest": digest(_state_view(production)),
                    "shadow_state_digest": digest(_state_view(shadow)),
                    "seed_checkpoint_ref": str(checkpoint["checkpoint_id"]),
                    "safe_cursor_event_count": int(cursor["event_count"]),
                    "native_transaction_delta_count": delta_count,
                    "differences": differences,
                    "gap_count": len(materialization.get("gaps") or []),
                    "gap_codes": gap_codes,
                    "block_code": None,
                })
            except (CheckpointError, ReconciliationError, PortfolioShadowError, ValueError) as exc:
                code = getattr(exc, "code", None) or "shadow_projection_blocked"
                rows.append({
                    "portfolio_ref": portfolio_ref,
                    "status": "blocked",
                    "production_snapshot_digest": production_snapshot_digest,
                    "production_state_digest": digest(_state_view(production)),
                    "shadow_state_digest": None,
                    "seed_checkpoint_ref": None,
                    "safe_cursor_event_count": None,
                    "native_transaction_delta_count": 0,
                    "differences": empty_diff,
                    "gap_count": 0,
                    "gap_codes": [],
                    "block_code": str(code),
                })

    return {
        "protocol_version": PROTOCOL_VERSION,
        "shadow_report_version": 1,
        "generated_at": report_time,
        "privacy_mode": "summary_only",
        "input_fingerprints": {
            "personal_manifest_sha256": _sha256_bytes(manifest_bytes),
            "native_write_journal_present": journal_present,
            "native_write_journal_sha256": journal_sha,
            "native_write_event_count": event_count,
        },
        "summary": {
            "portfolio_count": len(rows),
            "exact_count": sum(1 for row in rows if row["status"] == "exact"),
            "divergent_count": sum(1 for row in rows if row["status"] == "divergent"),
            "blocked_count": sum(1 for row in rows if row["status"] == "blocked"),
        },
        "portfolios": rows,
    }
