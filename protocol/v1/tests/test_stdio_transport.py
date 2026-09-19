from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.gateway_facade import (  # noqa: E402
    ReadOnlyGatewayFacade,
    dumps_public,
)
from protocol.v1.adapters.market_legacy import adapt_market_result  # noqa: E402
from protocol.v1.tests.fixture_handlers import synthetic_handlers  # noqa: E402
from protocol.v1.transport.stdio_rpc import LocalToolRouter, load_tool_catalog, serve_lines  # noqa: E402


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        schema = _load(path)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def _validate(value: Any, schema_name: str) -> None:
    schema = _load(SCHEMAS / schema_name)
    jsonschema.Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(value)


def _fixture_market_handler(capability: str):
    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        symbols = sorted(str(v) for v in payload.get("symbols", []))
        if capability == "market.quote":
            raw = {
                "source": "private-fixture-provider-name",
                "retrieved_at": "2026-09-15T09:45:08+09:00",
                "as_of": "2026-09-15T09:45:08+09:00",
                "quotes": {symbol: {"symbol": symbol, "price": 250000} for symbol in symbols},
                "components": {"quotes": {"status": "available"}},
            }
        else:
            raw = {
                "source": "private-fixture-provider-name",
                "retrieved_at": "2026-09-15T09:45:08+09:00",
                "series": [
                    {"symbol": symbol, "bars": [{"timestamp": "2026-09-15T00:00:00+09:00", "close": 250000}]}
                    for symbol in symbols
                ],
                "components": {"candles": {"status": "available"}},
            }
        return adapt_market_result(raw, capability=capability, source_mode="fixture")

    return handler


def _router() -> LocalToolRouter:
    handlers = synthetic_handlers(
        market_handlers={
            "market.quote": _fixture_market_handler("market.quote"),
            "market.ohlcv": _fixture_market_handler("market.ohlcv"),
        },
    )
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=handlers,
    )
    return LocalToolRouter(gateway)


def _call(router: LocalToolRouter, request_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return router.handle_request({
        "request_id": request_id,
        "method": "tools.call",
        "params": {"name": name, "arguments": arguments},
    })


def test_tool_catalog_marks_connector_mutations_and_matches_gateway_surface() -> None:
    catalog = load_tool_catalog()
    names = {tool["name"] for tool in catalog["tools"]}
    assert names == {
        "get_capabilities",
        "list_portfolios",
        "get_portfolio_state",
        "get_open_items",
        "get_current_policy",
        "get_opinion_weighting",
        "build_opinion_consensus",
        "get_active_plans",
        "get_current_knowledge",
        "search_knowledge",
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
        "get_market_quote",
        "get_market_ohlcv",
        "build_decision_context",
        "get_decision_history",
        "get_transactions",
        "start_reflection",
    }
    by_name = {tool["name"]: tool for tool in catalog["tools"]}
    assert by_name["preview_knowledge_update"]["read_only"] is True
    assert by_name["preview_portfolio_update"]["read_only"] is True
    assert by_name["preview_account_sync"]["read_only"] is True
    assert by_name["preview_policy_update"]["read_only"] is True
    assert by_name["preview_opinion_weighting_update"]["read_only"] is True
    assert by_name["apply_knowledge_update"]["read_only"] is False
    assert by_name["apply_portfolio_update"]["read_only"] is False
    assert by_name["apply_account_sync"]["read_only"] is False
    assert by_name["apply_policy_update"]["read_only"] is False
    assert by_name["apply_opinion_weighting_update"]["read_only"] is False
    assert all(
        tool["read_only"] is True
        for tool in catalog["tools"]
        if tool["name"] not in {
            "apply_knowledge_update", "apply_portfolio_update", "apply_account_sync",
            "apply_policy_update", "apply_opinion_weighting_update",
        }
    )


def test_tools_list_exposes_machine_readable_input_contracts() -> None:
    response = _router().handle_request({"request_id": "list-1", "method": "tools.list", "params": {}})
    assert response["ok"] is True
    assert len(response["result"]["tools"]) == 16
    portfolios = next(tool for tool in response["result"]["tools"] if tool["name"] == "list_portfolios")
    assert portfolios["input_schema"] == {"type": "object", "properties": {}, "additionalProperties": False}
    assert portfolios["output_contract"] == "portfolio-scope-list"
    quote = next(tool for tool in response["result"]["tools"] if tool["name"] == "get_market_quote")
    assert quote["input_schema"]["required"] == ["symbols"]
    assert quote["output_contract"] == "capability-result.schema.json"
    caps = next(tool for tool in response["result"]["tools"] if tool["name"] == "get_capabilities")
    assert caps["output_contract"] == "capability-discovery.schema.json"


def test_tool_call_rejects_unknown_and_invalid_arguments_without_stack_trace() -> None:
    router = _router()
    unknown = _call(router, "bad-tool", "does_not_exist", {})
    assert unknown == {
        "request_id": "bad-tool",
        "ok": False,
        "error": {"code": "tool_not_found", "message": "Unknown TradeMind tool"},
    }
    invalid = _call(router, "bad-args", "get_portfolio_state", {"portfolio_id": "portfolio-alpha", "workspace": "/secret"})
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "invalid_arguments"
    assert "traceback" not in dumps_public(invalid).casefold()


def test_stdio_transport_executes_read_tools_and_hides_implementation() -> None:
    router = _router()
    requests = [
        {"request_id": "caps", "method": "tools.call", "params": {"name": "get_capabilities", "arguments": {}}},
        {"request_id": "portfolio", "method": "tools.call", "params": {"name": "get_portfolio_state", "arguments": {"portfolio_id": "fixture"}}},
        {"request_id": "knowledge", "method": "tools.call", "params": {"name": "search_knowledge", "arguments": {"query": "fixture", "limit": 2}}},
        {"request_id": "quote", "method": "tools.call", "params": {"name": "get_market_quote", "arguments": {"symbols": ["005930"]}}},
        {"request_id": "reflection", "method": "tools.call", "params": {"name": "start_reflection", "arguments": {"mode": "pre"}}},
    ]
    input_stream = io.StringIO("\n".join(json.dumps(row, ensure_ascii=False) for row in requests) + "\n")
    output_stream = io.StringIO()
    assert serve_lines(router, input_stream, output_stream) == 0
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert [row["request_id"] for row in responses] == [row["request_id"] for row in requests]
    assert all(row["ok"] is True for row in responses)
    encoded = dumps_public(responses).casefold()
    for token in (
        "private-fixture-provider-name",
        "/github/",
        "trademind-framework/",
        "accounts/",
        "knowledge/",
        "official.compat",
        "legacy-",
        "legacy_",
        "project_legacy_records.py",
    ):
        assert token not in encoded


def test_stdio_transport_builds_decision_context_by_tool_name_only() -> None:
    router = _router()
    request = {
        "decision_request_id": "stdio-context-1",
        "actor": "local-smoke-client",
        "objective": "local transport에서 context 조립을 검증한다.",
        "subject_refs": ["005930"],
        "portfolio_id": "fixture",
        "account_ids": [],
        "horizon": "short_term",
        "as_of_request": {"value": "2026-09-15T09:45:09+09:00", "precision": "source_exact"},
        "constraints": None,
        "requested_context": ["portfolio.state", "market.quote", "market.ohlcv", "knowledge.current"]
    }
    response = _call(
        router,
        "context",
        "build_decision_context",
        {
            "request": request,
            "capability_inputs": {
                "portfolio.state": {"portfolio_id": "fixture"},
                "market.quote": {"symbols": ["005930"]},
                "market.ohlcv": {"symbols": ["005930"], "interval": "1d", "count": 20},
                "knowledge.current": {"max_outlook": 2}
            },
            "required_capabilities": ["portfolio.state", "market.quote", "knowledge.current"]
        },
    )
    assert response["ok"] is True
    context = response["result"]["context"]
    assert context["authority_summary"]["decision_ready"] is True
    assert set(context["market"]) == {"market.quote", "market.ohlcv"}


def test_stdio_transport_invalid_json_returns_bounded_error() -> None:
    output_stream = io.StringIO()
    serve_lines(_router(), io.StringIO("{not-json}\n"), output_stream)
    response = json.loads(output_stream.getvalue())
    assert response == {
        "request_id": None,
        "ok": False,
        "error": {"code": "invalid_json", "message": "Request line is not valid JSON"},
    }


def test_default_stdio_gateway_discovery_marks_unattached_market_runtime_unready() -> None:
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=synthetic_handlers(),
    )
    router = LocalToolRouter(gateway)
    response = _call(router, "caps-runtime", "get_capabilities", {})
    assert response["ok"] is True
    _validate(response["result"], "capability-discovery.schema.json")
    market = next(
        row for row in response["result"]["capabilities"]
        if row["capability"] == "market.quote"
    )
    assert market["ready"] is False
    assert market["client_usable"] is False
    assert market["reason"] == "runtime_handler_missing"
