#!/usr/bin/env python3
"""Self-contained read-only Toss market provider worker.

The worker owns the provider credential and all provider-specific network I/O.
It accepts one JSON request on stdin and emits one Capability Result Envelope on
stdout. It never imports the legacy TradeMind workspace and never exposes
credentials, provider URLs, tracebacks, or write/order methods.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import result_envelope, timepoint  # noqa: E402
from protocol.v1.adapters.market_legacy import adapt_market_result  # noqa: E402


BASE_URL = "https://openapi.tossinvest.com"
MAX_RESPONSE_BYTES = 2_000_000
INDEX_SYMBOLS = frozenset({"KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"})
# Toss Market Data uses one symbol namespace for Korean and U.S. securities.
# Official examples include 005930 and AAPL, and current KRX ETF identifiers can
# also be six-character alphanumeric values such as 0190C0. Keep this bounded
# to the provider-documented character set and a conservative maximum length.
SECURITY_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,32}$")
INTERVAL_RE = re.compile(r"^[1-9][0-9]*(?:m|h|d|w)$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _load_secret(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("market secret file must contain an object")
    client_id = value.get("client_id")
    client_secret = value.get("client_secret")
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("market client_id is missing")
    if not isinstance(client_secret, str) or not client_secret:
        raise ValueError("market client_secret is missing")
    return {"client_id": client_id, "client_secret": client_secret}


def _request_json(
    path: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    params: dict[str, str] | None = None,
) -> Any:
    query = "" if not params else "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        BASE_URL + path + query,
        headers={"Accept": "application/json", "User-Agent": "investkitchen-market/1", **(headers or {})},
        data=data,
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("market_auth_unavailable") from None
        if exc.code == 429:
            raise RuntimeError("market_rate_limited") from None
        raise RuntimeError("market_provider_http_unavailable") from None
    except Exception:
        raise RuntimeError("market_provider_http_unavailable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("market_response_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("market_payload_invalid") from None


def _access_token(secret: dict[str, str]) -> str:
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": secret["client_id"],
        "client_secret": secret["client_secret"],
    }).encode("utf-8")
    body = _request_json(
        "/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("market_auth_unavailable")
    return token


def _api_get(path: str, token: str, params: dict[str, str] | None = None) -> Any:
    body = _request_json(path, headers={"Authorization": "Bearer " + token}, params=params)
    return body.get("result", body) if isinstance(body, dict) else body


def _partition_symbols(values: Any, *, max_items: int) -> tuple[list[str], list[str], list[str]]:
    if not isinstance(values, list):
        raise ValueError("market_symbols_invalid")
    requested: list[str] = []
    supported: list[str] = []
    unsupported: list[str] = []
    for raw in values:
        symbol = str(raw or "").strip().upper()
        if not symbol:
            raise ValueError("market_symbol_invalid")
        if symbol in requested:
            continue
        requested.append(symbol)
        if symbol in INDEX_SYMBOLS or SECURITY_SYMBOL_RE.fullmatch(symbol):
            supported.append(symbol)
        else:
            unsupported.append(symbol)
    if not requested or len(requested) > max_items:
        raise ValueError("market_symbols_invalid")
    return supported, unsupported, requested


def _merge_unsupported_symbols(raw: dict[str, Any], *, requested: list[str], unsupported: list[str]) -> dict[str, Any]:
    if not unsupported:
        raw["requested_symbols"] = list(requested)
        return raw
    failed = sorted(set(str(value) for value in raw.get("failed_symbols") or []) | set(unsupported))
    raw["failed_symbols"] = failed
    raw["requested_symbols"] = list(requested)
    component_name = "quotes" if "quotes" in raw else "candles"
    component = (raw.get("components") or {}).get(component_name)
    if isinstance(component, dict):
        component["requested_symbols"] = list(requested)
        component["failed_symbols"] = failed
        if raw.get("quotes") or raw.get("series"):
            component["status"] = "partial"
    return raw


def _split_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    return ([value for value in symbols if value not in INDEX_SYMBOLS], [value for value in symbols if value in INDEX_SYMBOLS])


def _timestamp_value(row: dict[str, Any]) -> str | None:
    for key in ("timestamp", "updatedAt", "marketTimestamp", "market_timestamp"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _fetch_quotes(symbols: list[str], token: str) -> dict[str, Any]:
    retrieved = datetime.now(timezone.utc)
    stocks, indexes = _split_symbols(symbols)
    quotes: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    statuses: dict[str, dict[str, Any]] = {}

    for kind, requested, path in (
        ("stock_quotes", stocks, "/api/v1/prices"),
        ("index_quotes", indexes, "/api/v1/market-indicators/prices"),
    ):
        if not requested:
            statuses[kind] = {"status": "not_requested", "retrieved_at": retrieved.isoformat()}
            continue
        try:
            payload = _api_get(path, token, {"symbols": ",".join(requested)}) or []
            rows = payload if isinstance(payload, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "")
                if symbol in requested:
                    quotes[symbol] = dict(row)
            missing = [symbol for symbol in requested if symbol not in quotes]
            failed.extend(missing)
            statuses[kind] = {
                "status": "available" if not missing else "partial" if len(missing) < len(requested) else "unavailable",
                "retrieved_at": retrieved.isoformat(),
            }
        except Exception:
            failed.extend(requested)
            statuses[kind] = {"status": "unavailable", "retrieved_at": retrieved.isoformat()}

    stamps = [value for value in (_timestamp_value(row) for row in quotes.values()) if value]
    as_of = max(stamps) if stamps else retrieved.isoformat()
    failed = sorted(set(failed))
    overall = "available" if quotes and not failed else "partial" if quotes else "unavailable"
    statuses["quotes"] = {
        "status": overall,
        "retrieved_at": retrieved.isoformat(),
        "as_of": as_of,
        "requested_symbols": list(symbols),
        "succeeded_symbols": sorted(quotes),
        "failed_symbols": failed,
        "row_count": len(quotes),
    }
    exchange_rates: dict[str, dict[str, Any]] = {}
    if any(str(row.get("currency") or "").upper() == "USD" for row in quotes.values()):
        try:
            fx = _api_get(
                "/api/v1/exchange-rate",
                token,
                {"baseCurrency": "USD", "quoteCurrency": "KRW"},
            )
            if isinstance(fx, dict) and fx.get("rate") is not None:
                exchange_rates["USD/KRW"] = {
                    key: fx.get(key)
                    for key in (
                        "baseCurrency",
                        "quoteCurrency",
                        "rate",
                        "midRate",
                        "basisPoint",
                        "rateChangeType",
                        "validFrom",
                        "validUntil",
                    )
                    if fx.get(key) is not None
                }
                statuses["exchange_rate"] = {
                    "status": "available",
                    "retrieved_at": retrieved.isoformat(),
                }
        except Exception:
            # Quote availability remains usable, but KRW valuation for USD rows
            # is incomplete when the synchronized FX observation is unavailable.
            statuses["exchange_rate"] = {
                "status": "unavailable",
                "retrieved_at": retrieved.isoformat(),
            }
    return {
        "schema_version": "1.1",
        "source": "Toss Securities Open API",
        "retrieved_at": retrieved.isoformat(),
        "as_of": as_of,
        "quotes": dict(sorted(quotes.items())),
        "exchange_rates": exchange_rates,
        "failed_symbols": failed,
        "stale_symbols": [],
        "components": statuses,
        "requested_symbols": list(symbols),
    }


def _interval_delta(interval: str) -> timedelta | None:
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", interval)
    if not match:
        return None
    value = int(match.group(1))
    return {
        "m": timedelta(minutes=value),
        "h": timedelta(hours=value),
        "d": timedelta(days=value),
    }[match.group(2)]


def _normalize_candles(payload: Any, *, interval: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    rows = payload.get("candles", []) if isinstance(payload, dict) else []
    duration = _interval_delta(interval)
    result: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            continue
        try:
            started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if started.tzinfo is None:
            continue
        try:
            item = {
                "timestamp": timestamp,
                "open": float(raw["openPrice"]),
                "high": float(raw["highPrice"]),
                "low": float(raw["lowPrice"]),
                "close": float(raw["closePrice"]),
                "volume": float(raw.get("volume") or 0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if duration is not None:
            item["complete"] = started + duration <= retrieved_at
        result.append(item)
    result.sort(key=lambda row: str(row["timestamp"]))
    return result


def _fetch_ohlcv(symbols: list[str], token: str, *, interval: str, count: int) -> dict[str, Any]:
    if not INTERVAL_RE.fullmatch(interval):
        raise ValueError("market_interval_invalid")
    if count < 1 or count > 200:
        raise ValueError("market_count_invalid")
    retrieved = datetime.now(timezone.utc)
    series: list[dict[str, Any]] = []
    failed: list[str] = []

    def fetch_symbol(symbol: str) -> dict[str, Any] | None:
        try:
            if symbol in INDEX_SYMBOLS:
                payload = _api_get(
                    "/api/v1/market-indicators/" + urllib.parse.quote(symbol, safe="") + "/candles",
                    token,
                    {"interval": interval, "count": str(count)},
                )
                asset_type = "index"
            else:
                payload = _api_get(
                    "/api/v1/candles",
                    token,
                    {"symbol": symbol, "interval": interval, "count": str(count), "adjusted": "false"},
                )
                asset_type = "stock"
            bars = _normalize_candles(payload, interval=interval, retrieved_at=retrieved)
            if not bars:
                return None
            return {"symbol": symbol, "asset_type": asset_type, "interval": interval, "bars": bars}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as executor:
        futures = {executor.submit(fetch_symbol, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            row = future.result()
            if row is None:
                failed.append(symbol)
            else:
                series.append(row)
    series.sort(key=lambda row: str(row["symbol"]))
    failed = sorted(set(failed))
    status = "available" if series and not failed else "partial" if series else "unavailable"
    latest = [str(row["bars"][-1]["timestamp"]) for row in series if row.get("bars")]
    as_of = max(latest) if latest else retrieved.isoformat()
    return {
        "schema_version": "1.1",
        "source": "Toss Securities Open API",
        "retrieved_at": retrieved.isoformat(),
        "as_of": as_of,
        "series": series,
        "failed_symbols": failed,
        "stale_symbols": [],
        "components": {
            "candles": {
                "status": status,
                "retrieved_at": retrieved.isoformat(),
                "as_of": as_of,
                "requested_symbols": list(symbols),
                "succeeded_symbols": sorted(row["symbol"] for row in series),
                "failed_symbols": failed,
            }
        },
        "requested_symbols": list(symbols),
        "interval": interval,
        "count": count,
    }


def _unavailable(capability: str, reason_code: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return result_envelope(
        capability=capability,
        producer="investkitchen.market.toss-readonly",
        status="unavailable",
        data=None,
        authority="market_observation",
        generated_at=timepoint(now),
        freshness="unknown",
        source_mode="live_fetch",
        warnings=["Market provider is unavailable."],
        gaps=[{
            "gap_code": reason_code,
            "required_capability": capability,
            "scope": None,
            "reason": "The configured market provider could not complete the read request.",
            "impact": "Current market observations are unavailable for this request.",
            "recoverable": True,
        }],
        permissions_used=["market.read", "network.outbound", "secret.use"],
    )


def handle_request(secret_file: Path, request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return _unavailable("market.unknown", "market_request_invalid")
    capability = str(request.get("capability") or "")
    payload = request.get("input") if isinstance(request.get("input"), dict) else {}
    if capability not in {"market.quote", "market.ohlcv"}:
        return _unavailable(capability or "market.unknown", "market_capability_unsupported")
    try:
        symbols, unsupported, requested = _partition_symbols(
            payload.get("symbols"),
            max_items=100 if capability == "market.quote" else 20,
        )
        if not symbols:
            return _unavailable(capability, "market_symbol_invalid")
        token = _access_token(_load_secret(secret_file))
        if capability == "market.quote":
            raw = _fetch_quotes(symbols, token)
        else:
            raw = _fetch_ohlcv(
                symbols,
                token,
                interval=str(payload.get("interval") or "1d"),
                count=int(payload.get("count") or 120),
            )
        raw = _merge_unsupported_symbols(raw, requested=requested, unsupported=unsupported)
        return adapt_market_result(raw, capability=capability, source_mode="live_fetch")
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("market_") else "market_request_invalid"
        return _unavailable(capability, code)
    except Exception:
        return _unavailable(capability, "market_provider_unavailable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Kept as a no-op compatibility option for old local preflight commands.
    parser.add_argument("--provider-workspace", type=Path)
    parser.add_argument("--secret-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        request = None
    result = handle_request(args.secret_file.resolve(), request)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
