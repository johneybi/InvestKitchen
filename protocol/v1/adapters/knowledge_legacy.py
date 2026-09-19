from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import ADAPTER_VERSION, result_envelope, timepoint


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object expected: {path.name}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def get_current_knowledge(workspace: Path, *, max_outlook: int = 20) -> dict[str, Any]:
    """Read the committed current Knowledge view without exposing filesystem layout."""
    manifest = _load(workspace / "knowledge" / "views" / "public_manifest.json")
    view = _load(workspace / "knowledge" / "views" / "current_view.json")
    outlook = view.get("outlook") if isinstance(view.get("outlook"), list) else []
    truncated = len(outlook) > max_outlook
    gaps: list[dict[str, Any]] = []
    if truncated:
        gaps.append({
            "gap_code": "knowledge_outlook_truncated",
            "required_capability": "knowledge.current",
            "scope": {"returned": max_outlook, "available": len(outlook)},
            "reason": "The current Knowledge read is intentionally bounded.",
            "impact": "Additional durable outlook entries may exist outside this context.",
            "recoverable": True,
        })
    data = {
        "generation": {
            "generation_id": manifest.get("generation_id"),
            "commit_status": manifest.get("status"),
            "committed_at": manifest.get("committed_at"),
        },
        "as_of": view.get("as_of"),
        "valid_until": view.get("valid_until"),
        "freshness_status": view.get("freshness_status"),
        "situational_usable": view.get("situational_usable"),
        "market_state": view.get("market_state"),
        "stance": view.get("stance"),
        "confidence": view.get("confidence"),
        "summary": view.get("summary"),
        "action_bias": view.get("action_bias", []),
        "confirmation_conditions": view.get("confirmation_conditions", []),
        "invalidation_conditions": view.get("invalidation_conditions", []),
        "outlook": outlook[:max_outlook],
        "reconciliation_scope": view.get("reconciliation_scope"),
        "data_quality_notes": view.get("data_quality_notes", []),
    }
    raw_freshness = str(view.get("freshness_status") or "unknown")
    freshness = "current" if raw_freshness == "current" else "stale" if raw_freshness == "stale" else "unknown"
    status = "partial" if gaps or view.get("reconciliation_scope") == "partial" else "stale" if freshness == "stale" else "ok"
    generated = timepoint(str(view.get("generated_at"))) if view.get("generated_at") else timepoint()
    return result_envelope(
        capability="knowledge.current",
        producer="official.compat.knowledge-legacy",
        status=status,
        data=data,
        authority="knowledge_claim",
        generated_at=generated,
        freshness=freshness,
        source_mode="local_store",
        provenance=[{
            "source_type": "knowledge_generation",
            "source_id": str(manifest.get("generation_id") or "unknown-generation"),
            "producer": "official.compat.knowledge-legacy",
            "producer_version": ADAPTER_VERSION,
        }],
        warnings=[gap["reason"] for gap in gaps],
        gaps=gaps,
        permissions_used=["knowledge.read"],
        data_times={
            **({"effective_at": view.get("as_of")} if view.get("as_of") else {}),
            **({"recorded_at": view.get("generated_at")} if view.get("generated_at") else {}),
        },
    )


def search_knowledge(workspace: Path, query: str, *, limit: int = 20) -> dict[str, Any]:
    """Bounded deterministic search over canonical legacy Claims.

    This is deliberately a compatibility reader, not semantic/vector search.
    It exposes stable Claim/document identities and source-local locators without
    leaking repository storage paths.
    """
    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    terms = [term.casefold() for term in query.split() if term.strip()]
    rows: list[tuple[int, str, dict[str, Any]]] = []
    for row in _load_jsonl(workspace / "knowledge" / "events" / "claims.jsonl"):
        searchable = " ".join(
            str(row.get(key) or "")
            for key in ("claim", "subject", "speaker", "claim_type", "stance", "tags")
        ).casefold()
        score = sum(searchable.count(term) for term in terms) if terms else 1
        if score <= 0:
            continue
        rows.append((score, str(row.get("effective_at") or ""), row))
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = rows[:limit]
    manifest = _load(workspace / "knowledge" / "views" / "public_manifest.json")
    results = []
    for score, _, row in selected:
        results.append({
            "claim_id": row.get("claim_id"),
            "statement": row.get("claim"),
            "subject": row.get("subject"),
            "speaker": row.get("speaker"),
            "claim_type": row.get("claim_type"),
            "stance": row.get("stance"),
            "horizon": row.get("time_horizon"),
            "conditions": row.get("conditions", []),
            "invalidation": row.get("invalidation", []),
            "effective_at": row.get("effective_at"),
            "effective_at_precision": row.get("effective_at_precision"),
            "evidence_type": row.get("evidence_type"),
            "confidence": row.get("confidence"),
            "document_id": row.get("document_id"),
            "source_locator": row.get("source_locator"),
            "match_score": score,
        })
    return result_envelope(
        capability="knowledge.search",
        producer="official.compat.knowledge-legacy",
        status="ok",
        data={
            "query": query,
            "result_count": len(results),
            "results": results,
            "generation_id": manifest.get("generation_id"),
        },
        authority="knowledge_claim",
        freshness="local",
        source_mode="local_store",
        provenance=[{
            "source_type": "knowledge_generation",
            "source_id": str(manifest.get("generation_id") or "unknown-generation"),
            "producer": "official.compat.knowledge-legacy",
            "producer_version": ADAPTER_VERSION,
        }],
        permissions_used=["knowledge.read"],
    )
