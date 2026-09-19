from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Iterable

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint
from protocol.v1.runtime.asset_identity import canonical_asset_identity
from protocol.v1.runtime.native_write_store import NativeWriteStore, StoreConflict, StoreCorrupt
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    project_from_latest_checkpoint,
    validate_checkpoint_journal_rows,
)


class PortfolioReplayVerifyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_optional_regular_file(path: Path) -> tuple[bool, bytes]:
    try:
        if path.is_symlink():
            raise PortfolioReplayVerifyError("replay_source_symlink_not_allowed")
        if not path.exists():
            return False, b""
        if not path.is_file():
            raise PortfolioReplayVerifyError("replay_source_not_regular_file")
        return True, path.read_bytes()
    except OSError as exc:
        raise PortfolioReplayVerifyError("replay_source_read_failed") from exc


def _parse_jsonl(value: bytes, *, code: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PortfolioReplayVerifyError(code) from exc
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PortfolioReplayVerifyError(f"{code}_line_{line_number}") from exc
        if not isinstance(row, dict):
            raise PortfolioReplayVerifyError(f"{code}_line_{line_number}")
        rows.append(row)
    return rows


def _position_identity(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    account_id = str(row.get("account_id") or "")
    identity = canonical_asset_identity(row.get("asset"))
    if not account_id or identity is None:
        raise PortfolioReplayVerifyError("replay_portfolio_position_identity_incomplete")
    return account_id, identity


def _index(rows: Iterable[dict[str, Any]], key_fn) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PortfolioReplayVerifyError("replay_portfolio_row_invalid")
        key = key_fn(row)
        if key in result:
            raise PortfolioReplayVerifyError("replay_portfolio_identity_ambiguous")
        result[key] = row
    return result


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


def _state_view(portfolio: dict[str, Any]) -> dict[str, Any]:
    return {
        "accounts": portfolio.get("accounts") or [],
        "positions": portfolio.get("positions") or [],
        "cash": portfolio.get("cash") or [],
        "policies": portfolio.get("policies") or [],
        "completeness": portfolio.get("completeness") or {},
    }


def _state_differences(base: dict[str, Any], current: dict[str, Any]) -> dict[str, int]:
    return {
        "account_changes": _difference_count(
            list(base.get("accounts") or []),
            list(current.get("accounts") or []),
            key_fn=lambda row: str(row.get("account_id") or ""),
        ),
        "position_changes": _difference_count(
            list(base.get("positions") or []),
            list(current.get("positions") or []),
            key_fn=_position_identity,
        ),
        "cash_changes": _difference_count(
            list(base.get("cash") or []),
            list(current.get("cash") or []),
            key_fn=lambda row: str(row.get("cash_id") or ""),
        ),
    }


def _opaque_portfolio_ref(portfolio_id: str) -> str:
    return "portfolio-replay:" + hashlib.sha256(portfolio_id.encode("utf-8")).hexdigest()[:16]


def build_replay_verification_report(
    *,
    native_store_root: Path,
    checkpoint_root: Path,
    generated_at: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify checkpoint→native-Transaction replay without mutating production state."""

    native_journal = native_store_root.expanduser().resolve() / "write-journal.jsonl"
    checkpoint_journal = checkpoint_root.expanduser().resolve() / "checkpoints.jsonl"
    native_present, native_bytes = _read_optional_regular_file(native_journal)
    checkpoint_present, checkpoint_bytes = _read_optional_regular_file(checkpoint_journal)
    if not checkpoint_present or not checkpoint_bytes.strip():
        raise PortfolioReplayVerifyError("replay_checkpoint_journal_missing")

    write_rows = _parse_jsonl(native_bytes, code="replay_native_journal_invalid") if native_present else []
    checkpoint_rows = _parse_jsonl(checkpoint_bytes, code="replay_checkpoint_journal_invalid")
    try:
        validate_checkpoint_journal_rows(checkpoint_rows, write_rows)
    except CheckpointError as exc:
        raise PortfolioReplayVerifyError(exc.code) from exc

    portfolio_ids = sorted({str(row.get("portfolio_id") or "") for row in checkpoint_rows if row.get("portfolio_id")})
    if not portfolio_ids:
        raise PortfolioReplayVerifyError("replay_checkpoint_portfolios_missing")

    report_time = copy.deepcopy(generated_at or timepoint())
    rows: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="investkitchen-replay-") as tmp:
        temp_root = Path(tmp)
        temp_native = temp_root / "native-write"
        temp_checkpoints = temp_root / "portfolio-checkpoints"
        temp_native.mkdir(parents=True, mode=0o700)
        temp_checkpoints.mkdir(parents=True, mode=0o700)
        if native_present:
            (temp_native / "write-journal.jsonl").write_bytes(native_bytes)
        (temp_checkpoints / "checkpoints.jsonl").write_bytes(checkpoint_bytes)

        write_store = NativeWriteStore(temp_native)
        checkpoint_store = PortfolioCheckpointStore(temp_checkpoints)

        for portfolio_id in portfolio_ids:
            portfolio_ref = _opaque_portfolio_ref(portfolio_id)
            latest = next(
                (row for row in reversed(checkpoint_rows) if str(row.get("portfolio_id") or "") == portfolio_id),
                None,
            )
            if not isinstance(latest, dict):
                rows.append({
                    "portfolio_ref": portfolio_ref,
                    "status": "blocked",
                    "checkpoint_ref": None,
                    "checkpoint_base_state_digest": None,
                    "current_state_digest": None,
                    "native_transaction_delta_count": 0,
                    "journal_event_count": len(write_rows),
                    "differences": {"account_changes": 0, "position_changes": 0, "cash_changes": 0},
                    "gap_count": 0,
                    "gap_codes": [],
                    "block_code": "replay_checkpoint_not_found",
                })
                continue
            try:
                projection = project_from_latest_checkpoint(
                    checkpoint_store,
                    write_store,
                    portfolio_id=portfolio_id,
                    generated_at=report_time,
                )
                materialization = projection["materialization"]
                current = materialization["portfolio"]
                base = latest["base_portfolio"]
                delta_count = len(projection["delta"]["transaction_ids"])
                gaps = [gap for gap in materialization.get("gaps") or [] if isinstance(gap, dict)]
                gap_codes = sorted({str(gap.get("gap_code")) for gap in gaps if gap.get("gap_code")})
                if delta_count == 0:
                    status = "waiting_for_native_transaction"
                elif gaps:
                    status = "replayed_with_gaps"
                else:
                    status = "replayed"
                rows.append({
                    "portfolio_ref": portfolio_ref,
                    "status": status,
                    "checkpoint_ref": str(latest["checkpoint_id"]),
                    "checkpoint_base_state_digest": digest(_state_view(base)),
                    "current_state_digest": digest(_state_view(current)),
                    "native_transaction_delta_count": delta_count,
                    "journal_event_count": int(projection["delta"]["journal_event_count"]),
                    "differences": _state_differences(base, current),
                    "gap_count": len(gaps),
                    "gap_codes": gap_codes,
                    "block_code": None,
                })
            except (CheckpointError, StoreConflict, StoreCorrupt, PortfolioReplayVerifyError, ValueError) as exc:
                code = getattr(exc, "code", None) or "replay_projection_blocked"
                rows.append({
                    "portfolio_ref": portfolio_ref,
                    "status": "blocked",
                    "checkpoint_ref": str(latest.get("checkpoint_id") or "") or None,
                    "checkpoint_base_state_digest": digest(_state_view(latest.get("base_portfolio") or {})),
                    "current_state_digest": None,
                    "native_transaction_delta_count": 0,
                    "journal_event_count": len(write_rows),
                    "differences": {"account_changes": 0, "position_changes": 0, "cash_changes": 0},
                    "gap_count": 0,
                    "gap_codes": [],
                    "block_code": str(code),
                })

    total_delta = sum(int(row["native_transaction_delta_count"]) for row in rows)
    blocked = sum(1 for row in rows if row["status"] == "blocked")
    replayed = sum(1 for row in rows if row["status"] == "replayed")
    replayed_with_gaps = sum(1 for row in rows if row["status"] == "replayed_with_gaps")
    waiting = sum(1 for row in rows if row["status"] == "waiting_for_native_transaction")
    overall = "blocked" if blocked else ("waiting_for_native_transaction" if total_delta == 0 else ("replayed_with_gaps" if replayed_with_gaps else "replayed"))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "replay_report_version": 1,
        "generated_at": report_time,
        "privacy_mode": "summary_only",
        "status": overall,
        "input_fingerprints": {
            "native_write_journal_present": native_present,
            "native_write_journal_sha256": _sha256(native_bytes),
            "checkpoint_journal_sha256": _sha256(checkpoint_bytes),
            "native_write_event_count": len(write_rows),
            "checkpoint_count": len(checkpoint_rows),
        },
        "summary": {
            "portfolio_count": len(rows),
            "waiting_count": waiting,
            "replayed_count": replayed,
            "replayed_with_gaps_count": replayed_with_gaps,
            "blocked_count": blocked,
            "native_transaction_delta_count": total_delta,
        },
        "portfolios": rows,
    }
