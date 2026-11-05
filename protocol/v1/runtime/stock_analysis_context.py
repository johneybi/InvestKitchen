"""Shared read-only evidence collection for manual analysis and briefings."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from protocol.v1.runtime.briefing_analysis import observation_status, session_status, simple_moving_average

MAX_SYMBOLS = 30
_SYMBOL = re.compile(r"[0-9A-Z]{6}\Z")


def _time_value(value: Any) -> str | None:
    return value.get("value") if isinstance(value, dict) else value if isinstance(value, str) else None


def _read(call, *args, **kwargs):
    try:
        result = call(*args, **kwargs)
        return result if isinstance(result, dict) else {"status": "failed", "data": None}
    except Exception:
        # Provider errors may contain secrets or local paths; do not expose them.
        return {"status": "failed", "data": None}


def _market_status(result: dict, now: str, seconds: int) -> str:
    raw = result.get("status")
    if raw in {"blocked", "unavailable", "error", "failed"}:
        return "failed"
    if raw == "stale" or result.get("freshness") == "stale":
        return "stale"
    times = result.get("data_times") or {}
    # Retrieval time is not the market observation time.
    observed = _time_value(times.get("observed_at"))
    provenance = result.get("provenance") or []
    source = (provenance[0].get("source_id") or provenance[0].get("source_type")) if provenance and isinstance(provenance[0], dict) else None
    return observation_status({"status": raw, "source": source, "observed_at": observed},
                              now=now, max_age_seconds=seconds)


def collect_stock_context(gateway, portfolio_id: str, symbols: list[str], *, now: str | None = None) -> dict:
    """Read current scoped facts each invocation; never use conversation state."""
    if portfolio_id not in gateway.portfolio_ids:
        raise ValueError("portfolio is not configured")
    if len(symbols) > MAX_SYMBOLS or any(not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in symbols):
        raise ValueError("invalid symbol selection")
    now = now or datetime.now(timezone.utc).isoformat()
    portfolio = _read(gateway.get_portfolio_state, portfolio_id)
    policy = _read(gateway.get_current_policy, portfolio_id)
    plans = _read(gateway.get_active_plans, portfolio_id)
    data = portfolio.get("data") if isinstance(portfolio.get("data"), dict) else {}
    accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    account_reads = []
    if getattr(gateway, "account_state_reader", None) is not None and callable(getattr(gateway, "get_account_state", None)):
        for row in accounts[:30]:
            account_id = row.get("account_id") if isinstance(row, dict) else None
            if not isinstance(account_id, str) or not account_id:
                continue
            account = _read(gateway.get_account_state, portfolio_id, account_id, refresh=False)
            payload = account.get("data")
            if isinstance(payload, dict):
                payload = dict(payload)
                for key in ("holdings", "cash"):
                    if isinstance(payload.get(key), list) and len(payload[key]) > 100:
                        payload[key] = payload[key][:100]
                        account = {**account, "status": "partial", "analysis_truncated": True}
                account = {**account, "data": payload}
            account_reads.append({"account_id": account_id, "observation": account})
    # Include explicitly requested symbols first, then currently reported KRW holdings.
    selected = list(dict.fromkeys(symbols))
    omitted = 0
    positions = list(data.get("positions", [])) if isinstance(data.get("positions"), list) else []
    for account in account_reads:
        observation = account["observation"]
        account_data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
        if observation.get("status") in {"ok", "partial"} and isinstance(account_data.get("holdings"), list):
            positions.extend(account_data["holdings"])
    for row in positions:
        asset = row.get("asset", {}) if isinstance(row, dict) else {}
        symbol = asset.get("symbol") if isinstance(asset, dict) else None
        if not isinstance(asset, dict) or asset.get("currency") != "KRW" or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) or symbol in selected:
            continue
        if len(selected) == MAX_SYMBOLS:
            omitted += 1
        else:
            selected.append(symbol)
    # Bounds on returned private data are independent of the provider.
    truncated = []
    if data:
        data = dict(data)
        for key, limit in (("accounts", 30), ("positions", 100), ("cash", 100)):
            if isinstance(data.get(key), list) and len(data[key]) > limit:
                data[key] = data[key][:limit]
                truncated.append(key)
        # Transaction history is unrelated to this read and may be unbounded.
        data.pop("transactions", None)
        portfolio = {**portfolio, "data": data}
    quote = _read(gateway.get_market_quote, selected) if selected else {"status": "missing", "data": None}
    ohlcv = _read(gateway.get_market_ohlcv, selected, interval="1d", count=120) if selected else {"status": "missing", "data": None}
    quote_status = _market_status(quote, now, 900) if selected else "missing"
    bars_status = _market_status(ohlcv, now, 7 * 86400) if selected else "missing"
    # Some providers use retrieval time when a quote timestamp is absent, and
    # report the newest of several symbols as the aggregate observation time.
    # Neither proves the age of every quote in the batch.
    quote_rows = (quote.get("data") or {}).get("quotes", {}) if isinstance(quote.get("data"), dict) else {}
    if quote_status == "ok":
        for symbol in selected:
            row = quote_rows.get(symbol, {}) if isinstance(quote_rows, dict) else {}
            stamp = next((row.get(key) for key in ("timestamp", "updatedAt", "marketTimestamp", "market_timestamp")
                          if isinstance(row, dict) and row.get(key)), None)
            if observation_status({"status": "ok", "source": "market_provider", "observed_at": stamp},
                                  now=now, max_age_seconds=900) != "ok":
                quote_status = "partial"
                break
    indicators = {}
    # A derived average is available only when the series is complete and dated.
    if bars_status == "ok" and not ohlcv.get("gaps"):
        raw = ohlcv.get("data") or {}
        for series in raw.get("series", []) if isinstance(raw, dict) else []:
            if not isinstance(series, dict) or series.get("symbol") not in selected:
                continue
            bars = series.get("bars")
            if not isinstance(bars, list) or len(bars) > 120:
                continue
            try:
                dates = [datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00")) for bar in bars]
                if not dates or any(d.tzinfo is None for d in dates) or dates != sorted(set(dates)):
                    continue
                if observation_status({"status": "ok", "source": "market_provider", "observed_at": dates[-1].isoformat()},
                                      now=now, max_age_seconds=7 * 86400) != "ok":
                    continue
                values = [bar["close"] for bar in bars]
                indicators[series["symbol"]] = {f"sma_{n}": str(value) if value is not None else None
                    for n in (20, 60) for value in [simple_moving_average(values, n)]}
            except (KeyError, TypeError, ValueError):
                continue
    return {
        "kind": "stock_analysis_context", "as_of": now, "portfolio_id": portfolio_id,
        "portfolio": portfolio, "broker_accounts": account_reads, "policy": policy, "plans": plans,
        # Per-account observation recency and FX valuation are not yet validated.
        "account_impact_status": "limited",
        "market": {"symbols": selected, "omitted_holdings": omitted, "quote": quote,
                   "quote_status": quote_status, "ohlcv": ohlcv, "ohlcv_status": bars_status,
                   "indicators": indicators, "indicator_note": "Daily bars may include an unfinished session; session close is unverified.",
                   "venue_coverage": "unknown"},
        "session": session_status(None, now=now, max_age_seconds=0),
        "gaps": (["portfolio_response_truncated"] if truncated else [])
                + (["holding_symbols_truncated"] if omitted else [])
                + (["broker_account_reader_unavailable"] if not account_reads else [])
                + ["verified_trading_calendar_unavailable", "market_wide_universe_unavailable",
                   "public_news_and_disclosures_not_collected", "previous_briefing_not_connected"],
        "authority": "analysis_evidence_only", "execution_authorized": False,
    }
