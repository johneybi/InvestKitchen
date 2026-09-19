from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.deployment.secure_tunnel import (  # noqa: E402
    launcher_argv,
    run_preflight,
    tunnel_init_argv,
)
from protocol.v1.deployment.tunnel_stdio_launcher import sanitized_environment  # noqa: E402
from protocol.v1.runtime.reference_composition import _provider_child_env  # noqa: E402


def test_tunnel_child_environment_scrubs_control_plane_and_unrelated_secrets() -> None:
    source = {
        "CONTROL_PLANE_API_KEY": "control-secret",
        "OPENAI_API_KEY": "openai-secret",
        "TOSSINVEST_CLIENT_ID": "market-id",
        "TOSSINVEST_CLIENT_SECRET": "market-secret",
        "HTTPS_PROXY": "https://proxy-secret@example.test",
        "LANG": "C.UTF-8",
        "TZ": "Asia/Seoul",
    }
    child = sanitized_environment(source)
    assert child == {"LANG": "C.UTF-8", "TZ": "Asia/Seoul"}


def test_reference_wiring_contains_no_secret_values_or_repo_secret_file() -> None:
    wiring = json.loads(
        (PROTOCOL / "deployment" / "self-hosted-readonly.wiring.json").read_text(encoding="utf-8")
    )
    encoded = json.dumps(wiring, ensure_ascii=False)
    assert wiring["secure_tunnel"]["control_plane_secret_env"] == "CONTROL_PLANE_API_KEY"
    assert wiring["secure_tunnel"]["control_plane_secret_forwarded_to_mcp"] is False
    assert wiring["market_binding"]["secret_delivery"] == "runtime secret file"
    assert "client_secret\":" not in encoded
    assert "sk-" not in encoded


def test_provider_subprocess_environment_does_not_inherit_tunnel_or_market_secrets(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "control-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("TOSSINVEST_CLIENT_ID", "market-id")
    monkeypatch.setenv("TOSSINVEST_CLIENT_SECRET", "market-secret")
    monkeypatch.setenv("LANG", "C.UTF-8")
    child = _provider_child_env()
    assert child.get("LANG") == "C.UTF-8"
    assert "CONTROL_PLANE_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    assert "TOSSINVEST_CLIENT_ID" not in child
    assert "TOSSINVEST_CLIENT_SECRET" not in child
    assert "HTTPS_PROXY" not in child


def test_tunnel_init_argv_contains_mcp_command_but_no_control_plane_key() -> None:
    command = launcher_argv(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
    )
    rendered_mcp = " ".join(command)
    init = tunnel_init_argv(
        tunnel_id="tunnel_0123456789abcdef0123456789abcdef",
        profile="trademind-readonly",
        mcp_command=rendered_mcp,
    )
    encoded = " ".join(init)
    assert "tunnel_0123456789abcdef0123456789abcdef" in encoded
    assert "tunnel_stdio_launcher.py" in encoded
    assert "CONTROL_PLANE_API_KEY" not in encoded
    assert "control-secret" not in encoded


def test_tunnel_init_argv_rejects_noncanonical_tunnel_id() -> None:
    with pytest.raises(ValueError, match="tunnel_<32 lowercase hexadecimal characters>"):
        tunnel_init_argv(
            tunnel_id="tunnel_fixture_123",
            profile="trademind-readonly",
            mcp_command="python server.py",
        )


def test_tunnel_init_argv_rejects_nonhex_lowercase_characters() -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        tunnel_init_argv(
            tunnel_id="tunnel_0123456789abcdef0123456789abcdeg",
            profile="trademind-readonly",
            mcp_command="python server.py",
        )


def test_launcher_argv_carries_explicit_checkpoint_and_native_store_roots() -> None:
    native_root = ROOT / "fixture-native-write"
    checkpoint_root = ROOT / "fixture-checkpoints"
    command = launcher_argv(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        native_store_root=native_root,
        portfolio_checkpoint_root=checkpoint_root,
    )
    assert "--native-store-root" in command
    assert str(native_root) in command
    assert "--portfolio-checkpoint-root" in command
    assert str(checkpoint_root) in command


def test_launcher_argv_carries_optional_connector_write_authority_files() -> None:
    approval_root = ROOT / "fixture-approvals"
    principal = ROOT / "fixture-write-principal.json"
    grant = ROOT / "fixture-write-grant.json"
    command = launcher_argv(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        native_store_root=ROOT / "fixture-native-write",
        portfolio_checkpoint_root=ROOT / "fixture-checkpoints",
        approval_store_root=approval_root,
        write_principal=principal,
        write_grant=grant,
    )
    assert command[command.index("--approval-store-root") + 1] == str(approval_root)
    assert command[command.index("--write-principal") + 1] == str(principal)
    assert command[command.index("--write-grant") + 1] == str(grant)


def test_local_tunnel_preflight_executes_scrub_launcher_and_mcp_server() -> None:
    command = launcher_argv(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
    )
    result = run_preflight(command)
    assert result["ok"] is True
    assert result["supported_versions"] == ["2026-07-28"]
    assert "get_portfolio_state" not in result["tools"]
    assert "start_reflection" in result["tools"]
    assert "get_market_quote" not in result["tools"]


def test_market_provider_binding_can_be_discovered_without_network_call(tmp_path: Path) -> None:
    secret = tmp_path / "market-secret.json"
    secret.write_text(
        json.dumps({"client_id": "fixture-client", "client_secret": "fixture-secret"}),
        encoding="utf-8",
    )
    command = launcher_argv(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        legacy_workspace=ROOT,
        market_provider="toss-readonly-subprocess",
        market_secret_file=secret,
    )
    result = run_preflight(command)
    assert result["ok"] is True
    assert "get_market_quote" in result["tools"]
    assert "get_market_ohlcv" in result["tools"]
    encoded = json.dumps(result)
    assert "fixture-secret" not in encoded
    assert "fixture-client" not in encoded


def test_native_market_provider_binding_needs_no_legacy_workspace(tmp_path: Path) -> None:
    secret = tmp_path / "market-secret.json"
    secret.write_text(
        json.dumps({"client_id": "fixture-client", "client_secret": "fixture-secret"}),
        encoding="utf-8",
    )
    command = launcher_argv(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures" / "full-reference.instance.json",
        market_provider="toss-native-subprocess",
        market_secret_file=secret,
    )
    result = run_preflight(command)
    assert result["ok"] is True
    assert "get_market_quote" in result["tools"]
    assert "get_market_ohlcv" in result["tools"]
    assert "--legacy-workspace" not in command


def test_mcp_selected_market_provider_requires_secret_file() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROTOCOL / "transport" / "mcp_stdio.py"),
            "--market-provider",
            "toss-readonly-subprocess",
        ],
        cwd=ROOT,
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "TradeMind MCP configuration error\n"


def test_provider_worker_rejects_unknown_capability_without_reading_secret_or_network() -> None:
    worker = PROTOCOL / "providers" / "toss_readonly_worker.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(worker),
            "--provider-workspace",
            str(ROOT),
            "--secret-file",
            str(ROOT / "does-not-exist.json"),
        ],
        cwd=ROOT,
        input=json.dumps({"capability": "market.unsupported", "input": {}}),
        text=True,
        capture_output=True,
        check=True,
        env={"LANG": os.environ.get("LANG", "C.UTF-8")},
    )
    assert completed.stderr == ""
    value = json.loads(completed.stdout)
    assert value["status"] == "unavailable"
    assert value["gaps"][0]["gap_code"] == "market_capability_unsupported"
