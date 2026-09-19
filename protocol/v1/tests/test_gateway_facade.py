from __future__ import annotations

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

from protocol.v1.adapters.common import timepoint  # noqa: E402
from protocol.v1.adapters.gateway_facade import (  # noqa: E402
    ReadOnlyGatewayFacade,
    dumps_public,
)
from protocol.v1.adapters.market_legacy import adapt_market_result  # noqa: E402
from protocol.v1.tests.fixture_handlers import synthetic_handlers  # noqa: E402


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
                "schema_version": "1.1",
                "source": "Toss Securities Open API",
                "retrieved_at": "2026-09-15T09:45:08+09:00",
                "as_of": "2026-09-15T09:45:08+09:00",
                "quotes": {
                    symbol: {"symbol": symbol, "price": 250000 + index * 1000}
                    for index, symbol in enumerate(symbols)
                },
                "components": {"quotes": {"status": "available"}},
                "failed_symbols": [],
                "stale_symbols": [],
                "metrics": {"api_calls": 1},
            }
        else:
            raw = {
                "schema_version": "1.1",
                "source": "Toss Securities Open API",
                "retrieved_at": "2026-09-15T09:45:08+09:00",
                "series": [
                    {"symbol": symbol, "bars": [{"timestamp": "2026-09-15T00:00:00+09:00", "close": 250000}]}
                    for symbol in symbols
                ],
                "components": {"candles": {"status": "available"}},
                "metrics": {"api_calls": 1},
            }
        return adapt_market_result(raw, capability=capability, source_mode="fixture")

    return handler


def _gateway() -> ReadOnlyGatewayFacade:
    handlers = synthetic_handlers(
        market_handlers={
            "market.quote": _fixture_market_handler("market.quote"),
            "market.ohlcv": _fixture_market_handler("market.ohlcv"),
        },
    )
    return ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=handlers,
    )


def _assert_public_surface_has_no_implementation_leak(value: Any) -> None:
    encoded = dumps_public(value).casefold()
    forbidden = [
        "toss securities",
        "fetch_toss",
        "/github/",
        "trademind-framework/",
        "accounts/",
        "knowledge/",
        "tmp/",
        ".work/",
        "official.compat",
        "legacy-",
        "legacy_",
        "project_legacy_records.py",
    ]
    assert not [token for token in forbidden if token in encoded]


def test_gateway_capability_discovery_is_client_facing() -> None:
    result = _gateway().get_capabilities()
    _validate(result, "capability-discovery.schema.json")
    assert result["instance_id"] == "fixture-full-reference"
    assert all("provider" not in row for row in result["capabilities"])
    assert next(row for row in result["capabilities"] if row["capability"] == "market.ohlcv")["client_usable"] is True
    _assert_public_surface_has_no_implementation_leak(result)


def test_gateway_lists_only_server_configured_portfolio_scope() -> None:
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=synthetic_handlers(),
        portfolio_ids=["portfolio-b", "portfolio-a", "portfolio-a"],
    )
    assert gateway.list_portfolios() == {
        "protocol_version": "1.0-draft",
        "portfolio_ids": ["portfolio-a", "portfolio-b"],
        "scope_source": "server_configured_personal_scope",
    }


def test_gateway_read_tools_are_schema_valid_and_hide_implementation() -> None:
    gateway = _gateway()
    portfolio = gateway.get_portfolio_state("fixture")
    knowledge = gateway.get_current_knowledge(max_outlook=3)
    search = gateway.search_knowledge("SK하이닉스", limit=3)
    quote = gateway.get_market_quote(["005930", "000660"])
    ohlcv = gateway.get_market_ohlcv(["005930"], interval="1d", count=20)
    for result in (portfolio, knowledge, search, quote, ohlcv):
        _validate(result, "capability-result.schema.json")
        _assert_public_surface_has_no_implementation_leak(result)
    assert "source" not in quote["data"]
    assert "metrics" not in quote["data"]


def test_gateway_builds_context_from_stable_capabilities_only() -> None:
    gateway = _gateway()
    request = {
        "decision_request_id": "gateway-fixture-request",
        "actor": "web-gpt",
        "objective": "오늘 계좌 대응에 필요한 사실 컨텍스트를 구성한다.",
        "subject_refs": ["005930"],
        "portfolio_id": "fixture",
        "account_ids": [],
        "horizon": "short_term",
        "as_of_request": timepoint("2026-09-15T09:45:09+09:00"),
        "constraints": None,
        "requested_context": [
            "portfolio.state",
            "knowledge.current",
            "market.quote",
            "market.ohlcv",
        ],
    }
    bundle = gateway.build_decision_context(
        request,
        capability_inputs={
            "portfolio.state": {"portfolio_id": "fixture"},
            "knowledge.current": {"max_outlook": 3},
            "market.quote": {"symbols": ["005930"]},
            "market.ohlcv": {"symbols": ["005930"], "interval": "1d", "count": 20},
        },
        required_capabilities={"portfolio.state", "knowledge.current", "market.quote"},
    )
    _validate(bundle, "decision-context.schema.json")
    assert bundle["context"]["authority_summary"]["decision_ready"] is True
    assert set(bundle["context"]["market"]) == {"market.quote", "market.ohlcv"}
    assert bundle["assessments"] == []
    _assert_public_surface_has_no_implementation_leak(bundle)


def test_gateway_context_fails_readiness_when_required_market_handler_is_missing() -> None:
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=synthetic_handlers(),
    )
    request = {
        "decision_request_id": "gateway-missing-market-handler",
        "actor": "web-gpt",
        "objective": "필수 시세 handler 누락 시 fail-closed를 확인한다.",
        "subject_refs": ["005930"],
        "portfolio_id": None,
        "account_ids": [],
        "horizon": "intraday",
        "as_of_request": timepoint("2026-09-15T09:45:09+09:00"),
        "constraints": None,
        "requested_context": ["market.quote"],
    }
    bundle = gateway.build_decision_context(
        request,
        capability_inputs={"market.quote": {"symbols": ["005930"]}},
        required_capabilities={"market.quote"},
    )
    assert bundle["context"]["authority_summary"]["decision_ready"] is False
    assert bundle["context"]["authority_summary"]["missing_required"] == ["market.quote"]
    _assert_public_surface_has_no_implementation_leak(bundle)


def test_gateway_can_start_reflection_without_or_with_context() -> None:
    gateway = _gateway()
    standalone = gateway.start_reflection("pre")
    _validate(standalone, "capability-result.schema.json")
    assert standalone["data"]["context_ref"] is None

    request = {
        "decision_request_id": "reflection-context-request",
        "actor": "web-gpt",
        "objective": "행동 전 점검용 컨텍스트",
        "subject_refs": [],
        "portfolio_id": "fixture",
        "account_ids": [],
        "horizon": "short_term",
        "as_of_request": timepoint("2026-09-15T09:45:09+09:00"),
        "constraints": None,
        "requested_context": ["portfolio.state"],
    }
    bundle = gateway.build_decision_context(
        request,
        capability_inputs={"portfolio.state": {"portfolio_id": "fixture"}},
    )
    linked = gateway.start_reflection("post", context_bundle=bundle)
    assert linked["data"]["context_ref"] == bundle["context"]["context_id"]
    assert "portfolio" not in linked["data"]
    _assert_public_surface_has_no_implementation_leak(linked)


def test_gateway_fails_closed_when_supported_capability_has_no_handler() -> None:
    gateway = ReadOnlyGatewayFacade(
        workspace=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        handlers=synthetic_handlers(),
    )
    discovery = gateway.get_capabilities()
    market_row = next(row for row in discovery["capabilities"] if row["capability"] == "market.quote")
    assert market_row["ready"] is False
    assert market_row["client_usable"] is False
    assert market_row["reason"] == "runtime_handler_missing"
    result = gateway.get_market_quote(["005930"])
    _validate(result, "capability-result.schema.json")
    assert result["status"] == "unavailable"
    assert result["data"] is None
    assert result["gaps"][0]["gap_code"] == "capability_handler_missing"
    _assert_public_surface_has_no_implementation_leak(result)
