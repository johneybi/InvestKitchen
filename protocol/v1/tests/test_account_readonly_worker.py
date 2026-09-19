from __future__ import annotations

import os
import sys
import types

from protocol.v1.providers import account_readonly_worker as worker


def _binding(provider_id: str) -> dict[str, str]:
    return {
        "portfolio_id": "portfolio-alpha",
        "account_id": "account-alpha",
        "provider_id": provider_id,
        "provider_account_ref": "provider-account-ref:synthetic",
    }


def test_toss_worker_reads_only_account_list_and_holdings_and_marks_cash_unavailable(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker, "_toss_access_token", lambda _secret: "synthetic-token")

    def fake_request(url: str, **_kwargs):
        calls.append(url)
        if url.endswith("/api/v1/accounts"):
            return ({"result": [{"accountSeq": 7, "accountType": "BROKERAGE"}]}, {})
        if url.endswith("/api/v1/holdings"):
            return ({
                "result": {
                    "items": [{
                        "symbol": "005930",
                        "name": "Synthetic Security",
                        "marketCountry": "KR",
                        "currency": "KRW",
                        "quantity": "4",
                    }],
                }
            }, {"x-request-id": "request-synthetic"})
        raise AssertionError(url)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    result = worker._toss_snapshot(
        _binding("toss_securities"),
        {
            "provider_id": "toss_securities",
            "client_id": "synthetic-client",
            "client_secret": "synthetic-secret",
            "accounts": {"provider-account-ref:synthetic": {"account_seq": 7}},
        },
    )
    assert calls == [
        "https://openapi.tossinvest.com/api/v1/accounts",
        "https://openapi.tossinvest.com/api/v1/holdings",
    ]
    assert result["holdings"][0]["asset"]["symbol"] == "005930"
    assert result["holdings"][0]["quantity"] == 4
    assert result["cash"] == []
    assert result["completeness"] == {"holdings": "complete", "cash": "unavailable"}
    assert result["source_ref"] == "provider-observation:toss:request-synthetic"


def test_toss_worker_can_bind_unique_brokerage_without_storing_account_number(monkeypatch) -> None:
    monkeypatch.setattr(worker, "_toss_access_token", lambda _secret: "synthetic-token")

    def fake_request(url: str, **_kwargs):
        if url.endswith("/api/v1/accounts"):
            return ({"result": [{"accountSeq": 11, "accountType": "BROKERAGE"}]}, {})
        if url.endswith("/api/v1/holdings"):
            return ({"result": {"items": []}}, {"x-request-id": "request-unique"})
        raise AssertionError(url)

    monkeypatch.setattr(worker, "_request_json", fake_request)
    binding = _binding("toss_securities")
    binding["provider_account_ref"] = "unique_brokerage"
    result = worker._toss_snapshot(
        binding,
        {"client_id": "synthetic-client", "client_secret": "synthetic-secret"},
    )
    assert result["account_id"] == "account-alpha"
    assert result["holdings"] == []
    assert result["completeness"] == {"holdings": "complete", "cash": "unavailable"}


def test_toss_worker_selects_server_owned_credential_profile(monkeypatch) -> None:
    selected: list[str] = []

    def fake_token(secret):
        selected.append(str(secret["client_id"]))
        return "synthetic-token"

    def fake_request(url: str, **_kwargs):
        if url.endswith("/api/v1/accounts"):
            return ({"result": [{"accountSeq": 21, "accountType": "BROKERAGE"}]}, {})
        if url.endswith("/api/v1/holdings"):
            return ({"result": {"items": []}}, {"x-request-id": "request-profile"})
        raise AssertionError(url)

    monkeypatch.setattr(worker, "_toss_access_token", fake_token)
    monkeypatch.setattr(worker, "_request_json", fake_request)
    binding = _binding("toss_securities")
    binding["provider_account_ref"] = "unique_brokerage"
    binding["provider_credential_ref"] = "portfolio-b"
    result = worker._toss_snapshot(
        binding,
        {
            "provider_id": "toss_securities",
            "client_id": "portfolio-a-client",
            "client_secret": "portfolio-a-secret",
            "credentials": {
                "portfolio-b": {
                    "client_id": "portfolio-b-client",
                    "client_secret": "portfolio-b-secret",
                    "accounts": {},
                }
            },
        },
    )
    assert selected == ["portfolio-b-client"]
    assert result["portfolio_id"] == "portfolio-alpha"
    assert "provider_credential_ref" not in result


def test_nhplug_worker_uses_read_only_account_list_and_paginated_balance(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_call(path: str, params: dict):
        calls.append((path, dict(params)))
        assert path == "/n2/acctinfo"
        return {"Output_0": [{"acct_no": "00000000000", "acct_type": "01"}]}

    def fake_paginate(path: str, params: dict):
        calls.append((path, dict(params)))
        assert path == "/krstock/inquiry/v1/balance"
        yield {
            "Output_0": {"dca": "12345"},
            "Output_1": [{"pdno": "005930", "hldg_qty": "5", "prdt_name": "Synthetic Security"}],
        }

    monkeypatch.setitem(sys.modules, "nhplug", types.SimpleNamespace(call=fake_call, paginate=fake_paginate))
    before = {key: os.environ.get(key) for key in (
        "NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NHPLUG_BASE_URL", "NHPLUG_AUTH_URL", "NHPLUG_TOKEN_CACHE"
    )}
    try:
        result = worker._nhplug_snapshot(
            _binding("nhplug"),
            {
                "provider_id": "nhplug",
                "app_key": "synthetic-key",
                "app_secret": "synthetic-secret",
                "accounts": {
                    "provider-account-ref:synthetic": {"act_no": "00000000000", "acct_type": "01"}
                },
            },
        )
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert [path for path, _ in calls] == ["/n2/acctinfo", "/krstock/inquiry/v1/balance"]
    balance_params = calls[1][1]
    assert balance_params["act_no"] == "00000000000"
    assert balance_params["qut_dit_cd"] == "UNT"
    assert balance_params["aly_qut_cd"] == "1"
    assert result["holdings"][0]["asset"]["symbol"] == "005930"
    assert result["holdings"][0]["asset"]["currency"] == "KRW"
    assert result["cash"][0]["amount"] == 12345
    assert result["cash"][0]["cash_kind"] == "nominal_balance"
    assert result["completeness"] == {"holdings": "complete", "cash": "complete"}
