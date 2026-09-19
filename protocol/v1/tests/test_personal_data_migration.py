from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import canonical_json, result_envelope, timepoint  # noqa: E402
from protocol.v1.adapters.native_personal import (  # noqa: E402
    get_current_knowledge,
    get_portfolio_state,
    search_knowledge,
)
from protocol.v1.runtime.personal_data_migration import export_legacy_bundle, install_bundle  # noqa: E402
from protocol.v1.runtime.personal_data_store import PersonalDataIntegrityError, PersonalDataStore  # noqa: E402
from protocol.v1.runtime.reference_composition import build_reference_gateway  # noqa: E402
from protocol.v1.transport.mcp_stdio import TradeMindMCPServer, load_tool_catalog  # noqa: E402


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        schema = _load(path)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def _validate(value: Any, schema_name: str) -> None:
    schema = _load(SCHEMAS / schema_name)
    jsonschema.Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(value)


def _write_json(path: Path, value: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def _synthetic_bundle(root: Path) -> Path:
    generated = timepoint("2026-09-16T00:00:00Z")
    portfolio = {
        "protocol_version": "1.0-draft",
        "portfolio_id": "portfolio-alpha",
        "display_name": "Synthetic Portfolio",
        "generated_at": generated,
        "accounts": [],
        "positions": [],
        "cash": [],
        "transactions": [],
        "policies": [],
        "completeness": {
            "holdings": "complete",
            "cash": "complete",
            "valuation": "unknown",
            "fx": "unknown",
            "transactions": "complete",
            "accounts": [],
        },
        "migration_gaps": [],
    }
    current = result_envelope(
        capability="knowledge.current",
        producer="fixture.migration",
        status="ok",
        data={
            "generation": {
                "generation_id": "generation-alpha",
                "commit_status": "committed",
                "committed_at": generated,
            },
            "summary": "Synthetic knowledge",
            "outlook": [
                {"thesis_id": "thesis-a", "status": "active"},
                {"thesis_id": "thesis-b", "status": "active"},
            ],
        },
        authority="knowledge_claim",
        generated_at=generated,
        freshness="local",
        source_mode="local_store",
        permissions_used=["knowledge.read"],
    )
    projection = {
        "protocol_version": "1.0-draft",
        "generated_at": generated,
        "knowledge_generation": {
            "generation_id": "generation-alpha",
            "commit_status": "committed",
            "committed_at": generated,
            "registry_or_input_digest": None,
            "artifact_refs": [],
        },
        "evidence": [{
            "evidence_id": "evidence-alpha",
            "evidence_kind": "fixture",
            "subject_refs": ["asset-alpha"],
            "source": {"source_type": "fixture", "provider_or_publisher": "tests"},
            "authority_class": "user_supplied",
            "recorded_at": generated,
            "verification": "workflow_verified",
            "freshness": "current",
            "lifecycle_scope": "canonical",
        }],
        "claims": [{
            "claim_id": "claim-alpha",
            "statement": "Asset Alpha has a synthetic thesis.",
            "subject_refs": ["asset-alpha"],
            "speaker": "fixture",
            "claim_type": "outlook",
            "stance": "watch",
            "horizon": "short_term",
            "conditions": [],
            "invalidation": [],
            "effective_at": generated,
            "evidence_refs": ["evidence-alpha"],
            "derivation_type": "direct_statement",
            "registration_state": "canonical",
            "provenance_verification": "verified",
            "semantic_fidelity": "direct",
            "truth_status": "attributed_opinion",
            "applicability_status": "applicable",
            "confidence": "medium",
        }],
        "migration_gaps": [],
    }

    files = []
    for logical_name, relative, value, content_type, portfolio_id in (
        ("portfolio:portfolio-alpha", Path("portfolios/portfolio-alpha.json"), portfolio, "portfolio", "portfolio-alpha"),
        ("knowledge:current", Path("knowledge/current.json"), current, "knowledge_current", None),
        ("knowledge:projection", Path("knowledge/evidence-claims.json"), projection, "knowledge_projection", None),
    ):
        encoded = _write_json(root / relative, value)
        files.append({
            "logical_name": logical_name,
            "relative_path": relative.as_posix(),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
            "content_type": content_type,
            "portfolio_id": portfolio_id,
        })
    manifest = {
        "protocol_version": "1.0-draft",
        "bundle_format_version": 1,
        "bundle_id": "personal-data:fixture",
        "created_at": generated,
        "source_kind": "native_export",
        "portfolio_ids": ["portfolio-alpha"],
        "files": files,
    }
    _write_json(root / "personal-data-manifest.json", manifest)
    return root


def _mcp_meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "personal-data-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def test_personal_data_bundle_validates_and_native_adapters_read_without_legacy_workspace(tmp_path: Path) -> None:
    root = _synthetic_bundle(tmp_path / "personal")
    store = PersonalDataStore(root)
    verification = store.verify()
    assert verification["portfolio_ids"] == ["portfolio-alpha"]
    _validate(_load(root / "personal-data-manifest.json"), "personal-data-manifest.schema.json")

    portfolio = get_portfolio_state(store, "portfolio-alpha")
    knowledge = get_current_knowledge(store, max_outlook=1)
    search = search_knowledge(store, "Asset Alpha", limit=10)
    for result in (portfolio, knowledge, search):
        _validate(result, "capability-result.schema.json")
    _validate(portfolio["data"], "portfolio.schema.json")
    assert knowledge["status"] == "partial"
    assert knowledge["data"]["outlook"] == [{"thesis_id": "thesis-a", "status": "active"}]
    assert search["data"]["results"][0]["claim_id"] == "claim-alpha"


def test_personal_data_digest_tamper_fails_closed(tmp_path: Path) -> None:
    root = _synthetic_bundle(tmp_path / "personal")
    path = root / "portfolios/portfolio-alpha.json"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(PersonalDataIntegrityError, match="personal_data_file_digest_mismatch"):
        PersonalDataStore(root).verify()


def test_install_bundle_requires_empty_target_and_revalidates(tmp_path: Path) -> None:
    source = _synthetic_bundle(tmp_path / "bundle")
    target = tmp_path / "installed"
    result = install_bundle(source, target)
    assert result["installed"] is True
    assert PersonalDataStore(target).verify()["ok"] is True
    assert not list(target.rglob("*.lock"))


def test_reference_gateway_prefers_native_personal_data_over_legacy_binding(tmp_path: Path) -> None:
    personal = _synthetic_bundle(tmp_path / "personal")
    gateway = build_reference_gateway(
        runtime_root=ROOT,
        manifest=PROTOCOL / "fixtures/full-reference.instance.json",
        legacy_workspace=None,
        personal_data_root=personal,
    )
    server = TradeMindMCPServer(gateway, catalog=load_tool_catalog())
    listed = server.handle_request({"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {"_meta": _mcp_meta()}})
    assert listed is not None
    names = {row["name"] for row in listed["result"]["tools"]}
    assert {"get_portfolio_state", "get_current_knowledge", "search_knowledge"}.issubset(names)
    called = server.handle_request({
        "jsonrpc": "2.0",
        "id": "portfolio",
        "method": "tools/call",
        "params": {
            "_meta": _mcp_meta(),
            "name": "get_portfolio_state",
            "arguments": {"portfolio_id": "portfolio-alpha"},
        },
    })
    assert called is not None
    assert called["result"]["structuredContent"]["data"]["portfolio_id"] == "portfolio-alpha"
    encoded = json.dumps(called, ensure_ascii=False)
    assert "legacy:" not in encoded
    assert "accounts/" not in encoded


@pytest.mark.skipif(not os.environ.get("TRADEMIND_LEGACY_WORKSPACE"), reason="legacy workspace not configured")
def test_real_legacy_export_is_read_only_and_verifiable(tmp_path: Path) -> None:
    ids = [value for value in (
        os.environ.get("TRADEMIND_LEGACY_PORTFOLIO_PRIMARY"),
        os.environ.get("TRADEMIND_LEGACY_PORTFOLIO_SECONDARY"),
    ) if value]
    if not ids:
        pytest.skip("legacy portfolio IDs not configured")
    output = export_legacy_bundle(
        runtime_root=ROOT,
        legacy_workspace=Path(os.environ["TRADEMIND_LEGACY_WORKSPACE"]),
        output_dir=tmp_path / "export",
        portfolio_ids=ids,
    )
    result = PersonalDataStore(output).verify()
    assert result["portfolio_ids"] == sorted(ids)
