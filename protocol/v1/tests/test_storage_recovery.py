from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
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

from protocol.v1.runtime.native_write_store import NativeWriteStore, apply_mutation  # noqa: E402
from protocol.v1.runtime.native_knowledge_store import (  # noqa: E402
    NativeKnowledgeStore,
    apply_knowledge_commit,
    build_knowledge_preview,
)
from protocol.v1.runtime.portfolio_checkpoint import (  # noqa: E402
    PortfolioCheckpointStore,
    build_checkpoint,
    persist_checkpoint,
)
from protocol.v1.runtime.storage_recovery import (  # noqa: E402
    BackupIntegrityError,
    RestoreRefused,
    StorageLayout,
    create_backup,
    restore_backup,
    verify_backup,
)
from protocol.v1.runtime.write_preview import build_mutation_preview  # noqa: E402
from protocol.v1.security.trusted_approval import TrustedApprovalStore  # noqa: E402


NOW = datetime(2026, 9, 16, 0, 30, 0, tzinfo=timezone.utc)


def _tp(value: str) -> dict[str, str]:
    return {"value": value, "precision": "source_exact"}


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def _validate(value: Any, schema_name: str) -> None:
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(value)


def _principal() -> dict[str, Any]:
    return {
        "subject_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "authentication_event_id": "auth:fixture",
        "authenticated_at": _tp("2026-09-16T00:00:00Z"),
        "expires_at": _tp("2026-09-16T02:00:00Z"),
    }


def _grant() -> dict[str, Any]:
    return {
        "grant_id": "grant:backup-fixture",
        "instance_id": "fixture-full-reference",
        "subject_user_id": "user:alpha",
        "client_id": "client:web-gpt",
        "credential_binding_id": "credential:fixture",
        "permissions": ["operation.approve", "decision.create", "transaction.record", "knowledge.commit"],
        "portfolio_scope": ["portfolio-alpha"],
        "tool_allowlist": [],
        "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
        "policy_version": "backup-fixture-v1",
        "issued_at": _tp("2026-09-16T00:00:00Z"),
        "expires_at": _tp("2026-09-16T02:00:00Z"),
    }


def _decision_request() -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "request_type": "decision.create",
        "portfolio_id": "portfolio-alpha",
        "account_ids": ["account-alpha"],
        "subject_refs": ["000660"],
        "statement": "185 회복 시 재매수를 검토한다.",
        "action_intent": {
            "action": "buy",
            "asset_ref": "000660",
            "quantity": {"value": 3, "unit": "shares"},
            "notes": None,
        },
        "conditions": ["185 회복"],
        "invalidation_conditions": ["회복 실패"],
        "rationale_summary": None,
        "source_context_ref": None,
        "decided_at": _tp("2026-09-16T00:10:00Z"),
        "authority_basis": "explicit_user_decision",
    }


def _portfolio_snapshot() -> dict[str, Any]:
    return {
        "protocol_version": "1.0-draft",
        "portfolio_id": "portfolio-alpha",
        "display_name": "Synthetic Portfolio",
        "generated_at": _tp("2026-09-16T00:20:00Z"),
        "accounts": [{
            "account_id": "account-alpha",
            "portfolio_id": "portfolio-alpha",
            "display_name": "Synthetic Account",
            "provider_id": None,
            "account_type": "general",
            "base_currency": "KRW",
            "role": "core",
            "status": "active",
            "constraints": [],
        }],
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
            "accounts": [{
                "account_id": "account-alpha",
                "holdings": "complete",
                "cash": "complete",
                "observed_at": _tp("2026-09-16T00:20:00Z"),
            }],
        },
        "migration_gaps": [],
    }


def _seed_state(state_root: Path) -> dict[str, Any]:
    preview = build_mutation_preview(_decision_request(), now=NOW)
    approval_store = TrustedApprovalStore(state_root / "approvals")
    approval = approval_store.issue(
        preview,
        principal=_principal(),
        grant=_grant(),
        interaction_ref="interaction:backup-fixture",
        approval_method="test_surface",
        now=NOW,
    )
    write_store = NativeWriteStore(state_root / "native-write")
    return apply_mutation(
        preview,
        approval,
        principal=_principal(),
        grant=_grant(),
        approval_verifier=approval_store.verify,
        store=write_store,
        now=NOW,
    )


def _seed_checkpoint(state_root: Path, write_result: dict[str, Any]) -> dict[str, Any]:
    write_store = NativeWriteStore(state_root / "native-write")
    checkpoint = build_checkpoint(
        _portfolio_snapshot(),
        write_store,
        snapshot_effective_at=_tp("2026-09-16T00:20:00Z"),
        through_commit_id=str(write_result["receipt"]["applied_version"]),
        source_kind="manual_snapshot",
        source_ref="snapshot:backup-fixture",
        created_at=_tp("2026-09-16T00:21:00Z"),
    )
    store = PortfolioCheckpointStore(state_root / "portfolio-checkpoints")
    persist_checkpoint(store, write_store, checkpoint)
    return checkpoint


def _knowledge_request() -> dict[str, Any]:
    generated = _tp("2026-09-16T00:25:00Z")
    return {
        "protocol_version": "1.0-draft",
        "request_type": "knowledge.commit",
        "generation_id": "knowledge:backup-fixture",
        "generated_at": generated,
        "current_state": {
            "as_of": generated,
            "freshness_status": "current",
            "situational_usable": True,
            "summary": "Synthetic native Knowledge backup fixture.",
            "outlook": [],
        },
        "evidence": [{
            "evidence_id": "evidence:backup-fixture",
            "evidence_kind": "fixture",
            "subject_refs": ["000660"],
            "source": {
                "source_type": "fixture",
                "provider_or_publisher": "tests",
            },
            "authority_class": "user_supplied",
            "verification": "verified",
            "freshness": "current",
            "recorded_at": generated,
            "lifecycle_scope": "canonical",
        }],
        "claims": [{
            "claim_id": "claim:backup-fixture",
            "statement": "Synthetic native Knowledge survives backup and restore.",
            "subject_refs": ["000660"],
            "claim_type": "fixture",
            "conditions": [],
            "invalidation": [],
            "evidence_refs": ["evidence:backup-fixture"],
            "derivation_type": "direct_statement",
            "registration_state": "canonical",
            "provenance_verification": "verified",
            "semantic_fidelity": "direct",
            "truth_status": "observed_fact",
            "applicability_status": "applicable",
        }],
    }


def _seed_knowledge(state_root: Path) -> dict[str, Any]:
    store = NativeKnowledgeStore(state_root / "native-write")
    preview = build_knowledge_preview(
        _knowledge_request(),
        base_generation_id=store.latest_generation_id(),
        now=NOW,
    )
    approval_store = TrustedApprovalStore(state_root / "approvals")
    approval = approval_store.issue(
        preview,
        principal=_principal(),
        grant=_grant(),
        interaction_ref="interaction:knowledge-backup-fixture",
        approval_method="test_surface",
        now=NOW,
    )
    return apply_knowledge_commit(
        preview,
        approval,
        principal=_principal(),
        grant=_grant(),
        approval_verifier=approval_store.verify,
        store=store,
        now=NOW,
    )


def test_storage_layout_fixture_is_host_neutral_with_synology_reference_only() -> None:
    value = json.loads((PROTOCOL / "deployment" / "self-hosted-storage.layout.json").read_text(encoding="utf-8"))
    _validate(value, "storage-layout.schema.json")
    assert value["state_root_env"] == "TRADEMIND_V1_STATE_ROOT"
    assert value["backup_root_env"] == "TRADEMIND_V1_BACKUP_ROOT"
    assert value["relative_paths"]["native_write_journal"] == "native-write/write-journal.jsonl"
    assert value["relative_paths"]["native_knowledge_journal"] == "native-write/knowledge-journal.jsonl"
    assert value["relative_paths"]["portfolio_checkpoint_journal"] == "portfolio-checkpoints/checkpoints.jsonl"
    assert value["backup_policy"]["consistency"] == "triple_journal_locked_snapshot"
    assert value["backup_policy"]["overwrite_nonempty_target"] is False
    assert value["backup_policy"]["automatic_prune"] is False
    assert value["synology_reference"]["state_root"].startswith("/volume1/")


def test_consistent_backup_manifest_validates_and_excludes_lock_files(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = _seed_state(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    manifest = json.loads((snapshot / "backup-manifest.json").read_text(encoding="utf-8"))
    _validate(manifest, "backup-manifest.schema.json")
    verified = verify_backup(snapshot, expected_instance_id="fixture-full-reference")
    assert verified["ok"] is True
    assert verified["write_records"] == 1
    assert verified["approval_records"] == 1
    assert verified["checkpoint_records"] == 0
    assert verified["knowledge_records"] == 0
    assert verified["backup_format_version"] == 2
    assert not list(snapshot.rglob("*.lock"))
    assert result["resource"]["decision_id"] in (snapshot / "native-write/write-journal.jsonl").read_text(encoding="utf-8")


def test_v2_backup_restores_checkpoint_journal_and_cursor_linkage(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_result = _seed_state(state)
    checkpoint = _seed_checkpoint(state, write_result)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    manifest = json.loads((snapshot / "backup-manifest.json").read_text(encoding="utf-8"))
    _validate(manifest, "backup-manifest.schema.json")
    assert manifest["backup_format_version"] == 2
    assert manifest["consistency"] == "triple_journal_locked_snapshot"
    assert {row["logical_name"] for row in manifest["files"]} == {
        "native_write_journal",
        "native_knowledge_journal",
        "approval_journal",
        "portfolio_checkpoint_journal",
    }
    verified = verify_backup(snapshot, expected_instance_id="fixture-full-reference")
    assert verified["checkpoint_records"] == 1

    target = tmp_path / "restored-v2"
    restored = restore_backup(snapshot, target, expected_instance_id="fixture-full-reference")
    assert restored["backup_format_version"] == 2
    restored_checkpoint = PortfolioCheckpointStore(target / "portfolio-checkpoints").latest("portfolio-alpha")
    assert restored_checkpoint["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert restored_checkpoint["write_cursor"] == checkpoint["write_cursor"]


def test_v2_backup_restores_native_knowledge_journal_with_existing_triple_semantics(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_result = _seed_state(state)
    _seed_checkpoint(state, write_result)
    knowledge = _seed_knowledge(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    manifest = json.loads((snapshot / "backup-manifest.json").read_text(encoding="utf-8"))
    _validate(manifest, "backup-manifest.schema.json")
    assert manifest["backup_format_version"] == 2
    assert manifest["consistency"] == "triple_journal_locked_snapshot"
    assert {row["logical_name"] for row in manifest["files"]} == {
        "native_write_journal",
        "native_knowledge_journal",
        "approval_journal",
        "portfolio_checkpoint_journal",
    }
    verified = verify_backup(snapshot, expected_instance_id="fixture-full-reference")
    assert verified["knowledge_records"] == 1
    assert verified["write_records"] == 1
    assert verified["approval_records"] == 2
    assert verified["checkpoint_records"] == 1

    target = tmp_path / "restored-with-knowledge"
    restored = restore_backup(snapshot, target, expected_instance_id="fixture-full-reference")
    assert restored["knowledge_records"] == 1
    restored_knowledge = NativeKnowledgeStore(target / "native-write")
    assert restored_knowledge.latest_generation_id() == knowledge["generation_id"]
    assert restored_knowledge.overlay()["claims"][0]["claim_id"] == "claim:backup-fixture"


def test_pre_knowledge_v2_snapshot_remains_verifiable_and_restorable(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_result = _seed_state(state)
    _seed_checkpoint(state, write_result)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    knowledge_path = snapshot / "native-write/knowledge-journal.jsonl"
    knowledge_path.unlink()
    manifest_path = snapshot / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["layout"].pop("native_knowledge_journal", None)
    manifest["files"] = [
        row for row in manifest["files"] if row["logical_name"] != "native_knowledge_journal"
    ]
    manifest["creation_checks"] = [
        value for value in manifest["creation_checks"] if not value.startswith("knowledge_")
    ]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    _validate(manifest, "backup-manifest.schema.json")
    verified = verify_backup(snapshot, expected_instance_id="fixture-full-reference")
    assert verified["backup_format_version"] == 2
    assert verified["knowledge_records"] == 0
    target = tmp_path / "restored-old-v2"
    restore_backup(snapshot, target, expected_instance_id="fixture-full-reference")
    assert not (target / "native-write/knowledge-journal.jsonl").exists()


def test_checkpoint_write_cursor_tamper_is_detected_even_with_rehashed_manifest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_result = _seed_state(state)
    _seed_checkpoint(state, write_result)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    write_path = snapshot / "native-write/write-journal.jsonl"
    row = json.loads(write_path.read_text(encoding="utf-8").strip())
    row["committed_at"] = _tp("2026-09-16T00:29:00Z")
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    write_path.write_bytes(encoded)
    manifest_path = snapshot / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    write_manifest = next(row for row in manifest["files"] if row["logical_name"] == "native_write_journal")
    write_manifest["sha256"] = hashlib.sha256(encoded).hexdigest()
    write_manifest["size_bytes"] = len(encoded)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(BackupIntegrityError, match="checkpoint_cursor_prefix_mismatch"):
        verify_backup(snapshot, expected_instance_id="fixture-full-reference")


def test_v2_backup_missing_checkpoint_file_is_not_treated_as_empty(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_state(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    (snapshot / "portfolio-checkpoints/checkpoints.jsonl").unlink()
    with pytest.raises(BackupIntegrityError, match="backup_file_missing"):
        verify_backup(snapshot, expected_instance_id="fixture-full-reference")


def test_legacy_v1_snapshot_remains_verifiable_and_restorable(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_state(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    checkpoint_path = snapshot / "portfolio-checkpoints/checkpoints.jsonl"
    checkpoint_path.unlink()
    checkpoint_path.parent.rmdir()
    (snapshot / "native-write/knowledge-journal.jsonl").unlink()
    manifest_path = snapshot / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backup_format_version"] = 1
    manifest["consistency"] = "dual_journal_locked_snapshot"
    manifest["layout"].pop("portfolio_checkpoint_journal", None)
    manifest["layout"].pop("native_knowledge_journal", None)
    manifest["files"] = [
        row for row in manifest["files"]
        if row["logical_name"] not in {"portfolio_checkpoint_journal", "native_knowledge_journal"}
    ]
    manifest["creation_checks"] = [
        value for value in manifest["creation_checks"]
        if not value.startswith("checkpoint_") and not value.startswith("knowledge_")
    ]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    _validate(manifest, "backup-manifest.schema.json")
    verified = verify_backup(snapshot, expected_instance_id="fixture-full-reference")
    assert verified["backup_format_version"] == 1
    assert verified["checkpoint_records"] == 0

    target = tmp_path / "restored-v1"
    restored = restore_backup(snapshot, target, expected_instance_id="fixture-full-reference")
    assert restored["backup_format_version"] == 1
    assert not (target / "portfolio-checkpoints/checkpoints.jsonl").exists()
    assert not (target / "native-write/knowledge-journal.jsonl").exists()


def test_knowledge_tamper_is_detected_even_with_rehashed_manifest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_knowledge(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    knowledge_path = snapshot / "native-write/knowledge-journal.jsonl"
    row = json.loads(knowledge_path.read_text(encoding="utf-8").strip())
    row["approval_verification_ref"] = "approval-verification:not-issued"
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    knowledge_path.write_bytes(encoded)
    manifest_path = snapshot / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    knowledge_manifest = next(row for row in manifest["files"] if row["logical_name"] == "native_knowledge_journal")
    knowledge_manifest["sha256"] = hashlib.sha256(encoded).hexdigest()
    knowledge_manifest["size_bytes"] = len(encoded)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(BackupIntegrityError, match="knowledge_missing_trusted_approval"):
        verify_backup(snapshot, expected_instance_id="fixture-full-reference")


def test_backup_tamper_is_detected_before_restore(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_state(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    journal = snapshot / "native-write/write-journal.jsonl"
    journal.write_text(journal.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(BackupIntegrityError, match="backup_file_digest_mismatch"):
        verify_backup(snapshot, expected_instance_id="fixture-full-reference")


def test_backup_missing_file_is_not_treated_as_an_empty_journal(tmp_path: Path) -> None:
    state = tmp_path / "empty-state"
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    (snapshot / "approvals/approval-journal.jsonl").unlink()
    with pytest.raises(BackupIntegrityError, match="backup_file_missing"):
        verify_backup(snapshot, expected_instance_id="fixture-full-reference")


def test_restore_requires_matching_instance_and_empty_target_then_revalidates(tmp_path: Path) -> None:
    state = tmp_path / "state"
    source = _seed_state(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    with pytest.raises(BackupIntegrityError, match="backup_instance_mismatch"):
        restore_backup(snapshot, tmp_path / "wrong-instance", expected_instance_id="other-instance")

    target = tmp_path / "restored"
    restored = restore_backup(snapshot, target, expected_instance_id="fixture-full-reference")
    assert restored["restored"] is True
    resources = NativeWriteStore(target / "native-write").list_resources("decision")
    assert resources == [source["resource"]]
    approvals = TrustedApprovalStore(target / "approvals").read_journal()
    assert len(approvals) == 1
    assert verify_backup(snapshot, expected_instance_id="fixture-full-reference")["ok"] is True


def test_restore_refuses_nonempty_target_without_destructive_overwrite(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_state(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    target = tmp_path / "live-state"
    target.mkdir()
    sentinel = target / "do-not-overwrite.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(RestoreRefused, match="restore_target_not_empty"):
        restore_backup(snapshot, target, expected_instance_id="fixture-full-reference")
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_cross_journal_approval_linkage_failure_is_detected_even_with_rehashed_manifest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_state(state)
    snapshot = create_backup(
        StorageLayout(state_root=state, backup_root=tmp_path / "backups", instance_id="fixture-full-reference"),
        now=NOW,
    )
    approval_path = snapshot / "approvals/approval-journal.jsonl"
    approval_path.write_bytes(b"")
    manifest_path = snapshot / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    approval_row = next(row for row in manifest["files"] if row["logical_name"] == "approval_journal")
    approval_row["sha256"] = hashlib.sha256(b"").hexdigest()
    approval_row["size_bytes"] = 0
    approval_row["jsonl_records"] = 0
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(BackupIntegrityError, match="write_missing_trusted_approval"):
        verify_backup(snapshot, expected_instance_id="fixture-full-reference")
