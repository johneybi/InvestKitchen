from __future__ import annotations

import json
import stat
import time

import pytest
from starlette.testclient import TestClient

from protocol.v1.tests.test_mcp_http import HEADERS, _gateway, _jwt, _post, _verifier, signing_key
from protocol.v1.transport.http_diagnostics import TransportDiagnostics, private_transport_diagnostics
from protocol.v1.transport.mcp_http import create_http_app
from protocol.v1.transport.mcp_read_surface import READ_TOOLS


def test_http_diagnostics_only_record_bounded_transport_metadata(tmp_path, signing_key):
    root = tmp_path / "diagnostics"; root.mkdir(mode=0o700)
    events = TransportDiagnostics(root, set(READ_TOOLS))
    gateway = _gateway()
    gateway.handlers["knowledge.current"] = lambda payload: (_ for _ in ()).throw(RuntimeError("private-path secret-note"))
    with TestClient(create_http_app(gateway, _verifier(signing_key), diagnostics=events),
                    base_url="https://mcp.example.test") as client:
        headers = {**HEADERS, "Authorization": "Bearer " + _jwt(signing_key),
                   "Cookie": "private-cookie", "Mcp-Session-Id": "private-session"}
        assert _post(client, "tools/list", headers=headers).status_code == 200
        result = _post(client, "tools/call", {"name": "get_current_knowledge", "arguments": {}}, headers=headers)
        assert result.json()["result"]["isError"]
        _post(client, "tools/call", {"name": "private-tool-name", "arguments": {"secret": "private-argument"}}, headers=headers)
        denied = _post(client, "tools/list", headers={**headers, "Authorization": "Bearer private-token"})
        assert denied.status_code == 401
    text = events.path.read_text()
    for secret in ["private-path", "secret-note", "private-cookie", "private-session", "private-token",
                   "private-tool-name", "private-argument", headers["Authorization"]]:
        assert secret not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert any(row.get("outcome") == "internal_error" for row in rows)
    assert any(row.get("status") == 401 for row in rows)
    assert any(row.get("event") == "tools_listed" for row in rows)
    assert stat.S_IMODE(events.path.stat().st_mode) == 0o600
    assert events.path.stat().st_size <= events.max_bytes


def test_diagnostics_reject_shared_roots_links_and_existing_shared_files(tmp_path):
    root = tmp_path / "shared"; root.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        TransportDiagnostics(root, set())
    root.chmod(0o700)
    target = root / "transport-events.jsonl"; target.write_text("private"); target.chmod(0o644)
    with pytest.raises(ValueError):
        TransportDiagnostics(root, set())
    target.unlink(); destination = tmp_path / "destination"; destination.write_text("unchanged")
    target.symlink_to(destination)
    with pytest.raises(ValueError):
        TransportDiagnostics(root, set())
    assert destination.read_text() == "unchanged"


def test_diagnostic_limit_and_unavailable_sink_do_not_change_reads(tmp_path, signing_key):
    root = tmp_path / "diagnostics"; root.mkdir(mode=0o700)
    events = TransportDiagnostics(root, set(READ_TOOLS), max_bytes=1)
    events.tool("get_capabilities", "ok")
    assert events.path.read_bytes() == b""
    events.path.unlink(); root.rmdir()
    with TestClient(create_http_app(_gateway(), _verifier(signing_key), diagnostics=events),
                    base_url="https://mcp.example.test") as client:
        result = _post(client, "tools/call", {"name": "list_portfolios", "arguments": {}},
                       headers={**HEADERS, "Authorization": "Bearer " + _jwt(signing_key)})
        assert result.json()["result"]["structuredContent"]["portfolio_ids"] == ["portfolio-alpha"]


def test_diagnostics_cannot_overlap_source_configuration_or_canonical_stores(tmp_path):
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    inside = state / "diagnostics"; inside.mkdir(mode=0o700)
    config = state / "config.json"; config.write_text("{}")
    for root, protected in [(state, [state]), (inside, [state]), (state, [config])]:
        with pytest.raises(ValueError):
            private_transport_diagnostics(root, protected, set())
    separate = tmp_path / "diagnostics"; separate.mkdir(mode=0o700)
    log = private_transport_diagnostics(separate, [state, config], set())
    assert not log.path.exists()  # --check-config must not write an event.


def test_authorization_diagnostics_distinguish_missing_expired_and_wrong_audience_without_data(tmp_path, signing_key):
    root = tmp_path / "diagnostics"; root.mkdir(mode=0o700)
    events = TransportDiagnostics(root, set(READ_TOOLS))
    with TestClient(create_http_app(_gateway(), _verifier(signing_key), diagnostics=events),
                    base_url="https://mcp.example.test") as client:
        cases = [
            (None, None),
            (_jwt(signing_key, exp=int(time.time()) - 1), "expired"),
            (_jwt(signing_key, aud="https://private-audience.example.invalid/mcp"), "wrong_audience"),
            (_jwt(signing_key, iss="https://private-issuer.example.invalid/"), "wrong_issuer"),
            (_jwt(signing_key, iat=int(time.time()) + 3600), "not_yet_valid"),
            ("private-malformed-token", "malformed"),
            (_jwt(signing_key), "accepted"),
        ]
        for token, expected in cases:
            start = len(events.path.read_text().splitlines()) if events.path.exists() else 0
            headers = {**HEADERS, **({"Authorization": "Bearer " + token} if token else {})}
            response = _post(client, "tools/list", headers=headers)
            assert response.status_code == (200 if expected == "accepted" else 401)
            rows = [json.loads(line) for line in events.path.read_text().splitlines()[start:]]
            http = [row for row in rows if row["event"] == "http"]
            assert http[-1]["has_bearer_token"] is (token is not None)
            reasons = [row["outcome"] for row in rows if row["event"] == "authorization"]
            assert reasons == ([expected] if token else [])
            if expected != "accepted" and expected is not None:
                assert expected not in response.text and expected not in str(response.headers)
    text = events.path.read_text()
    for private in ["private-audience", "private-issuer", "private-malformed-token",
                    "fixture-reader", "gemini-fixture", *[token for token, _ in cases if token]]:
        assert private not in text
    events.authorization("private-unknown-reason")
    assert json.loads(events.path.read_text().splitlines()[-1])["outcome"] == "other"


def test_authorization_observer_failure_cannot_change_validation(tmp_path, signing_key, monkeypatch):
    root = tmp_path / "diagnostics"; root.mkdir(mode=0o700)
    events = TransportDiagnostics(root, set(READ_TOOLS))
    def broken_observer(outcome):
        raise RuntimeError("private-sink-failure")
    monkeypatch.setattr(events, "authorization", broken_observer)
    with TestClient(create_http_app(_gateway(), _verifier(signing_key), diagnostics=events),
                    base_url="https://mcp.example.test") as client:
        for token, status in [(_jwt(signing_key), 200), (_jwt(signing_key, exp=1), 401)]:
            response = _post(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer " + token})
            assert response.status_code == status
            assert "private-sink-failure" not in response.text
