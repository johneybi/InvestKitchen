from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.gateway_facade import (  # noqa: E402
    ReadOnlyGatewayFacade,
)
from protocol.v1.adapters.market_legacy import adapt_market_result  # noqa: E402
from protocol.v1.tests.fixture_handlers import synthetic_handlers  # noqa: E402
from protocol.v1.transport.mcp_stdio import (  # noqa: E402
    MCP_PROTOCOL_VERSION,
    TradeMindMCPServer,
    serve_lines,
)


def _meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "trademind-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _request(method: str, params: dict[str, Any] | None = None, *, request_id: Any = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**(params or {}), "_meta": _meta()},
    }


def _fixture_market_handler(capability: str):
    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        symbols = sorted(str(value) for value in payload.get("symbols", []))
        raw: dict[str, Any]
        if capability == "market.quote":
            raw = {
                "source": "fixture-provider",
                "retrieved_at": "2026-09-15T09:45:08+09:00",
                "as_of": "2026-09-15T09:45:08+09:00",
                "quotes": {symbol: {"symbol": symbol, "price": 250000} for symbol in symbols},
                "components": {"quotes": {"status": "available"}},
            }
        else:
            raw = {
                "source": "fixture-provider",
                "retrieved_at": "2026-09-15T09:45:08+09:00",
                "series": [
                    {"symbol": symbol, "bars": [{"timestamp": "2026-09-15T00:00:00+09:00", "close": 250000}]}
                    for symbol in symbols
                ],
                "components": {"candles": {"status": "available"}},
            }
        return adapt_market_result(raw, capability=capability, source_mode="fixture")

    return handler


def _server(*, with_market: bool = True) -> TradeMindMCPServer:
    market_handlers = None
    if with_market:
        market_handlers = {
            "market.quote": _fixture_market_handler("market.quote"),
            "market.ohlcv": _fixture_market_handler("market.ohlcv"),
        }
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=synthetic_handlers(market_handlers=market_handlers),
    )
    return TradeMindMCPServer(gateway)


def test_mcp_discover_reports_modern_stateless_protocol() -> None:
    response = _server().handle_request(_request("server/discover", request_id="discover"))
    assert response is not None
    assert response["result"]["resultType"] == "complete"
    assert response["result"]["supportedVersions"] == ["2026-07-28"]
    assert response["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert response["result"]["cacheScope"] == "private"
    assert response["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "trademind"


def test_mcp_requires_per_request_protocol_metadata() -> None:
    response = _server().handle_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    })
    assert response is not None
    assert response["error"]["code"] == -32602


def test_mcp_rejects_unsupported_protocol_version_with_standard_code() -> None:
    request = _request("server/discover")
    request["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2025-11-25"
    response = _server().handle_request(request)
    assert response is not None
    assert response["error"]["code"] == -32022
    assert response["error"]["data"]["supportedVersions"] == ["2026-07-28"]


def test_mcp_tools_list_is_deterministic_read_only_and_runtime_filtered() -> None:
    response = _server().handle_request(_request("tools/list"))
    assert response is not None
    tools = response["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "get_capabilities",
        "list_portfolios",
        "get_portfolio_state",
        "get_current_knowledge",
        "search_knowledge",
        "get_market_quote",
        "get_market_ohlcv",
        "build_decision_context",
        "start_reflection",
    ]
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in tools)
    assert all(tool["annotations"]["destructiveHint"] is False for tool in tools)
    assert all("inputSchema" in tool for tool in tools)
    quote = next(tool for tool in tools if tool["name"] == "get_market_quote")
    portfolio = next(tool for tool in tools if tool["name"] == "get_portfolio_state")
    assert quote["annotations"]["openWorldHint"] is True
    assert portfolio["annotations"]["openWorldHint"] is False
    assert response["result"]["ttlMs"] == 0
    assert response["result"]["cacheScope"] == "private"


def test_mcp_list_portfolios_never_requires_guessing_scope() -> None:
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=synthetic_handlers(),
        portfolio_ids=["portfolio-b", "portfolio-a", "portfolio-a"],
    )
    server = TradeMindMCPServer(gateway)
    response = server.handle_request(
        _request("tools/call", {"name": "list_portfolios", "arguments": {}})
    )
    assert response is not None
    assert response["result"]["structuredContent"] == {
        "protocol_version": "1.0-draft",
        "portfolio_ids": ["portfolio-a", "portfolio-b"],
        "scope_source": "server_configured_personal_scope",
    }

    without_market = _server(with_market=False).handle_request(_request("tools/list"))
    assert without_market is not None
    filtered_names = [tool["name"] for tool in without_market["result"]["tools"]]
    assert "get_market_quote" not in filtered_names
    assert "get_market_ohlcv" not in filtered_names


def test_mcp_tool_call_returns_structured_and_text_content() -> None:
    response = _server().handle_request(
        _request(
            "tools/call",
            {"name": "search_knowledge", "arguments": {"query": "fixture", "limit": 1}},
        )
    )
    assert response is not None
    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["capability"] == "knowledge.search"
    assert json.loads(result["content"][0]["text"]) == structured


def test_mcp_tool_input_error_is_visible_to_model_not_protocol_failure() -> None:
    response = _server().handle_request(
        _request("tools/call", {"name": "get_portfolio_state", "arguments": {}})
    )
    assert response is not None
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "missing required field" in response["result"]["content"][0]["text"]


def test_mcp_unavailable_runtime_tool_is_not_discoverable_or_callable() -> None:
    server = _server(with_market=False)
    response = server.handle_request(
        _request("tools/call", {"name": "get_market_quote", "arguments": {"symbols": ["005930"]}})
    )
    assert response is not None
    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "Unknown or unavailable tool"


def test_mcp_has_server_side_visibility_and_authorization_injection_seam() -> None:
    base = _server()
    server = TradeMindMCPServer(
        base.gateway,
        visibility_filter=lambda names, _meta: names - {"get_portfolio_state"},
        tool_authorizer=lambda name, _arguments, _meta: name != "search_knowledge",
    )
    listed = server.handle_request(_request("tools/list"))
    assert listed is not None
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "get_portfolio_state" not in names
    assert "search_knowledge" in names

    denied = server.handle_request(
        _request("tools/call", {"name": "search_knowledge", "arguments": {"query": "fixture"}})
    )
    assert denied is not None
    assert denied["error"]["code"] == -32602
    assert denied["error"]["message"] == "Unknown or unavailable tool"


def test_mcp_portfolio_result_keeps_gateway_implementation_hardening() -> None:
    response = _server().handle_request(
        _request("tools/call", {"name": "get_portfolio_state", "arguments": {"portfolio_id": "fixture"}})
    )
    assert response is not None
    encoded = json.dumps(response, ensure_ascii=False).casefold()
    for token in (
        "official.compat",
        "legacy-",
        "legacy_",
        "accounts/",
        "knowledge/",
        "project_legacy_records.py",
        "trademind-framework/",
    ):
        assert token not in encoded


def test_mcp_legacy_initialize_is_not_part_of_modern_adapter() -> None:
    response = _server().handle_request(_request("initialize"))
    assert response is not None
    assert response["error"]["code"] == -32601


def test_mcp_notifications_do_not_receive_responses() -> None:
    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"_meta": _meta()},
    }
    assert _server().handle_request(notification) is None


def test_mcp_stdio_wire_is_newline_delimited_json_rpc_only() -> None:
    requests = [
        _request("server/discover", request_id="d"),
        _request("tools/list", request_id="l"),
        _request(
            "tools/call",
            {"name": "get_capabilities", "arguments": {}},
            request_id="c",
        ),
    ]
    input_stream = io.StringIO("\n".join(json.dumps(row) for row in requests) + "\n")
    output_stream = io.StringIO()
    assert serve_lines(_server(), input_stream, output_stream) == 0
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert [row["id"] for row in responses] == ["d", "l", "c"]
    assert all(row["jsonrpc"] == "2.0" for row in responses)


def test_mcp_stdio_actual_process_smoke() -> None:
    request = _request("server/discover", request_id="subprocess-discover")
    completed = subprocess.run(
        [sys.executable, str(PROTOCOL / "transport" / "mcp_stdio.py")],
        cwd=ROOT,
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stderr == ""
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert response["id"] == "subprocess-discover"
    assert response["result"]["supportedVersions"] == ["2026-07-28"]
