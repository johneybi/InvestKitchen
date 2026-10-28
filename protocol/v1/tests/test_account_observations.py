from __future__ import annotations

import copy
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from protocol.v1.adapters.common import digest, timepoint
from protocol.v1.adapters.materialized_portfolio import get_materialized_portfolio_state
from protocol.v1.runtime.account_observations import (
    ApprovedObservationStore, CurrentAccountService, ObservationConsentError,
    _atomic_json, approve_policy_from_tty, overlay_current_accounts,
)
from protocol.v1.tests.test_account_state import _fresh, _reader
from protocol.v1.tests.test_gateway_facade import _validate
from protocol.v1.tests.test_portfolio_update_service import _stores


def _approved(tmp_path, reader):
    policy, key, root = tmp_path / "consent.json", tmp_path / "private.key", tmp_path / "observations"
    key.write_bytes(os.urandom(32)); key.chmod(0o600)
    # Synthetic trusted-operator policy; tests do not claim an actual human consent.
    signer = object.__new__(ApprovedObservationStore)
    signer.key_file = key
    binding = reader.registry.resolve("portfolio-alpha", "account-alpha")
    value = {"version": 1, "policy_id": "account-consent:synthetic", "enabled": True,
             "approved_at": timepoint(), "approved_by": "trusted_operator_tty", "accounts": [{
                 "portfolio_id": "portfolio-alpha", "account_id": "account-alpha",
                 "binding_digest": digest(binding.provider_request_value()), "interval_seconds": 300, "cache_ttl_seconds": 60,
             }]}
    _atomic_json(policy, signer._signed(value, "account-observation-policy-v1"))
    return ApprovedObservationStore(root, policy, key)


def test_automatic_latest_snapshot_persists_and_fresh_reads_share_it_without_checkpoint_changes(tmp_path):
    reader, clock, calls = _reader()
    store = _approved(tmp_path, reader)
    service = CurrentAccountService(reader, store)
    native, checkpoints = _stores(tmp_path / "canonical")
    before = copy.deepcopy(checkpoints.read_all())
    first = service.read("portfolio-alpha", "account-alpha")
    assert first["status"] == "ok" and len(list(store.root.glob("*.json"))) == 1
    another_reader, _, another_calls = _reader()
    another_reader.clock = clock.now
    another = CurrentAccountService(another_reader, ApprovedObservationStore(store.root, store.policy_file, store.key_file))
    clock.seconds = 59
    assert another.read("portfolio-alpha", "account-alpha")["source_mode"] == "cache"
    assert another_calls == []
    clock.seconds = 61
    assert service.read("portfolio-alpha", "account-alpha")["source_mode"] == "live_fetch"
    assert len(calls) == 2 and len(list(store.root.glob("*.json"))) == 1
    assert checkpoints.read_all() == before


def test_five_minute_worker_checks_skip_recent_user_refreshes(tmp_path):
    reader, clock, calls = _reader()
    service = CurrentAccountService(reader, _approved(tmp_path, reader))
    assert service.refresh_due() == {"refreshed": 1, "skipped": 0, "failed": 0}
    clock.seconds = 240
    assert service.refresh_due() == {"refreshed": 0, "skipped": 1, "failed": 0}
    service.read("portfolio-alpha", "account-alpha")
    clock.seconds = 300
    assert service.refresh_due()["skipped"] == 1
    clock.seconds = 540
    assert service.refresh_due()["refreshed"] == 1
    assert len(calls) == 3


def test_approved_observations_update_current_portfolio_view_and_preserve_canonical_records(tmp_path):
    def provider(clock, binding):
        value = _fresh(clock, binding)
        value["holdings"][0]["quantity"] = 12
        value["cash"][0].update(cash_kind="provider_specific", amount=8000, provider_label="Synthetic cash buying power")
        value["completeness"]["cash"] = "partial"
        return value
    reader, _, _ = _reader(provider)
    service = CurrentAccountService(reader, _approved(tmp_path, reader))
    native, checkpoints = _stores(tmp_path / "canonical")
    before = copy.deepcopy(checkpoints.read_all())
    base = get_materialized_portfolio_state(checkpoints, native, "portfolio-alpha")
    base_before = copy.deepcopy(base)
    current = overlay_current_accounts(base, service, "portfolio-alpha")
    _validate(current, "capability-result.schema.json")
    _validate(current["data"], "portfolio.schema.json")
    positions = {p["asset"]["symbol"]: p for p in current["data"]["positions"]}
    assert positions["005930"]["quantity"] == 12
    assert positions["005930"]["position_id"] == "position:005930"
    assert positions["AAPL"]["quantity"] == 0.25
    assert positions["AAPL"]["avg_cost_values"][0]["amount"] == 80.12
    cash = {(p["cash_kind"], p["value"]["amount"]) for p in current["data"]["cash"]}
    assert ("nominal_balance", 1000) in cash and ("provider_specific", 8000) in cash
    assert current["data"]["transactions"] == base["data"]["transactions"]
    assert current["data"]["account_observation_overlays"][0]["canonical_checkpoint_changed"] is False
    assert checkpoints.read_all() == before and base == base_before


def test_partial_holdings_and_provider_failure_never_clear_missing_positions(tmp_path):
    fail = [False]
    def provider(clock, binding):
        if fail[0]: raise RuntimeError("synthetic provider outage")
        value = _fresh(clock, binding)
        return value
    reader, clock, _ = _reader(provider)
    service = CurrentAccountService(reader, _approved(tmp_path, reader))
    native, checkpoints = _stores(tmp_path / "canonical")
    base = get_materialized_portfolio_state(checkpoints, native, "portfolio-alpha")
    service.read("portfolio-alpha", "account-alpha")
    fail[0] = True
    clock.seconds = 301
    old = overlay_current_accounts(base, service, "portfolio-alpha")
    assert old["status"] == old["freshness"] == "stale"
    assert old["data"]["account_observation_overlays"][0]["observed_at"]["value"] == "2026-09-17T01:02:00+00:00"
    clock.seconds = 901
    unavailable = overlay_current_accounts(base, service, "portfolio-alpha")
    assert unavailable["status"] == "partial"
    assert unavailable["data"]["positions"] == base["data"]["positions"]


@pytest.mark.parametrize("target", ["policy", "cache", "key_permissions", "binding"])
def test_missing_or_tampered_prior_consent_fails_closed(tmp_path, target):
    reader, clock, calls = _reader()
    store = _approved(tmp_path, reader)
    service = CurrentAccountService(reader, store)
    service.read("portfolio-alpha", "account-alpha")
    binding = reader.registry.resolve("portfolio-alpha", "account-alpha")
    if target == "policy":
        value = json.loads(store.policy_file.read_text()); value["accounts"][0]["portfolio_id"] = "other"
        store.policy_file.write_text(json.dumps(value))
    elif target == "cache":
        path = store._path(binding); value = json.loads(path.read_text()); value["result"]["data"]["holdings"][0]["quantity"] = 999
        path.write_text(json.dumps(value))
    elif target == "key_permissions": store.key_file.chmod(0o644)
    else:
        from protocol.v1.runtime.portfolio_account_sync import AccountBindingRegistry
        rows = [{**binding.provider_request_value(), "provider_account_ref": "provider-account-ref:rebound"}]
        reader.registry = AccountBindingRegistry(rows)
    result = service.read("portfolio-alpha", "account-alpha")
    assert result["status"] == "blocked" and result["data"] is None
    assert len(calls) == 1


def test_revocation_stops_automatic_reflection_without_disabling_ordinary_reads(tmp_path):
    reader, _, calls = _reader()
    store = _approved(tmp_path, reader)
    service = CurrentAccountService(reader, store)
    service.read("portfolio-alpha", "account-alpha")
    previous = store._path(reader.registry.resolve("portfolio-alpha", "account-alpha")).read_bytes()
    value = store.policy(); value["enabled"] = False
    _atomic_json(store.policy_file, store._signed(value, "account-observation-policy-v1"))
    native, checkpoints = _stores(tmp_path / "canonical")
    base = get_materialized_portfolio_state(checkpoints, native, "portfolio-alpha")
    assert overlay_current_accounts(base, service, "portfolio-alpha") == base
    assert service.refresh_due() == {"refreshed": 0, "skipped": 0, "failed": 0}
    assert service.read("portfolio-alpha", "account-alpha", refresh=True)["status"] == "ok"
    assert store._path(reader.registry.resolve("portfolio-alpha", "account-alpha")).read_bytes() == previous


def test_noninteractive_process_cannot_mint_standing_consent(tmp_path):
    with pytest.raises(ObservationConsentError, match="Interactive operator consent"):
        approve_policy_from_tty(tmp_path / "cache", tmp_path / "policy.json", tmp_path / "secret.key", [])
    assert list(tmp_path.iterdir()) == []


def test_interactive_operator_consent_creates_private_verified_policy(tmp_path, monkeypatch):
    import sys
    class OperatorTTY(io.StringIO):
        def isatty(self):
            return True
    reader, _, _ = _reader()
    existing = _approved(tmp_path, reader)
    accounts = existing.policy()["accounts"]
    private = tmp_path / "new-consent"
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", OperatorTTY("ENABLE ACCOUNT OBSERVATIONS\n"))
        patch.setattr(sys, "stdout", OperatorTTY())
        store = approve_policy_from_tty(tmp_path / "new-cache", private / "policy.json", private / "key", accounts)
    assert store.authorize(reader.registry.resolve("portfolio-alpha", "account-alpha")) is not None
    assert store.policy()["approved_by"] == "trusted_operator_tty"
    assert store.key_file.stat().st_mode & 0o077 == 0
    assert store.policy_file.stat().st_mode & 0o077 == 0


def test_shared_disk_cache_coalesces_independent_reader_requests(tmp_path):
    count = []
    def provider(clock, binding):
        count.append(1); time.sleep(0.03)
        return _fresh(clock, binding)
    reader, _, _ = _reader(provider)
    store = _approved(tmp_path, reader)
    other_reader, _, _ = _reader(provider)
    services = [CurrentAccountService(reader, store), CurrentAccountService(other_reader, store)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: s.read("portfolio-alpha", "account-alpha"), services))
    assert all(r["status"] == "ok" for r in results) and len(count) == 1


def test_failed_refresh_backoff_is_shared_without_changing_observation_times(tmp_path):
    fail = [False]
    def provider(clock, binding):
        if fail[0]:
            raise RuntimeError("synthetic outage")
        return _fresh(clock, binding)
    reader, clock, calls = _reader(provider)
    store = _approved(tmp_path, reader)
    service = CurrentAccountService(reader, store)
    service.read("portfolio-alpha", "account-alpha")
    binding = reader.registry.resolve("portfolio-alpha", "account-alpha")
    original = store.load(binding)
    fail[0] = True
    clock.seconds = 61
    assert service.read("portfolio-alpha", "account-alpha")["status"] == "stale"
    another_reader, _, other_calls = _reader(provider)
    another_reader.clock = clock.now
    another = CurrentAccountService(another_reader, store)
    clock.seconds = 62
    assert another.read("portfolio-alpha", "account-alpha")["status"] == "stale"
    assert len(calls) == 2 and other_calls == []
    saved = store.load(binding)
    assert saved["stored_at"] == original["stored_at"]
    assert saved["result"]["data"]["snapshot_effective_at"] == original["result"]["data"]["snapshot_effective_at"]
    fail[0] = False
    clock.seconds = 92
    assert another.read("portfolio-alpha", "account-alpha")["status"] == "ok"
    assert len(other_calls) == 1 and "retry_not_before" not in store.load(binding)


def test_incomplete_holdings_do_not_remove_saved_positions_or_write_latest_cache(tmp_path):
    def provider(clock, binding):
        value = _fresh(clock, binding)
        value["holdings"] = value["holdings"][:1]
        value["completeness"]["holdings"] = "partial"
        return value
    reader, _, _ = _reader(provider)
    store = _approved(tmp_path, reader)
    service = CurrentAccountService(reader, store)
    native, checkpoints = _stores(tmp_path / "canonical")
    base = get_materialized_portfolio_state(checkpoints, native, "portfolio-alpha")
    current = overlay_current_accounts(base, service, "portfolio-alpha")
    assert current["status"] == "partial" and current["data"]["positions"] == base["data"]["positions"]
    assert list(store.root.glob("*.json")) == []


def test_market_identity_does_not_match_another_venue_with_same_symbol(tmp_path):
    reader, _, _ = _reader()
    service = CurrentAccountService(reader, _approved(tmp_path, reader))
    native, checkpoints = _stores(tmp_path / "canonical")
    base = get_materialized_portfolio_state(checkpoints, native, "portfolio-alpha")
    base["data"]["positions"][0]["asset"]["venue"] = "OTHER_SYNTHETIC_VENUE"
    current = overlay_current_accounts(base, service, "portfolio-alpha")
    observed = next(p for p in current["data"]["positions"] if p["asset"]["symbol"] == "005930")
    assert observed["position_id"] != base["data"]["positions"][0]["position_id"]
    assert observed["asset"]["venue"] != "OTHER_SYNTHETIC_VENUE"


def test_operator_cli_can_revoke_scope_without_enabling_a_policy(tmp_path):
    from protocol.v1.deployment.account_observation_cli import main
    reader, _, _ = _reader()
    store = _approved(tmp_path, reader)
    binding = reader.registry.resolve("portfolio-alpha", "account-alpha")
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps([binding.provider_request_value()]))
    args = ["--account-binding-file", str(bindings), "--observation-root", str(store.root),
            "--policy-file", str(store.policy_file), "--key-file", str(store.key_file)]
    previous_umask = os.umask(0o077)
    try:
        assert main(["disable", *args, "--portfolio-id", "portfolio-alpha", "--account-id", "account-alpha"]) == 0
        assert store.scopes() == [] and store.policy()["enabled"] is False
        assert main(["authorize", *args, "--portfolio-id", "portfolio-alpha", "--account-id", "account-alpha"]) == 2
        assert store.policy()["enabled"] is False
    finally:
        os.umask(previous_umask)
