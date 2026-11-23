from datetime import datetime, timedelta, timezone

import pytest

from protocol.v1.runtime.stock_analysis_context import collect_stock_context
from protocol.v1.security.remote_gateway import portfolio_ids_for_request, required_permissions, load_tool_catalog
from protocol.v1.transport.stdio_rpc import LocalToolRouter, RequestError


NOW = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)


class Gateway:
    portfolio_ids = ["fixture-a"]

    def __init__(self):
        self.calls = []
        self.fail_policy = False

    def get_portfolio_state(self, portfolio_id):
        self.calls.append("portfolio")
        return {"status": "partial", "data": {"positions": [
            {"asset": {"symbol": "005930", "currency": "KRW"}}],
            "cash": [], "transactions": [{"private": "not needed"}],
            "completeness": {"cash": "partial"}}}

    def get_current_policy(self, portfolio_id):
        self.calls.append("policy")
        if self.fail_policy:
            raise RuntimeError("private provider failure")
        return {"status": "ok", "data": {"policies": []}}

    def get_active_plans(self, portfolio_id):
        self.calls.append("plans")
        return {"status": "ok", "data": {"plans": []}}

    def get_market_quote(self, symbols):
        self.calls.append("quotes")
        return {"status": "ok", "freshness": "current", "data_times": {"observed_at": NOW.isoformat()},
                "provenance": [{"source_type": "market_provider"}],
                "data": {"quotes": {s: {"timestamp": NOW.isoformat(), "price": 10} for s in symbols}}}

    def get_market_ohlcv(self, symbols, **kwargs):
        self.calls.append("ohlcv")
        bars = [{"timestamp": (NOW - timedelta(days=59-i)).isoformat(), "close": i + 1} for i in range(60)]
        return {"status": "ok", "freshness": "current", "data_times": {"observed_at": NOW.isoformat()},
                "provenance": [{"source_type": "market_provider"}],
                "data": {"series": [{"symbol": s, "bars": bars} for s in symbols]}}


def test_every_invocation_reads_sources_and_computes_indicator_without_inventing_candidates():
    gateway = Gateway()
    first = collect_stock_context(gateway, "fixture-a", [], now=NOW.isoformat())
    gateway.fail_policy = True
    second = collect_stock_context(gateway, "fixture-a", [], now=NOW.isoformat())
    assert gateway.calls == ["portfolio", "policy", "plans", "quotes", "ohlcv"] * 2
    assert first["market"]["symbols"] == ["005930"]
    assert first["market"]["indicators"]["005930"] == {"sma_20": "50.5", "sma_60": "30.5"}
    assert first["market"]["quote_status"] == "ok"
    assert second["policy"] == {"status": "failed", "data": None}
    assert second["account_impact_status"] == "limited"
    assert "transactions" not in first["portfolio"]["data"]
    assert first["session"]["session"] == "unknown"
    assert "market_wide_universe_unavailable" in first["gaps"]


def test_missing_quote_time_and_stale_bars_disable_fresh_claims():
    gateway = Gateway()
    result = collect_stock_context(gateway, "fixture-a", [], now=(NOW + timedelta(days=8)).isoformat())
    assert result["market"]["quote_status"] == "stale"
    assert result["market"]["ohlcv_status"] == "stale"
    assert result["market"]["indicators"] == {}
    original = gateway.get_market_quote
    def untimed(symbols):
        result = original(symbols)
        del result["data"]["quotes"]["005930"]["timestamp"]
        return result
    gateway.get_market_quote = untimed
    assert collect_stock_context(gateway, "fixture-a", [], now=NOW.isoformat())["market"]["quote_status"] == "partial"


def test_router_bounds_and_remote_authorization_require_scope_and_all_permissions():
    gateway = Gateway()
    router = LocalToolRouter(gateway)
    assert router.call_tool("get_stock_analysis_context", {"portfolio_id": "fixture-a"})["portfolio_id"] == "fixture-a"
    for args in ({"portfolio_id": "other"}, {"portfolio_id": "fixture-a", "symbols": ["../etc"]},
                 {"portfolio_id": "fixture-a", "symbols": ["005930"] * 31}):
        with pytest.raises(RequestError):
            router.call_tool("get_stock_analysis_context", args)
    assert portfolio_ids_for_request("get_stock_analysis_context", {"portfolio_id": "fixture-a"}) == ["fixture-a"]
    assert required_permissions("get_stock_analysis_context", {}, load_tool_catalog()) == [
        "market.read", "plan.read", "policy.read", "portfolio.read"]


def test_direct_account_observation_is_read_per_run_when_available():
    gateway = Gateway()
    original = gateway.get_portfolio_state
    def portfolio(pid):
        result = original(pid)
        result["data"]["accounts"] = [{"account_id": "linked"}, {"account_id": "manual"}]
        return result
    gateway.get_portfolio_state = portfolio
    gateway.account_state_reader = object()
    def account(pid, account_id, refresh=False):
        gateway.calls.append("account:" + account_id)
        if account_id == "manual":
            return {"status": "unavailable", "data": None}
        return {"status": "partial", "data": {"holdings": [
            {"asset": {"symbol": "000660", "currency": "KRW"}}], "cash": []}}
    gateway.get_account_state = account
    result = collect_stock_context(gateway, "fixture-a", [], now=NOW.isoformat())
    assert "account:linked" in gateway.calls and "account:manual" in gateway.calls
    assert result["market"]["symbols"] == ["005930", "000660"]
    assert result["broker_accounts"][0]["observation"]["status"] == "partial"
    assert result["broker_accounts"][1]["observation"]["status"] == "unavailable"
