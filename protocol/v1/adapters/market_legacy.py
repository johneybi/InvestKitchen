from __future__ import annotations

from typing import Any

from .common import ADAPTER_VERSION, result_envelope, timepoint


def _component_statuses(raw: dict[str, Any]) -> list[str]:
    statuses: list[str] = []
    for item in (raw.get("components") or {}).values():
        if isinstance(item, dict) and item.get("status"):
            statuses.append(str(item["status"]).lower())
    return statuses


def _status(raw: dict[str, Any]) -> str:
    statuses = _component_statuses(raw)
    has_data = bool(raw.get("quotes") or raw.get("series") or raw.get("flows"))
    if "partial" in statuses or (has_data and "unavailable" in statuses):
        return "partial"
    if "stale" in statuses or raw.get("stale_symbols"):
        return "stale"
    if has_data or "available" in statuses:
        return "ok"
    if statuses and all(s in {"not_requested", "empty"} for s in statuses):
        return "partial"
    return "unavailable"


def _gaps(raw: dict[str, Any], capability: str) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    failed = [str(v) for v in raw.get("failed_symbols", [])]
    stale = [str(v) for v in raw.get("stale_symbols", [])]
    if failed:
        gaps.append({
            "gap_code": "market_symbols_unavailable",
            "required_capability": capability,
            "scope": {"symbols": failed},
            "reason": "The legacy market provider did not return all requested symbols.",
            "impact": "Market context is incomplete for the listed symbols.",
            "recoverable": True,
        })
    if stale:
        gaps.append({
            "gap_code": "market_symbols_stale",
            "required_capability": capability,
            "scope": {"symbols": stale},
            "reason": "The provider used stale observations for some symbols.",
            "impact": "Current-timing claims for those symbols are not authoritative.",
            "recoverable": True,
        })
    for component_name, component in (raw.get("components") or {}).items():
        if not isinstance(component, dict):
            continue
        if component.get("status") in {"partial", "unavailable", "stale"}:
            gaps.append({
                "gap_code": f"market_component_{component.get('status')}",
                "required_capability": capability,
                "scope": {"component": component_name},
                "reason": f"Market component {component_name} is {component.get('status')}.",
                "impact": "The composed market observation may be incomplete.",
                "recoverable": True,
            })
    return gaps


def adapt_market_result(
    raw: dict[str, Any],
    *,
    capability: str = "market.snapshot",
    source_mode: str = "live_fetch",
) -> dict[str, Any]:
    """Normalize a legacy Toss-shaped market result without performing network I/O."""
    retrieved_at = str(raw.get("retrieved_at") or raw.get("as_of") or "")
    generated_at = timepoint(retrieved_at) if retrieved_at else timepoint()
    status = _status(raw)
    gaps = _gaps(raw, capability)
    public_data = {
        key: value
        for key, value in raw.items()
        if key not in {"metrics"}
    }
    freshness = "stale" if status == "stale" else "current" if status in {"ok", "partial"} else "unknown"
    return result_envelope(
        capability=capability,
        producer="official.compat.market-legacy",
        status=status,
        data=public_data,
        authority="market_observation",
        generated_at=generated_at,
        freshness=freshness,
        source_mode=source_mode,
        provenance=[{
            "source_type": "market_provider",
            "source_id": str(raw.get("source") or "legacy-market-provider"),
            "producer": "official.compat.market-legacy",
            "producer_version": ADAPTER_VERSION,
        }],
        warnings=[gap["reason"] for gap in gaps],
        gaps=gaps,
        permissions_used=["market.read"],
        data_times={
            **({"observed_at": raw.get("as_of")} if raw.get("as_of") else {}),
            **({"retrieved_at": raw.get("retrieved_at")} if raw.get("retrieved_at") else {}),
        },
    )
