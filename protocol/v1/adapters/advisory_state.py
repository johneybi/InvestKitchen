from __future__ import annotations

from typing import Any

from protocol.v1.adapters.common import PROTOCOL_VERSION, result_envelope, timepoint
from protocol.v1.runtime.advisory_state_store import AdvisoryPlanStore, AdvisoryPolicyStore, OpenItemStore


def get_open_items(store: OpenItemStore, *, portfolio_id: str, limit: int = 50) -> dict[str, Any]:
    rows = [row for row in store.list_items() if row.get("portfolio_id") == portfolio_id]
    rows.sort(key=lambda row: ({"high": 0, "medium": 1, "low": 2}.get(str(row.get("priority")), 9), str(row.get("item_id"))))
    bounded_limit = max(1, min(int(limit), 100))
    items = rows[:bounded_limit]
    generated_at = timepoint()
    gaps = [{
        "gap_code": str(row["item_id"]),
        "scope": portfolio_id,
        "reason": str(row["question"]),
        "impact": str(row.get("impact") or row.get("rule") or "Unresolved source item remains open."),
        "recoverable": True,
    } for row in items]
    return result_envelope(
        capability="openitem.current",
        producer="investkitchen.advisory-state",
        status="ok",
        data={"portfolio_id": portfolio_id, "items": items, "count": len(items), "total_matching": len(rows), "truncated": len(rows) > len(items)},
        authority="unresolved_data_gap",
        generated_at=generated_at,
        freshness="local",
        source_mode="local_store",
        permissions_used=["openitem.read"],
        gaps=gaps,
    )


def get_current_policy(store: AdvisoryPolicyStore, *, portfolio_id: str) -> dict[str, Any]:
    rows = [row for row in store.list_policies() if row.get("portfolio_id") == portfolio_id and row.get("status") == "active"]
    generated_at = timepoint()
    return result_envelope(
        capability="policy.current",
        producer="investkitchen.advisory-state",
        status="ok" if rows else "partial",
        data={"portfolio_id": portfolio_id, "policies": rows},
        authority="user_policy_record",
        generated_at=generated_at,
        freshness="local",
        source_mode="local_store",
        permissions_used=["policy.read"],
    )


def get_active_plans(store: AdvisoryPlanStore, *, portfolio_id: str) -> dict[str, Any]:
    rows = [row for row in store.list_plans() if row.get("portfolio_id") == portfolio_id and row.get("status") == "active"]
    generated_at = timepoint()
    return result_envelope(
        capability="plan.active",
        producer="investkitchen.advisory-state",
        status="ok" if rows else "partial",
        data={"portfolio_id": portfolio_id, "plans": rows},
        authority="advisory_plan",
        generated_at=generated_at,
        freshness="local",
        source_mode="local_store",
        permissions_used=["plan.read"],
    )


def get_plan_history(store: AdvisoryPlanStore, *, portfolio_id: str, plan_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    state = store.snapshot()
    plans = [row for row in state["plans"] if row["portfolio_id"] == portfolio_id
             and (plan_id is None or row["plan_id"] == plan_id)]
    events = [row for row in reversed(state["history"]) if row["portfolio_id"] == portfolio_id
              and (plan_id is None or plan_id in {row["plan_id"], row["prior_plan_id"]})]
    limit = max(1, min(int(limit), 100))
    fields = {"event_id", "revision", "operation", "plan_id", "prior_plan_id", "reason", "source_ref", "occurred_at"}
    return {
        "portfolio_id": portfolio_id, "plans": list(reversed(plans))[:limit],
        "events": [{key: value for key, value in event.items() if key in fields} for event in events[:limit]],
        "total_plans": len(plans), "total_events": len(events),
        "truncated": len(plans) > limit or len(events) > limit,
    }
