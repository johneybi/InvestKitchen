#!/usr/bin/env python3
"""Private operator consent and low-frequency broker observation refresh.

This command has no checkpoint, Transaction, Decision, order or execution writer.
Only `authorize` can enable a policy, and requires an actual interactive TTY.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import digest
from protocol.v1.runtime.account_state import AccountStateReader
from protocol.v1.runtime.account_observations import (
    ApprovedObservationStore, CurrentAccountService, ObservationConsentError,
    _atomic_json, approve_policy_from_tty,
)
from protocol.v1.runtime.portfolio_account_sync import AccountBindingRegistry
from protocol.v1.runtime.reference_composition import build_account_readonly_subprocess_provider


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("authorize", "disable", "status", "tick"))
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    parser.add_argument("--account-binding-file", type=Path, required=True)
    parser.add_argument("--observation-root", type=Path, required=True)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--portfolio-id")
    parser.add_argument("--account-id")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--cache-ttl-seconds", type=int, default=60)
    parser.add_argument("--toss-account-secret-file", type=Path)
    parser.add_argument("--nhplug-account-secret-file", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        protected = [ROOT.resolve(), args.runtime_root.resolve()]
        for path in (args.observation_root, args.policy_file, args.key_file):
            if any(path.resolve() == root or path.resolve().is_relative_to(root) for root in protected):
                raise ObservationConsentError("Observation data and authorization must stay outside the product repository")
        rows = json.loads(args.account_binding_file.read_text())
        registry = AccountBindingRegistry(rows)
        if args.action == "authorize":
            if not args.portfolio_id or not args.account_id:
                raise ObservationConsentError("One explicit portfolio and account are required")
            if not 300 <= args.interval_seconds <= 86400 or not 30 <= args.cache_ttl_seconds <= 300:
                raise ObservationConsentError("Refresh policy is outside the supported bounds")
            binding = registry.resolve(args.portfolio_id, args.account_id)
            accounts = []
            if args.policy_file.exists():
                previous = ApprovedObservationStore(args.observation_root, args.policy_file, args.key_file)
                accounts = previous.scopes()
            accounts = [row for row in accounts if (row["portfolio_id"], row["account_id"]) != (args.portfolio_id, args.account_id)]
            accounts.append({"portfolio_id": args.portfolio_id, "account_id": args.account_id,
                             "binding_digest": digest(binding.provider_request_value()),
                             "interval_seconds": args.interval_seconds, "cache_ttl_seconds": args.cache_ttl_seconds})
            approve_policy_from_tty(args.observation_root, args.policy_file, args.key_file, accounts)
            print("Automatic broker observations authorized; canonical account writes remain approval-bound.")
            return 0
        store = ApprovedObservationStore(args.observation_root, args.policy_file, args.key_file)
        if args.action == "disable":
            value = store.policy()
            if bool(args.portfolio_id) != bool(args.account_id):
                raise ObservationConsentError("Scoped revocation requires both portfolio and account")
            if args.portfolio_id:
                remaining = [row for row in value["accounts"]
                             if (row["portfolio_id"], row["account_id"]) != (args.portfolio_id, args.account_id)]
                if len(remaining) == len(value["accounts"]):
                    raise ObservationConsentError("Observation consent scope is not configured")
                if remaining:
                    value["accounts"] = remaining
                else:
                    value["enabled"] = False
            else:
                value["enabled"] = False
            _atomic_json(store.policy_file, store._signed(value, "account-observation-policy-v1"))
            print("Automatic broker observations disabled; canonical records retained.")
            return 0
        if args.action == "status":
            print(json.dumps({"enabled": store.policy()["enabled"], "approved_account_count": len(store.scopes())}))
            return 0
        providers = {}
        for name, path in (("toss_securities", args.toss_account_secret_file), ("nhplug", args.nhplug_account_secret_file)):
            if path is not None:
                providers[name] = build_account_readonly_subprocess_provider(args.runtime_root, provider_id=name, secret_file=path)
        service = CurrentAccountService(AccountStateReader(registry, providers,
            portfolio_ids=sorted({row["portfolio_id"] for row in store.scopes()})), store)
        result = service.refresh_due()
        print(json.dumps(result, sort_keys=True))
        return 1 if result["failed"] else 0
    except (OSError, ValueError, RuntimeError):
        print("Account observation operation unavailable; check private policy, binding and service configuration.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
