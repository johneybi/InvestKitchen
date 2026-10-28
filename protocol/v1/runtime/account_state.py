"""Current broker observations: read permission, automatic freshness, no approvals."""
from __future__ import annotations

import copy
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from protocol.v1.adapters.common import digest, result_envelope, timepoint
from protocol.v1.runtime.asset_identity import canonical_asset_identity
from protocol.v1.runtime.holding_observations import HOLDING_VALUE_FIELDS, VALUATION_NUMBERS
from protocol.v1.runtime.portfolio_account_sync import (
    AccountBindingRegistry, AccountSnapshotReader, AccountSyncError, validate_account_snapshot,
)


class AccountStateReader:
    """An in-memory, account-scoped observation cache; never a Portfolio writer.

    Cache expiry causes a new provider read, not an approval deadline. Scope is
    checked on every call, including cache hits. Failures cannot refresh a cache
    timestamp, and only an explicitly stale prior observation may be returned.
    """

    def __init__(
        self, registry: AccountBindingRegistry, providers: Mapping[str, AccountSnapshotReader],
        *, portfolio_ids: list[str], cache_ttl_seconds: int = 30,
        max_age_seconds: int = 300, max_stale_seconds: int = 900,
        clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int)
                or not 0 <= cache_ttl_seconds <= 300
                or type(max_age_seconds) is not int or max_age_seconds < 0
                or type(max_stale_seconds) is not int or max_stale_seconds < max_age_seconds):
            raise ValueError("invalid account read freshness policy")
        self.registry, self.providers = registry, dict(providers)
        self.portfolio_ids = frozenset(portfolio_ids)
        self.cache_ttl_seconds, self.max_age_seconds = cache_ttl_seconds, max_age_seconds
        self.max_stale_seconds = max_stale_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self._cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}

    @staticmethod
    def _age(snapshot: dict[str, Any], now: datetime) -> float:
        stamp = snapshot["snapshot_effective_at"]["value"]
        return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()

    @staticmethod
    def _point(value: dict[str, Any]) -> dict[str, str]:
        return {"value": value["value"], "precision": value["precision"]}

    @staticmethod
    def _label(value: Any) -> Any:
        if isinstance(value, str) and re.search(r"(?:/(?:Users|home|volume\d+|private|tmp)/|file://|[A-Za-z]:[\\/])", value):
            return "broker-ref:" + digest(value)[:24]
        return value[:256] if isinstance(value, str) else value

    @staticmethod
    def _validate_public_fields(snapshot: dict[str, Any]) -> None:
        for holding in snapshot["holdings"]:
            asset = holding["asset"]
            if (asset.get("asset_type") not in {"stock", "etf", "fund", "index", "cash", "bond", "commodity", "crypto", "other", "unknown"}
                    or not isinstance(asset.get("display_name"), str) or not asset["display_name"].strip()
                    or any(asset.get(k) is not None and not isinstance(asset[k], str) for k in ("symbol", "venue"))
                    or (asset.get("currency") is not None and re.fullmatch(r"[A-Z]{3}", str(asset["currency"])) is None)):
                raise AccountSyncError("account_read_asset_fields_invalid")
        for cash in snapshot["cash"]:
            if (re.fullmatch(r"[A-Z]{3}", cash["currency"]) is None
                    or cash["cash_kind"] not in {"nominal_balance", "settled_cash", "buying_power", "withdrawable_cash", "displayed_settlement", "provider_specific", "unknown"}):
                raise AccountSyncError("account_read_cash_fields_invalid")

    @staticmethod
    def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        ref = "account-observation:" + digest([
            snapshot["portfolio_id"], snapshot["account_id"], snapshot["provider_id"], snapshot["source_ref"],
            snapshot["snapshot_effective_at"], snapshot["holdings"], snapshot["cash"],
        ])[:24]
        holdings = []
        for row in snapshot["holdings"]:
            asset = row["asset"]
            public_asset = {key: AccountStateReader._label(asset.get(key)) for key in (
                "asset_type", "symbol", "venue", "currency", "display_name",
            )}
            public_asset.update({"provider_refs": {}, "asset_id": "broker-asset:" + digest(canonical_asset_identity(asset))[:24]})
            holding = {"asset": public_asset, "quantity": row["quantity"], "source_evidence": [ref]}
            for field in HOLDING_VALUE_FIELDS:
                if field not in row:
                    continue
                if field == "valuation":
                    valuation = row[field]
                    holding[field] = {key: copy.deepcopy(valuation[key]) for key in (
                        *VALUATION_NUMBERS, "currency", "value_basis", "observed_at",
                    ) if key in valuation}
                    holding[field]["source_evidence"] = [ref]
                    holding[field]["observed_at"] = AccountStateReader._point(holding[field]["observed_at"])
                else:
                    holding[field] = [{
                        **{key: copy.deepcopy(value[key]) for key in ("amount", "currency", "value_basis", "as_of") if key in value},
                        "source_evidence": [ref],
                    } for value in row[field]]
                    for value in holding[field]:
                        if "as_of" in value:
                            value["as_of"] = AccountStateReader._point(value["as_of"])
            holdings.append(holding)
        cash = [{
            **{key: AccountStateReader._label(row[key]) for key in ("currency", "cash_kind", "amount", "provider_label") if key in row},
            "source_evidence": [ref],
        } for row in snapshot["cash"]]
        return {
            "portfolio_id": snapshot["portfolio_id"], "account_id": snapshot["account_id"],
            "provider_id": snapshot["provider_id"],
            "snapshot_effective_at": AccountStateReader._point(snapshot["snapshot_effective_at"]),
            "retrieved_at": AccountStateReader._point(snapshot["retrieved_at"]),
            "holdings": holdings, "cash": cash, "completeness": copy.deepcopy(snapshot["completeness"]),
            "observation_ref": ref, "canonical_state_updated": False, "approval_required": False,
        }

    def _result(self, snapshot: dict[str, Any] | None, *, status: str, mode: str,
                now: datetime, code: str | None = None) -> dict[str, Any]:
        warnings = []
        if status == "stale":
            warnings.append("Broker refresh failed. Returned observations are stale; use their original source time.")
        elif status == "partial":
            warnings.append("The broker observation is incomplete; missing holdings or cash must not be treated as zero.")
        if code:
            warnings.append("Current broker account observations could not be refreshed.")
        return result_envelope(
            capability="account.state", producer="investkitchen.account-state", status=status,
            data=self._public_snapshot(snapshot) if snapshot is not None else None,
            authority="portfolio_fact" if snapshot is not None else "unknown",
            generated_at=timepoint(now.isoformat()), source_mode=mode,
            freshness="stale" if status == "stale" else "cached" if mode == "cache" else "current" if snapshot else "unknown",
            data_times={"observed_at": self._point(snapshot["snapshot_effective_at"]),
                        "retrieved_at": self._point(snapshot["retrieved_at"])} if snapshot else {},
            provenance=[{"source_type": "broker_observation"}] if snapshot else [],
            warnings=warnings,
            gaps=[{"gap_code": code, "reason": "The bound broker observation is unavailable or unusable.",
                   "impact": "Do not assume this is a fresh account balance.", "recoverable": True}] if code else [],
            permissions_used=["portfolio.read"],
        )

    def read(self, portfolio_id: str, account_id: str, *, refresh: bool = False) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        if portfolio_id not in self.portfolio_ids:
            return self._result(None, status="blocked", mode="unknown", now=now, code="account_read_scope_denied")
        if not isinstance(refresh, bool):
            return self._result(None, status="blocked", mode="unknown", now=now, code="account_read_arguments_invalid")
        try:
            binding = self.registry.resolve(portfolio_id, account_id)
        except AccountSyncError:
            return self._result(None, status="unavailable", mode="unknown", now=now, code="account_read_binding_unavailable")
        provider = self.providers.get(binding.provider_id)
        if provider is None:
            return self._result(None, status="unavailable", mode="unknown", now=now, code="account_read_provider_unavailable")
        key = (portfolio_id, account_id)
        lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            now = self.clock().astimezone(timezone.utc)
            cached = self._cache.get(key)
            if cached is not None:
                snapshot, fetched = cached
                elapsed = self.monotonic() - fetched
                age = self._age(snapshot, now)
                if not refresh and 0 <= elapsed < self.cache_ttl_seconds and 0 <= age <= self.max_age_seconds:
                    status = "ok" if all(v == "complete" for v in snapshot["completeness"].values()) else "partial"
                    return self._result(snapshot, status=status, mode="cache", now=now)
            try:
                raw = provider(binding.provider_request_value())
            except Exception:
                # Provider messages may contain credentials, URLs or local paths.
                if cached is not None and 0 <= self._age(cached[0], now) <= self.max_stale_seconds:
                    return self._result(cached[0], status="stale", mode="cache", now=now, code="account_read_refresh_failed")
                return self._result(None, status="unavailable", mode="unknown", now=now, code="account_read_provider_unavailable")
            now = self.clock().astimezone(timezone.utc)
            try:
                snapshot = validate_account_snapshot(raw, binding, now=now, max_age_seconds=self.max_age_seconds)
                if len(snapshot["holdings"]) > 1000 or len(snapshot["cash"]) > 50:
                    raise AccountSyncError("account_read_result_too_large")
                self._validate_public_fields(snapshot)
                states = snapshot["completeness"]
                if (all(v == "unavailable" for v in states.values())
                        or any(v == "stale" for v in states.values())
                        or (states["holdings"] == "unavailable" and snapshot["holdings"])
                        or (states["cash"] == "unavailable" and snapshot["cash"])):
                    raise AccountSyncError("account_read_components_unusable")
            except (AccountSyncError, TypeError, ValueError):
                return self._result(None, status="unavailable", mode="unknown", now=now, code="account_read_observation_invalid")
            if states["holdings"] == "complete":
                self._cache[key] = (copy.deepcopy(snapshot), self.monotonic())
            status = "ok" if all(v == "complete" for v in states.values()) else "partial"
            return self._result(snapshot, status=status, mode="live_fetch", now=now)
