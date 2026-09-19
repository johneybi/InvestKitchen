#!/usr/bin/env python3
"""InvestKitchen MCP adapter over stdio.

The adapter targets MCP protocol revision 2026-07-28. That revision is
stateless: there is no initialize/initialized handshake and every request
carries its protocol version and client capabilities in params._meta.

This module intentionally implements only the small server subset InvestKitchen
currently needs: server/discover, tools/list, and tools/call. It wraps the
existing transport-neutral gateway; write tools remain narrowly bound to the
native Knowledge and Portfolio observation services.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.gateway_facade import ReadOnlyGatewayFacade  # noqa: E402
from protocol.v1.runtime.reference_composition import build_reference_gateway  # noqa: E402
from protocol.v1.transport.stdio_rpc import (  # noqa: E402
    LocalToolRouter,
    RequestError,
    load_tool_catalog,
)


MCP_PROTOCOL_VERSION = "2026-07-28"
SERVER_INFO = {
    "name": "trademind",
    "title": "InvestKitchen Gateway",
    "version": "0.1.0",
}

_PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
_CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"

_TOOL_CAPABILITY = {
    "get_capabilities": None,
    "list_portfolios": None,
    "get_portfolio_state": "portfolio.state",
    "get_open_items": "openitem.current",
    "get_current_policy": "policy.current",
    "get_opinion_weighting": "opinion.weighting.current",
    "build_opinion_consensus": "opinion.consensus",
    "get_active_plans": "plan.active",
    "get_current_knowledge": "knowledge.current",
    "search_knowledge": "knowledge.search",
    "get_market_quote": "market.quote",
    "get_market_ohlcv": "market.ohlcv",
    "build_decision_context": "decision.context",
    "get_decision_history": "decision.history",
    "get_transactions": "transaction.history",
    "start_reflection": "reflection.session",
    "preview_knowledge_update": None,
    "apply_knowledge_update": None,
    "preview_portfolio_update": None,
    "apply_portfolio_update": None,
    "preview_account_sync": None,
    "apply_account_sync": None,
    "preview_policy_update": None,
    "apply_policy_update": None,
    "preview_opinion_weighting_update": None,
    "apply_opinion_weighting_update": None,
}

_CONNECTOR_WRITE_TOOLS = {
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
}

ToolVisibilityFilter = Callable[[set[str], dict[str, Any]], set[str]]
ToolAuthorizer = Callable[[str, dict[str, Any], dict[str, Any]], bool]


class MCPError(ValueError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _server_meta() -> dict[str, Any]:
    return {_SERVER_INFO_META: dict(SERVER_INFO)}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body.setdefault("resultType", "complete")
    meta = body.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    body["_meta"] = {**meta, **_server_meta()}
    return {"jsonrpc": "2.0", "id": request_id, "result": body}


def _validate_modern_meta(params: dict[str, Any]) -> dict[str, Any]:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise MCPError(-32602, "Missing required MCP request metadata")

    version = meta.get(_PROTOCOL_VERSION_META)
    capabilities = meta.get(_CLIENT_CAPABILITIES_META)
    if not isinstance(version, str) or not version:
        raise MCPError(-32602, "Missing MCP protocol version")
    if version != MCP_PROTOCOL_VERSION:
        raise MCPError(
            -32022,
            "Unsupported protocol version",
            {
                "requestedVersion": version,
                "supportedVersions": [MCP_PROTOCOL_VERSION],
            },
        )
    if not isinstance(capabilities, dict):
        raise MCPError(-32602, "Missing MCP client capabilities")

    client_info = meta.get(_CLIENT_INFO_META)
    if client_info is not None:
        if not isinstance(client_info, dict):
            raise MCPError(-32602, "Invalid MCP client info")
        if not isinstance(client_info.get("name"), str) or not isinstance(client_info.get("version"), str):
            raise MCPError(-32602, "Invalid MCP client info")
    return meta


def _public_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "title": tool.get("title") or tool["name"].replace("_", " ").title(),
        "description": tool["description"],
        "inputSchema": tool["input_schema"],
        "annotations": {
            "readOnlyHint": bool(tool.get("read_only", False)),
            "destructiveHint": bool(tool.get("destructive", False)),
            "openWorldHint": bool(tool.get("open_world", False)),
        },
        "_meta": {
            "io.trademind/outputContract": tool.get("output_contract"),
            "io.trademind/requiresExplicitConfirmation": bool(tool.get("requires_confirmation", False)),
        },
    }


class TradeMindMCPServer:
    """Small MCP 2026-07-28 adapter around the bounded Gateway facade."""

    def __init__(
        self,
        gateway: ReadOnlyGatewayFacade,
        *,
        catalog: dict[str, Any] | None = None,
        visibility_filter: ToolVisibilityFilter | None = None,
        tool_authorizer: ToolAuthorizer | None = None,
    ) -> None:
        self.gateway = gateway
        self.router = LocalToolRouter(gateway, catalog=catalog)
        # These hooks are the transport/auth integration seam. The local
        # read-only smoke server leaves them unset; a remote deployment can
        # inject a server-side Principal/Grant-bound policy without trusting
        # client-supplied MCP metadata as authentication authority.
        self.visibility_filter = visibility_filter
        self.tool_authorizer = tool_authorizer

    def _runtime_usable_tools(self, request_meta: dict[str, Any]) -> set[str]:
        capability_rows = {
            str(row["capability"]): row
            for row in self.gateway.get_capabilities().get("capabilities", [])
            if isinstance(row, dict) and row.get("capability")
        }
        names: set[str] = set()
        for name in self.router.available_tool_names():
            if name in _CONNECTOR_WRITE_TOOLS:
                names.add(name)
                continue
            capability = _TOOL_CAPABILITY.get(name)
            if capability is None:
                names.add(name)
                continue
            row = capability_rows.get(capability)
            if row and row.get("client_usable") is True:
                names.add(name)
        if self.visibility_filter is not None:
            names = set(self.visibility_filter(set(names), dict(request_meta)))
        return names

    def discover(self) -> dict[str, Any]:
        return {
            "resultType": "complete",
            "supportedVersions": [MCP_PROTOCOL_VERSION],
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": (
                "InvestKitchen exposes investment context plus explicitly confirmed native Knowledge "
                "and Portfolio observation updates. Treat preview confirmation, freshness, authority, "
                "and portfolio scope as binding."
            ),
            "ttlMs": 0,
            "cacheScope": "private",
        }

    def list_tools(self, params: dict[str, Any], request_meta: dict[str, Any]) -> dict[str, Any]:
        cursor = params.get("cursor")
        if cursor not in (None, ""):
            raise MCPError(-32602, "This TradeMind tool list is not paginated")
        usable = self._runtime_usable_tools(request_meta)
        tools = [
            _public_tool(tool)
            for tool in self.router.catalog["tools"]
            if tool.get("name") in usable
        ]
        return {
            "resultType": "complete",
            "tools": tools,
            "ttlMs": 0,
            "cacheScope": "private",
        }

    def call_tool(self, params: dict[str, Any], request_meta: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            raise MCPError(-32602, "tools/call requires a tool name")
        if name not in self._runtime_usable_tools(request_meta):
            raise MCPError(-32602, "Unknown or unavailable tool")
        if self.tool_authorizer is not None and not self.tool_authorizer(name, dict(arguments), dict(request_meta)):
            # Do not distinguish policy-denied from non-visible tools at this
            # layer. Detailed reasons belong in the authenticated gateway audit,
            # not in the model-facing MCP protocol error.
            raise MCPError(-32602, "Unknown or unavailable tool")
        try:
            value = self.router.call_tool(name, arguments)
        except RequestError as exc:
            return {
                "resultType": "complete",
                "content": [{"type": "text", "text": exc.message}],
                "isError": True,
            }

        is_error = isinstance(value, dict) and value.get("status") in {
            "unavailable",
            "blocked",
            "error",
        }
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": serialized}],
            "structuredContent": value,
            "isError": bool(is_error),
        }

    def handle_request(self, request: Any) -> dict[str, Any] | None:
        request_id: Any = None
        try:
            if not isinstance(request, dict):
                raise MCPError(-32600, "Invalid Request")
            if request.get("jsonrpc") != "2.0":
                raise MCPError(-32600, "Invalid Request")

            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(method, str) or not method:
                raise MCPError(-32600, "Invalid Request")
            if not isinstance(params, dict):
                raise MCPError(-32602, "Invalid params")

            # Notifications never receive a JSON-RPC response. There are no
            # client-to-server notifications needed by this read-only modern
            # adapter, so they are ignored after syntax validation.
            if "id" not in request:
                return None

            request_meta = _validate_modern_meta(params)

            if method == "server/discover":
                payload = self.discover()
            elif method == "tools/list":
                payload = self.list_tools(params, request_meta)
            elif method == "tools/call":
                payload = self.call_tool(params, request_meta)
            else:
                raise MCPError(-32601, "Method not found")
            return _result(request_id, payload)
        except MCPError as exc:
            return _error(request_id, exc.code, exc.message, exc.data)
        except Exception:
            return _error(request_id, -32603, "Internal error")


def serve_lines(server: TradeMindMCPServer, input_stream, output_stream) -> int:
    """Serve newline-delimited MCP JSON-RPC messages over stdio."""
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        else:
            response = server.handle_request(request)
        if response is None:
            continue
        output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        output_stream.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    parser.add_argument("--legacy-workspace", type=Path)
    parser.add_argument("--personal-data-root", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "protocol" / "v1" / "fixtures" / "full-reference.instance.json",
    )
    parser.add_argument(
        "--market-provider",
        choices=("none", "toss-readonly-subprocess", "toss-native-subprocess"),
        default="none",
    )
    parser.add_argument("--market-secret-file", type=Path)
    parser.add_argument("--account-binding-file", type=Path)
    parser.add_argument("--toss-account-secret-file", type=Path)
    parser.add_argument("--nhplug-account-secret-file", type=Path)
    parser.add_argument("--account-sync-max-age-seconds", type=int, default=300)
    parser.add_argument("--native-store-root", type=Path)
    parser.add_argument("--advisory-state-root", type=Path)
    parser.add_argument("--historical-decision-root", type=Path)
    parser.add_argument("--historical-transaction-root", type=Path)
    parser.add_argument("--portfolio-checkpoint-root", type=Path)
    parser.add_argument("--approval-store-root", type=Path)
    parser.add_argument("--write-principal", type=Path)
    parser.add_argument("--write-grant", type=Path)
    args = parser.parse_args()
    try:
        write_principal = None
        write_grant = None
        account_bindings = None
        account_provider_secret_files: dict[str, Path] = {}
        if args.write_principal is not None or args.write_grant is not None:
            if args.write_principal is None or args.write_grant is None:
                raise ValueError("write principal and grant must be configured together")
            write_principal = json.loads(args.write_principal.read_text(encoding="utf-8"))
            write_grant = json.loads(args.write_grant.read_text(encoding="utf-8"))
            if not isinstance(write_principal, dict) or not isinstance(write_grant, dict):
                raise ValueError("write principal and grant must be objects")
        if args.account_binding_file is not None:
            account_bindings = json.loads(args.account_binding_file.read_text(encoding="utf-8"))
            if not isinstance(account_bindings, list) or any(not isinstance(row, dict) for row in account_bindings):
                raise ValueError("account binding file must contain an array of objects")
        if args.toss_account_secret_file is not None:
            account_provider_secret_files["toss_securities"] = args.toss_account_secret_file
        if args.nhplug_account_secret_file is not None:
            account_provider_secret_files["nhplug"] = args.nhplug_account_secret_file
        if account_provider_secret_files and account_bindings is None:
            raise ValueError("account binding file is required with account provider secrets")
        if args.account_sync_max_age_seconds < 0:
            raise ValueError("account sync max age must be nonnegative")
        gateway = build_reference_gateway(
            runtime_root=args.runtime_root,
            manifest=args.manifest,
            legacy_workspace=args.legacy_workspace,
            personal_data_root=args.personal_data_root,
            market_provider=args.market_provider,
            market_secret_file=args.market_secret_file,
            native_store_root=args.native_store_root,
            advisory_state_root=args.advisory_state_root,
            historical_decision_root=args.historical_decision_root,
            historical_transaction_root=args.historical_transaction_root,
            portfolio_checkpoint_root=args.portfolio_checkpoint_root,
            approval_store_root=args.approval_store_root,
            write_principal=write_principal,
            write_grant=write_grant,
            account_bindings=account_bindings,
            account_provider_secret_files=account_provider_secret_files,
            account_sync_max_age_seconds=args.account_sync_max_age_seconds,
        )
    except (OSError, ValueError):
        sys.stderr.write("TradeMind MCP configuration error\n")
        return 2
    server = TradeMindMCPServer(gateway, catalog=load_tool_catalog())
    return serve_lines(server, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
