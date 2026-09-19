from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"


def test_private_generated_projections_are_not_present_in_repository() -> None:
    generated = PROTOCOL / "fixtures" / "generated"
    assert not generated.exists() or not any(generated.iterdir())


def test_gitignore_blocks_runtime_state_secrets_backups_and_generated_projections() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in (
        "state/",
        "runtime-data/",
        "backups/",
        "secrets/",
        "protocol/v1/fixtures/generated/",
    ):
        assert required in text


def test_reference_wiring_keeps_legacy_workspace_external() -> None:
    wiring = json.loads(
        (PROTOCOL / "deployment" / "self-hosted-readonly.wiring.json").read_text(encoding="utf-8")
    )
    assert wiring["mcp"]["runtime_root"] == "this repository"
    assert "external" in wiring["mcp"]["legacy_workspace"]


def test_repository_contains_no_common_secret_file_names() -> None:
    forbidden_names = {
        "control-plane-api-key",
        "client-secret",
        "credentials.json",
        ".env",
    }
    found = {
        path.name
        for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.parts and path.name in forbidden_names
    }
    assert found == set()
