from __future__ import annotations

import json
import stat

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
