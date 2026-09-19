from __future__ import annotations

from typing import Any, Iterable

from .common import PROTOCOL_VERSION, digest, timepoint


_DOMAIN_BY_CAPABILITY = {
    "portfolio.state": "portfolio",
    "knowledge.current": "knowledge",
    "policy.current": "policy",
}


def compose_decision_context(
    request: dict[str, Any],
    results: Iterable[dict[str, Any]],
    *,
    required_capabilities: set[str] | None = None,
    generated_at: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compose bounded context from capability results; make no investment judgment."""
    generated_at = generated_at or timepoint()
    required = set(required_capabilities or set())
    ordered = sorted(list(results), key=lambda item: str(item.get("capability") or ""))
    domain: dict[str, Any] = {
        "portfolio": None,
        "policy": None,
        "active_plans": [],
        "market": None,
        "macro": None,
        "knowledge": None,
        "ephemeral_evidence": [],
        "previous_decisions": [],
    }
    market_components: dict[str, Any] = {}
    macro_components: dict[str, Any] = {}
    all_gaps: list[dict[str, Any]] = []
    all_conflicts: list[dict[str, Any]] = []
    freshness_summary: dict[str, str] = {}
    missing_required: list[str] = []
    fingerprints: dict[str, str] = {}

    for result in ordered:
        capability = str(result.get("capability") or "")
        freshness_summary[capability] = str(result.get("freshness") or "unknown")
        all_gaps.extend(result.get("gaps") or [])
        all_conflicts.extend(result.get("conflicts") or [])
        status = result.get("status")
        if capability in required and status in {"unavailable", "blocked", "error", "stale"}:
            missing_required.append(capability)
        data = result.get("data")
        if data is not None:
            fingerprints[capability] = digest(data)
        if capability in _DOMAIN_BY_CAPABILITY:
            domain[_DOMAIN_BY_CAPABILITY[capability]] = data
        elif capability == "plan.active" and isinstance(data, dict):
            plans = data.get("plans")
            domain["active_plans"] = list(plans) if isinstance(plans, list) else []
        elif capability.startswith("market."):
            market_components[capability] = data
        elif capability.startswith("macro."):
            macro_components[capability] = data

    if market_components:
        domain["market"] = market_components
    if macro_components:
        domain["macro"] = macro_components

    decision_ready = not missing_required
    basis = {
        "request": request,
        "generated_at": generated_at,
        **domain,
        "component_results": ordered,
        "gaps": all_gaps,
        "conflicts": all_conflicts,
        "freshness_summary": freshness_summary,
        "authority_summary": {
            "decision_ready": decision_ready,
            "required_capabilities": sorted(required),
            "missing_required": sorted(missing_required),
        },
        "input_fingerprints": fingerprints,
    }
    context_digest = digest(basis)
    context = {
        "context_id": f"context:{context_digest[:24]}",
        "context_version": PROTOCOL_VERSION,
        **basis,
        "context_digest": context_digest,
    }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "context": context,
        "client_metadata": {},
        "diagnostics": {},
        "assessments": [],
    }
