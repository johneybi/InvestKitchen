#!/usr/bin/env python3
"""Read-only compatibility projector from current TradeMind records to protocol/v1.

This script deliberately does not mutate any canonical account, knowledge,
decision, monitor, or runtime record. It only reads the current repository and
writes projection fixtures to an explicitly supplied output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_VERSION = "1.0-draft"
ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def now_timepoint() -> dict[str, str]:
    return {
        "value": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "precision": "source_exact",
    }


def timepoint(raw: Any, precision: str | None = None) -> dict[str, str] | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return {"value": text, "precision": precision or "source_exact"}
    except ValueError:
        pass
    match = ISO_DATE_RE.search(text)
    if match:
        return {"value": match.group(1), "precision": precision or "date_only"}
    return None


def evidence_ref(relative_path: str, fragment: str) -> str:
    return f"legacy:{relative_path}#{fragment}"


def nonempty_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def sanitize_for_protocol(value: Any) -> Any:
    """Drop implementation locators from legacy packet fragments.

    Protocol v1 must not make local paths/workspaces/generated files into stable
    domain identity. Human-readable source locators remain in Knowledge objects,
    but runtime filesystem diagnostics are removed here.
    """
    if isinstance(value, list):
        return [sanitize_for_protocol(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        low = key.lower()
        if low in {
            "workspace",
            "source_path",
            "normalized_path",
            "canonical_path",
            "generated_files",
            "internal_id_notice",
        }:
            continue
        if low.endswith("_path") or low.endswith("_file") or low.endswith("_dir"):
            continue
        out[key] = sanitize_for_protocol(item)
    return out


def asset_ref(holding_or_tx: dict[str, Any]) -> dict[str, Any]:
    symbol = holding_or_tx.get("quote_symbol")
    currency = holding_or_tx.get("quote_currency")
    asset = {
        "asset_type": "unknown",
        "symbol": symbol,
        "venue": None,
        "currency": currency if isinstance(currency, str) and len(currency) == 3 else None,
        "display_name": str(holding_or_tx.get("name") or symbol or "Unknown asset"),
        "provider_refs": {},
    }
    return asset


def quantity_semantics(raw_status: Any, carry_forward: bool) -> tuple[str, str]:
    status = str(raw_status or "").lower()
    if "minimum" in status:
        quantity_status = "confirmed_minimum"
    elif status.startswith("confirmed"):
        quantity_status = "confirmed"
    elif carry_forward:
        quantity_status = "estimated_current"
    else:
        quantity_status = "unknown"

    if "execution_adjusted" in status:
        basis = "execution_adjusted"
    elif "screenshot" in status:
        basis = "direct_observation"
    elif "user_stated" in status or "user_executed" in status or status == "confirmed":
        basis = "user_confirmation"
    elif "reconcil" in status:
        basis = "balance_reconciled"
    elif carry_forward:
        basis = "carried_forward"
    else:
        basis = "unknown"
    return quantity_status, basis


def money_values(holding: dict[str, Any], prefix: str, *, avg: bool = False) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    key_map = (
        [("average_cost_displayed", "observed"), ("average_cost_derived", "derived")]
        if avg
        else [("cost_basis_krw", "observed"), ("cost_basis_usd", "observed")]
    )
    for key, value_basis in key_map:
        if holding.get(key) is None:
            continue
        currency = "USD" if key.endswith("_usd") else str(holding.get("quote_currency") or "KRW")
        if avg and currency not in {"KRW", "USD"}:
            currency = "KRW"
        values.append(
            {
                "amount": float(holding[key]),
                "currency": currency,
                "value_basis": value_basis,
                "source_evidence": [],
            }
        )
    if not avg and holding.get("manual_market_value_krw") is not None and prefix == "cost_basis":
        # Manual market value is intentionally not mislabeled as cost basis.
        pass
    return values


def cash_kind(row: dict[str, Any]) -> tuple[str, str | None]:
    raw = str(row.get("kind") or row.get("status") or "").lower()
    if "d_plus_2" in raw or "d+2" in raw:
        return "displayed_settlement", row.get("kind") or row.get("status")
    if "orderable" in raw:
        # Legacy label is retained without asserting exchange settlement semantics.
        return "provider_specific", row.get("kind") or row.get("status")
    if "displayed" in raw:
        return "provider_specific", row.get("kind") or row.get("status")
    if "reconstructed" in raw or "adjusted" in raw:
        return "provider_specific", row.get("kind") or row.get("status")
    return "unknown", row.get("kind") or row.get("status")


def transaction_type_and_side(side: Any) -> tuple[str, str | None]:
    raw = str(side or "").lower()
    if raw in {"buy", "sell"}:
        return "trade", raw
    if raw in {"cash_transfer_in", "cash_transfer_out"}:
        return "cash_transfer", raw.removeprefix("cash_")
    if raw == "deposit":
        return "deposit", raw
    if raw == "withdrawal":
        return "withdrawal", raw
    return "other", raw or None


def execution_basis(source_type: Any) -> str:
    raw = str(source_type or "").lower()
    if "official" in raw or "execution_sms" in raw:
        return "official_notice"
    if "explicit_user" in raw:
        return "explicit_user_confirmation"
    if "snapshot" in raw or "balance" in raw:
        return "balance_reconciliation"
    if "broker" in raw and "fill" in raw:
        return "provider_fill"
    return "other"


def project_portfolio(workspace: Path, portfolio_id: str) -> dict[str, Any]:
    registry = load_json(workspace / "accounts" / "registry.json")
    registry_row = next(p for p in registry["portfolios"] if p["portfolio_id"] == portfolio_id)
    root = workspace / "accounts" / portfolio_id
    profile = load_json(root / "profile.json")
    state = load_json(root / "state.json")
    positions_doc = load_json(root / "positions.json")
    carry_forward = bool(positions_doc.get("carry_forward"))

    tactical = set(
        profile.get("preferences", {})
        .get("tactical_trading", {})
        .get("tactical_sleeve_designated_accounts", [])
    )
    core = set(
        profile.get("preferences", {})
        .get("tactical_trading", {})
        .get("core_designated_accounts", [])
    )
    profile_accounts = {a["account_id"]: a for a in profile.get("accounts", [])}
    accounts: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    cash_rows: list[dict[str, Any]] = []
    account_completeness: list[dict[str, Any]] = []

    for account in positions_doc.get("accounts", []):
        account_id = account["account_id"]
        meta = profile_accounts.get(account_id, {})
        role = "tactical" if account_id in tactical else "core" if account_id in core else "unknown"
        accounts.append(
            {
                "account_id": account_id,
                "portfolio_id": portfolio_id,
                "display_name": str(meta.get("label") or account.get("account_label") or account_id),
                "provider_id": meta.get("brokerage"),
                "account_type": meta.get("type"),
                "base_currency": profile.get("base_currency"),
                "role": role,
                "status": "active",
                "constraints": [str(v) for v in meta.get("constraints", [])],
            }
        )
        account_tp = timepoint(account.get("account_as_of") or positions_doc.get("account_as_of", {}).get(account_id))
        account_completeness.append(
            {
                "account_id": account_id,
                "holdings": "complete" if account.get("holdings") is not None else "unknown",
                "cash": "partial" if account.get("cash") else "unknown",
                **({"observed_at": account_tp} if account_tp else {}),
            }
        )
        for index, holding in enumerate(account.get("holdings", [])):
            quantity_status, quantity_basis = quantity_semantics(holding.get("quantity_status"), carry_forward)
            source_id = evidence_ref(
                f"accounts/{portfolio_id}/positions.json",
                f"{account_id}.holdings[{index}]",
            )
            observed = timepoint(holding.get("confirmed_as_of") or account.get("account_as_of") or positions_doc.get("updated_on"))
            recorded = timepoint(positions_doc.get("updated_on"))
            position = {
                "position_id": f"legacy-position:{portfolio_id}:{account_id}:{index}",
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "asset": asset_ref(holding),
                "quantity": float(holding.get("quantity", 0)),
                "quantity_status": quantity_status,
                "quantity_basis": quantity_basis,
                "cost_basis_values": money_values(holding, "cost_basis"),
                "avg_cost_values": money_values(holding, "avg_cost", avg=True),
                "source_evidence": [source_id],
                "authority": "portfolio_fact" if quantity_status.startswith("confirmed") else "estimated_portfolio_state",
                "state": "closed" if float(holding.get("quantity", 0)) == 0 else "open",
            }
            if observed:
                position["observed_at"] = observed
            if recorded:
                position["recorded_at"] = recorded
            positions.append(position)

        for index, row in enumerate(account.get("cash", [])):
            kind, provider_label = cash_kind(row)
            tp = timepoint(row.get("as_of") or row.get("base_as_of") or row.get("adjusted_through") or account.get("account_as_of"))
            recorded = timepoint(positions_doc.get("updated_on"))
            source_id = evidence_ref(
                f"accounts/{portfolio_id}/positions.json",
                f"{account_id}.cash[{index}]",
            )
            basis_text = str(row.get("status") or row.get("kind") or "").lower()
            value_basis = "reconstructed" if ("reconstruct" in basis_text or "adjusted" in basis_text) else "observed"
            cash = {
                "cash_id": f"legacy-cash:{portfolio_id}:{account_id}:{index}",
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "currency": str(row.get("currency") or profile.get("base_currency") or "KRW"),
                "cash_kind": kind,
                "provider_label": str(provider_label) if provider_label else None,
                "value": {
                    "amount": float(row.get("amount", 0)),
                    "currency": str(row.get("currency") or profile.get("base_currency") or "KRW"),
                    "value_basis": value_basis,
                    "source_evidence": [source_id],
                    **({"as_of": tp} if tp else {}),
                },
                "source_evidence": [source_id],
                "authority": "portfolio_fact" if str(row.get("status") or row.get("kind") or "").startswith(("confirmed", "displayed", "orderable")) else "estimated_portfolio_state",
            }
            if tp:
                cash["observed_at"] = tp
            if recorded:
                cash["recorded_at"] = recorded
            cash_rows.append(cash)

    tx_rows: list[dict[str, Any]] = []
    tx_path = root / "transactions" / "2026-09.jsonl"
    for index, row in enumerate(load_jsonl(tx_path)):
        tx_type, side = transaction_type_and_side(row.get("side"))
        quote_currency = row.get("quote_currency")
        if not quote_currency:
            quote_currency = "USD" if row.get("quote_symbol") in {"AAPL", "JEPQ", "DRAM", "IAU", "JEPI", "QQQM"} else profile.get("base_currency", "KRW")
        occurrence = "confirmed" if str(row.get("status", "")).startswith("confirmed_execution") else "candidate"
        detail = "complete" if all(row.get(k) is not None for k in ("price", "fees_krw", "taxes_krw")) else "partial"
        source_id = evidence_ref(f"accounts/{portfolio_id}/transactions/2026-09.jsonl", str(index + 1))
        effective = timepoint(row.get("trade_date")) or timepoint(row.get("confirmed_on"))
        tx: dict[str, Any] = {
            "transaction_id": row["transaction_id"],
            "portfolio_id": portfolio_id,
            "account_id": row["account_id"],
            "transaction_type": tx_type,
            "side": side,
            "quantity": float(row["quantity"]) if row.get("quantity") is not None else None,
            "price": (
                {
                    "amount": float(row["price"]),
                    "currency": str(quote_currency),
                    "value_basis": "observed" if execution_basis(row.get("source_type")) != "balance_reconciliation" else "derived",
                    "source_evidence": [source_id],
                }
                if row.get("price") is not None
                else None
            ),
            "amount": (
                {
                    "amount": float(row["gross_amount"]),
                    "currency": str(quote_currency),
                    "value_basis": "observed" if execution_basis(row.get("source_type")) != "balance_reconciliation" else "derived",
                    "source_evidence": [source_id],
                }
                if row.get("gross_amount") is not None
                else None
            ),
            "effective_at": effective or {"value": row.get("trade_date", "1970-01-01"), "precision": "date_only"},
            "occurrence_status": occurrence,
            "detail_status": detail,
            "execution_basis": execution_basis(row.get("source_type")),
            "source_type": str(row.get("source_type") or "legacy_unknown"),
            "source_evidence": [source_id],
            "provider_execution_id": None,
            "transfer_group_id": None,
            "lineage": None,
        }
        if row.get("quote_symbol") or row.get("name"):
            tx["asset"] = asset_ref({**row, "quote_currency": quote_currency})
        recorded = timepoint(row.get("confirmed_on"))
        if recorded:
            tx["recorded_at"] = recorded
        tx_rows.append(tx)

    policy_rules = {
        "decision_policy": profile.get("decision_policy", {}),
        "stock_sector_targets": profile.get("stock_sector_targets", {}),
        "preferences": profile.get("preferences", {}),
    }
    policy = {
        "policy_id": f"legacy-policy:{portfolio_id}:profile",
        "portfolio_id": portfolio_id,
        "scope": "portfolio",
        "version": str(profile.get("schema_version") or "legacy"),
        "status": "active",
        "rules": sanitize_for_protocol(policy_rules),
        "authority": "legacy_user_policy_record",
        "supersedes": None,
    }

    completeness_text = str(positions_doc.get("completeness") or state.get("completeness") or "").lower()
    holdings_state = "complete" if "holdings_complete" in completeness_text else "partial"
    generated = now_timepoint()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "portfolio_id": portfolio_id,
        "display_name": registry_row.get("display_alias", portfolio_id),
        "generated_at": generated,
        "accounts": accounts,
        "positions": positions,
        "cash": cash_rows,
        "transactions": tx_rows,
        "policies": [policy],
        "completeness": {
            "holdings": holdings_state,
            "cash": "partial",
            "valuation": "partial",
            "fx": "partial" if any(p["asset"].get("currency") == "USD" for p in positions) else "unknown",
            "transactions": "partial",
            "accounts": account_completeness,
        },
        "migration_gaps": [
            {
                "gap_code": "legacy_cash_semantics",
                "reason": "Legacy cash labels do not consistently distinguish nominal, settled, buying-power, and withdrawable cash.",
                "impact": "Cash subtypes remain provider_specific/unknown where semantics are not explicit.",
                "recoverable": True,
            },
            {
                "gap_code": "legacy_execution_identity",
                "reason": "Most legacy transaction rows do not carry broker/provider execution IDs.",
                "impact": "Migration must preserve source evidence and local transaction identity; tuple similarity is not a safe dedupe key.",
                "recoverable": False,
            },
            {
                "gap_code": "legacy_transfer_pair_identity",
                "reason": "Internal cash-transfer in/out rows do not carry an explicit shared transfer_group_id.",
                "impact": "The projector does not infer pair identity from matching amount/date alone.",
                "recoverable": True,
            },
        ],
    }


def load_document_index(workspace: Path) -> dict[str, dict[str, Any]]:
    rows = load_jsonl(workspace / "knowledge" / "indexes" / "documents.jsonl")
    return {row["document_id"]: row for row in rows if row.get("document_id")}


def project_knowledge(workspace: Path) -> dict[str, Any]:
    registry = load_json(workspace / "knowledge" / "events" / "reconciliation_registry.json")
    docs = registry.get("documents", [])
    preferred = ["doc-b9dbefc8f42327f4fff3", "doc-bbb1240d28395841d5da"]
    by_id = {d.get("document_id"): d for d in docs}
    selected = [by_id[d] for d in preferred if d in by_id]
    if len(selected) < 2:
        verified = [d for d in docs if d.get("status") == "VERIFIED"]
        verified.sort(key=lambda d: str(d.get("verified_at") or d.get("last_changed_at") or ""), reverse=True)
        for row in verified:
            if row not in selected:
                selected.append(row)
            if len(selected) >= 2:
                break

    index = load_document_index(workspace)
    evidence_rows: list[dict[str, Any]] = []
    selected_ids = {d["document_id"] for d in selected}
    for row in selected:
        doc_id = row["document_id"]
        meta = index.get(doc_id, {})
        authority = meta.get("authority")
        if authority not in {"primary", "transcript", "secondary", "derived"}:
            authority = "primary" if row.get("document_type") == "mentor_comment" else "secondary"
        published = timepoint(row.get("published_at"))
        recorded = timepoint(row.get("verified_at") or row.get("last_changed_at")) or now_timepoint()
        source = {
            "source_type": str(row.get("document_type") or "legacy_document"),
            "provider_or_publisher": str(meta.get("program") or "legacy-knowledge-store"),
            "speaker": meta.get("speaker"),
            "document_id": doc_id,
            "url_or_external_id": meta.get("source_url"),
        }
        evidence: dict[str, Any] = {
            "evidence_id": f"evidence:document:{doc_id}",
            "evidence_kind": "document",
            "subject_refs": [str(v) for v in meta.get("topics", [])],
            "source": source,
            "source_locator": row.get("source_ref") or f"document:{doc_id}",
            "content_ref_or_value": {"document_id": doc_id},
            "authority_class": authority,
            "recorded_at": recorded,
            "content_hash": row.get("source_sha256"),
            "verification": "workflow_verified" if row.get("status") == "VERIFIED" else "unverified",
            "freshness": "recent" if published and published["value"].startswith("2026-09") else "historical",
            "lifecycle_scope": "canonical",
            "context_id": None,
        }
        if published:
            evidence["published_at"] = published
        evidence_rows.append(evidence)

    claim_rows: list[dict[str, Any]] = []
    for row in load_jsonl(workspace / "knowledge" / "events" / "claims.jsonl"):
        if row.get("document_id") not in selected_ids:
            continue
        evidence_type = str(row.get("evidence_type") or "")
        if evidence_type == "direct_statement":
            fidelity = "direct"
        elif evidence_type == "inference":
            fidelity = "inference"
        else:
            fidelity = "unknown"
        verification = "verified" if row.get("primary_verification", {}).get("verified") is True else "unverified"
        precision = str(row.get("effective_at_precision") or "unknown")
        tp = timepoint(row.get("effective_at"), precision if precision in {"source_exact", "date_only", "session", "inferred", "unknown"} else "unknown")
        if tp and tp["precision"] in {"session", "inferred"}:
            tp["inference_basis"] = str(row.get("time_inference_basis") or "legacy record marked inferred/session without explicit basis in projection")
        claim = {
            "claim_id": row["claim_id"],
            "statement": str(row.get("claim") or row.get("statement") or "Legacy claim"),
            "subject_refs": nonempty_strings(row.get("subject")),
            "speaker": row.get("speaker"),
            "claim_type": str(row.get("claim_type") or "legacy_claim"),
            "stance": row.get("stance"),
            "horizon": row.get("time_horizon"),
            "conditions": nonempty_strings(row.get("conditions")),
            "invalidation": nonempty_strings(row.get("invalidation")),
            "evidence_refs": [f"evidence:document:{row['document_id']}"],
            "derivation_type": evidence_type if evidence_type in {"direct_statement", "paraphrase", "inference"} else "unknown",
            "registration_state": "canonical",
            "provenance_verification": verification,
            "semantic_fidelity": fidelity,
            "truth_status": "attributed_opinion",
            "applicability_status": "unresolved",
            "confidence": row.get("confidence") if row.get("confidence") in {"low", "medium", "high"} else "unknown",
        }
        if tp:
            claim["effective_at"] = tp
        claim_rows.append(claim)

    manifest = load_json(workspace / "knowledge" / "views" / "public_manifest.json")
    committed = timepoint(manifest.get("committed_at")) or now_timepoint()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": now_timepoint(),
        "knowledge_generation": {
            "generation_id": str(manifest.get("generation_id") or "legacy-unknown-generation"),
            "commit_status": str(manifest.get("status") or "unknown") if manifest.get("status") in {"committed", "installing", "failed"} else "unknown",
            "committed_at": committed,
            "registry_or_input_digest": None,
            "artifact_refs": sorted(str(k) for k in manifest.get("artifacts", {}).keys()),
        },
        "evidence": evidence_rows,
        "claims": claim_rows,
        "migration_gaps": [
            {
                "gap_code": "legacy_document_verified_overload",
                "reason": "Legacy document VERIFIED means workflow completion, not claim truth verification.",
                "impact": "Projected Evidence uses workflow_verified while Claims retain independent provenance/truth states.",
                "recoverable": False,
            }
        ],
    }


def normalize_status(raw: Any) -> str:
    value = str(raw or "unavailable").lower()
    if value in {"ok", "partial", "stale", "unavailable", "blocked", "error"}:
        return value
    if value in {"available", "fresh"}:
        return "ok"
    return "unavailable"


def normalize_freshness(raw: Any) -> tuple[str, str]:
    value = str(raw or "unknown").lower()
    if value == "live":
        return "current", "live_fetch"
    if value == "recent":
        return "recent", "live_fetch"
    if value == "cached":
        return "cached", "cache"
    if value == "stale":
        return "stale", "unknown"
    if value == "local_only":
        return "local", "local_store"
    return "unknown", "unknown"


def capability_authority(name: str) -> str:
    if name in {"load_account_records", "load_active_plans"}:
        return "portfolio_fact"
    if name in {"fetch_quotes", "fetch_market_snapshot"}:
        return "market_observation"
    if name == "load_current_knowledge":
        return "knowledge_claim"
    if name == "revalue_positions":
        return "derived_calculation"
    if name == "run_portfolio_advisor":
        return "analysis_assessment"
    return "unknown"


def project_decision_context(workspace: Path) -> dict[str, Any]:
    packet_path = workspace / "tmp" / "answer_packet" / "user-answer-packet.json"
    if not packet_path.exists():
        packet_path = workspace / ".work" / "answer-packet" / "user-answer-packet.json"
    packet = load_json(packet_path)
    generated_raw = packet.get("status", {}).get("generated_at") or packet.get("provenance", {}).get("generated_at")
    generated = timepoint(generated_raw) or now_timepoint()
    question = packet.get("question", {})
    account = sanitize_for_protocol(packet.get("account"))
    portfolio_id = account.get("portfolio_id") if isinstance(account, dict) else None
    request_basis = {
        "objective": str(question.get("text") or "Legacy projected decision request"),
        "scope": question.get("scope"),
        "horizon": question.get("horizon"),
        "portfolio_id": portfolio_id,
        "generated_at": generated,
    }
    request_id = f"legacy-decision-request:{digest(request_basis)[:24]}"

    cap_status = packet.get("capability_status", {})
    cap_freshness = packet.get("capability_freshness", {})
    cap_prov = packet.get("capability_provenance", {})
    cap_warnings = packet.get("capability_warnings", {})
    components: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    for name in sorted(cap_status):
        freshness, source_mode = normalize_freshness(cap_freshness.get(name))
        prov = cap_prov.get(name) if isinstance(cap_prov.get(name), dict) else {}
        tool_name = str(prov.get("tool") or name)
        producer_version = str(prov.get("version") or packet.get("schema_version") or "legacy")
        warnings = [str(v) for v in cap_warnings.get(name, [])] if isinstance(cap_warnings.get(name), list) else nonempty_strings(cap_warnings.get(name))
        gaps = [
            {
                "gap_code": f"legacy_capability_warning:{name}",
                "required_capability": name,
                "scope": None,
                "reason": warning,
                "impact": "Capability result may be incomplete for this DecisionContext.",
                "recoverable": True,
            }
            for warning in warnings
        ]
        all_gaps.extend(gaps)
        components.append(
            {
                "capability": name,
                "contract_version": "legacy-projection",
                "producer": {"id": tool_name, "version": producer_version},
                "status": normalize_status(cap_status.get(name)),
                "generated_at": generated,
                "data_times": {},
                "freshness": freshness,
                "source_mode": source_mode,
                "provenance": [
                    {
                        "source_type": "legacy_capability",
                        "source_id": name,
                        "producer": tool_name,
                        "producer_version": producer_version,
                    }
                ],
                "authority": capability_authority(name),
                "warnings": warnings,
                "gaps": gaps,
                "conflicts": [],
                "permissions_used": [],
            }
        )

    for index, reason in enumerate(packet.get("status", {}).get("data_gaps", [])):
        all_gaps.append(
            {
                "gap_code": f"legacy_packet_gap:{index}",
                "reason": str(reason),
                "impact": "Legacy packet reported an unresolved data gap.",
                "recoverable": True,
            }
        )

    domain = {
        "portfolio": account,
        "policy": account.get("allocation_policy") if isinstance(account, dict) else None,
        "active_plans": sanitize_for_protocol(packet.get("active_plans", [])),
        "market": sanitize_for_protocol(packet.get("market")),
        "macro": None,
        "knowledge": sanitize_for_protocol(packet.get("knowledge")),
        "ephemeral_evidence": sanitize_for_protocol([packet.get("external_evidence")]) if packet.get("external_evidence") else [],
        "previous_decisions": [],
    }
    fingerprints = {
        key: digest(value)
        for key, value in domain.items()
        if value not in (None, [], {})
    }
    context_without_identity = {
        "request": request_basis,
        "generated_at": generated,
        **domain,
        "component_results": components,
        "gaps": all_gaps,
        "conflicts": [],
        "freshness_summary": {name: normalize_freshness(cap_freshness.get(name))[0] for name in cap_status},
        "authority_summary": sanitize_for_protocol(packet.get("execution_constraints", {})),
        "input_fingerprints": fingerprints,
    }
    context_digest = digest(context_without_identity)
    context_id = f"legacy-context:{context_digest[:24]}"
    request = {
        "decision_request_id": request_id,
        "actor": "legacy-client-projection",
        "objective": str(question.get("text") or "Legacy projected decision request"),
        "subject_refs": nonempty_strings(question.get("scope", {}).get("subject") if isinstance(question.get("scope"), dict) else None),
        "portfolio_id": portfolio_id,
        "account_ids": [],
        "horizon": question.get("horizon"),
        "as_of_request": generated,
        "constraints": None,
        "requested_context": sorted(cap_status.keys()),
    }
    context = {
        "context_id": context_id,
        "context_version": "1.0-draft",
        "request": request,
        "generated_at": generated,
        **domain,
        "component_results": components,
        "gaps": all_gaps,
        "conflicts": [],
        "freshness_summary": {name: normalize_freshness(cap_freshness.get(name))[0] for name in cap_status},
        "authority_summary": sanitize_for_protocol(packet.get("execution_constraints", {})),
        "input_fingerprints": fingerprints,
        "context_digest": context_digest,
    }

    assessments: list[dict[str, Any]] = []
    guidance = sanitize_for_protocol(packet.get("decision_guidance"))
    if isinstance(guidance, dict) and guidance:
        assessment_basis = {"context": context_id, "guidance": guidance}
        assessments.append(
            {
                "assessment_id": f"legacy-assessment:{digest(assessment_basis)[:24]}",
                "assessment_type": "portfolio_advice_guidance",
                "producer": {"id": "legacy.run_portfolio_advisor", "version": str(packet.get("schema_version") or "legacy")},
                "input_context_id": context_id,
                "input_refs": [],
                "generated_at": generated,
                "observations": nonempty_strings(guidance.get("primary_status")),
                "interpretation": guidance,
                "conditions": guidance.get("decisive_conditions") if isinstance(guidance.get("decisive_conditions"), list) else nonempty_strings(guidance.get("decisive_conditions")),
                "invalidation": guidance.get("invalidation") if isinstance(guidance.get("invalidation"), list) else nonempty_strings(guidance.get("invalidation")),
                "confidence": "unknown",
                "authority": "analysis_assessment",
            }
        )

    return {
        "protocol_version": PROTOCOL_VERSION,
        "context": context,
        "client_metadata": {
            "response_mode": packet.get("response_mode"),
            "advisory_mode": packet.get("advisory_mode"),
            "editorial_guidance": sanitize_for_protocol(packet.get("editorial_guidance", {})),
        },
        "diagnostics": {
            "processing_metrics": sanitize_for_protocol(packet.get("processing_metrics", {})),
            "legacy_packet_schema": packet.get("schema_version"),
        },
        "assessments": assessments,
    }


def project_monitor_operation() -> dict[str, Any]:
    payload = {
        "asset": {"type": "stock", "symbol": "005930", "market": "KR"},
        "condition": {"operator": "lte", "value": 250000},
        "session": "KRX_REGULAR",
        "initial_behavior": "arm_only",
        "notification": {"channels": ["telegram"]},
        "monitor_purpose": "governed",
    }
    payload_digest = digest(payload)
    generated = {"value": "2026-09-15T07:00:00Z", "precision": "source_exact"}
    expires = {"value": "2026-09-15T07:15:00Z", "precision": "source_exact"}
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operation": {
            "operation_id": "legacy-monitor-create-fixture",
            "request_id": "fixture-request-create-monitor",
            "idempotency_key": "fixture-idempotency-create-monitor",
            "actor": "legacy-system",
            "action": "create_monitor",
            "target": {"resource_type": "monitor", "portfolio_id": "fixture-portfolio"},
            "requested_at": generated,
            "state": "completed",
        },
        "preview": {
            "preview_id": "fixture-preview-monitor",
            "operation_id": "legacy-monitor-create-fixture",
            "action": "create_monitor",
            "target": {"resource_type": "monitor", "portfolio_id": "fixture-portfolio"},
            "base_version": None,
            "canonical_payload": payload,
            "payload_digest": payload_digest,
            "expected_effect": "Register one governed monitor; readiness remains gated.",
            "warnings": [],
            "generated_at": generated,
            "expires_at": expires,
        },
        "approval": None,
        "legacy_confirmation": {
            "userConfirmed": True,
            "confirmation_hash": payload_digest,
            "binding": "payload_hash_plus_same_idempotency_key",
            "durable_authenticated_receipt": False,
        },
        "receipt": {
            "operation_id": "legacy-monitor-create-fixture",
            "action": "create_monitor",
            "target": {"resource_type": "monitor", "resource_id": "mon_fixture"},
            "state": "completed",
            "applied_version": None,
            "result_ref": "mon_fixture",
            "result_claim": "supported",
            "effect_scope": "resource_registered",
            "target_lifecycle_state_ref": "monitor=PENDING_GATE;gate=PENDING",
            "completed_at": generated,
            "failure_code": None,
            "audit_ref": "fixture:audit:create-monitor",
        },
        "target_lifecycle": {
            "registered_state": {"monitor": "PENDING_GATE", "gate": "PENDING"},
            "ready_state_after_worker_gate_success": {"monitor": "ARMED", "gate": "VALID"},
            "operation_completion_does_not_imply_ready": True,
            "fencing_fields_present_in_legacy_runtime": ["lease_owner", "lease_expires_at", "lease_fence"],
        },
        "migration_gaps": [
            {
                "gap_code": "approval_receipt_not_durable",
                "reason": "Legacy AI confirmation is an in-memory payload/hash binding and does not prove authenticated user/client approval across process restart.",
                "impact": "It cannot be promoted to a protocol/v1 ApprovalReceipt without a new authenticated approval flow.",
                "recoverable": True,
            }
        ],
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def project_all(workspace: Path, output_dir: Path, portfolio_ids: list[str]) -> list[Path]:
    outputs: list[Path] = []
    for portfolio_id in portfolio_ids:
        path = output_dir / f"portfolio-{portfolio_id}.json"
        write_json(path, project_portfolio(workspace, portfolio_id))
        outputs.append(path)
    knowledge = output_dir / "knowledge-projection.json"
    write_json(knowledge, project_knowledge(workspace))
    outputs.append(knowledge)
    context = output_dir / "decision-context-projection.json"
    write_json(context, project_decision_context(workspace))
    outputs.append(context)
    operation = output_dir / "monitor-operation-legacy-projection.json"
    write_json(operation, project_monitor_operation())
    outputs.append(operation)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--portfolio-id", action="append", dest="portfolio_ids", required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    output_dir = Path(args.output_dir).resolve()
    for path in project_all(workspace, output_dir, list(args.portfolio_ids)):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
