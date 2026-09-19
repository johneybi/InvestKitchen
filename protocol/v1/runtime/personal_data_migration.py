from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, timepoint
from protocol.v1.adapters.knowledge_legacy import get_current_knowledge
from protocol.v1.runtime.personal_data_store import MANIFEST_NAME, PersonalDataStore


class MigrationError(RuntimeError):
    pass


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _load_projector(runtime_root: Path):
    path = runtime_root / "protocol" / "v1" / "tools" / "project_legacy_records.py"
    spec = importlib.util.spec_from_file_location("trademind_personal_migration_projector", path)
    if spec is None or spec.loader is None:
        raise MigrationError("legacy_projector_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_private_json(path: Path, value: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    return encoded


def _file_row(logical_name: str, relative_path: Path, content: bytes, content_type: str, portfolio_id: str | None = None) -> dict[str, Any]:
    return {
        "logical_name": logical_name,
        "relative_path": relative_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "content_type": content_type,
        "portfolio_id": portfolio_id,
    }


def export_legacy_bundle(
    *,
    runtime_root: Path,
    legacy_workspace: Path,
    output_dir: Path,
    portfolio_ids: list[str],
    now: datetime | None = None,
) -> Path:
    if not portfolio_ids or len(set(portfolio_ids)) != len(portfolio_ids):
        raise MigrationError("portfolio_ids_invalid")
    runtime_root = runtime_root.resolve()
    legacy_workspace = legacy_workspace.resolve()
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists() and next(output_dir.iterdir(), None) is not None:
        raise MigrationError("migration_output_not_empty")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    projector = _load_projector(runtime_root)
    current = _utc_now(now)
    files: list[dict[str, Any]] = []
    try:
        for portfolio_id in portfolio_ids:
            value = projector.project_portfolio(legacy_workspace, portfolio_id)
            relative = Path("portfolios") / f"{portfolio_id}.json"
            content = _write_private_json(output_dir / relative, value)
            files.append(_file_row(f"portfolio:{portfolio_id}", relative, content, "portfolio", portfolio_id))

        knowledge_projection = projector.project_knowledge(legacy_workspace)
        projection_rel = Path("knowledge/evidence-claims.json")
        projection_bytes = _write_private_json(output_dir / projection_rel, knowledge_projection)
        files.append(_file_row("knowledge:projection", projection_rel, projection_bytes, "knowledge_projection"))

        knowledge_current = get_current_knowledge(legacy_workspace, max_outlook=1000000)
        current_rel = Path("knowledge/current.json")
        current_bytes = _write_private_json(output_dir / current_rel, knowledge_current)
        files.append(_file_row("knowledge:current", current_rel, current_bytes, "knowledge_current"))

        bundle_id = f"personal-data:{current.strftime('%Y%m%dT%H%M%SZ')}:{uuid.uuid4().hex[:12]}"
        manifest = {
            "protocol_version": PROTOCOL_VERSION,
            "bundle_format_version": 1,
            "bundle_id": bundle_id,
            "created_at": timepoint(current.isoformat().replace("+00:00", "Z")),
            "source_kind": "legacy_migration",
            "portfolio_ids": list(portfolio_ids),
            "files": files,
        }
        _write_private_json(output_dir / MANIFEST_NAME, manifest)
        PersonalDataStore(output_dir).verify()
        return output_dir
    except Exception:
        if output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        raise


def install_bundle(bundle_dir: Path, target_root: Path) -> dict[str, Any]:
    bundle = PersonalDataStore(bundle_dir.expanduser().resolve())
    verification = bundle.verify()
    target = target_root.expanduser().resolve()
    if target.exists() and next(target.iterdir(), None) is not None:
        raise MigrationError("personal_data_target_not_empty")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = target.parent / f".personal-install-{target.name}-{uuid.uuid4().hex}"
    shutil.copytree(bundle.resolved_root(), staging, symlinks=False)
    try:
        PersonalDataStore(staging).verify()
        for path in staging.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        if target.exists():
            target.rmdir()
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**verification, "installed": True, "target_root": str(target)}
