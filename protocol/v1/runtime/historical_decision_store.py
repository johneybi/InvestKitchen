from __future__ import annotations

import hashlib
import json
import os
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


def _tp(raw: Any) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("historical decision timepoint missing")
    return {"value": text, "precision": "source_exact" if "T" in text else "date_only"}


def _opaque_source_ref(relative_path: str, line_number: int, source_sha256: str) -> str:
    return "evidence-ref:" + digest([relative_path, line_number, source_sha256])[:24]


def _statement(row: dict[str, Any]) -> str:
    if isinstance(row.get("rule"), str) and row["rule"].strip():
        return row["rule"].strip()
    if str(row.get("status") or "") == "not_adopted":
        user_position = str(row.get("user_position") or "").strip()
        if user_position:
            return user_position
    for key in ("user_position", "proposal_reference", "note"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("historical decision statement missing")


def _historical_status(raw_status: Any) -> str:
    return "not_adopted" if str(raw_status or "") == "not_adopted" else "historical"


def _canonical_decision(
    row: dict[str, Any], *, portfolio_id: str, relative_path: str, line_number: int, source_sha256: str
) -> dict[str, Any]:
    decision_id = str(row.get("decision_id") or "").strip()
    if not decision_id:
        raise ValueError("historical decision_id missing")
    source_ref = _opaque_source_ref(relative_path, line_number, source_sha256)
    scope = str(row.get("scope") or "historical_decision").strip()
    execution_effect = str(row.get("execution_effect") or "Historical record only; no execution is authorized.")
    note = str(row.get("note") or "").strip() or None
    proposal = str(row.get("proposal_reference") or "").strip()
    user_position = str(row.get("user_position") or "").strip()
    rationale_parts = [part for part in (note, proposal, user_position) if part]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "decision_id": decision_id,
        "portfolio_id": portfolio_id,
        "account_ids": [],
        "subject_refs": [f"scope:{scope}"],
        "statement": _statement(row),
        "action_intent": {
            "action": "other",
            "asset_ref": None,
            "quantity": None,
            "notes": execution_effect,
        },
        "conditions": [],
        "invalidation_conditions": [],
        "rationale_summary": " ".join(rationale_parts) if rationale_parts else None,
        "source_context_ref": source_ref,
        "authority": "historical_migration",
        "status": _historical_status(row.get("status")),
        "historical_status": str(row.get("status") or "unknown"),
        "supersedes": None,
        "decided_at": _tp(row.get("decision_date")),
        "recorded_at": _tp(row.get("recorded_on") or row.get("decision_date")),
    }


class HistoricalDecisionStore:
    """Immutable decision-history store excluded from current Decision authority."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.manifest_path = self.root / "manifest.json"
        self.decisions_path = self.root / "decisions.jsonl"

    def exists(self) -> bool:
        return self.manifest_path.is_file() and self.decisions_path.is_file()

    def validate(self) -> dict[str, Any]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != STORE_SCHEMA_VERSION:
            raise ValueError("historical decision manifest invalid")
        decisions = self.list_decisions(validate_first=False)
        summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
        if summary.get("decisions") != len(decisions):
            raise ValueError("historical decision count mismatch")
        if manifest.get("decisions_sha256") != _sha256_file(self.decisions_path):
            raise ValueError("historical decision digest mismatch")
        return manifest

    def list_decisions(self, *, validate_first: bool = True) -> list[dict[str, Any]]:
        if validate_first:
            self.validate()
        rows: list[dict[str, Any]] = []
        ids: set[str] = set()
        for raw in self.decisions_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            wrapper = json.loads(raw)
            if not isinstance(wrapper, dict) or set(wrapper) != {"store_version", "decision", "migration_source"}:
                raise ValueError("historical decision row invalid")
            decision = wrapper.get("decision")
            if wrapper.get("store_version") != STORE_SCHEMA_VERSION or not isinstance(decision, dict):
                raise ValueError("historical decision row invalid")
            decision_id = str(decision.get("decision_id") or "")
            if not decision_id or decision_id in ids:
                raise ValueError("historical decision ids missing or duplicated")
            ids.add(decision_id)
            rows.append(decision)
        return rows



def validate_historical_decision_store(store_root: Path) -> dict[str, Any]:
    manifest = HistoricalDecisionStore(store_root).validate()
    return dict(manifest.get("summary") or {})
