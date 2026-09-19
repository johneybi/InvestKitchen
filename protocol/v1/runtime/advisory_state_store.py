from __future__ import annotations

import hashlib
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest


STORE_SCHEMA_VERSION = 1



def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _private_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)



class OpenItemStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.manifest_path = self.root / "manifest.json"
        self.items_path = self.root / "items.json"

    def exists(self) -> bool:
        return self.manifest_path.is_file() and self.items_path.is_file()

    def validate(self) -> dict[str, Any]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        items = json.loads(self.items_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != STORE_SCHEMA_VERSION:
            raise ValueError("open item manifest invalid")
        if not isinstance(items, list):
            raise ValueError("open item rows invalid")
        ids = [str(row.get("item_id") or "") for row in items if isinstance(row, dict)]
        if len(ids) != len(items) or not all(ids) or len(ids) != len(set(ids)):
            raise ValueError("open item identities invalid")
        summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
        if summary.get("items") != len(items):
            raise ValueError("open item count mismatch")
        if manifest.get("items_sha256") != _sha256_file(self.items_path):
            raise ValueError("open item digest mismatch")
        return manifest

    def list_items(self) -> list[dict[str, Any]]:
        self.validate()
        value = json.loads(self.items_path.read_text(encoding="utf-8"))
        return [dict(row) for row in value]


class AdvisoryPolicyStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.manifest_path = self.root / "manifest.json"
        self.policies_path = self.root / "policies.json"

    def exists(self) -> bool:
        return self.manifest_path.is_file() and self.policies_path.is_file()

    @property
    def lock_path(self) -> Path:
        return self.root / ".policy-store.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _validate_rows(policies: Any) -> list[dict[str, Any]]:
        if not isinstance(policies, list) or not policies:
            raise ValueError("advisory policy rows invalid")
        rows = [dict(row) for row in policies if isinstance(row, dict)]
        if len(rows) != len(policies):
            raise ValueError("advisory policy rows invalid")
        ids = [str(row.get("policy_id") or "") for row in rows]
        if not all(ids) or len(ids) != len(set(ids)):
            raise ValueError("advisory policy identities invalid")
        active_by_portfolio: dict[str, int] = {}
        for row in rows:
            portfolio_id = str(row.get("portfolio_id") or "")
            if not portfolio_id or row.get("authority") != "user_policy_record":
                raise ValueError("advisory policy scope invalid")
            if row.get("status") not in {"active", "superseded", "inactive"}:
                raise ValueError("advisory policy status invalid")
            if not isinstance(row.get("rules"), dict):
                raise ValueError("advisory policy rules invalid")
            if row.get("order_authorized") is not False:
                raise ValueError("advisory policy execution authority invalid")
            if row.get("status") == "active":
                active_by_portfolio[portfolio_id] = active_by_portfolio.get(portfolio_id, 0) + 1
        if any(count != 1 for count in active_by_portfolio.values()):
            raise ValueError("advisory policy active scope ambiguous")
        return rows

    def validate(self) -> dict[str, Any]:
        with self._locked(exclusive=False):
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            policies = json.loads(self.policies_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != STORE_SCHEMA_VERSION:
                raise ValueError("advisory policy manifest invalid")
            rows = self._validate_rows(policies)
            if manifest.get("policies_sha256") != _sha256_file(self.policies_path):
                raise ValueError("advisory policy digest mismatch")
            summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
            if summary.get("policies") not in (None, len(rows)):
                raise ValueError("advisory policy count mismatch")
            return manifest

    def list_policies(self) -> list[dict[str, Any]]:
        self.validate()
        with self._locked(exclusive=False):
            return [dict(row) for row in json.loads(self.policies_path.read_text(encoding="utf-8"))]

    def current(self, portfolio_id: str) -> dict[str, Any] | None:
        matches = [
            row for row in self.list_policies()
            if row.get("portfolio_id") == portfolio_id and row.get("status") == "active"
        ]
        if len(matches) > 1:
            raise ValueError("advisory policy active scope ambiguous")
        return dict(matches[0]) if matches else None

    def revision_digest(self) -> str:
        self.validate()
        return _sha256_file(self.policies_path)

    def replace_policies(
        self,
        policies: list[dict[str, Any]],
        *,
        expected_digest: str,
        mutation: dict[str, Any],
    ) -> str:
        rows = self._validate_rows(policies)
        with self._locked(exclusive=True):
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if _sha256_file(self.policies_path) != expected_digest:
                raise ValueError("advisory policy base version conflict")
            _private_write(self.policies_path, rows)
            new_digest = _sha256_file(self.policies_path)
            active_counts: dict[str, int] = {}
            for row in rows:
                if row.get("status") == "active":
                    pid = str(row["portfolio_id"])
                    active_counts[pid] = active_counts.get(pid, 0) + 1
            next_manifest = dict(manifest)
            next_manifest["summary"] = {
                "policies": len(rows),
                "active_by_portfolio": active_counts,
            }
            next_manifest["policies_sha256"] = new_digest
            history = list(next_manifest.get("mutation_history") or [])
            history.append(dict(mutation))
            next_manifest["mutation_history"] = history[-100:]
            _private_write(self.manifest_path, next_manifest)
        self.validate()
        return new_digest


class AdvisoryPlanStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.manifest_path = self.root / "manifest.json"
        self.plans_path = self.root / "plans.json"

    def exists(self) -> bool:
        return self.manifest_path.is_file() and self.plans_path.is_file()

    def validate(self) -> dict[str, Any]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        plans = json.loads(self.plans_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != STORE_SCHEMA_VERSION:
            raise ValueError("advisory plan manifest invalid")
        if not isinstance(plans, list):
            raise ValueError("advisory plan rows invalid")
        ids = [str(row.get("plan_id") or "") for row in plans if isinstance(row, dict)]
        if len(ids) != len(plans) or not all(ids) or len(ids) != len(set(ids)):
            raise ValueError("advisory plan identities invalid")
        if any(not str(row.get("portfolio_id") or "") for row in plans):
            raise ValueError("advisory plan scope invalid")
        if any(row.get("order_authorized") is not False for row in plans):
            raise ValueError("advisory plan execution authority invalid")
        if manifest.get("plans_sha256") != _sha256_file(self.plans_path):
            raise ValueError("advisory plan digest mismatch")
        summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
        if summary.get("plans") not in (None, len(plans)):
            raise ValueError("advisory plan count mismatch")
        return manifest

    def list_plans(self) -> list[dict[str, Any]]:
        self.validate()
        return [dict(row) for row in json.loads(self.plans_path.read_text(encoding="utf-8"))]



def validate_advisory_state(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    result: dict[str, Any] = {}
    for name, store in (
        ("open_items", OpenItemStore(root / "open-items")),
        ("policies", AdvisoryPolicyStore(root / "policies")),
        ("plans", AdvisoryPlanStore(root / "plans")),
    ):
        if store.exists():
            result[name] = store.validate().get("summary")
    return result
