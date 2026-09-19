from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol" / "v1"
SCHEMAS = PROTOCOL / "schemas"
GENERATED = PROTOCOL / "fixtures" / "generated"
LEGACY_WORKSPACE = Path(os.environ["TRADEMIND_LEGACY_WORKSPACE"]).resolve() if os.environ.get("TRADEMIND_LEGACY_WORKSPACE") else None
LEGACY_PORTFOLIO_PRIMARY = os.environ.get("TRADEMIND_LEGACY_PORTFOLIO_PRIMARY")
LEGACY_PORTFOLIO_SECONDARY = os.environ.get("TRADEMIND_LEGACY_PORTFOLIO_SECONDARY")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        schema = _load(path)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def _validate(instance_path: Path, schema_name: str) -> None:
    schema = _load(SCHEMAS / schema_name)
    jsonschema.Draft202012Validator(
        schema,
        registry=_schema_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(
        _load(instance_path)
    )


def _load_projector():
    path = PROTOCOL / "tools" / "project_legacy_records.py"
    spec = importlib.util.spec_from_file_location("project_legacy_records", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_static_instance_manifests_validate() -> None:
    _validate(PROTOCOL / "fixtures" / "reflection-only.instance.json", "instance-manifest.schema.json")
    _validate(PROTOCOL / "fixtures" / "full-reference.instance.json", "instance-manifest.schema.json")


def test_read_only_projection_validates_and_does_not_mutate_canonical(tmp_path: Path) -> None:
    if LEGACY_WORKSPACE is None or not LEGACY_PORTFOLIO_PRIMARY or not LEGACY_PORTFOLIO_SECONDARY:
        pytest.skip("set legacy workspace and portfolio IDs for compatibility integration")
    projector = _load_projector()
    watched = [
        LEGACY_WORKSPACE / "accounts" / LEGACY_PORTFOLIO_PRIMARY / "positions.json",
        LEGACY_WORKSPACE / "accounts" / LEGACY_PORTFOLIO_SECONDARY / "positions.json",
        LEGACY_WORKSPACE / "knowledge" / "events" / "claims.jsonl",
        LEGACY_WORKSPACE / "knowledge" / "events" / "reconciliation_registry.json",
        LEGACY_WORKSPACE / "tmp" / "answer_packet" / "user-answer-packet.json",
    ]
    before = {path: _digest(path) for path in watched}

    outputs = projector.project_all(
        LEGACY_WORKSPACE,
        tmp_path,
        [LEGACY_PORTFOLIO_PRIMARY, LEGACY_PORTFOLIO_SECONDARY],
    )

    assert {path.name for path in outputs} == {
        f"portfolio-{LEGACY_PORTFOLIO_PRIMARY}.json",
        f"portfolio-{LEGACY_PORTFOLIO_SECONDARY}.json",
        "knowledge-projection.json",
        "decision-context-projection.json",
        "monitor-operation-legacy-projection.json",
    }
    _validate(tmp_path / f"portfolio-{LEGACY_PORTFOLIO_PRIMARY}.json", "portfolio.schema.json")
    _validate(tmp_path / f"portfolio-{LEGACY_PORTFOLIO_SECONDARY}.json", "portfolio.schema.json")
    _validate(tmp_path / "knowledge-projection.json", "evidence-claim.schema.json")
    _validate(tmp_path / "decision-context-projection.json", "decision-context.schema.json")
    _validate(tmp_path / "monitor-operation-legacy-projection.json", "operation.schema.json")

    after = {path: _digest(path) for path in watched}
    assert after == before


def test_reflection_only_manifest_has_no_required_decision_or_portfolio_dependency() -> None:
    manifest = _load(PROTOCOL / "fixtures" / "reflection-only.instance.json")
    reflection = manifest["extensions"][0]
    assert reflection["extension_type"] == "reflection"
    assert all(dep["optional"] for dep in reflection["requires"])
    assert "reflection.session" in manifest["capability_bindings"]


def test_generated_portfolio_keeps_internal_transfer_unlinked_when_legacy_has_no_identity(tmp_path: Path) -> None:
    if LEGACY_WORKSPACE is None or not LEGACY_PORTFOLIO_SECONDARY:
        pytest.skip("set legacy workspace and secondary portfolio ID for compatibility integration")
    projector = _load_projector()
    data = projector.project_portfolio(LEGACY_WORKSPACE, LEGACY_PORTFOLIO_SECONDARY)
    transfers = [row for row in data["transactions"] if row["transaction_type"] == "cash_transfer"]
    assert len(transfers) >= 2
    assert all(row["transfer_group_id"] is None for row in transfers)
    assert any(gap["gap_code"] == "legacy_transfer_pair_identity" for gap in data["migration_gaps"])


def test_legacy_verified_knowledge_is_not_promoted_to_truth_verified() -> None:
    if LEGACY_WORKSPACE is None:
        pytest.skip("set TRADEMIND_LEGACY_WORKSPACE for legacy compatibility integration")
    projector = _load_projector()
    data = projector.project_knowledge(LEGACY_WORKSPACE)
    assert data["evidence"]
    assert all(row["verification"] == "workflow_verified" for row in data["evidence"])
    assert all(row["truth_status"] == "attributed_opinion" for row in data["claims"])


def test_monitor_operation_completion_does_not_claim_armed() -> None:
    projector = _load_projector()
    data = projector.project_monitor_operation()
    assert data["receipt"]["state"] == "completed"
    assert data["receipt"]["effect_scope"] == "resource_registered"
    assert data["target_lifecycle"]["registered_state"] == {"monitor": "PENDING_GATE", "gate": "PENDING"}
    assert data["approval"] is None
