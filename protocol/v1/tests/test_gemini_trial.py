from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from protocol.v1.runtime.personal_data_store import PersonalDataStore
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.security.oauth_resource import OAuthResourceConfig

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("gemini_trial", ROOT / "deploy/gemini/trial.py")
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def test_generated_trial_is_synthetic_and_readable_without_writing(tmp_path):
    root = tmp_path / "trial"
    trial.bootstrap(root, "investkitchen-http:verified-commit")
    trial.configure(root, "https://example.test", ["https://client.example.test/callback"])
    config = OAuthResourceConfig.model_validate_json((root / "oauth/oauth-resource.json").read_text())
    assert config.client_id_claim == "azp"
    assert config.grants[0].portfolio_scope == ("portfolio-alpha",)
    assert all("commit" not in p and "create" not in p for p in config.grants[0].permissions)
    realm = json.loads((root / "auth/import/investkitchen-realm.json").read_text())
    client = realm["clients"][0]
    assert client["redirectUris"] == ["https://client.example.test/callback"]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert not client["directAccessGrantsEnabled"] and not client["serviceAccountsEnabled"]
    assert realm["users"][0]["id"] == config.grants[0].subject
    assert (root / "credentials.json").stat().st_mode & 0o777 == 0o600
    store = PersonalDataStore(root / "synthetic/personal")
    assert store.verify()["ok"]
    assert store.portfolio("portfolio-alpha")["positions"] == []
    before = {p.relative_to(root): p.read_bytes() for p in (root / "synthetic").rglob("*") if p.is_file()}
    gateway = build_reference_gateway(runtime_root=ROOT,
        manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
        personal_data_root=root / "synthetic/personal", native_store_root=root / "synthetic/state/native-write",
        read_only_stores=True)
    assert list(gateway.portfolio_ids) == ["portfolio-alpha"]
    after = {p.relative_to(root): p.read_bytes() for p in (root / "synthetic").rglob("*") if p.is_file()}
    assert before == after
    with pytest.raises(ValueError, match="cannot be overwritten"):
        trial.configure(root, "https://another.example.test", ["https://client.example.test/callback"])


@pytest.mark.parametrize("value", ["http://example.test", "https://user:secret@example.test",
    "https://example.test/path", "https://example.test?x=1", "https://example.test/#fragment"])
def test_trial_requires_unambiguous_https_origin(value):
    with pytest.raises(ValueError):
        trial.origin(value)


@pytest.mark.parametrize("value", ["https://*.example.test/callback", "http://example.test/callback",
    "https://client.example.test/callback#fragment", "https://user:secret@client.example.test/callback"])
def test_trial_requires_exact_callback(value):
    with pytest.raises(ValueError):
        trial.redirect(value)


def test_trial_refuses_to_put_generated_credentials_in_git(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        trial.outside_git(tmp_path / "generated")
