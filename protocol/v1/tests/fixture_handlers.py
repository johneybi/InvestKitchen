from __future__ import annotations

from typing import Any

from protocol.v1.adapters.common import result_envelope, timepoint
from protocol.v1.adapters.gateway_facade import build_compatibility_handlers


def synthetic_handlers(*, market_handlers: dict[str, Any] | None = None) -> dict[str, Any]:
    handlers = build_compatibility_handlers(None, market_handlers=market_handlers)
    handlers.update(
        {
            "portfolio.state": _portfolio_state,
            "knowledge.current": _knowledge_current,
            "knowledge.search": _knowledge_search,
        }
    )
    return handlers


def _portfolio_state(payload: dict[str, Any]) -> dict[str, Any]:
    portfolio_id = str(payload["portfolio_id"])
    generated_at = timepoint("2026-09-16T00:00:00Z")
    return result_envelope(
        capability="portfolio.state",
        producer="trademind.fixture.portfolio",
        status="ok",
        data={
            "protocol_version": "1.0-draft",
            "portfolio_id": portfolio_id,
            "display_name": "Fixture Portfolio",
            "generated_at": generated_at,
            "accounts": [],
            "positions": [],
            "cash": [],
            "transactions": [],
            "policies": [],
            "completeness": {
                "holdings": "complete",
                "cash": "complete",
                "valuation": "unknown",
                "fx": "unknown",
                "transactions": "complete",
                "accounts": [],
            },
            "migration_gaps": [],
        },
        authority="portfolio_fact",
        generated_at=generated_at,
        freshness="local",
        source_mode="fixture",
        permissions_used=["portfolio.read"],
    )


def _knowledge_current(_payload: dict[str, Any]) -> dict[str, Any]:
    return result_envelope(
        capability="knowledge.current",
        producer="trademind.fixture.knowledge",
        status="ok",
        data={
            "generation": {"generation_id": "fixture-generation", "commit_status": "committed", "committed_at": None},
            "summary": "Synthetic committed knowledge fixture.",
            "outlook": [],
        },
        authority="knowledge_claim",
        freshness="local",
        source_mode="fixture",
        permissions_used=["knowledge.read"],
    )


def _knowledge_search(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "")
    limit = int(payload.get("limit", 20))
    results = []
    if query:
        results.append(
            {
                "claim_id": "fixture-claim-1",
                "statement": "Synthetic fixture claim",
                "subject": "fixture-asset",
                "match_score": 1,
            }
        )
    return result_envelope(
        capability="knowledge.search",
        producer="trademind.fixture.knowledge",
        status="ok",
        data={
            "query": query,
            "result_count": min(len(results), limit),
            "results": results[:limit],
            "generation_id": "fixture-generation",
        },
        authority="knowledge_claim",
        freshness="local",
        source_mode="fixture",
        permissions_used=["knowledge.read"],
    )
