from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

from protocol.v1.providers import toss_readonly_worker as worker  # noqa: E402


def _secret(tmp_path: Path) -> Path:
    path = tmp_path / "market-secret.json"
    path.write_text(json.dumps({"client_id": "fixture-id", "client_secret": "fixture-secret"}), encoding="utf-8")
    return path


def test_quote_worker_is_self_contained_and_returns_requested_rows(monkeypatch, tmp_path: Path) -> None:
    def fake_request(path: str, *, headers=None, data=None, params=None):
        if path == "/oauth2/token":
            return {"access_token": "token"}
        if path == "/api/v1/prices":
            assert params == {"symbols": "005930,000660"}
            return {
                "result": [
                    {"symbol": "005930", "price": 81200, "openPrice": 80500, "timestamp": "2026-09-17T11:00:00+09:00"},
                    {"symbol": "000660", "price": 184500, "openPrice": 181000, "timestamp": "2026-09-17T11:00:00+09:00"},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    result = worker.handle_request(
        _secret(tmp_path),
        {"capability": "market.quote", "input": {"symbols": ["005930", "000660"]}},
    )
    assert result["status"] == "ok"
    assert result["data"]["quotes"]["005930"]["price"] == 81200
    assert result["data"]["quotes"]["005930"]["openPrice"] == 80500
    encoded = json.dumps(result)
    assert "fixture-secret" not in encoded
    assert "fixture-id" not in encoded
    assert "openapi.tossinvest.com" not in encoded


def test_quote_worker_supports_kr_alphanumeric_and_us_symbols_with_fx(monkeypatch, tmp_path: Path) -> None:
    def fake_request(path: str, *, headers=None, data=None, params=None):
        if path == "/oauth2/token":
            return {"access_token": "token"}
        if path == "/api/v1/prices":
            assert params == {"symbols": "0190C0,0162Z0,AAPL,JEPQ"}
            return {
                "result": [
                    {"symbol": "0190C0", "lastPrice": "7700", "currency": "KRW", "timestamp": "2026-09-17T12:55:45+09:00"},
                    {"symbol": "0162Z0", "lastPrice": "13430", "currency": "KRW", "timestamp": "2026-09-17T12:55:45+09:00"},
                    {"symbol": "AAPL", "lastPrice": "333.43", "currency": "USD", "timestamp": "2026-09-17T12:55:45+09:00"},
                    {"symbol": "JEPQ", "lastPrice": "59.46", "currency": "USD", "timestamp": "2026-09-17T12:55:45+09:00"},
                ]
            }
        if path == "/api/v1/exchange-rate":
            assert params == {"baseCurrency": "USD", "quoteCurrency": "KRW"}
            return {"result": {
                "baseCurrency": "USD",
                "quoteCurrency": "KRW",
                "rate": "1381.1",
                "midRate": "1380.95",
                "validFrom": "2026-09-17T12:55:45+09:00",
                "validUntil": "2026-09-17T13:00:43+09:00",
            }}
        raise AssertionError(path)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    result = worker.handle_request(
        _secret(tmp_path),
        {"capability": "market.quote", "input": {"symbols": ["0190c0", "0162z0", "aapl", "JEPQ"]}},
    )
    assert result["status"] == "ok"
    assert sorted(result["data"]["quotes"]) == ["0162Z0", "0190C0", "AAPL", "JEPQ"]
    assert result["data"]["quotes"]["AAPL"]["currency"] == "USD"
    assert result["data"]["exchange_rates"]["USD/KRW"]["rate"] == "1381.1"



def test_quote_worker_keeps_us_quotes_but_marks_partial_when_fx_unavailable(monkeypatch, tmp_path: Path) -> None:
    def fake_request(path: str, *, headers=None, data=None, params=None):
        if path == "/oauth2/token":
            return {"access_token": "token"}
        if path == "/api/v1/prices":
            return {"result": [{"symbol": "AAPL", "lastPrice": "333.43", "currency": "USD", "timestamp": "2026-09-17T12:55:45+09:00"}]}
        if path == "/api/v1/exchange-rate":
            raise RuntimeError("fx unavailable")
        raise AssertionError(path)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    result = worker.handle_request(
        _secret(tmp_path),
        {"capability": "market.quote", "input": {"symbols": ["AAPL"]}},
    )
    assert result["status"] == "partial"
    assert result["data"]["quotes"]["AAPL"]["lastPrice"] == "333.43"
    assert result["data"]["exchange_rates"] == {}
    assert any(gap.get("scope") == {"component": "exchange_rate"} for gap in result["gaps"])


def test_ohlcv_worker_supports_kr_alphanumeric_and_us_symbols(monkeypatch, tmp_path: Path) -> None:
    def fake_request(path: str, *, headers=None, data=None, params=None):
        if path == "/oauth2/token":
            return {"access_token": "token"}
        if path == "/api/v1/candles":
            assert params["symbol"] in {"0190C0", "AAPL"}
            return {"result": {"candles": [{
                "timestamp": "2026-09-17T09:00:00+09:00",
                "openPrice": "10", "highPrice": "11", "lowPrice": "9", "closePrice": "10.5", "volume": "100",
            }]}}
        raise AssertionError(path)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    result = worker.handle_request(
        _secret(tmp_path),
        {"capability": "market.ohlcv", "input": {"symbols": ["0190c0", "aapl"], "interval": "1d", "count": 1}},
    )
    assert result["status"] == "ok"
    assert [row["symbol"] for row in result["data"]["series"]] == ["0190C0", "AAPL"]

def test_ohlcv_worker_normalizes_stock_and_index_candles(monkeypatch, tmp_path: Path) -> None:
    def fake_request(path: str, *, headers=None, data=None, params=None):
        if path == "/oauth2/token":
            return {"access_token": "token"}
        if path == "/api/v1/candles":
            assert params["symbol"] == "005930"
            return {"result": {"candles": [{
                "timestamp": "2026-09-17T09:00:00+09:00",
                "openPrice": "80000", "highPrice": "81000", "lowPrice": "79500", "closePrice": "80700", "volume": "1000",
            }]}}
        if path == "/api/v1/market-indicators/KOSPI/candles":
            return {"result": {"candles": [{
                "timestamp": "2026-09-17T09:01:00+09:00",
                "openPrice": "3400", "highPrice": "3410", "lowPrice": "3398", "closePrice": "3408", "volume": "2000",
            }]}}
        raise AssertionError(path)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    result = worker.handle_request(
        _secret(tmp_path),
        {"capability": "market.ohlcv", "input": {"symbols": ["005930", "KOSPI"], "interval": "1m", "count": 20}},
    )
    assert result["status"] == "ok"
    series = {row["symbol"]: row for row in result["data"]["series"]}
    assert series["005930"]["asset_type"] == "stock"
    assert series["KOSPI"]["asset_type"] == "index"
    assert series["005930"]["bars"][0]["open"] == 80000.0
    assert series["005930"]["bars"][0]["close"] == 80700.0


def test_worker_rejects_non_market_symbol_before_network(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker, "_request_json", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    result = worker.handle_request(
        _secret(tmp_path),
        {"capability": "market.quote", "input": {"symbols": ["../../secret"]}},
    )
    assert result["status"] == "unavailable"
    assert result["gaps"][0]["gap_code"] == "market_symbol_invalid"


def test_quote_worker_preserves_supported_rows_when_request_also_contains_invalid_symbols(monkeypatch, tmp_path: Path) -> None:
    def fake_request(path: str, *, headers=None, data=None, params=None):
        if path == "/oauth2/token":
            return {"access_token": "token"}
        if path == "/api/v1/prices":
            assert params == {"symbols": "005930,000660"}
            return {
                "result": [
                    {"symbol": "005930", "lastPrice": "253500", "timestamp": "2026-09-17T12:30:00+09:00"},
                    {"symbol": "000660", "lastPrice": "1745000", "timestamp": "2026-09-17T12:30:00+09:00"},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    result = worker.handle_request(
        _secret(tmp_path),
        {"capability": "market.quote", "input": {"symbols": ["005930", "../../secret", "000660"]}},
    )
    assert result["status"] == "partial"
    assert sorted(result["data"]["quotes"]) == ["000660", "005930"]
    assert result["data"]["failed_symbols"] == ["../../SECRET"]
    assert result["data"]["requested_symbols"] == ["005930", "../../SECRET", "000660"]
    assert result["gaps"][0]["gap_code"] == "market_symbols_unavailable"


def test_worker_rejects_unknown_capability_before_secret_read(tmp_path: Path) -> None:
    result = worker.handle_request(
        tmp_path / "missing-secret.json",
        {"capability": "market.write", "input": {}},
    )
    assert result["status"] == "unavailable"
    assert result["gaps"][0]["gap_code"] == "market_capability_unsupported"
