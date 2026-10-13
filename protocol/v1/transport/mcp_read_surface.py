"""A deliberately small, grant-filtered read surface for remote AI clients."""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from mcp.server.auth.provider import AccessToken

from protocol.v1.adapters.gateway_facade import ReadOnlyGatewayFacade
from protocol.v1.security.oauth_resource import OAuthResourceConfig, READ_SCOPE, ReadGrant
from protocol.v1.transport.stdio_rpc import LocalToolRouter, RequestError


READ_TOOLS = {
    "get_capabilities": None,
    "list_portfolios": None,
    "get_portfolio_state": "portfolio.state",
    "get_current_knowledge": "knowledge.current",
    "search_knowledge": "knowledge.search",
    "resolve_krx_securities": None,
    "get_market_quote": "market.quote",
    "get_market_ohlcv": "market.ohlcv",
    "build_decision_context": "decision.context",
}
CONTEXT_TOOLS = {capability: tool for tool, capability in READ_TOOLS.items()
                 if capability and capability != "decision.context"}
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


class ReadSurfaceError(ValueError):
    """A safe, bounded message suitable for a model-facing tool result."""


def _request_schema() -> dict[str, Any]:
    request = json.loads((SCHEMAS / "decision-context.schema.json").read_text())["$defs"]["decisionRequest"]
    common = json.loads((SCHEMAS / "common.schema.json").read_text())["$defs"]

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(common[value["$ref"].split("#/$defs/")[1]])
            return {key: inline(item) for key, item in value.items()}
        return value

    return inline(request)


def _context_input(capability: str, payload: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Normalize at the remote boundary, independently of Gateway version."""
    payload = dict(payload)
    if not capability.startswith("market."):
        return payload
    symbols = payload.get("symbols")
    if isinstance(symbols, str):
        symbols = [value for value in re.split(r"[\s,;/|]+", symbols.strip()) if value]
    if not symbols:
        symbols = []
        for ref in request.get("subject_refs", []):
            match = re.fullmatch(r"(?i)(?:asset:|KRX:)?([0-9A-Z]{6})", ref.strip())
            if not match:
                match = re.fullmatch(r"(?i)(?:NASDAQ|NYSE|AMEX):([A-Z][A-Z0-9.\-]{0,31})", ref.strip())
            if match and match.group(1).upper() not in symbols:
                symbols.append(match.group(1).upper())
    if symbols:
        payload["symbols"] = symbols
    return payload


class RemoteReadSurface:
    def __init__(self, gateway: ReadOnlyGatewayFacade, config: OAuthResourceConfig) -> None:
        self.gateway = gateway
        self.config = config
        self.router = LocalToolRouter(gateway)
        for grant in config.grants:
            if not set(grant.portfolio_scope).issubset(gateway.portfolio_ids):
                raise ValueError("Grant portfolio scope is not configured in the runtime")
        self.schemas = {name: copy.deepcopy(self.router.tools[name]["input_schema"])
                        for name in READ_TOOLS if name in self.router.tools}
        context = self.schemas["build_decision_context"]
        context["properties"]["request"] = _request_schema()
        context["properties"]["request"]["properties"]["requested_context"] = {
            "type": ["array", "null"], "items": {"enum": sorted(CONTEXT_TOOLS)},
            "maxItems": len(CONTEXT_TOOLS), "uniqueItems": True,
        }
        inputs = {}
        for capability, name in CONTEXT_TOOLS.items():
            schema = copy.deepcopy(self.schemas[name])
            schema["required"] = [key for key in schema.get("required", [])
                                  if key not in {"portfolio_id", "symbols"}]
            if capability.startswith("market."):
                schema["properties"]["symbols"] = {
                    **schema["properties"]["symbols"], "type": ["array", "string"],
                    "minLength": 1, "maxLength": 2048 if capability == "market.quote" else 512,
                }
            inputs[capability] = schema
        context["properties"]["capability_inputs"] = {
            "type": "object", "properties": inputs, "additionalProperties": False,
        }
        context["properties"]["required_capabilities"]["items"] = {"enum": sorted(CONTEXT_TOOLS)}

    def grant(self, token: AccessToken | None) -> ReadGrant:
        if (token is None or token.resource != self.config.resource_url
                or READ_SCOPE not in token.scopes or token.expires_at is None
                or token.expires_at <= time.time()):
            raise ReadSurfaceError("Authentication required")
        grant = self.config.grant_for(token.subject, token.client_id)
        if grant is None:
            raise ReadSurfaceError("Authentication required")
        return grant

    def tool_names(self, grant: ReadGrant) -> set[str]:
        capabilities = {row["capability"]: row["client_usable"]
                        for row in self.gateway.get_capabilities()["capabilities"]}
        return {name for name, capability in READ_TOOLS.items()
                if name in self.router.available_tool_names()
                and self.router.tools[name].get("read_only") is True
                and set(self.router.tools[name]["required_permissions"]).issubset(grant.permissions)
                and (capability is None or capabilities.get(capability) is True)}

    def list_tools(self, token: AccessToken | None) -> list[dict[str, Any]]:
        grant = self.grant(token)
        return [{
            "name": name,
            "description": self.router.tools[name]["description"],
            "inputSchema": self.schemas[name],
            "annotations": {
                "readOnlyHint": True, "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": bool(self.router.tools[name].get("open_world")),
            },
        } for name in sorted(self.tool_names(grant))]

    def call_tool(self, token: AccessToken | None, name: str, arguments: dict[str, Any]) -> Any:
        grant = self.grant(token)
        available = self.tool_names(grant)
        if name not in available:
            raise ReadSurfaceError("Unknown or unavailable tool")
        try:
            Draft202012Validator(self.schemas[name]).validate(arguments)
        except ValidationError:
            raise ReadSurfaceError("Invalid tool arguments") from None
        arguments = copy.deepcopy(arguments)
        if name == "get_portfolio_state":
            self._check_portfolio(grant, arguments["portfolio_id"])
        elif name == "build_decision_context":
            self._check_context(grant, available, arguments)
        try:
            result = self.router.call_tool(name, arguments)
        except RequestError:
            raise ReadSurfaceError("Invalid tool arguments") from None
        if name == "list_portfolios":
            result = {**result, "portfolio_ids": sorted(set(result["portfolio_ids"]) & set(grant.portfolio_scope))}
        elif name == "get_capabilities":
            permitted = {READ_TOOLS[tool] for tool in available}
            rows = []
            for row in result["capabilities"]:
                if row["capability"] not in permitted:
                    row = {**row, "client_usable": False, "client_exposure": "unsupported",
                           "reason": "remote_read_grant_not_available"}
                rows.append(row)
            result = {**result, "capabilities": rows}
        return result

    @staticmethod
    def _check_portfolio(grant: ReadGrant, portfolio_id: str) -> None:
        if portfolio_id not in grant.portfolio_scope:
            raise ReadSurfaceError("Requested portfolio is not authorized")

    def _check_context(self, grant: ReadGrant, available: set[str], arguments: dict[str, Any]) -> None:
        request = arguments["request"]
        inputs = arguments.get("capability_inputs", {})
        requested = set(request.get("requested_context") or []) | set(inputs) | set(arguments.get("required_capabilities", []))
        if any(CONTEXT_TOOLS.get(capability) not in available for capability in requested):
            raise ReadSurfaceError("Requested context is not authorized")
        default_portfolio = request.get("portfolio_id")
        if default_portfolio is not None:
            self._check_portfolio(grant, default_portfolio)
        portfolio_ids = {default_portfolio} if default_portfolio is not None else set()
        normalized = {}
        for capability in requested:
            payload = _context_input(capability, inputs.get(capability, {}), request)
            if capability == "portfolio.state":
                if "portfolio_id" not in payload and default_portfolio is not None:
                    payload["portfolio_id"] = default_portfolio
                self._check_portfolio(grant, payload.get("portfolio_id"))
                portfolio_ids.add(payload.get("portfolio_id"))
            try:
                Draft202012Validator(self.schemas[CONTEXT_TOOLS[capability]]).validate(payload)
            except ValidationError:
                raise ReadSurfaceError("Invalid context arguments") from None
            normalized[capability] = payload
        if len(portfolio_ids) > 1:
            raise ReadSurfaceError("Conflicting portfolio scope")
        arguments["capability_inputs"] = normalized
