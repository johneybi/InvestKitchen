#!/usr/bin/env python3
"""Line-delimited JSON local transport for the read-only TradeMind Gateway.

This is a local contract smoke harness, not a remote API, MCP server, or auth
boundary. One JSON request is read per line and exactly one JSON response is
written per line. No stack trace or local implementation path is returned.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.gateway_facade import ReadOnlyGatewayFacade  # noqa: E402


CATALOG_PATH = Path(__file__).with_name("tool_catalog.json")


class RequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load_tool_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("tools"), list):
        raise ValueError("invalid tool catalog")
    return value


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema(value: Any, schema: dict[str, Any], *, path: str = "arguments") -> None:
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, str(item)) for item in types):
            raise RequestError("invalid_arguments", f"{path} has an invalid type")

    if "enum" in schema and value not in schema["enum"]:
        raise RequestError("invalid_arguments", f"{path} is not an allowed value")

    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise RequestError("invalid_arguments", f"{path} is too short")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise RequestError("invalid_arguments", f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise RequestError("invalid_arguments", f"{path} exceeds the maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise RequestError("invalid_arguments", f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise RequestError("invalid_arguments", f"{path} has too many items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, path=f"{path}[{index}]")
    if isinstance(value, dict):
        required = [str(item) for item in schema.get("required", [])]
        missing = [key for key in required if key not in value]
        if missing:
            raise RequestError("invalid_arguments", f"{path} is missing required field: {missing[0]}")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise RequestError("invalid_arguments", f"{path} contains unsupported field: {unknown[0]}")
        for key, property_schema in properties.items():
            if key in value and isinstance(property_schema, dict):
                _validate_schema(value[key], property_schema, path=f"{path}.{key}")


class LocalToolRouter:
    """Dispatch stable tool names to a ReadOnlyGatewayFacade."""

    def __init__(self, gateway: ReadOnlyGatewayFacade, *, catalog: dict[str, Any] | None = None) -> None:
        self.gateway = gateway
        self.catalog = catalog or load_tool_catalog()
        self.tools = {
            str(tool["name"]): tool
            for tool in self.catalog["tools"]
            if isinstance(tool, dict) and tool.get("name")
        }

    def list_tools(self) -> dict[str, Any]:
        return {
            "protocol_version": self.catalog.get("protocol_version"),
            "tools": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "read_only": tool["read_only"],
                    "input_schema": tool["input_schema"],
                    "output_contract": tool["output_contract"],
                }
                for tool in self.catalog["tools"]
                if self._runtime_available(str(tool["name"]))
            ],
        }

    def _runtime_available(self, name: str) -> bool:
        if name in {
            "preview_knowledge_update",
            "apply_knowledge_update",
            "preview_portfolio_update",
            "apply_portfolio_update",
            "preview_account_sync",
            "apply_account_sync",
            "preview_policy_update",
            "apply_policy_update",
            "preview_opinion_weighting_update",
            "apply_opinion_weighting_update",
        }:
            return self.gateway.connector_tool_ready(name)
        return True

    def available_tool_names(self) -> set[str]:
        return {name for name in self.tools if self._runtime_available(name)}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None or not self._runtime_available(name):
            raise RequestError("tool_not_found", "Unknown TradeMind tool")
        if not isinstance(arguments, dict):
            raise RequestError("invalid_arguments", "Tool arguments must be an object")
        _validate_schema(arguments, tool["input_schema"])

        if name == "get_capabilities":
            return self.gateway.get_capabilities()
        if name == "list_portfolios":
            return self.gateway.list_portfolios()
        if name == "get_portfolio_state":
            return self.gateway.get_portfolio_state(arguments["portfolio_id"])
        if name == "get_open_items":
            return self.gateway.get_open_items(arguments["portfolio_id"], limit=arguments.get("limit", 50))
        if name == "get_current_policy":
            return self.gateway.get_current_policy(arguments["portfolio_id"])
        if name == "get_opinion_weighting":
            return self.gateway.get_opinion_weighting()
        if name == "build_opinion_consensus":
            return self.gateway.build_opinion_consensus(
                arguments["question"],
                horizon=arguments.get("horizon", "weeks"),
                mode=arguments.get("mode"),
            )
        if name == "get_active_plans":
            return self.gateway.get_active_plans(arguments["portfolio_id"])
        if name == "get_current_knowledge":
            return self.gateway.get_current_knowledge(max_outlook=arguments.get("max_outlook", 20))
        if name == "search_knowledge":
            return self.gateway.search_knowledge(arguments["query"], limit=arguments.get("limit", 20))
        if name == "preview_knowledge_update":
            return self.gateway.preview_knowledge_update(arguments["update"])
        if name == "apply_knowledge_update":
            return self.gateway.apply_knowledge_update(arguments["preview_id"], arguments["confirmation"])
        if name == "preview_portfolio_update":
            return self.gateway.preview_portfolio_update(arguments["update"])
        if name == "apply_portfolio_update":
            return self.gateway.apply_portfolio_update(arguments["preview_id"], arguments["confirmation"])
        if name == "preview_account_sync":
            return self.gateway.preview_account_sync(arguments["portfolio_id"], arguments["account_id"])
        if name == "apply_account_sync":
            return self.gateway.apply_account_sync(arguments["preview_id"], arguments["confirmation"])
        if name == "preview_policy_update":
            return self.gateway.preview_policy_update(arguments["update"])
        if name == "apply_policy_update":
            return self.gateway.apply_policy_update(arguments["preview_id"], arguments["confirmation"])
        if name == "preview_opinion_weighting_update":
            return self.gateway.preview_opinion_weighting_update(arguments["update"])
        if name == "apply_opinion_weighting_update":
            return self.gateway.apply_opinion_weighting_update(arguments["preview_id"], arguments["confirmation"])
        if name == "get_market_quote":
            return self.gateway.get_market_quote(arguments["symbols"])
        if name == "get_market_ohlcv":
            return self.gateway.get_market_ohlcv(
                arguments["symbols"],
                interval=arguments.get("interval", "1d"),
                count=arguments.get("count", 120),
            )
        if name == "build_decision_context":
            required = set(arguments.get("required_capabilities") or [])
            return self.gateway.build_decision_context(
                arguments["request"],
                capability_inputs=arguments.get("capability_inputs"),
                required_capabilities=required,
            )
        if name == "get_decision_history":
            return self.gateway.get_decision_history(
                arguments["portfolio_id"],
                status=arguments.get("status"),
                limit=arguments.get("limit", 50),
            )
        if name == "get_transactions":
            return self.gateway.get_transactions(
                arguments["portfolio_id"],
                account_id=arguments.get("account_id"),
                limit=arguments.get("limit", 100),
            )
        if name == "start_reflection":
            return self.gateway.start_reflection(
                arguments["mode"],
                context_bundle=arguments.get("context_bundle"),
            )
        raise RequestError("tool_not_found", "Unknown TradeMind tool")

    def handle_request(self, request: Any) -> dict[str, Any]:
        request_id: Any = None
        try:
            if not isinstance(request, dict):
                raise RequestError("invalid_request", "Request must be an object")
            request_id = request.get("request_id")
            if request_id is None:
                raise RequestError("invalid_request", "request_id is required")
            unknown = sorted(set(request) - {"request_id", "method", "params"})
            if unknown:
                raise RequestError("invalid_request", f"Unsupported request field: {unknown[0]}")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise RequestError("invalid_request", "params must be an object")

            if method == "tools.list":
                if params:
                    raise RequestError("invalid_request", "tools.list does not accept params")
                result = self.list_tools()
            elif method == "tools.call":
                if set(params) - {"name", "arguments"}:
                    raise RequestError("invalid_request", "tools.call contains unsupported params")
                name = params.get("name")
                if not isinstance(name, str) or not name:
                    raise RequestError("invalid_request", "tools.call requires a tool name")
                arguments = params.get("arguments", {})
                result = self.call_tool(name, arguments)
            else:
                raise RequestError("method_not_found", "Unknown local transport method")
            return {"request_id": request_id, "ok": True, "result": result}
        except RequestError as exc:
            return {
                "request_id": request_id,
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception:
            return {
                "request_id": request_id,
                "ok": False,
                "error": {"code": "tool_error", "message": "TradeMind tool invocation failed"},
            }


def serve_lines(router: LocalToolRouter, input_stream, output_stream) -> int:
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "request_id": None,
                "ok": False,
                "error": {"code": "invalid_json", "message": "Request line is not valid JSON"},
            }
        else:
            response = router.handle_request(request)
        output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        output_stream.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-workspace", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "protocol" / "v1" / "fixtures" / "full-reference.instance.json",
    )
    args = parser.parse_args()
    handlers = None
    if args.legacy_workspace is not None:
        from protocol.v1.adapters.gateway_facade import build_compatibility_handlers

        handlers = build_compatibility_handlers(args.legacy_workspace)
    gateway = ReadOnlyGatewayFacade(
        workspace=args.legacy_workspace or ROOT,
        manifest=args.manifest,
        handlers=handlers,
    )
    return serve_lines(LocalToolRouter(gateway), sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
