from __future__ import annotations

import copy
import fcntl
import json
import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint
from protocol.v1.runtime.native_write_store import NativeWriteStore, StoreConflict, StoreCorrupt
from protocol.v1.runtime.portfolio_materializer import MaterializationBlocked, materialize_portfolio


class CheckpointError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = copy.deepcopy(details) if details else None


def _point(value: Any, *, code: str) -> tuple[datetime, str]:
    if not isinstance(value, dict) or not isinstance(value.get("value"), str):
        raise CheckpointError(code)
    raw = value["value"]
    precision = str(value.get("precision") or "unknown")
    try:
        if "T" in raw:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            return parsed.astimezone(timezone.utc), precision
        parsed_date = date.fromisoformat(raw)
        return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc), precision
    except ValueError as exc:
        raise CheckpointError(code) from exc


def _transaction_is_after_checkpoint(transaction: dict[str, Any], checkpoint: dict[str, Any]) -> bool:
    tx_point, tx_precision = _point(transaction.get("effective_at"), code="transaction_effective_at_invalid")
    checkpoint_point, checkpoint_precision = _point(
        checkpoint.get("snapshot_effective_at"), code="checkpoint_effective_at_invalid"
    )
    tx_raw = str(transaction["effective_at"]["value"])
    checkpoint_raw = str(checkpoint["snapshot_effective_at"]["value"])

    # Date-only points describe an entire calendar day. If either side is date-only
    # and the calendar dates overlap, ordering is ambiguous and must not be guessed.
    if "T" not in tx_raw or "T" not in checkpoint_raw:
        if tx_point.date() == checkpoint_point.date():
            return False
    return tx_point > checkpoint_point


def _checkpoint_identity_material(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "portfolio_id": checkpoint.get("portfolio_id"),
        "base_portfolio_digest": checkpoint.get("base_portfolio_digest"),
        "snapshot_effective_at": checkpoint.get("snapshot_effective_at"),
        "source_kind": checkpoint.get("source_kind"),
        "source_ref": checkpoint.get("source_ref"),
        "reconciliation_candidate_ref": checkpoint.get("reconciliation_candidate_ref"),
        "acceptance_verification_ref": checkpoint.get("acceptance_verification_ref"),
        "write_cursor": checkpoint.get("write_cursor"),
        "cash_basis_refs": checkpoint.get("cash_basis_refs"),
        "short_allowed_account_ids": checkpoint.get("short_allowed_account_ids"),
        "created_at": checkpoint.get("created_at"),
    }


def _strictly_after_timepoint(current: Any, previous: Any) -> bool:
    current_point, _ = _point(current, code="checkpoint_effective_at_invalid")
    previous_point, _ = _point(previous, code="checkpoint_effective_at_invalid")
    current_raw = str(current["value"])
    previous_raw = str(previous["value"])
    if "T" not in current_raw or "T" not in previous_raw:
        if current_point.date() == previous_point.date():
            return False
    return current_point > previous_point


def _validate_checkpoint_record(checkpoint: dict[str, Any]) -> None:
    if checkpoint.get("protocol_version") != PROTOCOL_VERSION or checkpoint.get("checkpoint_version") != 1:
        raise CheckpointError("checkpoint_version_invalid")
    portfolio_id = str(checkpoint.get("portfolio_id") or "")
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
    if not portfolio_id or not checkpoint_id:
        raise CheckpointError("checkpoint_identity_invalid")
    base = checkpoint.get("base_portfolio")
    if not isinstance(base, dict) or base.get("portfolio_id") != portfolio_id:
        raise CheckpointError("checkpoint_base_portfolio_invalid")
    if digest(base) != checkpoint.get("base_portfolio_digest"):
        raise CheckpointError("checkpoint_base_digest_mismatch")
    expected_id = "portfolio-checkpoint:" + digest(_checkpoint_identity_material(checkpoint))[:24]
    if checkpoint_id != expected_id:
        raise CheckpointError("checkpoint_identity_digest_mismatch")
    _point(checkpoint.get("snapshot_effective_at"), code="checkpoint_effective_at_invalid")


def validate_checkpoint_journal_rows(
    checkpoints: Iterable[dict[str, Any]],
    write_entries: Iterable[dict[str, Any]],
) -> int:
    """Validate checkpoint records and their exact native-write journal prefixes.

    This pure helper is shared by the live checkpoint store and backup/restore
    verification.  It deliberately accepts already-parsed journal rows so a
    snapshot can be verified without materializing temporary live stores.
    """

    rows = [copy.deepcopy(row) for row in checkpoints]
    writes = [copy.deepcopy(row) for row in write_entries]
    commit_index: dict[str, int] = {}
    for offset, entry in enumerate(writes):
        commit_id = str(entry.get("commit_id") or "")
        if not commit_id or commit_id in commit_index:
            raise CheckpointError("journal_corrupt")
        commit_index[commit_id] = offset

    seen_ids: set[str] = set()
    previous_by_portfolio: dict[str, dict[str, Any]] = {}
    for checkpoint in rows:
        _validate_checkpoint_record(checkpoint)
        checkpoint_id = str(checkpoint["checkpoint_id"])
        if checkpoint_id in seen_ids:
            raise CheckpointError("checkpoint_identity_conflict")
        seen_ids.add(checkpoint_id)

        cursor = checkpoint.get("write_cursor")
        if not isinstance(cursor, dict):
            raise CheckpointError("checkpoint_cursor_invalid")
        through_commit_id = cursor.get("through_commit_id")
        if through_commit_id is None:
            prefix: list[dict[str, Any]] = []
        else:
            through_commit_id = str(through_commit_id)
            if through_commit_id not in commit_index:
                raise CheckpointError("journal_cursor_not_found")
            prefix = writes[: commit_index[through_commit_id] + 1]
        actual_cursor = {
            "through_commit_id": through_commit_id,
            "event_count": len(prefix),
            "prefix_digest": digest(prefix),
        }
        if actual_cursor != cursor:
            raise CheckpointError("checkpoint_cursor_prefix_mismatch")

        portfolio_id = str(checkpoint["portfolio_id"])
        previous = previous_by_portfolio.get(portfolio_id)
        if previous is not None:
            if not _strictly_after_timepoint(
                checkpoint.get("snapshot_effective_at"), previous.get("snapshot_effective_at")
            ):
                raise CheckpointError("checkpoint_effective_at_not_increasing")
            previous_count = int(previous.get("write_cursor", {}).get("event_count", -1))
            current_count = int(cursor.get("event_count", -1))
            if current_count < previous_count:
                raise CheckpointError("checkpoint_cursor_regression")
        previous_by_portfolio[portfolio_id] = checkpoint
    return len(rows)


class PortfolioCheckpointStore:
    """Append-only persistent Portfolio checkpoints kept outside the code repo."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.journal_path = self.root / "checkpoints.jsonl"
        self.lock_path = self.root / ".checkpoints.lock"

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CheckpointError(f"checkpoint_journal_invalid_line_{line_number}") from exc
                if not isinstance(row, dict):
                    raise CheckpointError(f"checkpoint_journal_invalid_line_{line_number}")
                _validate_checkpoint_record(row)
                rows.append(row)
        ids = [str(row["checkpoint_id"]) for row in rows]
        if len(ids) != len(set(ids)):
            raise CheckpointError("checkpoint_identity_conflict")
        return rows

    def read_all(self) -> list[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def latest(self, portfolio_id: str) -> dict[str, Any]:
        matches = [row for row in self.read_all() if row.get("portfolio_id") == portfolio_id]
        if not matches:
            raise CheckpointError("checkpoint_not_found")
        return matches[-1]

    def append(self, checkpoint: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                rows = self._read_unlocked()
                checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
                if not checkpoint_id or any(row.get("checkpoint_id") == checkpoint_id for row in rows):
                    raise CheckpointError("checkpoint_identity_conflict")
                encoded = (canonical_json(checkpoint) + "\n").encode("utf-8")
                out_fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    view = memoryview(encoded)
                    while view:
                        written = os.write(out_fd, view)
                        if written <= 0:
                            raise OSError("checkpoint journal write failed")
                        view = view[written:]
                    os.fsync(out_fd)
                finally:
                    os.close(out_fd)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def build_checkpoint(
    base_portfolio: dict[str, Any],
    write_store: NativeWriteStore,
    *,
    snapshot_effective_at: dict[str, Any],
    through_commit_id: str | None,
    source_kind: str,
    source_ref: str | None = None,
    reconciliation_candidate_ref: str | None = None,
    acceptance_verification_ref: str | None = None,
    cash_basis_refs: Iterable[dict[str, Any]] | None = None,
    short_allowed_account_ids: Iterable[str] | None = None,
    created_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(base_portfolio, dict) or base_portfolio.get("protocol_version") != PROTOCOL_VERSION:
        raise CheckpointError("checkpoint_base_portfolio_invalid")
    portfolio_id = str(base_portfolio.get("portfolio_id") or "")
    if not portfolio_id:
        raise CheckpointError("checkpoint_portfolio_id_missing")
    if source_kind not in {"migration_import", "reconciliation", "manual_snapshot", "materialized_snapshot"}:
        raise CheckpointError("checkpoint_source_kind_invalid")
    _point(snapshot_effective_at, code="checkpoint_effective_at_invalid")
    try:
        cursor = write_store.journal_cursor(through_commit_id)
    except StoreConflict as exc:
        raise CheckpointError(exc.code) from exc
    except StoreCorrupt as exc:
        raise CheckpointError("journal_corrupt") from exc
    cash_basis = [copy.deepcopy(value) for value in cash_basis_refs or []]
    short_allowed = sorted({str(value) for value in short_allowed_account_ids or []})
    base_digest = digest(base_portfolio)
    created = copy.deepcopy(created_at or timepoint())
    checkpoint = {
        "protocol_version": PROTOCOL_VERSION,
        "checkpoint_version": 1,
        "checkpoint_id": "",
        "portfolio_id": portfolio_id,
        "created_at": created,
        "snapshot_effective_at": copy.deepcopy(snapshot_effective_at),
        "source_kind": source_kind,
        "source_ref": source_ref,
        "reconciliation_candidate_ref": reconciliation_candidate_ref,
        "acceptance_verification_ref": acceptance_verification_ref,
        "base_portfolio_digest": base_digest,
        "base_portfolio": copy.deepcopy(base_portfolio),
        "write_cursor": cursor,
        "cash_basis_refs": cash_basis,
        "short_allowed_account_ids": short_allowed,
    }
    checkpoint["checkpoint_id"] = "portfolio-checkpoint:" + digest(_checkpoint_identity_material(checkpoint))[:24]
    return checkpoint


def persist_checkpoint(
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    checkpoint: dict[str, Any],
) -> None:
    _validate_checkpoint_record(checkpoint)
    cursor = checkpoint.get("write_cursor")
    if not isinstance(cursor, dict):
        raise CheckpointError("checkpoint_cursor_invalid")
    try:
        actual = write_store.journal_cursor(cursor.get("through_commit_id"))
    except StoreConflict as exc:
        raise CheckpointError(exc.code) from exc
    except StoreCorrupt as exc:
        raise CheckpointError("journal_corrupt") from exc
    if actual != cursor:
        raise CheckpointError("checkpoint_cursor_prefix_mismatch")

    existing = [row for row in checkpoint_store.read_all() if row.get("portfolio_id") == checkpoint.get("portfolio_id")]
    if existing:
        previous = existing[-1]
        if not _strictly_after_timepoint(checkpoint.get("snapshot_effective_at"), previous.get("snapshot_effective_at")):
            raise CheckpointError("checkpoint_effective_at_not_increasing")
        previous_cursor = previous.get("write_cursor")
        if not isinstance(previous_cursor, dict):
            raise CheckpointError("checkpoint_cursor_invalid")
        try:
            previous_actual = write_store.journal_cursor(previous_cursor.get("through_commit_id"))
        except StoreConflict as exc:
            raise CheckpointError(exc.code) from exc
        except StoreCorrupt as exc:
            raise CheckpointError("journal_corrupt") from exc
        if previous_actual != previous_cursor:
            raise CheckpointError("checkpoint_previous_cursor_prefix_mismatch")
        previous_count = int(previous.get("write_cursor", {}).get("event_count", -1))
        current_count = int(cursor.get("event_count", -1))
        if current_count < previous_count:
            raise CheckpointError("checkpoint_cursor_regression")
    checkpoint_store.append(checkpoint)


def bind_reconciliation_acceptance(
    checkpoint: dict[str, Any],
    *,
    candidate_ref: str,
    verification_ref: str,
) -> dict[str, Any]:
    """Return a re-identified checkpoint with durable reconciliation acceptance refs."""

    _validate_checkpoint_record(checkpoint)
    if not candidate_ref or not verification_ref:
        raise CheckpointError("checkpoint_acceptance_ref_invalid")
    bound = copy.deepcopy(checkpoint)
    bound["reconciliation_candidate_ref"] = candidate_ref
    bound["acceptance_verification_ref"] = verification_ref
    bound["checkpoint_id"] = ""
    bound["checkpoint_id"] = "portfolio-checkpoint:" + digest(_checkpoint_identity_material(bound))[:24]
    _validate_checkpoint_record(bound)
    return bound


def project_from_latest_checkpoint(
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    *,
    portfolio_id: str,
    generated_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checkpoint = checkpoint_store.latest(portfolio_id)
    _validate_checkpoint_record(checkpoint)
    base = checkpoint["base_portfolio"]

    cursor = checkpoint.get("write_cursor")
    if not isinstance(cursor, dict):
        raise CheckpointError("checkpoint_cursor_invalid")
    try:
        actual_cursor = write_store.journal_cursor(cursor.get("through_commit_id"))
        delta = write_store.transactions_after_cursor(
            portfolio_id=portfolio_id,
            through_commit_id=cursor.get("through_commit_id"),
        )
    except StoreConflict as exc:
        raise CheckpointError(exc.code) from exc
    except StoreCorrupt as exc:
        raise CheckpointError("journal_corrupt") from exc
    if actual_cursor != cursor:
        raise CheckpointError("checkpoint_cursor_prefix_mismatch")

    for transaction in delta["transactions"]:
        if not _transaction_is_after_checkpoint(transaction, checkpoint):
            raise CheckpointError("transaction_effective_at_not_after_checkpoint")

    try:
        materialization = materialize_portfolio(
            base,
            delta["transactions"],
            checkpoint_ref=str(checkpoint["checkpoint_id"]),
            cash_basis=checkpoint.get("cash_basis_refs") or [],
            short_allowed_account_ids=checkpoint.get("short_allowed_account_ids") or [],
            generated_at=generated_at,
        )
    except MaterializationBlocked as exc:
        raise CheckpointError(exc.code, details=exc.details) from exc

    return {
        "protocol_version": PROTOCOL_VERSION,
        "projection_version": 1,
        "checkpoint": copy.deepcopy(checkpoint),
        "delta": {
            "from_commit_id": delta["from_commit_id"],
            "through_commit_id": delta["through_commit_id"],
            "journal_event_count": delta["journal_event_count"],
            "transaction_commit_ids": list(delta["transaction_commit_ids"]),
            "transaction_ids": [str(row["transaction_id"]) for row in delta["transactions"]],
        },
        "materialization": materialization,
    }


def project_from_latest_checkpoint_through_cursor(
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    *,
    portfolio_id: str,
    through_commit_id: str | None,
    generated_at: dict[str, Any] | None = None,
    identity_scope_account_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Project the latest checkpoint through one explicit later journal cursor."""

    checkpoint = checkpoint_store.latest(portfolio_id)
    _validate_checkpoint_record(checkpoint)
    cursor = checkpoint.get("write_cursor")
    if not isinstance(cursor, dict):
        raise CheckpointError("checkpoint_cursor_invalid")
    try:
        actual_checkpoint_cursor = write_store.journal_cursor(cursor.get("through_commit_id"))
        target_cursor = write_store.journal_cursor(through_commit_id)
        delta = write_store.transactions_between_cursors(
            portfolio_id=portfolio_id,
            after_commit_id=cursor.get("through_commit_id"),
            through_commit_id=through_commit_id,
        )
    except StoreConflict as exc:
        raise CheckpointError(exc.code) from exc
    except StoreCorrupt as exc:
        raise CheckpointError("journal_corrupt") from exc
    if actual_checkpoint_cursor != cursor:
        raise CheckpointError("checkpoint_cursor_prefix_mismatch")
    if int(target_cursor.get("event_count", -1)) < int(cursor.get("event_count", -1)):
        raise CheckpointError("checkpoint_cursor_regression")

    for transaction in delta["transactions"]:
        if not _transaction_is_after_checkpoint(transaction, checkpoint):
            raise CheckpointError("transaction_effective_at_not_after_checkpoint")
    try:
        materialization = materialize_portfolio(
            checkpoint["base_portfolio"],
            delta["transactions"],
            checkpoint_ref=str(checkpoint["checkpoint_id"]),
            cash_basis=checkpoint.get("cash_basis_refs") or [],
            short_allowed_account_ids=checkpoint.get("short_allowed_account_ids") or [],
            generated_at=generated_at,
            identity_scope_account_ids=identity_scope_account_ids,
        )
    except MaterializationBlocked as exc:
        raise CheckpointError(exc.code, details=exc.details) from exc
    return {
        "protocol_version": PROTOCOL_VERSION,
        "projection_version": 1,
        "checkpoint": copy.deepcopy(checkpoint),
        "delta": {
            "from_commit_id": delta["from_commit_id"],
            "through_commit_id": delta["through_commit_id"],
            "journal_event_count": delta["journal_event_count"],
            "transaction_commit_ids": list(delta["transaction_commit_ids"]),
            "transaction_ids": [str(row["transaction_id"]) for row in delta["transactions"]],
        },
        "materialization": materialization,
    }
