"""Atomic native Plan revisions, with an immutable imported baseline.

The single native snapshot contains both projection and complete history. A
locked compare-and-swap and atomic rename publish them together; readers never
create directories or lock files, including on read-only mounts.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from protocol.v1.adapters.common import canonical_json, digest


class AdvisoryPlanStore:
    def __init__(self, root: Path, *, read_only: bool = False) -> None:
        self.root = root.expanduser().resolve()
        self.read_only = read_only
        self.manifest_path = self.root / "manifest.json"
        self.plans_path = self.root / "plans.json"
        self.native_path = self.root / "native-state.json"

    def exists(self) -> bool:
        return self.native_path.is_file() or self.manifest_path.is_file() or self.plans_path.is_file()

    @staticmethod
    def _validate_rows(rows: Any) -> None:
        if not isinstance(rows, list):
            raise ValueError("advisory plan rows invalid")
        identities: set[str] = set()
        active: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("advisory plan row invalid")
            plan_id, portfolio_id = row.get("plan_id"), row.get("portfolio_id")
            if not isinstance(plan_id, str) or not plan_id or plan_id in identities:
                raise ValueError("advisory plan identity invalid")
            identities.add(plan_id)
            if not isinstance(portfolio_id, str) or not portfolio_id:
                raise ValueError("advisory plan scope invalid")
            if row.get("status") not in {"active", "superseded", "closed"}:
                raise ValueError("advisory plan status invalid")
            if (row.get("order_authorized") is not False or row.get("automatic_execution") is not False
                    or row.get("requires_current_portfolio_and_market_revalidation") is not True
                    or row.get("trigger_is_not_execution_authority") is not True):
                raise ValueError("advisory plan execution authority invalid")
            if not isinstance(row.get("stages"), list):
                raise ValueError("advisory plan stages invalid")
            if row["status"] == "active":
                if portfolio_id in active:
                    raise ValueError("advisory plan active scope ambiguous")
                active.add(portfolio_id)

    def snapshot(self) -> dict[str, Any]:
        if self.native_path.is_file():
            state = json.loads(self.native_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("schema_version") != 2:
                raise ValueError("advisory plan native state invalid")
            expected = state.get("state_digest")
            material = {key: value for key, value in state.items() if key != "state_digest"}
            if expected != digest(material) or not isinstance(state.get("history"), list):
                raise ValueError("advisory plan state digest invalid")
            self._validate_rows(state.get("plans"))
            previous = None
            for index, event in enumerate(state["history"], 1):
                if (not isinstance(event, dict) or event.get("revision") != index
                        or event.get("previous_event_digest") != previous
                        or event.get("event_digest") != digest({k: v for k, v in event.items() if k != "event_digest"})):
                    raise ValueError("advisory plan history invalid")
                previous = event["event_digest"]
            if state["history"] and state["history"][-1].get("plans_digest") != digest(state["plans"]):
                raise ValueError("advisory plan projection mismatch")
            return state
        if self.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            raw = self.plans_path.read_bytes()
            if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                    or manifest.get("plans_sha256") != hashlib.sha256(raw).hexdigest()):
                raise ValueError("advisory plan manifest invalid")
            rows = json.loads(raw)
        else:
            rows = []
        self._validate_rows(rows)
        state = {"schema_version": 2, "plans": rows, "history": []}
        return {**state, "state_digest": digest(state)}

    def validate(self) -> dict[str, Any]:
        state = self.snapshot()
        if not self.native_path.is_file() and self.manifest_path.is_file():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return {"schema_version": 2, "summary": {"plans": len(state["plans"]), "revisions": len(state["history"])}}

    def list_plans(self) -> list[dict[str, Any]]:
        return self.snapshot()["plans"]

    def replace_plans(self, rows: list[dict[str, Any]], *, expected_digest: str, event: dict[str, Any]) -> str:
        if self.read_only:
            raise ValueError("plan_store_read_only")
        self._validate_rows(rows)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.root / ".plan-store.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                current = self.snapshot()
                if any(row.get("preview_id") == event["preview_id"] for row in current["history"]):
                    raise ValueError("plan_preview_replayed")
                if current["state_digest"] != expected_digest:
                    raise ValueError("plan_preview_stale")
                history = current["history"]
                revision = {
                    **copy.deepcopy(event), "revision": len(history) + 1,
                    "previous_event_digest": history[-1]["event_digest"] if history else None,
                    "plans_digest": digest(rows),
                }
                revision["event_digest"] = digest(revision)
                state = {"schema_version": 2, "plans": rows, "history": [*history, revision]}
                state["state_digest"] = digest(state)
                self._publish(state)
                return state["state_digest"]
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _publish(self, state: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".plan-state-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(state) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.native_path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
