"""Authenticated Streamable HTTP reads for Gemini and other MCP clients.

The official SDK serves modern discovery and legacy initialization. Existing
OpenAI stdio transport and all write services keep their separate lifecycles.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import uvicorn
from mcp.server import Server
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic import AnyHttpUrl
from starlette.datastructures import MutableHeaders

from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.security.oauth_resource import JWTTokenVerifier, READ_SCOPE, load_oauth_resource
from protocol.v1.transport.mcp_read_surface import ReadSurfaceError, RemoteReadSurface


class NoStoreMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        async def no_cache(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, no_cache)


def create_http_app(gateway, verifier: JWTTokenVerifier):
    surface = RemoteReadSurface(gateway, verifier.config)

    async def list_tools(ctx, params):
        if params is not None and params.cursor:
            raise ReadSurfaceError("Tool list is not paginated")
        tools = surface.list_tools(get_access_token())
        return ListToolsResult(tools=[Tool.model_validate(tool) for tool in tools], ttlMs=0, cacheScope="private")

    async def call_tool(ctx, params):
        token = get_access_token()
        try:
            value = await anyio.to_thread.run_sync(surface.call_tool, token, params.name, params.arguments or {})
            is_error = isinstance(value, dict) and value.get("status") in {"blocked", "unavailable", "error"}
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False, separators=(",", ":")))],
                structuredContent=value, isError=is_error,
            )
        except ReadSurfaceError as exc:
            message = str(exc)
        except Exception:
            message = "InvestKitchen read failed"
        return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)

    server = Server(
        "investkitchen", title="InvestKitchen", version="0.1.0",
        instructions="Read authoritative InvestKitchen facts. Discover portfolio scope first. This client is read-only; it cannot preview, approve or apply changes.",
        on_list_tools=list_tools, on_call_tool=call_tool,
    )
    resource = urlsplit(verifier.config.resource_url)
    origin = f"https://{resource.netloc}"
    app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True,
        max_request_body_size=65536, max_sessions=100, session_idle_timeout=300,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(verifier.config.issuer_url),
            resource_server_url=AnyHttpUrl(verifier.config.resource_url),
            required_scopes=[READ_SCOPE], validate_token_resource=True,
        ),
        token_verifier=verifier,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[resource.netloc], allowed_origins=[origin],
        ),
    )
    # Authenticated reads and errors must not be cached by an ingress proxy.
    app.add_middleware(NoStoreMiddleware)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oauth-config", type=Path, required=True)
    parser.add_argument("--public-jwks", type=Path, required=True)
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--runtime-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--personal-data-root", type=Path, required=True)
    parser.add_argument("--native-store-root", type=Path)
    parser.add_argument("--advisory-state-root", type=Path)
    parser.add_argument("--historical-decision-root", type=Path)
    parser.add_argument("--historical-transaction-root", type=Path)
    parser.add_argument("--portfolio-checkpoint-root", type=Path)
    parser.add_argument("--market-provider", choices=("none", "toss-native-subprocess"), default="none")
    parser.add_argument("--market-secret-file", type=Path)
    args = parser.parse_args()
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError("Invalid port")
        verifier = load_oauth_resource(args.oauth_config, args.public_jwks)
        gateway = build_reference_gateway(
            runtime_root=args.runtime_root, manifest=args.manifest,
            personal_data_root=args.personal_data_root,
            native_store_root=args.native_store_root, advisory_state_root=args.advisory_state_root,
            historical_decision_root=args.historical_decision_root,
            historical_transaction_root=args.historical_transaction_root,
            portfolio_checkpoint_root=args.portfolio_checkpoint_root,
            market_provider=args.market_provider, market_secret_file=args.market_secret_file,
        )
        app = create_http_app(gateway, verifier)
    except (OSError, ValueError, KeyError, TypeError):
        sys.stderr.write("InvestKitchen HTTP MCP configuration error\n")
        return 2
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning", proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
