from __future__ import annotations

import copy
from typing import Any

from protocol.v1.adapters.common import ADAPTER_VERSION, result_envelope, timepoint
from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore
from protocol.v1.runtime.personal_data_store import PersonalDataStore


def get_portfolio_state(store: PersonalDataStore, portfolio_id: str) -> dict[str, Any]:
    data = store.portfolio(portfolio_id)
    gaps = list(data.get("migration_gaps") or [])
    return result_envelope(
        capability="portfolio.state",
        producer="trademind.native.personal-data",
        status="partial" if gaps else "ok",
        data=data,
        authority="portfolio_fact",
        generated_at=data.get("generated_at") or timepoint(),
        freshness="local",
        source_mode="local_store",
        provenance=[{
            "source_type": "personal_data_bundle",
            "source_id": str(store.load_manifest().get("bundle_id") or "unknown-bundle"),
            "producer": "trademind.native.personal-data",
            "producer_version": ADAPTER_VERSION,
        }],
        warnings=[str(gap.get("reason") or "") for gap in gaps if isinstance(gap, dict)],
        gaps=gaps,
        permissions_used=["portfolio.read.positions", "portfolio.read.cash", "transaction.read", "policy.read"],
    )


def get_current_knowledge(
    store: PersonalDataStore | None,
    *,
    max_outlook: int = 20,
    native_knowledge: NativeKnowledgeStore | None = None,
) -> dict[str, Any]:
    if max_outlook < 1 or max_outlook > 100:
        raise ValueError("max_outlook must be between 1 and 100")
    overlay = native_knowledge.overlay() if native_knowledge is not None else None
    if store is None and overlay is None:
        raise ValueError("knowledge store is unavailable")
    stored = copy.deepcopy(store.knowledge_current()) if store is not None else result_envelope(
        capability="knowledge.current",
        producer="investkitchen.native.knowledge",
        status="ok",
        data={},
        authority="knowledge_claim",
        freshness="local",
        source_mode="local_store",
        permissions_used=["knowledge.read"],
    )
    data = stored.get("data") if isinstance(stored.get("data"), dict) else {}
    if overlay is not None:
        current_patch = copy.deepcopy(overlay["current_state"])
        # Keep the existing client-facing Knowledge current shape stable even
        # though native writes validate these fields as TimePoint objects.
        for key in ("as_of", "valid_until"):
            point = current_patch.get(key)
            if isinstance(point, dict) and isinstance(point.get("value"), str):
                current_patch[key] = point["value"]
        data.update(current_patch)
        data["generation"] = copy.deepcopy(overlay["knowledge_generation"])
    outlook = data.get("outlook") if isinstance(data.get("outlook"), list) else []
    truncated = len(outlook) > max_outlook
    data["outlook"] = outlook[:max_outlook]
    gaps = [
        gap for gap in stored.get("gaps") or []
        if isinstance(gap, dict) and gap.get("gap_code") != "knowledge_outlook_truncated"
    ]
    if truncated:
        gaps.append({
            "gap_code": "knowledge_outlook_truncated",
            "required_capability": "knowledge.current",
            "scope": {"returned": max_outlook, "available": len(outlook)},
            "reason": "The current Knowledge read is intentionally bounded.",
            "impact": "Additional durable outlook entries exist outside this response.",
            "recoverable": True,
        })
    status = str(stored.get("status") or "ok")
    freshness = str(stored.get("freshness") or "local")
    if overlay is not None:
        native_freshness = str(overlay["current_state"].get("freshness_status") or "unknown")
        freshness = native_freshness if native_freshness in {"current", "recent", "stale"} else "local"
        if native_freshness == "stale":
            status = "stale"
        elif status == "stale":
            status = "ok"
    if truncated and status == "ok":
        status = "partial"
    producer_id = "investkitchen.native.knowledge" if overlay is not None else "trademind.native.personal-data"
    provenance = list(stored.get("provenance") or [])
    if overlay is not None:
        provenance.append({
            "source_type": "native_knowledge_journal",
            "source_id": str(overlay["knowledge_generation"]["generation_id"]),
            "producer": "investkitchen.native.knowledge",
            "producer_version": ADAPTER_VERSION,
        })
    return {
        **stored,
        "producer": {"id": producer_id, "version": ADAPTER_VERSION},
        "status": status,
        "data": data,
        "gaps": gaps,
        "freshness": freshness,
        "generated_at": copy.deepcopy(overlay["generated_at"]) if overlay is not None else stored.get("generated_at") or timepoint(),
        "provenance": provenance,
        "warnings": list(dict.fromkeys([
            *[str(value) for value in stored.get("warnings") or []],
            *(["The current Knowledge read is intentionally bounded."] if truncated else []),
        ])),
        "source_mode": "local_store",
        "permissions_used": ["knowledge.read"],
    }


def search_knowledge(
    store: PersonalDataStore | None,
    query: str,
    *,
    limit: int = 20,
    native_knowledge: NativeKnowledgeStore | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    terms = [term.casefold() for term in query.split() if term.strip()]
    baseline = store.knowledge_projection() if store is not None else {"claims": [], "evidence": []}
    overlay = native_knowledge.overlay() if native_knowledge is not None else None
    if store is None and overlay is None:
        raise ValueError("knowledge store is unavailable")
    by_claim = {
        str(row.get("claim_id")): dict(row)
        for row in baseline.get("claims") or []
        if isinstance(row, dict) and row.get("claim_id")
    }
    if overlay is not None:
        for row in overlay["claims"]:
            by_claim[str(row["claim_id"])] = dict(row)
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for row in by_claim.values():
        if row.get("registration_state") != "canonical":
            continue
        searchable = " ".join([
            str(row.get("statement") or ""),
            " ".join(str(value) for value in row.get("subject_refs") or []),
            str(row.get("speaker") or ""),
            str(row.get("claim_type") or ""),
            str(row.get("stance") or ""),
        ]).casefold()
        score = sum(searchable.count(term) for term in terms) if terms else 1
        if score > 0:
            effective = row.get("effective_at") if isinstance(row.get("effective_at"), dict) else {}
            ranked.append((score, str(effective.get("value") or ""), row))
    ranked.sort(key=lambda item: (item[0], item[1], str(item[2].get("claim_id") or "")), reverse=True)
    results = []
    for score, _, row in ranked[:limit]:
        results.append({
            "claim_id": row.get("claim_id"),
            "statement": row.get("statement"),
            "subject_refs": row.get("subject_refs", []),
            "speaker": row.get("speaker"),
            "claim_type": row.get("claim_type"),
            "stance": row.get("stance"),
            "horizon": row.get("horizon"),
            "conditions": row.get("conditions", []),
            "invalidation": row.get("invalidation", []),
            "effective_at": row.get("effective_at"),
            "valid_until": row.get("valid_until"),
            "confidence": row.get("confidence"),
            "evidence_refs": row.get("evidence_refs", []),
            "registration_state": row.get("registration_state"),
            "provenance_verification": row.get("provenance_verification"),
            "semantic_fidelity": row.get("semantic_fidelity"),
            "truth_status": row.get("truth_status"),
            "applicability_status": row.get("applicability_status"),
            "match_score": score,
        })
    current = get_current_knowledge(store, max_outlook=1, native_knowledge=native_knowledge)
    generation = ((current.get("data") or {}).get("generation") or {}) if isinstance(current.get("data"), dict) else {}
    return result_envelope(
        capability="knowledge.search",
        producer="investkitchen.native.knowledge" if overlay is not None else "trademind.native.personal-data",
        status="ok",
        data={
            "query": query,
            "result_count": len(results),
            "results": results,
            "generation_id": generation.get("generation_id"),
        },
        authority="knowledge_claim",
        freshness="local",
        source_mode="local_store",
        provenance=list(current.get("provenance") or []),
        permissions_used=["knowledge.read"],
    )
