"""Opt-in durable broker observations, independent of canonical checkpoint writes."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from protocol.v1.adapters.common import canonical_json, digest, timepoint
from protocol.v1.runtime.account_state import AccountStateReader
from protocol.v1.runtime.asset_identity import canonical_asset_identity
from protocol.v1.runtime.portfolio_account_sync import AccountSyncError


class ObservationConsentError(ValueError):
    pass


def _validate_policy(value: dict[str, Any]) -> None:
    if (set(value) != {"version", "policy_id", "enabled", "approved_at", "approved_by", "accounts", "signature"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["enabled"], bool)
            or not isinstance(value["policy_id"], str) or not value["policy_id"].startswith("account-consent:")
            or value["approved_by"] != "trusted_operator_tty" or not isinstance(value["accounts"], list)
            or not 1 <= len(value["accounts"]) <= 100):
        raise ObservationConsentError("Observation policy is invalid")
    try:
        point = value["approved_at"]
        stamp = datetime.fromisoformat(point["value"].replace("Z", "+00:00"))
        if point["precision"] != "source_exact" or stamp.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ObservationConsentError("Observation consent time is invalid") from None
    seen = set()
    for row in value["accounts"]:
        if (not isinstance(row, dict) or set(row) != {"portfolio_id", "account_id", "binding_digest", "interval_seconds", "cache_ttl_seconds"}
                or not all(isinstance(row[k], str) and row[k].strip() for k in ("portfolio_id", "account_id", "binding_digest"))
                or re.fullmatch(r"[0-9a-f]{64}", row["binding_digest"]) is None
                or type(row["interval_seconds"]) is not int or not 300 <= row["interval_seconds"] <= 86400
                or type(row["cache_ttl_seconds"]) is not int or not 30 <= row["cache_ttl_seconds"] <= 300):
            raise ObservationConsentError("Observation account policy is invalid")
        scope = (row["portfolio_id"], row["account_id"])
        if scope in seen:
            raise ObservationConsentError("Observation scope is duplicated")
        seen.add(scope)


def _private_bytes(path: Path, *, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in {0, os.geteuid()}:
            raise ObservationConsentError("Private observation configuration permissions are invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise ObservationConsentError("Observation configuration is too large")
        return value
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise ObservationConsentError("Observation symlink rejected")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ApprovedObservationStore:
    """One signed latest snapshot per approved scope, never a transaction journal.

    A trusted operator approves this read-model policy once. The authenticated
    policy is rechecked for every refresh/cache write; remote tools cannot sign
    or enable it. Cache and policy signatures use different domains.
    """

    def __init__(self, root: Path, policy_file: Path, key_file: Path):
        if root.is_symlink() or policy_file.is_symlink() or key_file.is_symlink():
            raise ObservationConsentError("Observation symlink rejected")
        self.root, self.policy_file, self.key_file = root.resolve(), policy_file.resolve(), key_file.resolve()
        if self.key_file.is_relative_to(self.root) or self.policy_file.is_relative_to(self.root):
            raise ObservationConsentError("Consent and signing key must be outside observation data")
        self.policy()

    def _key(self) -> bytes:
        value = _private_bytes(self.key_file, maximum=128)
        if len(value) < 32:
            raise ObservationConsentError("Observation signing key is invalid")
        return value

    def _signed(self, value: dict[str, Any], domain: str) -> dict[str, Any]:
        body = copy.deepcopy(value)
        body.pop("signature", None)
        signature = hmac.new(self._key(), (domain + "\n" + canonical_json(body)).encode(), hashlib.sha256).hexdigest()
        return {**body, "signature": signature}

    def _verified(self, value: Any, domain: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("signature"), str):
            raise ObservationConsentError("Observation signature is missing")
        if not hmac.compare_digest(self._signed(value, domain)["signature"], value["signature"]):
            raise ObservationConsentError("Observation signature is invalid")
        return value

    def policy(self) -> dict[str, Any]:
        value = self._verified(json.loads(_private_bytes(self.policy_file, maximum=65536)), "account-observation-policy-v1")
        _validate_policy(value)
        return value

    def authorize(self, binding) -> dict[str, Any] | None:
        policy = self.policy()
        if not policy["enabled"]:
            return None
        for row in policy["accounts"]:
            if (row["portfolio_id"], row["account_id"]) == (binding.portfolio_id, binding.account_id):
                if row["binding_digest"] != digest(binding.provider_request_value()):
                    raise ObservationConsentError("Approved provider binding changed")
                return row
        return None

    def scopes(self, portfolio_id: str | None = None) -> list[dict[str, Any]]:
        policy = self.policy()
        return [copy.deepcopy(row) for row in policy["accounts"]
                if policy["enabled"] and (portfolio_id is None or row["portfolio_id"] == portfolio_id)]

    def _path(self, binding) -> Path:
        return self.root / (digest([binding.portfolio_id, binding.account_id]) + ".json")

    @contextmanager
    def locked(self, binding):
        if self.authorize(binding) is None:
            yield
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ObservationConsentError("Observation directory must be private")
        lock = self._path(binding).with_suffix(".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load(self, binding) -> dict[str, Any] | None:
        if self.authorize(binding) is None:
            return None
        try:
            row = self._verified(json.loads(_private_bytes(self._path(binding), maximum=4_000_000)), "account-observation-cache-v1")
        except FileNotFoundError:
            return None
        if (row.get("policy_id") != self.policy()["policy_id"]
                or row.get("binding_digest") != digest(binding.provider_request_value())):
            return None
        result = row.get("result", {})
        data = result.get("data", {})
        if (data.get("portfolio_id"), data.get("account_id")) != (binding.portfolio_id, binding.account_id):
            raise ObservationConsentError("Stored observation scope changed")
        return row

    def save(self, binding, result: dict[str, Any], now: datetime) -> None:
        if self.authorize(binding) is None:
            raise ObservationConsentError("Automatic observation consent is not active")
        data = result.get("data", {})
        if (result.get("capability") != "account.state" or result.get("status") not in {"ok", "partial"}
                or data.get("completeness", {}).get("holdings") != "complete"
                or (data.get("portfolio_id"), data.get("account_id")) != (binding.portfolio_id, binding.account_id)):
            raise ObservationConsentError("Only validated complete holding observations may be stored")
        row = {"policy_id": self.policy()["policy_id"], "binding_digest": digest(binding.provider_request_value()),
               "stored_at": timepoint(now.isoformat()), "result": copy.deepcopy(result)}
        _atomic_json(self._path(binding), self._signed(row, "account-observation-cache-v1"))

    def note_failure(self, binding, row: dict[str, Any], now: datetime, *, invalid: bool) -> None:
        if self.authorize(binding) is None:
            raise ObservationConsentError("Automatic observation consent is not active")
        value = copy.deepcopy(row)
        value.update(retry_not_before=timepoint((now + timedelta(seconds=30)).isoformat()), invalid_refresh=invalid)
        # Keep stored_at and all observation times unchanged after a failed read.
        _atomic_json(self._path(binding), self._signed(value, "account-observation-cache-v1"))


def approve_policy_from_tty(root: Path, policy_file: Path, key_file: Path,
                            accounts: list[dict[str, Any]], *, enabled: bool = True) -> ApprovedObservationStore:
    """Trusted local entry point. No MCP tool or caller-supplied attestation can call it."""
    import sys
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ObservationConsentError("Interactive operator consent is required")
    value = {"version": 1, "policy_id": "account-consent:" + uuid.uuid4().hex, "enabled": enabled,
             "approved_at": timepoint(), "approved_by": "trusted_operator_tty", "accounts": copy.deepcopy(accounts), "signature": "pending"}
    _validate_policy(value)
    print("Automatic broker observations: update the latest snapshot and current Portfolio view only.")
    print("No order, Transaction, Decision, Policy or canonical checkpoint is created.")
    print(json.dumps(accounts, ensure_ascii=False, indent=2))
    if input("Type ENABLE ACCOUNT OBSERVATIONS to consent: ").strip() != "ENABLE ACCOUNT OBSERVATIONS":
        raise ObservationConsentError("Automatic observations were not approved")
    if root.is_symlink() or policy_file.is_symlink() or key_file.is_symlink():
        raise ObservationConsentError("Observation symlink rejected")
    if key_file.resolve().is_relative_to(root.resolve()) or policy_file.resolve().is_relative_to(root.resolve()):
        raise ObservationConsentError("Consent and signing key must be outside observation data")
    if not key_file.exists():
        key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.urandom(32))
    key = _private_bytes(key_file, maximum=128)
    if len(key) < 32:
        raise ObservationConsentError("Observation signing key is invalid")
    value.pop("signature")
    signature = hmac.new(key, ("account-observation-policy-v1\n" + canonical_json(value)).encode(), hashlib.sha256).hexdigest()
    _atomic_json(policy_file, {**value, "signature": signature})
    return ApprovedObservationStore(root, policy_file, key_file)


class CurrentAccountService:
    def __init__(self, reader: AccountStateReader, store: ApprovedObservationStore):
        self.reader, self.store = reader, store

    @staticmethod
    def _age(point, now):
        return (now - datetime.fromisoformat(point["value"].replace("Z", "+00:00"))).total_seconds()

    def _failed_result(self, cached, now, *, invalid=False):
        age = self._age(cached["data"]["snapshot_effective_at"], now)
        if invalid or not 0 <= age <= self.reader.max_stale_seconds:
            return self.reader._result(None, status="unavailable", mode="unknown", now=now,
                code="account_read_observation_invalid" if invalid else "account_read_refresh_failed")
        value = copy.deepcopy(cached)
        value.update(status="stale", freshness="stale", source_mode="cache", generated_at=timepoint(now.isoformat()))
        value["warnings"] = ["Broker refresh failed; the stored observation retains its original source time."]
        value["gaps"] = [{"gap_code": "account_read_refresh_failed", "reason": "Broker refresh is unavailable.",
                          "impact": "These holdings are stale.", "recoverable": True}]
        return value

    def read(self, portfolio_id: str, account_id: str, *, refresh: bool = False):
        if portfolio_id not in self.reader.portfolio_ids or not isinstance(refresh, bool):
            return self.reader.read(portfolio_id, account_id, refresh=refresh)
        try:
            binding = self.reader.registry.resolve(portfolio_id, account_id)
        except AccountSyncError:
            return self.reader.read(portfolio_id, account_id, refresh=refresh)
        now = self.reader.clock().astimezone(timezone.utc)
        try:
            policy = self.store.authorize(binding)
            if policy is None:
                return self.reader.read(portfolio_id, account_id, refresh=refresh)
            with self.store.locked(binding):
                row = self.store.load(binding)
                cached = row["result"] if row else None
                age = self._age(cached["data"]["snapshot_effective_at"], now) if cached else None
                stored_age = self._age(row["stored_at"], now) if row else None
                if (not refresh and row and row.get("retry_not_before")
                        and -30 <= self._age(row["retry_not_before"], now) < 0):
                    return self._failed_result(cached, now, invalid=row.get("invalid_refresh", False))
                if (not refresh and cached and 0 <= stored_age < policy["cache_ttl_seconds"]
                        and 0 <= age <= self.reader.max_age_seconds):
                    value = copy.deepcopy(cached)
                    value.update(source_mode="cache", freshness="cached", generated_at=timepoint(now.isoformat()))
                    return value
                value = self.reader.read(portfolio_id, account_id, refresh=True)
                if value["status"] in {"ok", "partial"}:
                    if value["data"]["completeness"]["holdings"] == "complete":
                        self.store.save(binding, value, self.reader.clock().astimezone(timezone.utc))
                    return value
                invalid = any(g.get("gap_code") == "account_read_observation_invalid" for g in value.get("gaps", []))
                if cached:
                    now = self.reader.clock().astimezone(timezone.utc)
                    self.store.note_failure(binding, row, now, invalid=invalid)
                    return self._failed_result(cached, now, invalid=invalid)
                return value
        except (ObservationConsentError, OSError, ValueError):
            return self.reader._result(None, status="blocked", mode="unknown", now=now, code="account_observation_consent_or_store_invalid")

    def refresh_due(self) -> dict[str, int]:
        counts = {"refreshed": 0, "skipped": 0, "failed": 0}
        for row in self.store.scopes():
            try:
                binding = self.reader.registry.resolve(row["portfolio_id"], row["account_id"])
                saved = self.store.load(binding)
                now = self.reader.clock().astimezone(timezone.utc)
                if saved and 0 <= self._age(saved["stored_at"], now) < row["interval_seconds"]:
                    counts["skipped"] += 1
                    continue
                value = self.read(row["portfolio_id"], row["account_id"])
                complete = (value.get("data") or {}).get("completeness", {}).get("holdings") == "complete"
                counts["refreshed" if value["status"] in {"ok", "partial"} and complete else "failed"] += 1
            except (ObservationConsentError, OSError, ValueError, AccountSyncError):
                counts["failed"] += 1
        return counts


def _mark_partial(result: dict[str, Any]) -> None:
    if result.get("status") not in {"stale", "blocked", "unavailable"}:
        result["status"] = "partial"


def overlay_current_accounts(base: dict[str, Any], service: CurrentAccountService, portfolio_id: str):
    """Compose a current read model; the original checkpoint remains untouched."""
    if not isinstance(base.get("data"), dict) or not isinstance(base["data"].get("positions"), list):
        return base
    result = copy.deepcopy(base)
    data = result["data"]
    overlays = []
    try:
        scopes = service.store.scopes(portfolio_id)
    except (ObservationConsentError, OSError, ValueError):
        scopes = []
        _mark_partial(result)
        result.setdefault("gaps", []).append({"gap_code": "account_observation_consent_or_store_invalid",
            "reason": "Automatic observation consent is unavailable.", "impact": "Saved checkpoint facts are not a fresh balance.", "recoverable": True})
        result.setdefault("warnings", []).append("Automatic account observation consent is unavailable; saved checkpoint facts are retained.")
    for scope in scopes:
        account_id = scope["account_id"]
        if not any(a.get("account_id") == account_id for a in data.get("accounts", [])):
            continue
        live = service.read(portfolio_id, account_id)
        observed = live.get("data")
        if observed is None or observed["completeness"]["holdings"] != "complete":
            _mark_partial(result)
            result.setdefault("gaps", []).extend(live.get("gaps", []))
            result.setdefault("warnings", []).append("A current account observation is unavailable; retained facts are not a fresh balance.")
            continue
        try:
            binding = service.reader.registry.resolve(portfolio_id, account_id)
            if service.store.authorize(binding) is None:
                continue
        except (ObservationConsentError, OSError, ValueError):
            _mark_partial(result)
            result.setdefault("warnings", []).append("Automatic account observation consent changed; saved facts are retained.")
            continue
        current = [p for p in data["positions"] if p.get("account_id") == account_id]
        positions = []
        for holding in observed["holdings"]:
            asset = holding["asset"]
            matches = [p for p in current if
                "broker-asset:" + digest(canonical_asset_identity(p.get("asset")))[:24] == asset.get("asset_id")]
            existing = matches[0] if len(matches) == 1 else None
            positions.append({
                "position_id": existing["position_id"] if existing else "observed-position:" + digest([portfolio_id, account_id, asset["asset_id"]])[:24],
                "portfolio_id": portfolio_id, "account_id": account_id,
                "asset": copy.deepcopy(existing["asset"] if existing else asset), "quantity": holding["quantity"],
                "quantity_status": "confirmed", "quantity_basis": "direct_observation", "authority": "portfolio_fact",
                "source_evidence": holding["source_evidence"], "observed_at": observed["snapshot_effective_at"],
                "state": "closed" if holding["quantity"] == 0 else "open",
                **{field: copy.deepcopy(holding[field]) for field in ("cost_basis_values", "avg_cost_values", "valuation") if field in holding},
            })
        data["positions"] = [p for p in data["positions"] if p.get("account_id") != account_id] + positions
        cash_complete = observed["completeness"]["cash"] == "complete"
        for cash in observed["cash"]:
            key = (cash["currency"], cash["cash_kind"])
            matches = [c for c in data["cash"] if c.get("account_id") == account_id and (c.get("currency"), c.get("cash_kind")) == key]
            old = matches[0] if len(matches) == 1 else None
            data["cash"] = [c for c in data["cash"] if not (c.get("account_id") == account_id and (c.get("currency"), c.get("cash_kind")) == key)]
            data["cash"].append({
                "cash_id": old["cash_id"] if old else "observed-cash:" + digest([portfolio_id, account_id, key])[:24],
                "portfolio_id": portfolio_id, "account_id": account_id, "currency": cash["currency"],
                "cash_kind": cash["cash_kind"], "provider_label": cash.get("provider_label"),
                "value": {"amount": cash["amount"], "currency": cash["currency"], "value_basis": "observed", "as_of": observed["snapshot_effective_at"]},
                "authority": "portfolio_fact", "source_evidence": cash["source_evidence"], "observed_at": observed["snapshot_effective_at"],
            })
        if cash_complete:
            keys = {(c["currency"], c["cash_kind"]) for c in observed["cash"]}
            data["cash"] = [c for c in data["cash"] if c.get("account_id") != account_id or (c.get("currency"), c.get("cash_kind")) in keys]
        for row in data.get("completeness", {}).get("accounts", []):
            if row["account_id"] == account_id:
                row.update(holdings="stale" if live["status"] == "stale" else "complete",
                           cash="stale" if live["status"] == "stale" else "complete" if cash_complete else "partial",
                           observed_at=observed["snapshot_effective_at"])
        overlays.append({"account_id": account_id, "observed_at": observed["snapshot_effective_at"],
                         "freshness": live["freshness"], "canonical_checkpoint_changed": False,
                         "observation_ref": observed["observation_ref"]})
        result.setdefault("provenance", []).append({"source_type": "broker_observation", "source_id": observed["observation_ref"]})
        if live["status"] != "ok":
            if live["status"] == "stale":
                result["status"] = "stale"
            else:
                _mark_partial(result)
            result.setdefault("gaps", []).extend(live.get("gaps", []))
            result.setdefault("warnings", []).extend(live.get("warnings", []))
            if live["status"] == "stale":
                result["freshness"] = "stale"
    if overlays:
        data["account_observation_overlays"] = overlays
        data["generated_at"] = copy.deepcopy(result.get("generated_at") or timepoint())
        for field in ("holdings", "cash"):
            states = [row[field] for row in data["completeness"].get("accounts", [])]
            data["completeness"][field] = "complete" if states and all(s == "complete" for s in states) else "stale" if "stale" in states else "partial"
    return result
