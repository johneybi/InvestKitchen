from __future__ import annotations

import json
from pathlib import Path

from protocol.v1.runtime.historical_knowledge_promotion import _is_durable_claim, build_promotion_request
from protocol.v1.runtime.native_knowledge_store import validate_knowledge_request


def test_promotion_request_is_claim_only_and_current_state_neutral() -> None:
    review = {
        "entries": [{
            "document_id": "lecture-a",
            "document_type": "lecture",
            "speaker": "멘토",
            "published_at": "2026-07-15T00:00:00+09:00",
            "source_sha256": "a" * 64,
        }],
        "selected_claims": [{
            "claim_id": "claim-a",
            "claim": "확인 후 분할 진입한다.",
            "claim_type": "durable_rule",
            "conditions": ["확인"],
            "invalidation": ["저점 이탈"],
            "document_id": "lecture-a",
            "effective_at": "2026-07-15T00:00:00+09:00",
            "evidence_type": "paraphrase",
            "speaker": "멘토",
            "stance": "confirmation_first",
            "subject": "매수 원칙",
            "time_horizon": "durable",
            "confidence": "high",
        }],
    }
    request = build_promotion_request(review, generated_at="2026-09-17T14:00:00Z")
    validate_knowledge_request(request)
    assert request["current_state"] == {}
    assert len(request["evidence"]) == 1
    assert len(request["claims"]) == 1
    assert request["claims"][0]["applicability_status"] == "conditional"
    assert request["claims"][0]["truth_status"] == "attributed_opinion"
    assert request["evidence"][0]["freshness"] == "historical"


def test_time_bound_technical_rule_is_not_automatic_durable_promotion() -> None:
    assert _is_durable_claim({"claim_type": "technical_rule", "time_horizon": "days_to_weeks"}) is False
    assert _is_durable_claim({"claim_type": "durable_rule", "time_horizon": "days_to_weeks"}) is True
    assert _is_durable_claim({"claim_type": "mentor_view", "time_horizon": "continuous_discipline"}) is False
