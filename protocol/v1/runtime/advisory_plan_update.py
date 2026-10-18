"""Non-executing Plan lifecycle shared by authenticated MCP transports."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from protocol.v1.adapters.common import PROTOCOL_VERSION, digest, timepoint
from protocol.v1.runtime.advisory_plan_store import AdvisoryPlanStore
from protocol.v1.security.trusted_approval import TrustedApprovalStore


class PlanUpdateRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _point(value: datetime) -> dict[str, str]:
    return timepoint(value.isoformat().replace("+00:00", "Z"))


def _parse(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(value["value"].replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PlanUpdateRejected("plan_authority_time_invalid") from None


def authorize_plan(principal: dict, grant: dict, portfolio_id: str, *, approve: bool = False) -> None:
    for key in ("subject_user_id", "client_id", "credential_binding_id"):
        if not principal.get(key) or principal.get(key) != grant.get(key):
            raise PlanUpdateRejected("plan_identity_mismatch")
    if min(_parse(principal.get("expires_at")), _parse(grant.get("expires_at"))) <= _now():
        raise PlanUpdateRejected("plan_authority_expired")
    required = {"plan.update"} | ({"operation.approve"} if approve else set())
    if not required.issubset(grant.get("permissions") or []):
        raise PlanUpdateRejected("plan_permission_denied")
    if portfolio_id not in (grant.get("portfolio_scope") or []):
        raise PlanUpdateRejected("plan_portfolio_scope_denied")


def _validate_request(request: Any) -> None:
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/plan-update.schema.json").read_text())
    def validate(value: Any, rule: dict) -> None:
        kind = rule.get("type")
        if (kind == "object" and not isinstance(value, dict)
                or kind == "array" and not isinstance(value, list)
                or kind == "string" and not isinstance(value, str)):
            raise ValueError
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError
        if isinstance(value, dict):
            properties = rule.get("properties", {})
            if (not set(rule.get("required", [])).issubset(value)
                    or rule.get("additionalProperties") is False and set(value) - set(properties)):
                raise ValueError
            for key in set(value) & set(properties):
                validate(value[key], properties[key])
        if isinstance(value, list):
            if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 100000):
                raise ValueError
            for item in value:
                validate(item, rule.get("items", {}))
        if isinstance(value, str) and (not value.strip() or not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 100000)):
            raise ValueError
    try:
        validate(request, schema)
        if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode()) > 60000:
            raise PlanUpdateRejected("plan_update_too_large")
    except (ValueError, TypeError):
        raise PlanUpdateRejected("plan_update_invalid") from None
    operation = request["operation"]
    if (operation == "create" and "plan_id" in request
            or operation in {"replace", "close"} and "plan_id" not in request
            or operation == "close" and "plan" in request
            or operation != "close" and "plan" not in request):
        raise PlanUpdateRejected("plan_update_shape_invalid")


def build_plan_update_preview(store: AdvisoryPlanStore, request: dict[str, Any]) -> dict[str, Any]:
    _validate_request(request)
    state = store.snapshot()
    portfolio_id, operation = request["portfolio_id"], request["operation"]
    active = [row for row in state["plans"] if row["portfolio_id"] == portfolio_id and row["status"] == "active"]
    if operation == "create" and active:
        raise PlanUpdateRejected("plan_active_exists_use_replace")
    if operation != "create" and (not active or active[0]["plan_id"] != request["plan_id"]):
        raise PlanUpdateRejected("plan_target_not_active")
    now = _now()
    next_plan = None
    if operation != "close":
        next_plan = {
            **copy.deepcopy(request["plan"]),
            "protocol_version": PROTOCOL_VERSION,
            "plan_id": "plan:" + uuid.uuid4().hex,
            "portfolio_id": portfolio_id, "status": "active", "authority": "advisory_plan",
            "created_at": _point(now), "effective_on": _point(now),
            "source_evidence": [request["source_ref"]],
            "order_authorized": False, "automatic_execution": False,
            "requires_current_portfolio_and_market_revalidation": True,
            "trigger_is_not_execution_authority": True,
        }
        if active:
            next_plan["supersedes"] = active[0]["plan_id"]
    candidate = {
        "operation": operation, "portfolio_id": portfolio_id,
        "target_plan_id": request.get("plan_id"), "next_plan": next_plan,
        "reason": request["reason"], "source_ref": request["source_ref"],
        "base_digest": state["state_digest"], "prepared_at": _point(now),
        "expires_at": _point(now + timedelta(seconds=300)),
    }
    candidate_digest = digest(candidate)
    return {
        "status": "review_required", "preview_id": "plan-preview:" + uuid.uuid4().hex,
        "candidate": candidate, "candidate_digest": candidate_digest,
        "confirmation": "APPROVE PLAN UPDATE " + candidate_digest[:12],
    }


def approval_envelope(preview: dict) -> dict:
    candidate = preview["candidate"]
    target = {"portfolio_id": candidate["portfolio_id"]}
    return {
        "operation": {"action": "plan.update", "target": target, "state": "awaiting_approval"},
        "approval_required": True, "apply_state": "not_applied",
        "preview": {
            "preview_id": preview["preview_id"], "operation_id": "operation:" + digest(preview)[:24],
            "action": "plan.update", "target": target, "base_version": candidate["base_digest"],
            "canonical_payload": preview, "payload_digest": digest(preview),
            "expires_at": candidate["expires_at"],
        },
    }


def apply_plan_update(
    store: AdvisoryPlanStore, preview: dict, approval: dict, *, principal: dict, grant: dict,
    approval_verifier: Callable[[dict, dict, dict], dict],
) -> dict:
    candidate = preview.get("candidate")
    if (preview.get("status") != "review_required" or not isinstance(candidate, dict)
            or preview.get("candidate_digest") != digest(candidate)):
        raise PlanUpdateRejected("plan_preview_invalid")
    operation = candidate.get("operation")
    next_plan = candidate.get("next_plan")
    request = {key: candidate.get(key) for key in ("operation", "portfolio_id", "source_ref", "reason")}
    if candidate.get("target_plan_id") is not None:
        request["plan_id"] = candidate["target_plan_id"]
    if next_plan is not None:
        if not isinstance(next_plan, dict):
            raise PlanUpdateRejected("plan_preview_invalid")
        content_keys = {"title", "objective", "stages", "global_invalidation", "confirmation_sequence"}
        request["plan"] = {key: value for key, value in next_plan.items() if key in content_keys}
        if (next_plan.get("portfolio_id") != candidate.get("portfolio_id")
                or next_plan.get("status") != "active" or next_plan.get("authority") != "advisory_plan"
                or next_plan.get("source_evidence") != [candidate.get("source_ref")]
                or next_plan.get("supersedes") != candidate.get("target_plan_id")):
            raise PlanUpdateRejected("plan_candidate_scope_or_lineage_invalid")
    _validate_request(request)
    portfolio_id = candidate["portfolio_id"]
    authorize_plan(principal, grant, portfolio_id, approve=True)
    if min(_parse(candidate["expires_at"]), _parse(approval.get("expires_at"))) <= _now():
        raise PlanUpdateRejected("plan_preview_or_approval_expired")
    expected = approval_envelope(preview)["preview"]
    if (approval.get("preview_id") != preview["preview_id"]
            or approval.get("authenticated_user_id") != principal["subject_user_id"]
            or approval.get("client_id") != principal["client_id"]
            or approval.get("approved_payload_digest") != expected["payload_digest"]
            or approval.get("base_version") != candidate["base_digest"]
            or approval.get("target") != expected["target"]):
        raise PlanUpdateRejected("plan_approval_binding_mismatch")
    verification = approval_verifier(approval, principal, grant)
    if verification.get("verified") is not True or not verification.get("verification_ref"):
        raise PlanUpdateRejected("plan_trusted_approval_required")
    state = store.snapshot()
    if state["state_digest"] != candidate["base_digest"]:
        raise PlanUpdateRejected("plan_preview_stale")
    rows = copy.deepcopy(state["plans"])
    before = next((row for row in rows if row["plan_id"] == candidate["target_plan_id"]), None)
    original = copy.deepcopy(before)
    if operation != "create":
        if before is None or before["portfolio_id"] != portfolio_id or before["status"] != "active":
            raise PlanUpdateRejected("plan_target_not_active")
        before["status"] = "closed" if operation == "close" else "superseded"
        before["ended_at"] = _point(_now())
        before["end_reason"] = candidate["reason"]
        if candidate["next_plan"] is not None:
            before["superseded_by"] = candidate["next_plan"]["plan_id"]
    if operation != "close":
        rows.append(copy.deepcopy(candidate["next_plan"]))
    event = {
        "event_id": "plan-event:" + uuid.uuid4().hex, "preview_id": preview["preview_id"],
        "portfolio_id": portfolio_id, "operation": operation,
        "plan_id": candidate["next_plan"]["plan_id"] if operation != "close" else candidate["target_plan_id"],
        "prior_plan_id": candidate["target_plan_id"], "before": original,
        "after": copy.deepcopy(candidate["next_plan"] if operation != "close" else before),
        "reason": candidate["reason"], "source_ref": candidate["source_ref"],
        "occurred_at": _point(_now()), "approval_id": approval["approval_id"],
        "verification_ref": verification["verification_ref"],
    }
    try:
        new_digest = store.replace_plans(rows, expected_digest=candidate["base_digest"], event=event)
    except ValueError as exc:
        raise PlanUpdateRejected(str(exc)) from None
    # Read through a fresh instance, including checksum/history validation.
    read_back = AdvisoryPlanStore(store.root).snapshot()
    if (read_back["state_digest"] != new_digest or read_back["plans"] != rows
            or read_back["history"][-1]["event_id"] != event["event_id"]):
        raise PlanUpdateRejected("plan_readback_mismatch")
    return {
        "status": "applied", "preview_id": preview["preview_id"], "operation": operation,
        "portfolio_id": portfolio_id, "plan_id": event["plan_id"],
        "prior_plan_id": event["prior_plan_id"], "revision": len(read_back["history"]),
        "read_back": {"matches": True, "active_plan_ids": [row["plan_id"] for row in rows
                      if row["portfolio_id"] == portfolio_id and row["status"] == "active"]},
    }


class PlanUpdateCoordinator:
    """Identity-bound, single-use, expiring previews; restart invalidates them."""
    def __init__(self, store: AdvisoryPlanStore, approval_store: TrustedApprovalStore, principal: dict, grant: dict) -> None:
        self.store, self.approval_store = store, approval_store
        self.principal, self.grant = copy.deepcopy(principal), copy.deepcopy(grant)
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()

    def review(self, preview_id: str) -> dict:
        with self.lock:
            preview = self.pending.get(preview_id)
            if preview is None or _parse(preview["candidate"]["expires_at"]) <= _now():
                raise PlanUpdateRejected("plan_preview_not_found_or_expired")
            return copy.deepcopy(preview)

    def discard(self, preview_id: str) -> None:
        with self.lock:
            self.pending.pop(preview_id, None)

    def preview(self, payload: dict) -> dict:
        update = payload.get("update")
        _validate_request(update)
        authorize_plan(self.principal, self.grant, update["portfolio_id"])
        preview = build_plan_update_preview(self.store, update)
        with self.lock:
            self.pending = {key: value for key, value in self.pending.items()
                            if _parse(value["candidate"]["expires_at"]) > _now()}
            if len(self.pending) >= 100:
                raise PlanUpdateRejected("plan_pending_limit")
            self.pending[preview["preview_id"]] = copy.deepcopy(preview)
        return copy.deepcopy(preview)

    def apply(self, payload: dict) -> dict:
        with self.lock:
            preview = self.pending.pop(str(payload.get("preview_id") or ""), None)
        if preview is None:
            raise PlanUpdateRejected("plan_preview_not_found_or_consumed")
        authorize_plan(self.principal, self.grant, preview["candidate"]["portfolio_id"], approve=True)
        if payload.get("confirmation") != preview["confirmation"]:
            raise PlanUpdateRejected("plan_confirmation_mismatch")
        approval = self.approval_store.issue(
            approval_envelope(preview), principal=self.principal, grant=self.grant,
            interaction_ref="mcp-confirmation:" + digest([preview["preview_id"], preview["confirmation"]])[:24],
            approval_method="mcp_explicit_confirmation", ttl_seconds=300,
        )
        return apply_plan_update(self.store, preview, approval, principal=self.principal, grant=self.grant,
                                 approval_verifier=self.approval_store.verify)
