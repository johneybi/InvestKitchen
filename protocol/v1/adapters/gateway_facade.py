from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any, Callable

from .capability_registry import get_capabilities, load_instance_manifest
from .common import PROTOCOL_VERSION, hide_local_locators, timepoint
from .context_composer import compose_decision_context
from .knowledge_legacy import get_current_knowledge, search_knowledge
from .portfolio_legacy import get_portfolio_state
from .reflection_adapter import start_reflection


CapabilityHandler = Callable[[dict[str, Any]], dict[str, Any]]
ConnectorHandler = Callable[[dict[str, Any]], dict[str, Any]]


_FORBIDDEN_PUBLIC_KEYS = {
    "metrics",
    "workspace",
    "source_path",
    "canonical_path",
    "normalized_path",
    "generated_files",
    "migration_gaps",
}


_COMPAT_ID_PREFIXES = {
    "legacy-position:": "position-ref:",
    "legacy-cash:": "cash-ref:",
    "legacy-policy:": "policy-ref:",
    "legacy-evidence:": "evidence-ref:",
    "legacy-object-ref:": "object-ref:",
}


def _public_scalar(value: str) -> str:
    if value == "legacy_user_policy_record":
        return "user_policy_record"
    if value.startswith("legacy:") and ("/" in value or "\\" in value):
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
        return f"evidence-ref:{suffix}"
    for internal_prefix, public_prefix in _COMPAT_ID_PREFIXES.items():
        if value.startswith(internal_prefix):
            suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
            return f"{public_prefix}{suffix}"
    return hide_local_locators(value)


def _public_message(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\blegacy\b", "source", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcompatibility\b", "source", text, flags=re.IGNORECASE)
    return str(_public_scalar(text))


def _public_gap(gap: dict[str, Any]) -> dict[str, Any]:
    code = str(gap.get("gap_code") or "source_gap")
    if code.startswith("legacy_"):
        code = code.removeprefix("legacy_")
    return _public_data({
        **gap,
        "gap_code": code,
        "reason": _public_message(gap.get("reason")),
        "impact": _public_message(gap.get("impact")),
    })


def _public_data(value: Any) -> Any:
    if isinstance(value, list):
        return [_public_data(item) for item in value]
    if not isinstance(value, dict):
        return _public_scalar(value) if isinstance(value, str) else value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _FORBIDDEN_PUBLIC_KEYS:
            continue
        if key == "source" and isinstance(item, str):
            continue
        result[key] = _public_data(item)
    return result


def publicize_capability_result(result: dict[str, Any]) -> dict[str, Any]:
    capability = str(result.get("capability") or "unknown")
    public_gaps = [
        _public_gap(gap)
        for gap in result.get("gaps") or []
        if isinstance(gap, dict)
    ]
    raw_warnings = [
        _public_message(warning)
        for warning in result.get("warnings") or []
    ]
    public = {
        **result,
        "producer": {
            "id": f"trademind.capability.{capability}",
            "version": str(result.get("contract_version") or PROTOCOL_VERSION),
        },
        "data": _public_data(result.get("data")),
        "provenance": [],
        "warnings": list(dict.fromkeys(raw_warnings)),
        "gaps": public_gaps,
    }
    for provenance in result.get("provenance") or []:
        if not isinstance(provenance, dict):
            continue
        source_type = str(provenance.get("source_type") or "capability_source")
        if source_type.startswith("legacy_"):
            source_type = source_type.removeprefix("legacy_")
        row: dict[str, Any] = {"source_type": source_type}
        source_id = provenance.get("source_id")
        if source_id and source_type not in {"market_provider", "legacy_capability"}:
            row["source_id"] = str(source_id)
        public["provenance"].append(row)
    return _public_data(public)


def build_compatibility_handlers(
    workspace: Path | None,
    *,
    market_handlers: dict[str, CapabilityHandler] | None = None,
    history_handlers: dict[str, CapabilityHandler] | None = None,
) -> dict[str, CapabilityHandler]:
    workspace = workspace.resolve() if workspace is not None else None
    handlers: dict[str, CapabilityHandler] = {
        "reflection.session": lambda payload: start_reflection(
            str(payload["mode"]),
            context=payload.get("context"),
        ),
    }
    if workspace is not None:
        handlers.update(
            {
                "portfolio.state": lambda payload: get_portfolio_state(
                    workspace,
                    str(payload["portfolio_id"]),
                ),
                "knowledge.current": lambda payload: get_current_knowledge(
                    workspace,
                    max_outlook=int(payload.get("max_outlook", 20)),
                ),
                "knowledge.search": lambda payload: search_knowledge(
                    workspace,
                    str(payload.get("query") or ""),
                    limit=int(payload.get("limit", 20)),
                ),
            }
        )
    handlers.update(market_handlers or {})
    handlers.update(history_handlers or {})
    return handlers


class ReadOnlyGatewayFacade:
    """Transport-neutral Web-GPT-facing read-only TradeMind boundary."""

    def __init__(
        self,
        *,
        workspace: Path,
        manifest: dict[str, Any] | Path,
        handlers: dict[str, CapabilityHandler] | None = None,
        connector_handlers: dict[str, ConnectorHandler] | None = None,
        portfolio_ids: list[str] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.manifest = load_instance_manifest(manifest) if isinstance(manifest, Path) else manifest
        self.handlers = handlers or build_compatibility_handlers(self.workspace)
        self.connector_handlers = dict(connector_handlers or {})
        self.portfolio_ids = sorted({str(value) for value in (portfolio_ids or []) if str(value)})

    def _capability_row(self, capability: str) -> dict[str, Any] | None:
        registry = get_capabilities(self.manifest)
        return next(
            (row for row in registry["capabilities"] if row["capability"] == capability),
            None,
        )

    def _unavailable_result(self, capability: str, *, status: str, gap_code: str, reason: str) -> dict[str, Any]:
        return {
            "capability": capability,
            "contract_version": PROTOCOL_VERSION,
            "producer": {"id": f"trademind.capability.{capability}", "version": PROTOCOL_VERSION},
            "status": status,
            "data": None,
            "generated_at": timepoint(),
            "data_times": {},
            "freshness": "unknown",
            "source_mode": "unknown",
            "provenance": [],
            "authority": "unknown",
            "warnings": ["Capability is unavailable to this client."],
            "gaps": [{
                "gap_code": gap_code,
                "required_capability": capability,
                "scope": None,
                "reason": reason,
                "impact": "The requested TradeMind capability cannot complete.",
                "recoverable": True,
            }],
            "conflicts": [],
            "permissions_used": [],
        }

    def _invoke(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._capability_row(capability)
        if row is None or not row.get("client_usable"):
            return self._unavailable_result(
                capability,
                status="blocked",
                gap_code="capability_not_client_usable",
                reason=str((row or {}).get("reason") or "not_exposed_or_not_ready"),
            )
        handler = self.handlers.get(capability)
        if handler is None:
            return self._unavailable_result(
                capability,
                status="unavailable",
                gap_code="capability_handler_missing",
                reason="The instance declares the capability but no runtime handler is attached.",
            )
        return publicize_capability_result(handler(dict(payload)))

    def connector_tool_ready(self, tool_name: str) -> bool:
        return tool_name in self.connector_handlers

    def _invoke_connector(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = self.connector_handlers.get(tool_name)
        if handler is None:
            return {
                "status": "blocked",
                "error_code": "connector_write_unavailable",
            }
        try:
            return _public_data(handler(dict(payload)))
        except Exception as exc:
            code = str(getattr(exc, "code", "connector_write_rejected") or "connector_write_rejected")
            result = {
                "status": "blocked",
                "error_code": _public_message(code),
            }
            details = getattr(exc, "details", None)
            if isinstance(details, dict) and details:
                result["details"] = _public_data(details)
            return result

    def get_capabilities(self) -> dict[str, Any]:
        projected = get_capabilities(self.manifest)
        rows = []
        for row in projected["capabilities"]:
            capability = str(row["capability"])
            runtime_handler_ready = capability == "decision.context" or capability in self.handlers
            runtime_ready = bool(row["ready"] and runtime_handler_ready)
            client_usable = bool(runtime_ready and row["client_exposure"] == "supported")
            reason = row["reason"]
            if row["ready"] and not runtime_handler_ready:
                reason = "runtime_handler_missing"
            rows.append({
                "capability": capability,
                "installed": row["installed"],
                "ready": runtime_ready,
                "client_exposure": row["client_exposure"],
                "client_usable": client_usable,
                "reason": reason,
            })
        return {
            "protocol_version": PROTOCOL_VERSION,
            "instance_id": projected.get("instance_id"),
            "profile": projected.get("profile"),
            "capabilities": rows,
        }

    def get_portfolio_state(self, portfolio_id: str) -> dict[str, Any]:
        return self._invoke("portfolio.state", {"portfolio_id": portfolio_id})

    def list_portfolios(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "portfolio_ids": list(self.portfolio_ids),
            "scope_source": "server_configured_personal_scope",
        }

    def get_current_knowledge(self, *, max_outlook: int = 20) -> dict[str, Any]:
        return self._invoke("knowledge.current", {"max_outlook": max_outlook})

    def search_knowledge(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        return self._invoke("knowledge.search", {"query": query, "limit": limit})

    def preview_knowledge_update(self, update: dict[str, Any]) -> dict[str, Any]:
        return self._invoke_connector("preview_knowledge_update", {"update": update})

    def apply_knowledge_update(self, preview_id: str, confirmation: str) -> dict[str, Any]:
        return self._invoke_connector(
            "apply_knowledge_update",
            {"preview_id": preview_id, "confirmation": confirmation},
        )

    def preview_portfolio_update(self, update: dict[str, Any]) -> dict[str, Any]:
        return self._invoke_connector("preview_portfolio_update", {"update": update})

    def apply_portfolio_update(self, preview_id: str, confirmation: str) -> dict[str, Any]:
        return self._invoke_connector(
            "apply_portfolio_update",
            {"preview_id": preview_id, "confirmation": confirmation},
        )

    def preview_account_sync(self, portfolio_id: str, account_id: str) -> dict[str, Any]:
        return self._invoke_connector(
            "preview_account_sync",
            {"portfolio_id": portfolio_id, "account_id": account_id},
        )

    def apply_account_sync(self, preview_id: str, confirmation: str) -> dict[str, Any]:
        return self._invoke_connector(
            "apply_account_sync",
            {"preview_id": preview_id, "confirmation": confirmation},
        )

    def get_market_quote(self, symbols: list[str]) -> dict[str, Any]:
        return self._invoke("market.quote", {"symbols": list(symbols)})

    def get_market_ohlcv(
        self,
        symbols: list[str],
        *,
        interval: str = "1d",
        count: int = 120,
    ) -> dict[str, Any]:
        return self._invoke(
            "market.ohlcv",
            {"symbols": list(symbols), "interval": interval, "count": count},
        )

    def get_decision_history(
        self,
        portfolio_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._invoke(
            "decision.history",
            {"portfolio_id": portfolio_id, "status": status, "limit": limit},
        )

    def get_transactions(
        self,
        portfolio_id: str,
        *,
        account_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._invoke(
            "transaction.history",
            {"portfolio_id": portfolio_id, "account_id": account_id, "limit": limit},
        )

    def get_open_items(self, portfolio_id: str, *, limit: int = 50) -> dict[str, Any]:
        return self._invoke("openitem.current", {"portfolio_id": portfolio_id, "limit": limit})

    def get_current_policy(self, portfolio_id: str) -> dict[str, Any]:
        return self._invoke("policy.current", {"portfolio_id": portfolio_id})

    def get_opinion_weighting(self) -> dict[str, Any]:
        return self._invoke("opinion.weighting.current", {})

    def build_opinion_consensus(
        self,
        question: str,
        *,
        horizon: str = "weeks",
        mode: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "opinion.consensus",
            {"question": question, "horizon": horizon, "mode": mode},
        )

    def preview_policy_update(self, update: dict[str, Any]) -> dict[str, Any]:
        return self._invoke_connector("preview_policy_update", {"update": update})

    def apply_policy_update(self, preview_id: str, confirmation: str) -> dict[str, Any]:
        return self._invoke_connector(
            "apply_policy_update",
            {"preview_id": preview_id, "confirmation": confirmation},
        )

    def preview_opinion_weighting_update(self, update: dict[str, Any]) -> dict[str, Any]:
        return self._invoke_connector("preview_opinion_weighting_update", {"update": update})

    def apply_opinion_weighting_update(self, preview_id: str, confirmation: str) -> dict[str, Any]:
        return self._invoke_connector(
            "apply_opinion_weighting_update",
            {"preview_id": preview_id, "confirmation": confirmation},
        )

    def get_active_plans(self, portfolio_id: str) -> dict[str, Any]:
        return self._invoke("plan.active", {"portfolio_id": portfolio_id})

    def build_decision_context(
        self,
        request: dict[str, Any],
        *,
        capability_inputs: dict[str, dict[str, Any]] | None = None,
        required_capabilities: set[str] | None = None,
    ) -> dict[str, Any]:
        inputs = capability_inputs or {}
        required = set(required_capabilities or set())
        requested = list(dict.fromkeys(
            [str(v) for v in request.get("requested_context") or []]
            + [str(v) for v in inputs]
            + sorted(required)
        ))
        scoped_capabilities = {
            "portfolio.state", "decision.history", "transaction.history",
            "openitem.current", "policy.current", "plan.active",
        }
        default_portfolio_id = request.get("portfolio_id")
        normalized_inputs: dict[str, dict[str, Any]] = {}
        for capability in requested:
            payload = dict(inputs.get(capability, {}))
            if (
                capability in scoped_capabilities
                and "portfolio_id" not in payload
                and isinstance(default_portfolio_id, str)
                and default_portfolio_id
            ):
                payload["portfolio_id"] = default_portfolio_id
            normalized_inputs[capability] = payload
        normalized_request = dict(request)
        normalized_request["requested_context"] = requested
        results = [
            self._invoke(capability, normalized_inputs.get(capability, {}))
            for capability in requested
            if capability != "decision.context"
        ]
        return compose_decision_context(
            normalized_request,
            results,
            required_capabilities=required,
        )

    def start_reflection(
        self,
        mode: str,
        *,
        context_bundle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = None
        if isinstance(context_bundle, dict):
            candidate = context_bundle.get("context", context_bundle)
            if isinstance(candidate, dict):
                context = candidate
        return self._invoke("reflection.session", {"mode": mode, "context": context})


def dumps_public(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
