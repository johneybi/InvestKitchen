from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol.v1.adapters.common import timepoint


CATALOG_PATH = Path(__file__).resolve().parents[1] / "transport" / "tool_catalog.json"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_timepoint(value: dict[str, Any]) -> datetime:
    raw = value.get("value") if isinstance(value, dict) else None
    if not isinstance(raw, str):
        raise ValueError("invalid time point")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time point must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def load_tool_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("tools"), list):
        raise ValueError("invalid tool catalog")
    return value


def _tool_map(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(tool["name"]): tool
        for tool in catalog.get("tools", [])
        if isinstance(tool, dict) and tool.get("name")
    }


_CAPABILITY_PERMISSION = {
    "portfolio.state": "portfolio.read",
    "knowledge.current": "knowledge.read",
    "knowledge.search": "knowledge.read",
    "market.quote": "market.read",
    "market.ohlcv": "market.read",
    "decision.context": "decision.context.build",
    "reflection.session": "reflection.session.start",
}


def required_permissions(tool_name: str, arguments: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    tool = _tool_map(catalog).get(tool_name)
    if tool is None:
        return []
    permissions = set(str(value) for value in tool.get("required_permissions", []))
    if tool_name == "build_decision_context":
        request = arguments.get("request") if isinstance(arguments.get("request"), dict) else {}
        for capability in request.get("requested_context") or []:
            permission = _CAPABILITY_PERMISSION.get(str(capability))
            if permission:
                permissions.add(permission)
    return sorted(permissions)


def portfolio_ids_for_request(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    values: set[str] = set()
    if tool_name in {"get_portfolio_state", "get_decision_history", "get_transactions"}:
        portfolio_id = arguments.get("portfolio_id")
        if portfolio_id:
            values.add(str(portfolio_id))
    elif tool_name == "build_decision_context":
        request = arguments.get("request") if isinstance(arguments.get("request"), dict) else {}
        if request.get("portfolio_id"):
            values.add(str(request["portfolio_id"]))
        inputs = arguments.get("capability_inputs") if isinstance(arguments.get("capability_inputs"), dict) else {}
        portfolio_input = inputs.get("portfolio.state") if isinstance(inputs.get("portfolio.state"), dict) else {}
        if portfolio_input.get("portfolio_id"):
            values.add(str(portfolio_input["portfolio_id"]))
    elif tool_name == "preview_portfolio_update":
        update = arguments.get("update") if isinstance(arguments.get("update"), dict) else {}
        if update.get("portfolio_id"):
            values.add(str(update["portfolio_id"]))
    elif tool_name == "preview_account_sync":
        if arguments.get("portfolio_id"):
            values.add(str(arguments["portfolio_id"]))
    return sorted(values)


@dataclass
class ReplayGuard:
    """In-memory contract smoke guard; production persistence is intentionally undecided."""

    seen: set[tuple[str, str]] = field(default_factory=set)

    def accept_once(self, client_id: str, nonce: str) -> bool:
        key = (client_id, nonce)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def _audit_id(request_id: str, request_digest: str) -> str:
    return f"audit:{_digest([request_id, request_digest])[:24]}"


def authorize_authenticated_request(
    request: dict[str, Any],
    *,
    principal: dict[str, Any],
    grant: dict[str, Any],
    replay_guard: ReplayGuard,
    now: datetime,
    catalog: dict[str, Any] | None = None,
    rate_limit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Authorize one already-authenticated wire request.

    `principal` and `grant` are server-side inputs supplied by the future auth
    transport. They are never read from the client wire request.
    """
    catalog = catalog or load_tool_catalog()
    rate_limit = rate_limit or {"status": "ok", "policy_id": "default-read", "scope": "client", "retry_after_seconds": None}
    request_id = str(request.get("request_id") or "unknown-request")
    request_digest = _digest(request)
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    tool_name = None
    arguments: dict[str, Any] = {}
    if method == "tools.call":
        tool_name = params.get("name") if isinstance(params.get("name"), str) else None
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    elif method == "tools.list":
        tool_name = "tools.list"

    reason = "allowed"
    permissions: list[str] = []
    portfolio_ids: list[str] = []
    allowed = True

    try:
        required_fields = {"request_id", "instance_id", "method", "params", "issued_at", "expires_at", "nonce"}
        forbidden_identity_fields = {"authenticated_user_id", "subject_user_id", "client_id", "credential_binding_id", "permissions", "grant_id"}
        if set(request) != required_fields or set(request) & forbidden_identity_fields:
            raise ValueError("malformed_request")
        if method not in {"tools.list", "tools.call"}:
            raise ValueError("malformed_request")
        if str(request["instance_id"]) != str(grant["instance_id"]):
            raise ValueError("instance_mismatch")
        if str(principal["client_id"]) != str(grant["client_id"]):
            raise ValueError("client_mismatch")
        if str(principal["subject_user_id"]) != str(grant["subject_user_id"]):
            raise ValueError("subject_mismatch")
        if str(principal["credential_binding_id"]) != str(grant["credential_binding_id"]):
            raise ValueError("credential_binding_mismatch")
        if _parse_timepoint(principal["expires_at"]) <= now:
            raise ValueError("principal_expired")
        if _parse_timepoint(grant["expires_at"]) <= now:
            raise ValueError("grant_expired")
        issued_at = _parse_timepoint(request["issued_at"])
        expires_at = _parse_timepoint(request["expires_at"])
        request_policy = grant.get("request_policy") if isinstance(grant.get("request_policy"), dict) else {}
        max_ttl_seconds = int(request_policy.get("max_ttl_seconds", 0))
        max_future_skew_seconds = int(request_policy.get("max_future_skew_seconds", 0))
        if expires_at <= now:
            raise ValueError("request_expired")
        if issued_at.timestamp() > now.timestamp() + max_future_skew_seconds:
            raise ValueError("request_not_yet_valid")
        if max_ttl_seconds <= 0 or (expires_at - issued_at).total_seconds() > max_ttl_seconds:
            raise ValueError("request_ttl_exceeded")
        if not replay_guard.accept_once(str(principal["client_id"]), str(request["nonce"])):
            raise ValueError("replay_detected")
        if rate_limit.get("status") == "limited":
            raise ValueError("rate_limited")

        if method == "tools.list":
            permissions = ["system.tools.list"]
            if "system.tools.list" not in set(str(value) for value in grant.get("permissions", [])):
                raise ValueError("permission_denied")
        else:
            tool = _tool_map(catalog).get(str(tool_name))
            if tool is None:
                raise ValueError("tool_not_found")
            allowlist = [str(value) for value in grant.get("tool_allowlist", [])]
            if allowlist and str(tool_name) not in allowlist:
                raise ValueError("tool_not_allowed")
            permissions = required_permissions(str(tool_name), arguments, catalog)
            granted_permissions = set(str(value) for value in grant.get("permissions", []))
            if not set(permissions).issubset(granted_permissions):
                raise ValueError("permission_denied")
            portfolio_ids = portfolio_ids_for_request(str(tool_name), arguments)
            if len(portfolio_ids) > 1:
                raise ValueError("portfolio_scope_conflict")
            granted_portfolios = set(str(value) for value in grant.get("portfolio_scope", []))
            if any(portfolio_id not in granted_portfolios for portfolio_id in portfolio_ids):
                raise ValueError("portfolio_scope_denied")

    except (KeyError, TypeError, ValueError) as exc:
        allowed = False
        candidate = str(exc.args[0]) if exc.args else "malformed_request"
        reason = candidate if candidate in {
            "malformed_request", "instance_mismatch", "principal_expired", "grant_expired",
            "request_expired", "request_not_yet_valid", "request_ttl_exceeded", "client_mismatch", "subject_mismatch",
            "credential_binding_mismatch", "replay_detected", "tool_not_found", "tool_not_allowed",
            "permission_denied", "portfolio_scope_denied", "portfolio_scope_conflict", "rate_limited",
        } else "malformed_request"

    decided_at = timepoint(now.isoformat().replace("+00:00", "Z"))
    audit_ref = _audit_id(request_id, request_digest)
    decision = {
        "decision_id": f"authorization:{_digest([request_id, reason, request_digest])[:24]}",
        "request_id": request_id,
        "allowed": allowed,
        "reason_code": reason,
        "tool_name": tool_name,
        "permissions_evaluated": sorted(set(permissions)),
        "portfolio_scope_evaluated": sorted(set(portfolio_ids)),
        "request_digest": request_digest,
        "decided_at": decided_at,
        "audit_ref": audit_ref,
    }
    audit = {
        "audit_event_id": audit_ref,
        "request_id": request_id,
        "instance_id": str(request.get("instance_id") or grant.get("instance_id") or "unknown-instance"),
        "subject_user_id": str(principal.get("subject_user_id") or "unknown-subject"),
        "client_id": str(principal.get("client_id") or "unknown-client"),
        "credential_binding_ref": str(principal.get("credential_binding_id") or "unknown-binding"),
        "tool_name": tool_name,
        "permissions": sorted(set(permissions)),
        "portfolio_ids": sorted(set(portfolio_ids)),
        "authorization": "allowed" if allowed else "denied",
        "reason_code": reason,
        "request_digest": request_digest,
        "occurred_at": decided_at,
        "result_status": None,
    }
    return decision, audit


def filter_tool_catalog_for_grant(catalog: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
    permissions = set(str(value) for value in grant.get("permissions", []))
    allowlist = set(str(value) for value in grant.get("tool_allowlist", []))
    tools = []
    for tool in catalog.get("tools", []):
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        name = str(tool["name"])
        if allowlist and name not in allowlist:
            continue
        required = set(str(value) for value in tool.get("required_permissions", []))
        if not required.issubset(permissions):
            continue
        tools.append(tool)
    return {
        "protocol_version": catalog.get("protocol_version"),
        "transport_surface": catalog.get("transport_surface"),
        "tools": tools,
    }
