from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from protocol.v1.adapters.common import PROTOCOL_VERSION
from protocol.v1.runtime.legacy_knowledge_migration import build_canonical_queue

REVIEW_SCHEMA_VERSION = 1
_DIRECT_TYPES = {"lecture", "mentor_comment"}
_SECONDARY_TYPES = {"youtube_summary"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_document_ids(value: Any) -> set[str]:
    found: set[str] = set()
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"document_id", "source_document_id"} and isinstance(item, str):
                    found.add(item)
                elif key == "source_document_ids" and isinstance(item, list):
                    found.update(v for v in item if isinstance(v, str))
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return found


def _collect_claim_ids(value: Any) -> set[str]:
    found: set[str] = set()
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "claim_id" and isinstance(item, str):
                    found.add(item)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return found


def native_baseline_refs(personal_data_root: Path) -> tuple[set[str], set[str]]:
    document_ids: set[str] = set()
    claim_ids: set[str] = set()
    for rel in ("knowledge/evidence-claims.json", "knowledge/current.json"):
        path = personal_data_root / rel
        if not path.is_file():
            continue
        value = _read_json(path)
        document_ids |= _collect_document_ids(value)
        claim_ids |= _collect_claim_ids(value)
    return document_ids, claim_ids


def native_journal_refs(journal_path: Path | None) -> tuple[set[str], set[str]]:
    document_ids: set[str] = set()
    claim_ids: set[str] = set()
    if journal_path is None or not journal_path.is_file():
        return document_ids, claim_ids
    for raw in journal_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        request = row.get("request") if isinstance(row, dict) else None
        if isinstance(request, dict):
            document_ids |= _collect_document_ids(request)
            claim_ids |= _collect_claim_ids(request)
    return document_ids, claim_ids


def _legacy_claims(legacy_root: Path) -> dict[str, list[dict[str, Any]]]:
    by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    path = legacy_root / "knowledge/events/claims.jsonl"
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        document_id = row.get("document_id")
        if isinstance(document_id, str):
            by_document[document_id].append(row)
    return by_document


def _is_durable_claim(row: dict[str, Any]) -> bool:
    horizon = str(row.get("time_horizon") or "").lower()
    claim_type = str(row.get("claim_type") or "")
    # Automatic promotion is intentionally narrower than "looks like a rule".
    # A legacy technical/trading/risk label may still describe a dated example
    # or one security. Require the corpus' explicit durable horizon or its
    # strongest durable_rule classification. "continuous_discipline" remains a
    # manual-review signal because the underlying statement can still contain a
    # sector/example-specific recommendation.
    return horizon == "durable" or claim_type == "durable_rule"


def _is_manual_rule_like(row: dict[str, Any]) -> bool:
    horizon = str(row.get("time_horizon") or "").lower()
    claim_type = str(row.get("claim_type") or "")
    return horizon == "continuous_discipline" or claim_type in {
        "risk_management_rule", "technical_rule", "trading_rule",
        "investment_principle", "sell_discipline", "allocation_rule", "asset_allocation_rule",
    }


def build_semantic_review(
    legacy_root: Path,
    personal_data_root: Path,
    *,
    native_journal_path: Path | None = None,
) -> dict[str, Any]:
    queue = build_canonical_queue(legacy_root)
    historical = {
        row["document_id"]: row for row in queue["entries"]
        if row.get("status") == "historical_knowledge_source"
    }
    baseline_docs, baseline_claims = native_baseline_refs(personal_data_root)
    journal_docs, journal_claims = native_journal_refs(native_journal_path)
    native_docs = baseline_docs | journal_docs
    native_claims = baseline_claims | journal_claims
    legacy_claims = _legacy_claims(legacy_root)

    review_rows: list[dict[str, Any]] = []
    selected_claims: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for document_id, meta in sorted(historical.items()):
        claims = legacy_claims.get(document_id, [])
        remaining_claims = [r for r in claims if r.get("claim_id") not in native_claims]
        durable = [r for r in remaining_claims if _is_durable_claim(r)]
        manual_rule_like = [r for r in remaining_claims if _is_manual_rule_like(r)]
        document_type = meta.get("document_type")
        speaker = meta.get("speaker")

        if document_id in native_docs:
            classification = "already_represented_native"
            reason = "native baseline/current/journal already references this legacy source document"
        elif not claims:
            classification = "historical_source_only"
            reason = "source is preserved but legacy corpus has no structured claim to promote"
        elif not remaining_claims:
            classification = "already_represented_claims"
            reason = "all structured legacy claim ids are already present in native Knowledge"
        elif not durable and document_type in _DIRECT_TYPES and manual_rule_like:
            classification = "manual_review_direct_rule"
            reason = "direct source contains rule-like semantics but is time-bound/example-specific or lacks an explicit durable horizon"
        elif not durable and document_type in _SECONDARY_TYPES and manual_rule_like:
            classification = "manual_review_secondary_rule"
            reason = "secondary summary contains rule-like semantics but needs source/speaker verification and applicability review"
        elif not durable:
            classification = "historical_time_bound"
            reason = "structured claims are situational/outlook/time-bound rather than durable rules"
        elif document_type in _DIRECT_TYPES:
            promotable = [
                r for r in durable
                if r.get("confidence") in {"medium", "high"}
                and r.get("evidence_type") in {"paraphrase", "direct_statement", "quote"}
                and isinstance(r.get("speaker"), str) and r.get("speaker").strip()
            ]
            if promotable:
                classification = "promote_direct_durable"
                reason = "direct lecture/mentor source with medium/high-confidence durable attributed rule"
                selected_claims.extend(promotable)
            else:
                classification = "manual_review_direct_durable"
                reason = "direct source has durable semantics but confidence/provenance is insufficient for automatic promotion"
        elif document_type in _SECONDARY_TYPES:
            classification = "manual_review_secondary_durable"
            reason = "secondary summary contains durable semantics but requires manual source/speaker verification"
        else:
            classification = "archive_noncanonical"
            reason = "derived/other source is not eligible for automatic canonical claim promotion"

        counts[classification] += 1
        review_rows.append({
            "document_id": document_id,
            "classification": classification,
            "reason": reason,
            "document_type": document_type,
            "speaker": speaker,
            "published_at": meta.get("published_at"),
            "source_sha256": meta.get("source_sha256"),
            "legacy_claim_count": len(claims),
            "remaining_claim_count": len(remaining_claims),
            "durable_claim_ids": [str(r.get("claim_id")) for r in durable if r.get("claim_id")],
            "manual_rule_claim_ids": [str(r.get("claim_id")) for r in manual_rule_like if r.get("claim_id")],
            "selected_claim_ids": [str(r.get("claim_id")) for r in selected_claims if r.get("document_id") == document_id],
        })

    selected_claims.sort(key=lambda row: str(row.get("claim_id")))
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "policy": {
            "current_state_mutation": False,
            "automatic_promotion": "direct lecture/mentor durable claims only; medium/high confidence; attributed; workflow-preserved provenance",
            "secondary_summary": "manual review only",
            "time_bound": "historical-only",
        },
        "summary": {
            "historical_source_documents": len(historical),
            "native_referenced_documents": len(set(historical) & native_docs),
            "reviewed_documents": len(review_rows),
            "by_classification": dict(sorted(counts.items())),
            "selected_claims": len(selected_claims),
            "selected_documents": len({r.get("document_id") for r in selected_claims}),
        },
        "selected_claims": selected_claims,
        "entries": review_rows,
    }


def _timepoint(raw: str | None, *, fallback: str) -> dict[str, str]:
    value = raw or fallback
    precision = "source_exact" if "T" in value else "date_only"
    return {"value": value, "precision": precision}


def build_promotion_request(review: dict[str, Any], *, generated_at: str | None = None) -> dict[str, Any]:
    selected = review.get("selected_claims")
    if not isinstance(selected, list):
        raise ValueError("semantic review selected_claims is invalid")
    generated = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        if not isinstance(row, dict) or not isinstance(row.get("document_id"), str):
            raise ValueError("selected claim is invalid")
        by_document[row["document_id"]].append(row)

    evidence: list[dict[str, Any]] = []
    evidence_id_by_document: dict[str, str] = {}
    review_entries = {row["document_id"]: row for row in review.get("entries", []) if isinstance(row, dict) and isinstance(row.get("document_id"), str)}
    for document_id in sorted(by_document):
        meta = review_entries[document_id]
        evidence_id = "evidence:legacy-durable:" + hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:24]
        evidence_id_by_document[document_id] = evidence_id
        source = {
            "source_type": str(meta.get("document_type") or "historical_document"),
            "provider_or_publisher": "legacy-trademind-framework",
            "document_id": document_id,
        }
        if isinstance(meta.get("speaker"), str) and meta.get("speaker"):
            source["speaker"] = meta["speaker"]
        row: dict[str, Any] = {
            "evidence_id": evidence_id,
            "evidence_kind": "historical_source",
            "subject_refs": ["historical_knowledge", "durable_rule"],
            "source": source,
            "authority_class": "primary",
            "verification": "workflow_verified",
            "freshness": "historical",
            "recorded_at": _timepoint(generated, fallback=generated),
            "content_ref_or_value": f"historical-source:{document_id}",
            "content_hash": meta.get("source_sha256"),
            "lifecycle_scope": "canonical",
        }
        if isinstance(meta.get("published_at"), str) and meta.get("published_at"):
            row["published_at"] = _timepoint(meta["published_at"], fallback=generated)
        evidence.append(row)

    claims: list[dict[str, Any]] = []
    for legacy in selected:
        doc = str(legacy["document_id"])
        effective = legacy.get("effective_at") if isinstance(legacy.get("effective_at"), str) else None
        claim: dict[str, Any] = {
            "claim_id": str(legacy["claim_id"]),
            "statement": str(legacy["claim"]),
            "subject_refs": [str(legacy.get("subject") or "durable_rule")],
            "claim_type": str(legacy.get("claim_type") or "durable_rule"),
            "conditions": [str(v) for v in (legacy.get("conditions") or [])],
            "invalidation": [str(v) for v in (legacy.get("invalidation") or [])],
            "evidence_refs": [evidence_id_by_document[doc]],
            "derivation_type": "paraphrase" if legacy.get("evidence_type") == "paraphrase" else "direct_statement",
            "registration_state": "canonical",
            "provenance_verification": "verified",
            "semantic_fidelity": "faithful_paraphrase",
            "truth_status": "attributed_opinion",
            "applicability_status": "conditional",
            "speaker": legacy.get("speaker"),
            "stance": legacy.get("stance"),
            "horizon": str(legacy.get("time_horizon") or "durable"),
            "confidence": str(legacy.get("confidence") or "unknown"),
        }
        if effective:
            claim["effective_at"] = _timepoint(effective, fallback=generated)
        claims.append(claim)

    digest_seed = "|".join(sorted(str(c["claim_id"]) for c in claims))
    generation_id = "knowledge-generation:legacy-durable-rules-" + hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:16]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_type": "knowledge.commit",
        "generation_id": generation_id,
        "generated_at": _timepoint(generated, fallback=generated),
        "current_state": {},
        "evidence": evidence,
        "claims": claims,
    }


def write_semantic_review(review: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_promotion_request(request: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
