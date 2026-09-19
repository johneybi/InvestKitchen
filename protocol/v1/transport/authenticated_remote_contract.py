from __future__ import annotations

from datetime import datetime
from typing import Any

from protocol.v1.security.remote_gateway import (
    ReplayGuard,
    authorize_authenticated_request,
    filter_tool_catalog_for_grant,
    load_tool_catalog,
)
from protocol.v1.transport.stdio_rpc import LocalToolRouter


class AuthenticatedRemoteContractHarness:
    """Transport-neutral authenticated boundary smoke harness.

    Authentication itself is intentionally external. A future HTTP/MCP layer
    validates credentials, then supplies `principal` and `grant` server-side.
    Neither object is accepted from the wire request.
    """

    def __init__(
        self,
        router: LocalToolRouter,
        *,
        principal: dict[str, Any],
        grant: dict[str, Any],
        replay_guard: ReplayGuard | None = None,
        catalog: dict[str, Any] | None = None,
    ) -> None:
        self.router = router
        self.principal = principal
        self.grant = grant
        self.replay_guard = replay_guard or ReplayGuard()
        self.catalog = catalog or load_tool_catalog()
        self.audit_events: list[dict[str, Any]] = []

    def handle_authenticated_request(
        self,
        request: dict[str, Any],
        *,
        now: datetime,
        rate_limit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rate_limit = rate_limit or {
            "status": "ok",
            "policy_id": "default-read",
            "scope": "client",
            "retry_after_seconds": None,
        }
        decision, audit = authorize_authenticated_request(
            request,
            principal=self.principal,
            grant=self.grant,
            replay_guard=self.replay_guard,
            now=now,
            catalog=self.catalog,
            rate_limit=rate_limit,
        )
        security = {
            "audit_ref": decision["audit_ref"],
            "authorization": "allowed" if decision["allowed"] else "denied",
            "rate_limit": rate_limit,
        }
        if not decision["allowed"]:
            audit["result_status"] = "denied"
            self.audit_events.append(audit)
            return {
                "request_id": request.get("request_id"),
                "ok": False,
                "error": {
                    "code": decision["reason_code"],
                    "message": "TradeMind remote request was not authorized",
                },
                "security": security,
            }

        if request["method"] == "tools.list":
            result = filter_tool_catalog_for_grant(self.catalog, self.grant)
            local = {"ok": True, "result": result}
        else:
            local = self.router.handle_request({
                "request_id": request["request_id"],
                "method": request["method"],
                "params": request["params"],
            })

        audit["result_status"] = "ok" if local.get("ok") else str((local.get("error") or {}).get("code") or "error")
        self.audit_events.append(audit)
        if local.get("ok"):
            return {
                "request_id": request["request_id"],
                "ok": True,
                "result": local.get("result"),
                "security": security,
            }
        return {
            "request_id": request["request_id"],
            "ok": False,
            "error": local.get("error") or {"code": "tool_error", "message": "TradeMind tool invocation failed"},
            "security": security,
        }
