from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, result_envelope, timepoint
from protocol.v1.runtime.historical_decision_store import HistoricalDecisionStore
from protocol.v1.runtime.historical_transaction_store import HistoricalTransactionStore
from protocol.v1.runtime.native_write_store import NativeWriteStore


def _recorded_key(record: dict[str, Any]) -> tuple[float, str]:
    for field in ("recorded_at", "effective_at", "decided_at"):
        point = record.get(field) if isinstance(record.get(field), dict) else {}
        raw = point.get("value")
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None and "T" not in raw:
                    parsed = datetime.fromisoformat(raw + "T00:00:00+00:00")
                if parsed.tzinfo is not None:
                    return (parsed.astimezone(timezone.utc).timestamp(), str(record.get("decision_id") or record.get("transaction_id") or ""))
            except ValueError:
                pass
    return (0.0, str(record.get("decision_id") or record.get("transaction_id") or ""))


def _bounded(items: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int, bool]:
    bounded_limit = max(1, min(int(limit), 200))
    ordered = sorted(items, key=_recorded_key, reverse=True)
    return ordered[:bounded_limit], len(ordered), len(ordered) > bounded_limit


def get_decision_history(
    store: NativeWriteStore,
    *,
    portfolio_id: str,
    status: str | None = None,
    limit: int = 50,
    historical_store: HistoricalDecisionStore | None = None,
) -> dict[str, Any]:
    live_decisions = [
        row for row in store.list_resources("decision")
        if row.get("portfolio_id") == portfolio_id
        and (status is None or row.get("status") == status)
    ]
    historical_decisions = [] if historical_store is None else [
        row for row in historical_store.list_decisions()
        if row.get("portfolio_id") == portfolio_id
        and (status is None or row.get("status") == status)
    ]
    decisions: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    for row in historical_decisions + live_decisions:
        decision_id = str(row.get("decision_id") or "")
        encoded = canonical_json(row)
        if decision_id in by_id:
            if by_id[decision_id] != encoded:
                raise ValueError("decision_history_identity_conflict")
            continue
        by_id[decision_id] = encoded
        decisions.append(row)
    items, total, truncated = _bounded(decisions, limit)
    generated_at = timepoint()
    data = {
        "protocol_version": PROTOCOL_VERSION,
        "history_type": "decision",
        "portfolio_id": portfolio_id,
        "status": status,
        "generated_at": generated_at,
        "items": items,
        "count": len(items),
        "total_matching": total,
        "truncated": truncated,
        "history_sources": [
            *(["historical_migration"] if historical_store is not None else []),
            "native_write",
        ],
    }
    return result_envelope(
        capability="decision.history",
        producer="trademind.native-history",
        status="ok",
        data=data,
        authority="decision_record",
        generated_at=generated_at,
        freshness="local",
        source_mode="local_store",
        permissions_used=["decision.history.read"],
    )


def get_transaction_history(
    store: NativeWriteStore,
    *,
    portfolio_id: str,
    account_id: str | None = None,
    limit: int = 100,
    historical_store: HistoricalTransactionStore | None = None,
) -> dict[str, Any]:
    live_transactions = [
        row for row in store.list_resources("transaction")
        if row.get("portfolio_id") == portfolio_id
        and (account_id is None or row.get("account_id") == account_id)
    ]
    historical_transactions = [] if historical_store is None else [
        row for row in historical_store.list_transactions()
        if row.get("portfolio_id") == portfolio_id
        and (account_id is None or row.get("account_id") == account_id)
    ]
    transactions: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    for row in historical_transactions + live_transactions:
        transaction_id = str(row.get("transaction_id") or "")
        encoded = canonical_json(row)
        if transaction_id in by_id:
            if by_id[transaction_id] != encoded:
                raise ValueError("transaction_history_identity_conflict")
            continue
        by_id[transaction_id] = encoded
        transactions.append(row)
    items, total, truncated = _bounded(transactions, limit)
    generated_at = timepoint()
    data = {
        "protocol_version": PROTOCOL_VERSION,
        "history_type": "transaction",
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "generated_at": generated_at,
        "items": items,
        "count": len(items),
        "total_matching": total,
        "truncated": truncated,
        "history_sources": [
            *(["historical_migration"] if historical_store is not None else []),
            "native_write",
        ],
    }
    return result_envelope(
        capability="transaction.history",
        producer="trademind.native-history",
        status="ok",
        data=data,
        authority="transaction_record",
        generated_at=generated_at,
        freshness="local",
        source_mode="local_store",
        permissions_used=["transaction.history.read"],
    )
