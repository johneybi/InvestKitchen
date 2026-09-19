from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.personal_data_store import MANIFEST_NAME, PersonalDataStore
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    build_checkpoint,
    project_from_latest_checkpoint,
    validate_checkpoint_journal_rows,
)
from protocol.v1.runtime.portfolio_reconciliation import ReconciliationError, journal_cursor_for_snapshot
from protocol.v1.runtime.portfolio_shadow import PortfolioShadowError, build_shadow_report


class PortfolioAuthoritySeedError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("seed write failed")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def infer_initial_cash_basis_refs(portfolio: dict[str, Any]) -> list[dict[str, str]]:
    """Choose only unambiguous nominal-balance rows for deterministic cash deltas.

    Legacy migrated cash frequently has provider-specific/unknown settlement
    semantics. Those rows remain valid snapshot facts but are deliberately not
    promoted into an execution-adjusted ledger basis by this initial seed.
    """

    accounts = {
        str(row.get("account_id") or "")
        for row in portfolio.get("accounts") or []
        if isinstance(row, dict) and row.get("account_id")
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in portfolio.get("cash") or []:
        if not isinstance(row, dict):
            raise PortfolioAuthoritySeedError("seed_cash_row_invalid")
        account_id = str(row.get("account_id") or "")
        currency = str(row.get("currency") or "")
        cash_id = str(row.get("cash_id") or "")
        value = row.get("value")
        if account_id not in accounts or len(currency) != 3 or not cash_id:
            raise PortfolioAuthoritySeedError("seed_cash_identity_invalid")
        if not isinstance(value, dict) or value.get("currency") != currency:
            raise PortfolioAuthoritySeedError("seed_cash_value_currency_mismatch")
        if row.get("cash_kind") != "nominal_balance":
            continue
        grouped.setdefault((account_id, currency), []).append(row)

    refs: list[dict[str, str]] = []
    for (account_id, currency), rows in sorted(grouped.items()):
        if len(rows) != 1:
            raise PortfolioAuthoritySeedError("seed_nominal_cash_basis_ambiguous")
        refs.append({
            "account_id": account_id,
            "currency": currency,
            "cash_id": str(rows[0]["cash_id"]),
        })
    return refs


def _checkpoint_rows_for_seed(
    personal_store: PersonalDataStore,
    write_store: NativeWriteStore,
    *,
    created_at: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    verification = personal_store.verify()
    bundle_id = str(personal_store.load_manifest().get("bundle_id") or "personal-data:unknown")
    checkpoints: list[dict[str, Any]] = []
    cash_basis_count = 0
    for portfolio_id in sorted(str(value) for value in verification.get("portfolio_ids") or []):
        portfolio = personal_store.portfolio(portfolio_id)
        snapshot_at = portfolio.get("generated_at")
        if not isinstance(snapshot_at, dict):
            raise PortfolioAuthoritySeedError("seed_snapshot_time_missing")
        try:
            cursor = journal_cursor_for_snapshot(
                write_store,
                portfolio_id=portfolio_id,
                snapshot_effective_at=snapshot_at,
            )
        except ReconciliationError as exc:
            raise PortfolioAuthoritySeedError(exc.code) from exc
        cash_basis = infer_initial_cash_basis_refs(portfolio)
        cash_basis_count += len(cash_basis)
        try:
            checkpoint = build_checkpoint(
                portfolio,
                write_store,
                snapshot_effective_at=snapshot_at,
                through_commit_id=cursor.get("through_commit_id"),
                source_kind="migration_import",
                source_ref=bundle_id,
                cash_basis_refs=cash_basis,
                short_allowed_account_ids=[],
                created_at=created_at,
            )
        except CheckpointError as exc:
            raise PortfolioAuthoritySeedError(exc.code) from exc
        if checkpoint.get("write_cursor") != cursor:
            raise PortfolioAuthoritySeedError("seed_cursor_changed_during_build")
        checkpoints.append(checkpoint)
    return checkpoints, cash_basis_count


def _atomic_install_checkpoint_rows(checkpoint_root: Path, rows: list[dict[str, Any]]) -> str:
    root = checkpoint_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / ".checkpoints.lock"
    journal_path = root / "checkpoints.jsonl"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "r+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if journal_path.exists() and journal_path.stat().st_size > 0:
                raise PortfolioAuthoritySeedError("seed_checkpoint_journal_not_empty")
            encoded = "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")
            fd, tmp_name = tempfile.mkstemp(prefix=".checkpoints.seed-", dir=root)
            tmp = Path(tmp_name)
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.replace(tmp, journal_path)
                _fsync_dir(root)
            finally:
                if tmp.exists():
                    tmp.unlink()
            return _sha256_bytes(encoded)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def verify_seeded_portfolio_authority(
    *,
    personal_data_root: Path,
    native_store_root: Path,
    checkpoint_root: Path,
    generated_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    personal_store = PersonalDataStore(personal_data_root)
    personal_verification = personal_store.verify()
    manifest_bytes = (personal_store.resolved_root() / MANIFEST_NAME).read_bytes()
    write_store = NativeWriteStore(native_store_root.expanduser().resolve())
    checkpoint_store = PortfolioCheckpointStore(checkpoint_root.expanduser().resolve())
    try:
        checkpoints = checkpoint_store.read_all()
        validate_checkpoint_journal_rows(checkpoints, write_store.read_journal())
    except CheckpointError as exc:
        raise PortfolioAuthoritySeedError(exc.code) from exc
    portfolio_ids = sorted(str(value) for value in personal_verification.get("portfolio_ids") or [])
    checkpoint_portfolios = {str(row.get("portfolio_id") or "") for row in checkpoints}
    if checkpoint_portfolios != set(portfolio_ids):
        raise PortfolioAuthoritySeedError("seed_checkpoint_portfolio_set_mismatch")
    if len(checkpoints) != len(portfolio_ids):
        raise PortfolioAuthoritySeedError("seed_checkpoint_count_mismatch")

    bundle_id = str(personal_store.load_manifest().get("bundle_id") or "personal-data:unknown")
    checkpoint_by_portfolio = {str(row["portfolio_id"]): row for row in checkpoints}
    for portfolio_id in portfolio_ids:
        checkpoint = checkpoint_by_portfolio[portfolio_id]
        if checkpoint.get("source_kind") != "migration_import" or checkpoint.get("source_ref") != bundle_id:
            raise PortfolioAuthoritySeedError("seed_checkpoint_source_mismatch")
        if checkpoint.get("base_portfolio_digest") != digest(personal_store.portfolio(portfolio_id)):
            raise PortfolioAuthoritySeedError("seed_personal_snapshot_changed")

    exact = 0
    partial = 0
    blocked = 0
    gap_count = 0
    delta_count = 0
    for portfolio_id in portfolio_ids:
        try:
            projection = project_from_latest_checkpoint(
                checkpoint_store,
                write_store,
                portfolio_id=portfolio_id,
                generated_at=generated_at or timepoint(),
            )
            materialization = projection["materialization"]
            gaps = list(materialization.get("gaps") or [])
            gap_count += len(gaps)
            delta_count += len(projection.get("delta", {}).get("transaction_ids") or [])
            if gaps:
                partial += 1
            else:
                exact += 1
        except CheckpointError:
            blocked += 1

    ready = (
        blocked == 0
        and partial == 0
        and exact == len(portfolio_ids)
        and delta_count == 0
        and gap_count == 0
    )
    journal_path = checkpoint_store.journal_path
    journal_bytes = journal_path.read_bytes() if journal_path.is_file() else b""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "seed_version": 1,
        "status": "ready" if ready else "blocked",
        "ready_for_cutover": ready,
        "generated_at": copy.deepcopy(generated_at or timepoint()),
        "personal_manifest_sha256": _sha256_bytes(manifest_bytes),
        "checkpoint_journal_sha256": _sha256_bytes(journal_bytes),
        "portfolio_count": len(portfolio_ids),
        "checkpoint_count": len(checkpoints),
        "exact_projection_count": exact,
        "partial_projection_count": partial,
        "blocked_projection_count": blocked,
        "materialization_gap_count": gap_count,
        "native_transaction_delta_count": delta_count,
        "native_write_event_count": len(write_store.read_journal()),
    }


def seed_initial_portfolio_authority(
    *,
    personal_data_root: Path,
    native_store_root: Path,
    checkpoint_root: Path,
    generated_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report_time = copy.deepcopy(generated_at or timepoint())
    personal_store = PersonalDataStore(personal_data_root)
    personal_store.verify()
    write_store = NativeWriteStore(native_store_root.expanduser().resolve())

    # Initial cutover is allowed only when the currently deployed snapshot is
    # reproduced exactly by the native authority path. Any post-snapshot delta or
    # ambiguity must be reconciled before seeding production authority.
    try:
        shadow = build_shadow_report(
            personal_data_root=personal_data_root,
            native_store_root=native_store_root,
            generated_at=report_time,
        )
    except (PortfolioShadowError, OSError, ValueError) as exc:
        code = getattr(exc, "code", None) or "seed_shadow_failed"
        raise PortfolioAuthoritySeedError(str(code)) from exc
    summary = shadow.get("summary") if isinstance(shadow.get("summary"), dict) else {}
    portfolio_count = int(summary.get("portfolio_count", -1))
    if (
        portfolio_count < 1
        or int(summary.get("exact_count", -1)) != portfolio_count
        or int(summary.get("divergent_count", -1)) != 0
        or int(summary.get("blocked_count", -1)) != 0
    ):
        raise PortfolioAuthoritySeedError("seed_shadow_not_exact")

    checkpoints, cash_basis_count = _checkpoint_rows_for_seed(
        personal_store,
        write_store,
        created_at=report_time,
    )
    try:
        validate_checkpoint_journal_rows(checkpoints, write_store.read_journal())
    except CheckpointError as exc:
        raise PortfolioAuthoritySeedError(exc.code) from exc
    checkpoint_sha = _atomic_install_checkpoint_rows(checkpoint_root, checkpoints)
    verification = verify_seeded_portfolio_authority(
        personal_data_root=personal_data_root,
        native_store_root=native_store_root,
        checkpoint_root=checkpoint_root,
        generated_at=report_time,
    )
    if verification.get("ready_for_cutover") is not True:
        raise PortfolioAuthoritySeedError("seed_postwrite_verification_failed")
    return {
        **verification,
        "status": "seeded",
        "checkpoint_journal_sha256": checkpoint_sha,
        "cash_basis_ref_count": cash_basis_count,
        "cash_basis_policy": "nominal_balance_only",
    }
