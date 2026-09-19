from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from protocol.v1.runtime.advisory_policy_update import (
    AdvisoryPolicyUpdateError,
    apply_policy_update,
    build_policy_update_preview,
)
from protocol.v1.adapters.common import canonical_json
from protocol.v1.runtime.advisory_state_store import AdvisoryPolicyStore
from protocol.v1.runtime.opinion_weighting import (
    OpinionWeightingError,
    OpinionWeightingStore,
    apply_weighting_update,
    build_opinion_consensus,
    build_weighting_update_preview,
)


def _policy_store(tmp_path: Path) -> AdvisoryPolicyStore:
    root = tmp_path / "advisory-state" / "policies"
    root.mkdir(parents=True, exist_ok=True)
    policies = [
        {
            "protocol_version": "1.0-draft",
            "policy_id": "policy:portfolio-a:fixture-v1",
            "portfolio_id": "portfolio-a",
            "scope": "portfolio_advisory",
            "status": "active",
            "effective_from": {"value": "2026-01-01", "precision": "date_only"},
            "authority": "user_policy_record",
            "order_authorized": False,
            "rules": {"risk_budget": {"minimum": 0.01, "maximum": 0.03}},
            "source_evidence": ["fixture:policy-a"],
            "data_quality_notes": [],
        },
        {
            "protocol_version": "1.0-draft",
            "policy_id": "policy:portfolio-b:fixture-v1",
            "portfolio_id": "portfolio-b",
            "scope": "portfolio_advisory",
            "status": "active",
            "effective_from": {"value": "2026-01-01", "precision": "date_only"},
            "authority": "user_policy_record",
            "order_authorized": False,
            "rules": {"rebalance_confirmation": ["signal", "risk_check"]},
            "source_evidence": ["fixture:policy-b"],
            "data_quality_notes": [],
        },
    ]
    encoded = canonical_json(policies) + "\n"
    (root / "policies.json").write_text(encoded, encoding="utf-8")
    sha = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": 1,
        "summary": {"policies": 2, "active_by_portfolio": {"portfolio-a": 1, "portfolio-b": 1}},
        "policies_sha256": sha,
    }
    (root / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return AdvisoryPolicyStore(root)


def _config() -> dict:
    return {
        "schema_version": "2.1",
        "policy_version": "fixture-v1",
        "policy_status": "experimental_descriptive_only",
        "decision_usage": "organize_source_views_only_not_action_generation",
        "speaker_aliases": {"Analyst B Alias": "Analyst B"},
        "mode_policies": {
            "short_term": {
                "label": "단기 대응",
                "horizons": ["days", "weeks"],
                "anchor_weights": {"Analyst A": 0.4, "Analyst B": 0.3},
                "supplemental_pool": 0.1,
                "base_reserve": 0.2,
            },
            "medium_term": {
                "label": "중기 포지션",
                "horizons": ["months"],
                "anchor_weights": {"Analyst A": 0.35, "Analyst B": 0.25},
                "supplemental_pool": 0.1,
                "base_reserve": 0.3,
            },
            "long_term": {
                "label": "장기 산업",
                "horizons": ["years"],
                "anchor_weights": {"Analyst A": 0.25, "Analyst B": 0.15},
                "supplemental_pool": 0.1,
                "base_reserve": 0.5,
            },
        },
        "policy": {"minimum_reserve": 0.1, "max_claims_per_speaker": 3},
        "domain_keywords": {
            "market_tactical": ["시장", "매수"],
            "portfolio_management": ["비중"],
            "risk_stress": ["위험"],
        },
        "claim_approaches": {
            "skin_in_the_game": {"claim_types": [], "keywords": ["직접 매수"]},
        },
        "user_preferences": {
            "speaker_multipliers": {},
            "speaker_approach_multipliers": {"Analyst B": {"skin_in_the_game": 1.3}},
            "speaker_approach_decision_roles": {},
            "excluded_speakers": [],
            "note": "fixture",
        },
        "revision_history": [],
    }


def _claim(claim_id: str, speaker: str, statement: str) -> dict:
    return {
        "claim_id": claim_id,
        "statement": statement,
        "subject_refs": ["market"],
        "speaker": speaker,
        "claim_type": "mentor_view",
        "stance": "conditional",
        "horizon": "weeks",
        "conditions": [],
        "invalidation": [],
        "evidence_refs": ["evidence:fixture"],
        "derivation_type": "direct_statement",
        "registration_state": "canonical",
        "provenance_verification": "verified",
        "semantic_fidelity": "direct",
        "truth_status": "attributed_opinion",
        "applicability_status": "applicable",
        "confidence": "high",
    }


def test_policy_preview_apply_supersedes_old_active_and_preserves_rules(tmp_path: Path) -> None:
    store = _policy_store(tmp_path)
    current = store.current("portfolio-a")
    assert current is not None
    preview = build_policy_update_preview(store, {
        "portfolio_id": "portfolio-a",
        "rules_patch": {
            "account_operating_policy": {
                "irp": "etf_centered_core",
                "isa": "medium_term_core",
                "toss": "tactical_high_volatility_growth",
                "sector_weight_scope": "whole_portfolio",
                "semiconductor_reference_band": {"minimum": 0.4, "center": 0.45, "maximum": 0.5},
            }
        },
        "source_ref": "user-statement:fixture",
        "note": "fixture account operating policy",
    })
    assert preview["status"] == "review_required"
    assert preview["next_policy"]["rules"]["risk_budget"] == current["rules"]["risk_budget"]
    applied = apply_policy_update(store, preview)
    assert applied["status"] == "applied"
    assert applied["read_back"]["matches"] is True
    active = store.current("portfolio-a")
    assert active is not None
    assert active["supersedes"] == current["policy_id"]
    assert active["rules"]["account_operating_policy"]["semiconductor_reference_band"]["center"] == 0.45
    old = next(row for row in store.list_policies() if row["policy_id"] == current["policy_id"])
    assert old["status"] == "superseded"


def test_policy_stale_preview_fails_closed(tmp_path: Path) -> None:
    store = _policy_store(tmp_path)
    first = build_policy_update_preview(store, {
        "portfolio_id": "portfolio-b", "rules_patch": {"x": 1}, "source_ref": "user:first",
    })
    second = build_policy_update_preview(store, {
        "portfolio_id": "portfolio-b", "rules_patch": {"x": 2}, "source_ref": "user:second",
    })
    apply_policy_update(store, second)
    with pytest.raises(AdvisoryPolicyUpdateError, match="policy_preview_stale"):
        apply_policy_update(store, first)


def test_opinion_weighting_update_versions_and_validates_budget(tmp_path: Path) -> None:
    store = OpinionWeightingStore(tmp_path / "opinion-weighting")
    store.initialize(_config(), source_ref="fixture:legacy-opinion-weighting")
    preview = build_weighting_update_preview(store, {
        "changes": {"user_preferences": {"speaker_multipliers": {"Analyst B": 1.15}}},
        "source_ref": "user-statement:fixture",
        "note": "raise Shin weighting",
    })
    result = apply_weighting_update(store, preview)
    assert result["status"] == "applied"
    assert store.read()["user_preferences"]["speaker_multipliers"]["Analyst B"] == 1.15
    assert store.read()["policy_version"].startswith("native-")

    with pytest.raises(OpinionWeightingError, match="opinion_weighting_budget_invalid"):
        build_weighting_update_preview(store, {
            "changes": {"mode_policies": {"short_term": {"base_reserve": 0.9}}},
            "source_ref": "user-statement:invalid",
        })


def test_opinion_consensus_keeps_missing_anchor_weight_in_reserve() -> None:
    config = _config()
    claims = [_claim("claim:analyst-b", "Analyst B", "market risk is checked before increasing exposure")]
    result = build_opinion_consensus(
        config,
        claims,
        question="이번 주 시장 위험과 매수 비중",
        horizon="weeks",
    )
    effective = {row["speaker"]: row["weight"] for row in result["effective_weights"]}
    assert "Analyst B" in effective
    assert "Analyst A" not in effective
    assert any(row["speaker"] == "Analyst A" and row["reason"] == "no_relevant_canonical_claim" for row in result["inactive_anchors"])
    assert result["reserve_weight"] >= 0.5


def test_mcp_policy_and_opinion_updates_preview_apply_readback(tmp_path: Path) -> None:
    from protocol.v1.runtime.reference_composition import build_reference_gateway
    from protocol.v1.transport.mcp_stdio import TradeMindMCPServer
    from protocol.v1.tests.test_mcp_write_surface import _call, _grant, _principal

    advisory = tmp_path / "advisory-state"
    seeded = _policy_store(tmp_path)
    assert seeded.current("portfolio-a") is not None
    opinion = OpinionWeightingStore(advisory / "opinion-weighting")
    opinion.initialize(_config(), source_ref="fixture:legacy-opinion-weighting")

    grant = _grant()
    grant["portfolio_scope"] = ["portfolio-a", "portfolio-b"]
    grant["permissions"].extend(["policy.update", "opinion.update", "opinion.read"])
    gateway = build_reference_gateway(
        runtime_root=Path(__file__).resolve().parents[3],
        manifest=Path(__file__).resolve().parents[3] / "protocol/v1/fixtures/full-reference.instance.json",
        native_store_root=tmp_path / "native-write",
        advisory_state_root=advisory,
        approval_store_root=tmp_path / "approvals",
        write_principal=_principal(),
        write_grant=grant,
    )
    server = TradeMindMCPServer(gateway)

    policy_preview = _call(server, "preview_policy_update", {"update": {
        "portfolio_id": "portfolio-a",
        "rules_patch": {"account_operating_policy": {"irp": "etf_centered_core"}},
        "source_ref": "user-statement:mcp-fixture",
    }}, request_id="policy-preview")
    assert policy_preview["status"] == "review_required"
    policy_apply = _call(server, "apply_policy_update", {
        "preview_id": policy_preview["preview_id"],
        "confirmation": policy_preview["confirmation"],
    }, request_id="policy-apply")
    assert policy_apply["status"] == "applied"
    assert policy_apply["read_back"]["matches"] is True
    current_policy = gateway.get_current_policy("portfolio-a")
    assert current_policy["data"]["policies"][0]["rules"]["account_operating_policy"]["irp"] == "etf_centered_core"

    weight_preview = _call(server, "preview_opinion_weighting_update", {"update": {
        "changes": {"user_preferences": {"speaker_multipliers": {"Analyst B": 1.2}}},
        "source_ref": "user-statement:mcp-fixture",
    }}, request_id="weight-preview")
    assert weight_preview["status"] == "review_required"
    weight_apply = _call(server, "apply_opinion_weighting_update", {
        "preview_id": weight_preview["preview_id"],
        "confirmation": weight_preview["confirmation"],
    }, request_id="weight-apply")
    assert weight_apply["status"] == "applied"
    current_weight = gateway.get_opinion_weighting()
    assert current_weight["data"]["user_preferences"]["speaker_multipliers"]["Analyst B"] == 1.2
