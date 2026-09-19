from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class AccountProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


AccountFetcher = Callable[[dict[str, Any]], Mapping[str, Any]]


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(value: Any, *, code: str, nonnegative: bool = False) -> int | float:
    if isinstance(value, bool):
        raise AccountProviderError(code)
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            raise AccountProviderError(code)
        try:
            number = float(value)
        except ValueError as exc:
            raise AccountProviderError(code) from exc
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise AccountProviderError(code)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise AccountProviderError(code)
    return int(number) if number.is_integer() else number


def _asset(row: Mapping[str, Any], *, default_market_type: str) -> dict[str, Any]:
    symbol_value = _first(row, "symbol", "code", "pdno", "iem_cd", "isu_cd")
    symbol = str(symbol_value).strip() if symbol_value is not None else ""
    display_value = _first(row, "display_name", "name", "productName", "product_name", "prdt_name", "prdt_abrv_name")
    display_name = str(display_value).strip() if display_value is not None else symbol
    asset_type = str(_first(row, "asset_type", "assetType") or (default_market_type if symbol else "unknown")).strip().lower()
    currency_value = _first(row, "currency", "currencyCode", "crncy_cd")
    currency = str(currency_value).strip().upper() if currency_value is not None else None
    venue_value = _first(row, "venue", "exchange", "market", "marketCode", "exch_cd")
    venue = str(venue_value).strip() if venue_value is not None else None
    provider_refs: dict[str, str] = {}
    aliases = {
        "product_code": ("product_code", "productCode", "prdt_cd"),
        "fund_code": ("fund_code", "fundCode"),
        "provider_product_id": ("provider_product_id", "providerProductId"),
        "provider_asset_id": ("provider_asset_id", "providerAssetId"),
    }
    for canonical, keys in aliases.items():
        value = _first(row, *keys)
        if value not in (None, ""):
            provider_refs[canonical] = str(value).strip()
    asset_id_value = _first(row, "asset_id", "assetId")
    asset: dict[str, Any] = {
        "asset_type": asset_type,
        "symbol": symbol or None,
        "venue": venue,
        "currency": currency,
        "display_name": display_name or "Unnamed provider asset",
        "provider_refs": provider_refs,
    }
    if asset_id_value not in (None, ""):
        asset["asset_id"] = str(asset_id_value).strip()
    return asset


def _snapshot(
    raw: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    provider_id: str,
    default_market_type: str,
    position_rows: list[Mapping[str, Any]],
    cash_rows: list[Mapping[str, Any]],
    default_currency: str | None = None,
) -> dict[str, Any]:
    required_binding = {key: str(binding.get(key) or "").strip() for key in ("portfolio_id", "account_id", "provider_id", "provider_account_ref")}
    if not all(required_binding.values()) or required_binding["provider_id"] != provider_id:
        raise AccountProviderError("account_provider_binding_invalid")
    observed_at = raw.get("observed_at")
    retrieved_at = raw.get("retrieved_at")
    source_ref = str(raw.get("source_ref") or "").strip()
    if not isinstance(observed_at, dict) or not isinstance(retrieved_at, dict) or not source_ref:
        raise AccountProviderError("account_provider_observation_metadata_missing")
    holdings = []
    for row in position_rows:
        normalized_row = dict(row)
        if default_currency and _first(normalized_row, "currency", "currencyCode", "crncy_cd") in (None, ""):
            normalized_row["currency"] = default_currency
        asset = _asset(normalized_row, default_market_type=default_market_type)
        quantity = _number(
            _first(normalized_row, "quantity", "qty", "holdingQuantity", "holding_quantity", "hldg_qty", "hold_qty"),
            code="account_provider_quantity_invalid",
            nonnegative=True,
        )
        holdings.append({"asset": asset, "quantity": quantity, "source_evidence": [source_ref]})
    cash: list[dict[str, Any]] = []
    for row in cash_rows:
        currency = str(_first(row, "currency", "currencyCode", "crncy_cd") or default_currency or "").strip().upper()
        if len(currency) != 3:
            raise AccountProviderError("account_provider_cash_currency_invalid")
        cash.append({
            "cash_id": str(row.get("cash_id") or "").strip() or None,
            "currency": currency,
            "cash_kind": str(row.get("cash_kind") or "nominal_balance"),
            "amount": _number(_first(row, "amount", "cash", "dnca_tot_amt"), code="account_provider_cash_amount_invalid"),
            "provider_label": row.get("provider_label") if isinstance(row.get("provider_label"), str) else None,
            "source_evidence": [source_ref],
        })
    completeness_raw = raw.get("completeness") if isinstance(raw.get("completeness"), Mapping) else {}
    holdings_state = str(completeness_raw.get("holdings") or "complete")
    cash_state = str(completeness_raw.get("cash") or ("complete" if cash else "unavailable"))
    return {
        "schema_version": "1.0",
        **required_binding,
        "snapshot_effective_at": copy.deepcopy(observed_at),
        "retrieved_at": copy.deepcopy(retrieved_at),
        "source_ref": source_ref,
        "holdings": holdings,
        "cash": cash,
        "completeness": {
            "holdings": holdings_state,
            "cash": cash_state,
        },
    }


def normalize_toss_account_snapshot(raw: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a read-only Toss holdings/balance result; never accepts orders."""

    if not isinstance(raw, Mapping):
        raise AccountProviderError("toss_account_payload_invalid")
    result = raw.get("result") if isinstance(raw.get("result"), Mapping) else raw
    if not isinstance(result, Mapping):
        raise AccountProviderError("toss_account_payload_invalid")
    items = result.get("items") if isinstance(result.get("items"), list) else result.get("positions")
    if not isinstance(items, list) or any(not isinstance(row, Mapping) for row in items):
        raise AccountProviderError("toss_account_holdings_invalid")
    cash_rows = raw.get("cash") if isinstance(raw.get("cash"), list) else []
    if any(not isinstance(row, Mapping) for row in cash_rows):
        raise AccountProviderError("toss_account_cash_invalid")
    metadata = {**raw, "result": result}
    return _snapshot(
        metadata,
        binding,
        provider_id="toss_securities",
        default_market_type="stock",
        position_rows=[dict(row) for row in items],
        cash_rows=[dict(row) for row in cash_rows],
    )


def normalize_namuh_account_snapshot(raw: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an NHPLUG/Namuh balance inquiry result into AccountSnapshot."""

    if not isinstance(raw, Mapping):
        raise AccountProviderError("namuh_account_payload_invalid")
    value = raw.get("value") if isinstance(raw.get("value"), Mapping) else raw
    if not isinstance(value, Mapping):
        raise AccountProviderError("namuh_account_payload_invalid")
    positions = value.get("positions") if isinstance(value.get("positions"), list) else value.get("Output_1")
    if not isinstance(positions, list) or any(not isinstance(row, Mapping) for row in positions):
        raise AccountProviderError("namuh_account_holdings_invalid")
    output = value.get("raw") if isinstance(value.get("raw"), Mapping) else value.get("Output_0")
    output = output if isinstance(output, Mapping) else value
    cash_value = _first(value, "cash")
    if cash_value is None:
        # Current NHPLUG domestic balance uses ``dca`` for deposit/cash.  Keep
        # the legacy field as a compatibility fallback, but never substitute
        # orderable/withdrawable buying-power fields for nominal cash.
        cash_value = _first(output, "dca", "dnca_tot_amt")
    cash_rows: list[dict[str, Any]] = []
    if cash_value is not None:
        cash_rows.append({"currency": "KRW", "cash_kind": "nominal_balance", "amount": cash_value})
    metadata = {**raw, "value": value}
    return _snapshot(
        metadata,
        binding,
        provider_id="nhplug",
        default_market_type="stock",
        position_rows=[dict(row) for row in positions],
        cash_rows=cash_rows,
        default_currency="KRW",
    )


@dataclass(frozen=True)
class ReadOnlyAccountAdapter:
    provider_id: str
    fetcher: AccountFetcher
    normalizer: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]

    def read_snapshot(self, binding: dict[str, Any]) -> dict[str, Any]:
        if str(binding.get("provider_id") or "") != self.provider_id:
            raise AccountProviderError("account_provider_binding_mismatch")
        raw = self.fetcher(copy.deepcopy(binding))
        if not isinstance(raw, Mapping):
            raise AccountProviderError("account_provider_payload_invalid")
        return self.normalizer(raw, binding)


__all__ = [
    "AccountFetcher",
    "AccountProviderError",
    "ReadOnlyAccountAdapter",
    "normalize_namuh_account_snapshot",
    "normalize_toss_account_snapshot",
]
