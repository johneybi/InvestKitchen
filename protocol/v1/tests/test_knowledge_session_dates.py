"""Publication-date regression using synthetic notes, never live records."""
import copy
import json

import pytest

from protocol.v1.adapters.common import digest
from protocol.v1.runtime.knowledge_session_dates import review_session_dates
from protocol.v1.runtime.native_knowledge_store import KnowledgeRejected
from protocol.v1.tests.test_mcp_confirmed_writes import setup, signing_key, _call
from protocol.v1.tests.test_native_knowledge_store import _request
from protocol.v1.tests.test_mcp_http import _post


def note(effective="2026-02-09"):
    update = _request()
    update["current_state"] = {}
    for claim in update["claims"]:
        claim["claim_type"] = "trading_material"
        claim["effective_at"] = {"value": effective, "precision": "source_exact" if "T" in effective else "date_only"}
        claim.pop("valid_until", None)
        claim["registration_state"] = "canonical"
        claim["applicability_status"] = "conditional"
        claim["horizon"] = "next Monday session; not machine-readable authority"
    for evidence in update["evidence"]:
        evidence["published_at"] = {"value": "2026-02-07T07:59:00+09:00", "precision": "source_exact"}
    return update


@pytest.mark.parametrize("setup", ["object", "json-string"], indirect=True)
def test_requested_session_is_checked_before_staging_and_does_not_rewrite_source(setup, request):
    client, headers, writes, _, backends = setup
    original = note("2026-02-07")
    def args(update):
        payload = ({"update_json": json.dumps(update), "expected_payload_digest": digest(update)}
                   if request.node.callspec.params["setup"] == "json-string" else {"update": update})
        return {**payload, "target_session_date": "2026-02-09"}
    rejected = _call(client, headers, "preview_knowledge_update", args(original))
    assert rejected.get("isError") and not writes.pending
    assert backends["fixture-reader"].get_knowledge_generation(original["generation_id"])["commit_status"] == "not_observed"
    update = note()
    frozen = copy.deepcopy(update)
    response = _call(client, headers, "preview_knowledge_update", args(update))
    assert not response.get("isError"), response
    preview = response["structuredContent"]
    assert preview["payload_digest"] == digest(frozen)
    assert preview["session_registration"] == {
        "target_session_date": "2026-02-09", "trading_claim_count": len(update["claims"]),
        "claim_effective_dates": ["2026-02-09"], "missing_effective_date_count": 0,
        "source_publication_dates": ["2026-02-07"], "alignment_checked": True,
        "scope": "date_alignment_only_not_market_calendar_or_symbol_validation", "session_published": False}
    assert backends["fixture-reader"].connector_review(preview["preview_id"])["canonical_payload"] == frozen
    # A date check neither commits Knowledge nor approves the staged change.
    applied = _call(client, headers, "apply_knowledge_update", {k: preview[k] for k in ["preview_id", "confirmation"]})
    assert applied["isError"]
    declarations = {t["name"]: t for t in _post(client, "tools/list", headers=headers).json()["result"]["tools"]}
    assert "target_session_date" in declarations["preview_knowledge_update"]["inputSchema"]["properties"]
    assert "target_session_date" not in declarations["preview_portfolio_update"]["inputSchema"]["properties"]


def test_date_check_uses_kst_and_never_infers_from_horizon_or_source_date():
    update = note("2026-02-08T15:00:00Z")
    assert review_session_dates(update, "2026-02-09")["alignment_checked"]
    for claim in update["claims"]:
        claim["effective_at"] = {"value": "2026-02-08T14:59:59Z", "precision": "source_exact"}
    with pytest.raises(KnowledgeRejected, match="date_mismatch"):
        review_session_dates(update, "2026-02-09")
    update["claims"][0].pop("effective_at")
    with pytest.raises(KnowledgeRejected, match="date_mismatch"):
        review_session_dates(update, "2026-02-09")


@pytest.mark.parametrize("target", ["20260209", "2026-02-30", "2026-2-9", "tomorrow", "", 20260209])
def test_invalid_explicit_target_is_rejected(target):
    with pytest.raises(KnowledgeRejected, match="date_invalid"):
        review_session_dates(note(), target)


def test_nontrading_notes_and_old_clients_remain_valid_without_a_publication_claim():
    update = note("2026-02-07")
    original = copy.deepcopy(update)
    result = review_session_dates(update)
    assert not result["alignment_checked"] and result["target_session_date"] is None
    assert update == original
    for claim in update["claims"]:
        claim["claim_type"] = "market_rule"
    assert review_session_dates(update)["trading_claim_count"] == 0
    with pytest.raises(KnowledgeRejected, match="no_trading_claims"):
        review_session_dates(update, "2026-02-09")


def test_mixed_dates_cannot_silently_drop_the_mismatched_material():
    update = note()
    other = copy.deepcopy(update["claims"][0]); other["claim_id"] += ":older"
    other["effective_at"]["value"] = "2026-02-07"
    update["claims"].append(other)
    with pytest.raises(KnowledgeRejected, match="date_mismatch"):
        review_session_dates(update, "2026-02-09")
