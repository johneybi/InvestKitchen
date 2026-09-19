from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"
LEGACY_WORKSPACE = Path(os.environ["TRADEMIND_LEGACY_WORKSPACE"]).resolve() if os.environ.get("TRADEMIND_LEGACY_WORKSPACE") else None
LEGACY_PORTFOLIO_PRIMARY = os.environ.get("TRADEMIND_LEGACY_PORTFOLIO_PRIMARY")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import timepoint  # noqa: E402
from protocol.v1.adapters.capability_registry import get_capabilities  # noqa: E402
from protocol.v1.adapters.context_composer import compose_decision_context  # noqa: E402
from protocol.v1.adapters.knowledge_legacy import get_current_knowledge, search_knowledge  # noqa: E402
from protocol.v1.adapters.market_legacy import adapt_market_result  # noqa: E402
from protocol.v1.adapters.portfolio_legacy import get_portfolio_state  # noqa: E402
from protocol.v1.adapters.reflection_adapter import start_reflection  # noqa: E402


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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def test_portfolio_adapter_is_read_only_and_hides_storage_locators() -> None:
    if LEGACY_WORKSPACE is None or not LEGACY_PORTFOLIO_PRIMARY:
        pytest.skip("set legacy workspace and primary portfolio ID for compatibility integration")
    watched = [
        LEGACY_WORKSPACE / "accounts" / LEGACY_PORTFOLIO_PRIMARY / "positions.json",
        LEGACY_WORKSPACE / "accounts" / LEGACY_PORTFOLIO_PRIMARY / "state.json",
        LEGACY_WORKSPACE / "accounts" / LEGACY_PORTFOLIO_PRIMARY / "transactions" / "2026-09.jsonl",
    ]
    before = {path: _sha(path) for path in watched}
    result = get_portfolio_state(LEGACY_WORKSPACE, LEGACY_PORTFOLIO_PRIMARY)
    after = {path: _sha(path) for path in watched}

    assert after == before
    _validate(result, "capability-result.schema.json")
    _validate(result["data"], "portfolio.schema.json")
    assert result["authority"] == "portfolio_fact"
    assert result["source_mode"] == "local_store"
    leaked = [
        text for text in _strings(result)
        if text.startswith(("accounts/", "knowledge/", "tmp/", ".work/"))
        or (text.startswith("/") and "trademind-framework/" in text)
    ]
    assert leaked == []


def test_market_adapter_normalizes_partial_provider_result_without_network() -> None:
    raw = {
        "schema_version": "1.1",
        "source": "fixture-market-provider",
        "retrieved_at": "2026-09-15T09:45:08+09:00",
        "as_of": "2026-09-15T09:45:08+09:00",
        "quotes": {"005930": {"symbol": "005930", "price": 251500}},
        "failed_symbols": ["000660"],
        "stale_symbols": [],
        "components": {
            "quotes": {"status": "partial", "requested_symbols": ["005930", "000660"]},
            "flows": {"status": "not_requested"},
        },
        "metrics": {"api_calls": 1},
    }
    result = adapt_market_result(raw, capability="market.quote", source_mode="fixture")
    _validate(result, "capability-result.schema.json")
    assert result["status"] == "partial"
    assert result["freshness"] == "current"
    assert result["authority"] == "market_observation"
    assert "metrics" not in result["data"]
    assert any(gap["gap_code"] == "market_symbols_unavailable" for gap in result["gaps"])


def test_knowledge_adapter_reads_committed_generation_without_paths() -> None:
    if LEGACY_WORKSPACE is None:
        pytest.skip("set TRADEMIND_LEGACY_WORKSPACE for legacy compatibility integration")
    watched = [
        LEGACY_WORKSPACE / "knowledge" / "views" / "public_manifest.json",
        LEGACY_WORKSPACE / "knowledge" / "views" / "current_view.json",
    ]
    before = {path: _sha(path) for path in watched}
    result = get_current_knowledge(LEGACY_WORKSPACE, max_outlook=5)
    after = {path: _sha(path) for path in watched}

    assert after == before
    _validate(result, "capability-result.schema.json")
    manifest = _load(LEGACY_WORKSPACE / "knowledge" / "views" / "public_manifest.json")
    assert result["data"]["generation"]["generation_id"] == manifest["generation_id"]
    assert result["authority"] == "knowledge_claim"
    assert all("/knowledge/" not in text and not text.startswith("knowledge/") for text in _strings(result))


def test_knowledge_search_is_bounded_and_storage_path_free() -> None:
    if LEGACY_WORKSPACE is None:
        pytest.skip("set TRADEMIND_LEGACY_WORKSPACE for legacy compatibility integration")
    result = search_knowledge(LEGACY_WORKSPACE, "SK하이닉스", limit=5)
    _validate(result, "capability-result.schema.json")
    assert result["capability"] == "knowledge.search"
    assert result["data"]["result_count"] <= 5
    assert result["data"]["results"]
    assert all("knowledge/" not in text for text in _strings(result))


def test_context_composer_only_composes_and_keeps_assessment_empty() -> None:
    if LEGACY_WORKSPACE is None or not LEGACY_PORTFOLIO_PRIMARY:
        pytest.skip("set legacy workspace and primary portfolio ID for compatibility integration")
    portfolio = get_portfolio_state(LEGACY_WORKSPACE, LEGACY_PORTFOLIO_PRIMARY)
    knowledge = get_current_knowledge(LEGACY_WORKSPACE, max_outlook=3)
    market = adapt_market_result(
        {
            "schema_version": "1.1",
            "source": "fixture-market-provider",
            "retrieved_at": "2026-09-15T09:45:08+09:00",
            "as_of": "2026-09-15T09:45:08+09:00",
            "quotes": {"005930": {"symbol": "005930", "price": 251500}},
            "components": {"quotes": {"status": "available"}},
            "failed_symbols": [],
            "stale_symbols": [],
        },
        capability="market.quote",
        source_mode="fixture",
    )
    request = {
        "decision_request_id": "fixture-request-1",
        "actor": "fixture-client",
        "objective": "오늘 보유 계좌 대응에 필요한 사실 컨텍스트를 조립한다.",
        "subject_refs": [],
        "portfolio_id": LEGACY_PORTFOLIO_PRIMARY,
        "account_ids": [],
        "horizon": "short_term",
        "as_of_request": timepoint("2026-09-15T09:45:09+09:00"),
        "constraints": None,
        "requested_context": ["portfolio.state", "knowledge.current", "market.quote"],
    }
    bundle = compose_decision_context(
        request,
        [portfolio, knowledge, market],
        required_capabilities={"portfolio.state", "knowledge.current", "market.quote"},
        generated_at=timepoint("2026-09-15T09:45:10+09:00"),
    )
    _validate(bundle, "decision-context.schema.json")
    assert bundle["assessments"] == []
    assert bundle["client_metadata"] == {}
    assert bundle["diagnostics"] == {}
    assert bundle["context"]["authority_summary"]["decision_ready"] is True
    assert bundle["context"]["portfolio"]["portfolio_id"] == LEGACY_PORTFOLIO_PRIMARY
    assert bundle["context"]["knowledge"]["generation"]["generation_id"]
    assert bundle["context"]["market"]["market.quote"]["quotes"]["005930"]["price"] == 251500


def test_context_composer_preserves_multiple_market_capabilities() -> None:
    quote = adapt_market_result(
        {
            "source": "fixture-market-provider",
            "retrieved_at": "2026-09-15T09:45:08+09:00",
            "quotes": {"005930": {"price": 251500}},
            "components": {"quotes": {"status": "available"}},
        },
        capability="market.quote",
        source_mode="fixture",
    )
    ohlcv = adapt_market_result(
        {
            "source": "fixture-market-provider",
            "retrieved_at": "2026-09-15T09:45:08+09:00",
            "series": [{"symbol": "005930", "bars": [{"close": 251500}]}],
            "components": {"candles": {"status": "available"}},
        },
        capability="market.ohlcv",
        source_mode="fixture",
    )
    request = {
        "decision_request_id": "fixture-request-multi-market",
        "actor": "fixture-client",
        "objective": "복수 시장 capability 보존을 검증한다.",
        "subject_refs": ["005930"],
        "portfolio_id": None,
        "account_ids": [],
        "horizon": "intraday",
        "as_of_request": timepoint("2026-09-15T09:45:09+09:00"),
        "constraints": None,
        "requested_context": ["market.quote", "market.ohlcv"],
    }
    bundle = compose_decision_context(request, [quote, ohlcv])
    _validate(bundle, "decision-context.schema.json")
    assert set(bundle["context"]["market"]) == {"market.quote", "market.ohlcv"}


def test_context_composer_fails_readiness_for_required_unavailable_capability() -> None:
    unavailable_market = adapt_market_result(
        {
            "schema_version": "1.1",
            "source": "fixture-market-provider",
            "retrieved_at": "2026-09-15T09:45:08+09:00",
            "quotes": {},
            "components": {"quotes": {"status": "unavailable"}},
            "failed_symbols": ["005930"],
            "stale_symbols": [],
        },
        capability="market.quote",
        source_mode="fixture",
    )
    request = {
        "decision_request_id": "fixture-request-2",
        "actor": "fixture-client",
        "objective": "필수 시세가 없는 상태를 검증한다.",
        "subject_refs": ["005930"],
        "portfolio_id": None,
        "account_ids": [],
        "horizon": "intraday",
        "as_of_request": timepoint("2026-09-15T09:45:09+09:00"),
        "constraints": None,
        "requested_context": ["market.quote"],
    }
    bundle = compose_decision_context(
        request,
        [unavailable_market],
        required_capabilities={"market.quote"},
        generated_at=timepoint("2026-09-15T09:45:10+09:00"),
    )
    assert bundle["context"]["authority_summary"]["decision_ready"] is False
    assert bundle["context"]["authority_summary"]["missing_required"] == ["market.quote"]


def test_reflection_session_works_without_portfolio_or_decision_context() -> None:
    result = start_reflection("pre", started_at=timepoint("2026-09-15T09:45:09+09:00"))
    _validate(result, "capability-result.schema.json")
    _validate(result["data"], "reflection.schema.json")
    assert result["authority"] == "reflection_session"
    assert result["data"]["context_ref"] is None
    assert all(dep["optional"] for dep in result["data"]["method"]["context_dependencies"])


def test_reflection_session_can_link_optional_context_without_copying_it() -> None:
    context = {"context_id": "context:fixture", "portfolio": {"sensitive": "not-copied"}}
    result = start_reflection(
        "post",
        context=context,
        started_at=timepoint("2026-09-15T10:00:00+09:00"),
    )
    _validate(result["data"], "reflection.schema.json")
    assert result["data"]["context_ref"] == "context:fixture"
    assert "sensitive" not in json.dumps(result["data"], ensure_ascii=False)


def test_instance_capability_projection_has_no_supported_but_unready_capability() -> None:
    for fixture_name in ("reflection-only.instance.json", "full-reference.instance.json"):
        manifest = _load(PROTOCOL / "fixtures" / fixture_name)
        projected = get_capabilities(manifest)
        assert not [
            row for row in projected["capabilities"]
            if row["client_exposure"] == "supported" and not row["ready"]
        ]
    reflection = get_capabilities(_load(PROTOCOL / "fixtures" / "reflection-only.instance.json"))
    row = next(item for item in reflection["capabilities"] if item["capability"] == "reflection.session")
    assert row["ready"] is True
    assert row["client_usable"] is True


def test_only_system_adapters_may_declare_direct_canonical_access() -> None:
    manifest = _load(PROTOCOL / "fixtures" / "full-reference.instance.json")
    for extension in manifest["extensions"]:
        direct = extension["storage"]["canonical_direct_access"]
        if direct:
            assert extension["extension_type"] == "system_adapter"


def test_regular_extension_cannot_request_direct_canonical_storage_access() -> None:
    manifest = _load(PROTOCOL / "fixtures" / "full-reference.instance.json")
    extension = next(item for item in manifest["extensions"] if item["extension_type"] == "provider")
    invalid = json.loads(json.dumps(extension))
    invalid["storage"]["canonical_direct_access"] = True
    with pytest.raises(jsonschema.ValidationError):
        schema = _load(SCHEMAS / "extension-manifest.schema.json")
        jsonschema.Draft202012Validator(
            schema,
            registry=_registry(),
            format_checker=jsonschema.FormatChecker(),
        ).validate(invalid)
