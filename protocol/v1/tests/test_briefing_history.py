import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from protocol.v1.runtime.briefing_history import apply_briefing, latest_briefing, preview_briefing
from protocol.v1.security.trusted_approval import TrustedApprovalStore


NOW = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)
TP = {"value": "2026-11-06T00:00:00Z", "precision": "source_exact"}
PRINCIPAL = {"subject_user_id": "fixture", "client_id": "test", "credential_binding_id": "fixture", "expires_at": TP}
GRANT = {**PRINCIPAL, "grant_id": "fixture", "permissions": ["briefing.record", "briefing.read", "operation.approve"],
         "portfolio_scope": ["fixture-a"]}


def issue(store, preview):
    return store.issue(preview, principal=PRINCIPAL, grant=GRANT, interaction_ref="fixture",
                       approval_method="fixture", now=NOW)


def test_trusted_approval_idempotency_and_scoped_read(tmp_path):
    store = TrustedApprovalStore(tmp_path / "approvals")
    root = tmp_path / "history"
    preview = preview_briefing("fixture-a", "session-1", {"summary": "synthetic"}, now=NOW)
    with pytest.raises(PermissionError):
        apply_briefing(root, preview, {}, principal=PRINCIPAL, grant=GRANT, approval_store=store, now=NOW)
    assert not root.exists()
    approval = issue(store, preview)
    assert apply_briefing(root, preview, approval, principal=PRINCIPAL, grant=GRANT, approval_store=store, now=NOW)["status"] == "recorded"
    assert apply_briefing(root, preview, approval, principal=PRINCIPAL, grant=GRANT, approval_store=store, now=NOW)["status"] == "already_recorded"
    latest = latest_briefing(root, "fixture-a", principal=PRINCIPAL, grant=GRANT, now=NOW)
    assert latest["record"]["result"] == {"summary": "synthetic"}
    assert latest["record"]["authority"] == "advice_record_only"
    with pytest.raises(PermissionError):
        latest_briefing(root, "other", principal=PRINCIPAL, grant=GRANT, now=NOW)
    changed = preview_briefing("fixture-a", "session-1", {"summary": "different"}, now=NOW)
    with pytest.raises(ValueError, match="conflict"):
        apply_briefing(root, changed, issue(store, changed), principal=PRINCIPAL, grant=GRANT, approval_store=store, now=NOW)


def test_tampering_and_missing_grant_fail_closed(tmp_path):
    store = TrustedApprovalStore(tmp_path / "approvals")
    preview = preview_briefing("fixture-a", "session-1", {"summary": "synthetic"}, now=NOW)
    approval = issue(store, preview)
    bad = copy.deepcopy(preview)
    bad["preview"]["canonical_payload"]["result"]["summary"] = "tampered"
    with pytest.raises(PermissionError):
        apply_briefing(tmp_path / "history", bad, approval, principal=PRINCIPAL, grant=GRANT, approval_store=store, now=NOW)
    with pytest.raises(PermissionError):
        apply_briefing(tmp_path / "history", preview, approval, principal=PRINCIPAL,
                       grant={**GRANT, "permissions": []}, approval_store=store, now=NOW)


def test_concurrent_retries_record_once(tmp_path):
    store = TrustedApprovalStore(tmp_path / "approvals")
    preview = preview_briefing("fixture-a", "repeat", {"summary": "synthetic"}, now=NOW)
    approval = issue(store, preview)
    def record(_):
        return apply_briefing(tmp_path / "history", preview, approval, principal=PRINCIPAL,
                              grant=GRANT, approval_store=store, now=NOW)["status"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sorted(executor.map(record, range(4))) == ["already_recorded"] * 3 + ["recorded"]
