"""Authenticated Streamable HTTP for Gemini and other MCP clients.

The official SDK serves modern discovery and legacy initialization. Existing
OpenAI stdio keeps its own lifecycle. Native writes require an optional grant
and independent authenticated browser review; the default remains read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import uvicorn
from mcp.server import Server
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import cors_middleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic import AnyHttpUrl
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.routing import Route

from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.security.oauth_resource import JWTTokenVerifier, load_oauth_resource
from protocol.v1.transport.client_schema import client_tool_schema
from protocol.v1.transport.mcp_read_surface import ReadSurfaceError, RemoteReadSurface
from protocol.v1.transport.mcp_confirmed_writes import (
    APPLY_TOOLS, WRITE_SCOPE, WRITE_TOOLS, ConfirmedWriteConfig, ConfirmedWriteSurface,
)


class NoStoreMiddleware:
    def __init__(self, app, authorization_scopes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.authorization_scopes = authorization_scopes

    async def __call__(self, scope, receive, send):
        async def no_cache(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                challenge = headers.get("WWW-Authenticate", "")
                if (scope.get("path") == "/mcp" and message["status"] == 401
                        and self.authorization_scopes and challenge.startswith("Bearer ")):
                    # Clients prioritize challenge scopes over discovery metadata.
                    # This requests consent for the optional write profile without
                    # granting it or rejecting existing read-only access tokens.
                    guidance = 'scope="' + " ".join(self.authorization_scopes) + '"'
                    if re.search(r'\bscope="[^"]*"', challenge):
                        challenge = re.sub(r'\bscope="[^"]*"', guidance, challenge)
                    else:
                        challenge += ", " + guidance
                    headers["WWW-Authenticate"] = challenge
            await send(message)

        await self.app(scope, receive, no_cache)


def create_http_app(gateway, verifier: JWTTokenVerifier, *, writes: ConfirmedWriteSurface | None = None):
    if writes is not None and writes.resource != verifier.config:
        raise ValueError("Read and write OAuth resource bindings must match")
    surface = RemoteReadSurface(gateway, verifier.config)

    async def list_tools(ctx, params):
        if params is not None and params.cursor:
            raise ReadSurfaceError("Tool list is not paginated")
        token = get_access_token()
        tools = surface.list_tools(token)
        if writes is not None:
            tools.extend(writes.list_tools(token))
        tools = [{**tool, "inputSchema": client_tool_schema(tool["inputSchema"])} for tool in tools]
        return ListToolsResult(tools=[Tool.model_validate(tool) for tool in tools], ttlMs=0, cacheScope="private")

    async def call_tool(ctx, params):
        token = get_access_token()
        try:
            selected = writes if writes is not None and params.name in (WRITE_TOOLS.keys() | APPLY_TOOLS.keys()) else surface
            value = await anyio.to_thread.run_sync(selected.call_tool, token, params.name, params.arguments or {})
            if params.name == "get_capabilities" and writes is not None:
                value = {**value, "remote_write_tools": [tool["name"] for tool in writes.list_tools(token)],
                         "write_approval": "separate_authenticated_browser_review"}
            is_error = isinstance(value, dict) and value.get("status") in {"blocked", "unavailable", "error"}
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False, separators=(",", ":")))],
                structuredContent=value, isError=is_error,
            )
        except ReadSurfaceError as exc:
            message = str(exc)
        except Exception:
            message = "InvestKitchen request failed"
        return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)

    server = Server(
        "investkitchen", title="InvestKitchen", version="0.1.1",
        description=(
            "InvestKitchen / 인베스트키친: retrieve the user's saved investment accounts, "
            "brokerage portfolios, holdings, quantities, cash balances and shared Knowledge. "
            "내 계좌 조회, 토스 계좌, 보유 종목, 잔고, ISA, IRP, 전체 포트폴리오 조회에 사용합니다. "
            "Reads return stored observations with source dates, not a direct brokerage login "
            "or a live balance refresh. Available tools and access depend on server-side grants."
        ),
        instructions=(
            "Use this app for saved account, holdings, cash and portfolio questions, including "
            "내 계좌, 토스 계좌, ISA and IRP. Discover the authorized scope with list_portfolios, "
            "then call get_portfolio_state for each requested portfolio; IDs alone are not account details. "
            "Match broker names and account types against returned facts, keeping portfolios separate. "
            "Use fresh reads for current-state follow-ups and preserve source dates, missing data and conflicts. "
            "These are stored InvestKitchen observations; do not require a new brokerage login for a read. " + (
            "Only explicitly granted native updates are available. Preview first, present approval_url to the user, "
            "wait for their independent NAS browser approval, then apply the exact preview and confirm read-back. "
            "Never claim an approved preview is already saved."
            if writes is not None else
            "This client is read-only; it cannot preview, approve or apply changes.")),
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
            required_scopes=list(verifier.config.required_scopes), validate_token_resource=True,
        ),
        token_verifier=verifier,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[resource.netloc], allowed_origins=[origin],
        ),
    )
    # OAuth issuer identifiers must match discovery and signed tokens exactly.
    # AnyHttpUrl adds '/' to origin-only URLs, so do not serialize that normalized
    # value into the protected-resource metadata supplied by the SDK.
    async def resource_metadata(request):
        return JSONResponse({
            "resource": verifier.config.resource_url,
            "authorization_servers": [verifier.config.issuer_url],
            "scopes_supported": list(verifier.config.required_scopes) + ([WRITE_SCOPE] if writes is not None else []),
            "bearer_methods_supported": ["header"],
        })

    metadata_path = "/.well-known/oauth-protected-resource/mcp"
    if not any(getattr(route, "path", None) == metadata_path for route in app.routes):
        raise RuntimeError("Protected resource metadata route is missing")
    app.router.routes = [
        Route(metadata_path, endpoint=cors_middleware(resource_metadata, ["GET", "OPTIONS"]),
              methods=["GET", "OPTIONS"])
        if getattr(route, "path", None) == metadata_path else route
        for route in app.routes
    ]
    if writes is not None:
        app.router.routes.extend(writes.routes())
    # Authenticated reads and errors must not be cached by an ingress proxy.
    app.add_middleware(NoStoreMiddleware, authorization_scopes=(
        (*verifier.config.required_scopes, WRITE_SCOPE) if writes is not None else ()
    ))
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oauth-config", type=Path, required=True)
    parser.add_argument("--public-jwks", type=Path, required=True)
    parser.add_argument("--write-config", type=Path,
                        help="Optional expiring grants and loopback browser-authentication binding")
    parser.add_argument("--approval-store-root", type=Path,
                        help="Private approval evidence, separate from source and authoritative stores")
    parser.add_argument("--check-config", action="store_true",
                        help="Validate configured surfaces and stores without listening or applying updates")
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--runtime-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--personal-data-root", type=Path, required=True)
    parser.add_argument("--native-store-root", type=Path)
    parser.add_argument("--advisory-state-root", type=Path)
    parser.add_argument("--advisory-plan-root", type=Path,
                        help="Shared authoritative Plan directory, independently mountable from other advisory state")
    parser.add_argument("--plan-verification-root", type=Path)
    parser.add_argument("--plan-verification-id", help="Optional verification: namespace backed by a separate synthetic Plan store")
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
        runtime_args = dict(
            runtime_root=args.runtime_root, manifest=args.manifest,
            personal_data_root=args.personal_data_root,
            native_store_root=args.native_store_root, advisory_state_root=args.advisory_state_root,
            advisory_plan_root=args.advisory_plan_root,
            plan_verification_root=args.plan_verification_root, plan_verification_id=args.plan_verification_id,
            historical_decision_root=args.historical_decision_root,
            historical_transaction_root=args.historical_transaction_root,
            portfolio_checkpoint_root=args.portfolio_checkpoint_root,
            market_provider=args.market_provider, market_secret_file=args.market_secret_file,
        )
        gateway = build_reference_gateway(**runtime_args, read_only_stores=True)
        writes = None
        if bool(args.write_config) != bool(args.approval_store_root):
            raise ValueError("Write configuration and approval store must be supplied together")
        if args.write_config is not None:
            if args.native_store_root is None:
                raise ValueError("Native stores are required for writes")
            config = ConfirmedWriteConfig.model_validate_json(args.write_config.read_text())
            config.validate_read_grants(verifier.config)
            approval_root = args.approval_store_root.resolve()
            # Approval evidence must not be mixed into product source or user data.
            protected = [args.runtime_root.resolve(), args.personal_data_root.resolve(), args.native_store_root.resolve()]
            protected.extend(root.resolve() for root in (
                args.portfolio_checkpoint_root, args.advisory_state_root,
                args.advisory_plan_root,
                args.plan_verification_root,
                args.historical_decision_root, args.historical_transaction_root,
            ) if root is not None)
            if any(approval_root == root or approval_root.is_relative_to(root) or root.is_relative_to(approval_root)
                   for root in protected):
                raise ValueError("Approval store must be a separate private directory")

            def writer_factory(grant):
                binding = hashlib.sha256(json.dumps([verifier.config.resource_url, *grant.identity]).encode()).hexdigest()
                expiry = {"value": grant.expires_at.isoformat(), "precision": "source_exact"}
                now = {"value": datetime.now(timezone.utc).isoformat(), "precision": "source_exact"}
                identity = {"subject_user_id": grant.subject, "client_id": grant.client_id,
                            "credential_binding_id": "oauth:" + binding}
                principal = {**identity, "authentication_event_id": "oauth-verified-http",
                             "authenticated_at": now, "expires_at": expiry}
                native_grant = {**identity, "grant_id": "http-write:" + binding,
                                "instance_id": gateway.get_capabilities()["instance_id"],
                                "permissions": [*grant.permissions, "operation.approve"],
                                "portfolio_scope": list(grant.portfolio_scope),
                                "tool_allowlist": [*WRITE_TOOLS, *APPLY_TOOLS],
                                "issued_at": now, "expires_at": expiry}
                return build_reference_gateway(**runtime_args, approval_store_root=approval_root / binding,
                                               plan_preview_ttl_seconds=config.plan_preview_ttl_seconds,
                                               write_principal=principal, write_grant=native_grant)

            writes = ConfirmedWriteSurface(verifier.config, config, writer_factory,
                                           audit_root=approval_root / "browser-confirmations")
        app = create_http_app(gateway, verifier, writes=writes)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        sys.stderr.write("InvestKitchen HTTP MCP configuration error\n")
        return 2
    if args.check_config:
        print("InvestKitchen HTTP MCP preflight: PASS")
        return 0
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning", proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
