"""Private briefing records, guarded by server-issued per-record approval.

No MCP write endpoint is exposed here. An authenticated transport must bind
principal, grant, preview and trusted approval before enabling this service.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from protocol.v1.adapters.common import canonical_json, digest, timepoint
from protocol.v1.security.trusted_approval import TrustedApprovalStore


def _point(value):
    return timepoint(value.astimezone(timezone.utc).isoformat())


def _parse(value):
    dt = datetime.fromisoformat(value["value"].replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(timezone.utc)


def preview_briefing(portfolio_id: str, run_id: str, result: dict, *, now: datetime | None = None) -> dict:
    if not isinstance(portfolio_id, str) or not 1 <= len(portfolio_id) <= 100 or not isinstance(run_id, str) or not 1 <= len(run_id) <= 120 or not isinstance(result, dict):
        raise ValueError("invalid briefing")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("timezone required")
    payload = {"request_type": "briefing.record", "portfolio_id": portfolio_id, "run_id": run_id,
               "result": result, "authority": "advice_record_only", "execution_authorized": False}
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 64000:
        raise ValueError("briefing too large")
    target = {"portfolio_id": portfolio_id}
    return {"operation": {"action": "briefing.record", "target": target, "state": "awaiting_approval"},
            "approval_required": True, "apply_state": "not_applied",
            "preview": {"preview_id": "briefing-preview:" + uuid.uuid4().hex,
                        "operation_id": "briefing-op:" + uuid.uuid4().hex,
                        "action": "briefing.record", "target": target, "base_version": None,
                        "canonical_payload": payload, "payload_digest": digest(payload),
                        "expires_at": _point(current + timedelta(minutes=5))}}


def apply_briefing(root: Path, preview: dict, approval: dict, *, principal: dict, grant: dict,
                   approval_store: TrustedApprovalStore, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    try:
        op, mutation = preview["operation"], preview["preview"]
        payload, target = mutation["canonical_payload"], mutation["target"]
        valid = (preview.get("approval_required") is True and preview.get("apply_state") == "not_applied"
                 and op.get("state") == "awaiting_approval" and op.get("action") == mutation.get("action") == payload.get("request_type") == "briefing.record"
                 and op.get("target") == target == approval.get("target") == {"portfolio_id": payload.get("portfolio_id")}
                 and mutation.get("payload_digest") == digest(payload) == approval.get("approved_payload_digest")
                 and mutation.get("preview_id") == approval.get("preview_id")
                 and mutation.get("base_version") is None and approval.get("base_version") is None
                 and payload.get("authority") == "advice_record_only" and payload.get("execution_authorized") is False
                 and isinstance(payload.get("run_id"), str) and 1 <= len(payload["run_id"]) <= 120
                 and isinstance(payload.get("result"), dict)
                 and len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) <= 64000
                 and all(principal.get(k) and principal[k] == grant.get(k) for k in ("subject_user_id", "client_id", "credential_binding_id"))
                 and approval.get("authenticated_user_id") == principal.get("subject_user_id")
                 and approval.get("client_id") == principal.get("client_id")
                 and "briefing.record" in grant.get("permissions", [])
                 and payload["portfolio_id"] in grant.get("portfolio_scope", [])
                 and _parse(principal["expires_at"]) > current and _parse(grant["expires_at"]) > current
                 and _parse(mutation["expires_at"]) > current
                 and _parse(approval["approved_at"]) <= current < _parse(approval["expires_at"])
                 and approval_store.verify(approval, principal, grant).get("verified") is True)
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise PermissionError("briefing authorization failed")
    root = Path(root).resolve()
    repo = Path(__file__).resolve().parents[3]
    if root == repo or root.is_relative_to(repo) or repo.is_relative_to(root):
        raise ValueError("briefing history must be outside product repository")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = digest([payload["portfolio_id"], payload["run_id"]])
    path = root / (key + ".json")
    lock = os.open(root / ".briefing.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError("invalid briefing history")
        if path.exists():
            existing = json.loads(path.read_text())
            if existing.get("payload_digest") != digest(payload):
                raise ValueError("briefing run id conflict")
            return {"status": "already_recorded", "record_id": "briefing:" + key[:24]}
        content = canonical_json({"payload_digest": digest(payload), "recorded_at": _point(current), "payload": payload})
        fd, temporary = tempfile.mkstemp(prefix=".briefing-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"status": "recorded", "record_id": "briefing:" + key[:24]}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def latest_briefing(root: Path, portfolio_id: str, *, principal: dict, grant: dict,
                    now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    try:
        allowed = (all(principal.get(k) and principal[k] == grant.get(k) for k in
                       ("subject_user_id", "client_id", "credential_binding_id"))
                   and "briefing.read" in grant.get("permissions", [])
                   and portfolio_id in grant.get("portfolio_scope", [])
                   and _parse(principal["expires_at"]) > current and _parse(grant["expires_at"]) > current)
    except (KeyError, TypeError, ValueError, AttributeError):
        allowed = False
    if not allowed:
        raise PermissionError("briefing read authorization failed")
    root = Path(root).resolve()
    repo = Path(__file__).resolve().parents[3]
    if root == repo or root.is_relative_to(repo) or repo.is_relative_to(root):
        raise ValueError("briefing history must be outside product repository")
    if not root.exists():
        return {"status": "missing", "record": None}
    files = list(root.glob("*.json"))
    if len(files) > 1000:
        raise ValueError("briefing history exceeds scan bound")
    rows = []
    for path in files:
        if path.is_symlink():
            raise ValueError("invalid briefing history")
        row = json.loads(path.read_text(encoding="utf-8"))
        payload = row.get("payload", {})
        if row.get("payload_digest") != digest(payload):
            raise ValueError("invalid briefing history")
        if payload.get("portfolio_id") == portfolio_id:
            rows.append(row)
    if not rows:
        return {"status": "missing", "record": None}
    row = max(rows, key=lambda value: (_parse(value["recorded_at"]), value["payload"]["run_id"]))
    return {"status": "ok", "record": {"recorded_at": row["recorded_at"], "result": row["payload"]["result"],
                                       "authority": "advice_record_only"}}
