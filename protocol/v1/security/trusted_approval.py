from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint


_ACTION_PERMISSION = {
    "decision.create": "decision.create",
    "transaction.record": "transaction.record",
    "knowledge.commit": "knowledge.commit",
}


class ApprovalRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ApprovalStoreCorrupt(RuntimeError):
    pass


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_timepoint(value: Any) -> datetime:
    raw = value.get("value") if isinstance(value, dict) else None
    if not isinstance(raw, str):
        raise ApprovalRejected("invalid_timepoint")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ApprovalRejected("invalid_timepoint")
    return parsed.astimezone(timezone.utc)


class TrustedApprovalStore:
    """Server-owned append-only evidence for trusted ApprovalReceipt issuance.

    The receipt is not trusted because a client can construct matching JSON. It
    is trusted only when the exact receipt digest exists as an active issuance
    event in this server-owned journal and identity binding still matches.
    """

    def __init__(self, root: Path, *, authority_id: str = "approval-authority:local-v1") -> None:
        self.root = root.resolve()
        self.authority_id = authority_id
        self.journal_path = self.root / "approval-journal.jsonl"
        self.lock_path = self.root / ".approval-journal.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with os.fdopen(fd, "r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            raise

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ApprovalStoreCorrupt(f"invalid approval journal line {line_number}") from exc
                if not isinstance(value, dict):
                    raise ApprovalStoreCorrupt(f"invalid approval journal line {line_number}")
                rows.append(value)
        return rows

    def read_journal(self) -> list[dict[str, Any]]:
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def _append_unlocked(self, row: dict[str, Any]) -> None:
        encoded = (canonical_json(row) + "\n").encode("utf-8")
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _validate_identity(principal: dict[str, Any], grant: dict[str, Any]) -> None:
        for field in ("subject_user_id", "client_id", "credential_binding_id"):
            if str(principal.get(field) or "") != str(grant.get(field) or ""):
                raise ApprovalRejected("principal_grant_mismatch")

    def issue(
        self,
        preview: dict[str, Any],
        *,
        principal: dict[str, Any],
        grant: dict[str, Any],
        interaction_ref: str,
        approval_method: str,
        now: datetime | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        current = _utc_now(now)
        if not interaction_ref or not approval_method:
            raise ApprovalRejected("approval_interaction_missing")
        if ttl_seconds < 1 or ttl_seconds > 900:
            raise ApprovalRejected("approval_ttl_invalid")
        if not isinstance(preview, dict):
            raise ApprovalRejected("malformed_preview")
        operation = preview.get("operation")
        mutation = preview.get("preview")
        if not isinstance(operation, dict) or not isinstance(mutation, dict):
            raise ApprovalRejected("malformed_preview")
        if preview.get("approval_required") is not True or preview.get("apply_state") != "not_applied":
            raise ApprovalRejected("malformed_preview")
        if operation.get("state") != "awaiting_approval":
            raise ApprovalRejected("operation_not_awaiting_approval")
        action = str(operation.get("action") or "")
        if action not in _ACTION_PERMISSION:
            raise ApprovalRejected("unsupported_action")
        payload = mutation.get("canonical_payload")
        if not isinstance(payload, dict) or mutation.get("payload_digest") != digest(payload):
            raise ApprovalRejected("payload_digest_invalid")

        self._validate_identity(principal, grant)
        if _parse_timepoint(principal.get("expires_at")) <= current:
            raise ApprovalRejected("principal_expired")
        if _parse_timepoint(grant.get("expires_at")) <= current:
            raise ApprovalRejected("grant_expired")
        preview_expires = _parse_timepoint(mutation.get("expires_at"))
        if preview_expires <= current:
            raise ApprovalRejected("preview_expired")

        permissions = {str(value) for value in grant.get("permissions") or []}
        if "operation.approve" not in permissions:
            raise ApprovalRejected("approval_permission_denied")
        if _ACTION_PERMISSION[action] not in permissions:
            raise ApprovalRejected("action_permission_denied")
        target = mutation.get("target")
        if not isinstance(target, dict) or canonical_json(target) != canonical_json(operation.get("target")):
            raise ApprovalRejected("target_mismatch")
        portfolio_id = target.get("portfolio_id")
        if portfolio_id is not None and str(portfolio_id) not in {str(value) for value in grant.get("portfolio_scope") or []}:
            raise ApprovalRejected("portfolio_scope_denied")

        expires = min(current + timedelta(seconds=ttl_seconds), preview_expires, _parse_timepoint(grant["expires_at"]))
        approval = {
            "approval_id": f"approval:{uuid.uuid4().hex}",
            "preview_id": mutation["preview_id"],
            "authenticated_user_id": principal["subject_user_id"],
            "client_id": principal["client_id"],
            "approved_payload_digest": mutation["payload_digest"],
            "target": target,
            "base_version": mutation.get("base_version"),
            "approved_at": timepoint(current.isoformat().replace("+00:00", "Z")),
            "expires_at": timepoint(expires.isoformat().replace("+00:00", "Z")),
            "approval_method": approval_method,
        }
        row = {
            "protocol_version": PROTOCOL_VERSION,
            "event_type": "approval_issued",
            "authority_id": self.authority_id,
            "approval_id": approval["approval_id"],
            "approval_digest": digest(approval),
            "approval": approval,
            "subject_user_id": principal["subject_user_id"],
            "client_id": principal["client_id"],
            "credential_binding_id": principal["credential_binding_id"],
            "grant_id": grant["grant_id"],
            "interaction_ref": interaction_ref,
            "revocation_reason": None,
            "occurred_at": timepoint(current.isoformat().replace("+00:00", "Z")),
        }
        with self._locked(exclusive=True):
            if any(existing.get("approval_id") == approval["approval_id"] for existing in self._read_unlocked()):
                raise ApprovalRejected("approval_id_conflict")
            self._append_unlocked(row)
        return approval

    def revoke(
        self,
        approval_id: str,
        *,
        principal: dict[str, Any],
        grant: dict[str, Any],
        reason: str,
        now: datetime | None = None,
    ) -> None:
        current = _utc_now(now)
        self._validate_identity(principal, grant)
        if "operation.approve" not in {str(value) for value in grant.get("permissions") or []}:
            raise ApprovalRejected("approval_permission_denied")
        if not reason:
            raise ApprovalRejected("revocation_reason_missing")
        with self._locked(exclusive=True):
            rows = self._read_unlocked()
            issued = next((row for row in rows if row.get("event_type") == "approval_issued" and row.get("approval_id") == approval_id), None)
            if issued is None:
                raise ApprovalRejected("approval_not_found")
            if any(row.get("event_type") == "approval_revoked" and row.get("approval_id") == approval_id for row in rows):
                return
            revoke_row = {
                "protocol_version": PROTOCOL_VERSION,
                "event_type": "approval_revoked",
                "authority_id": self.authority_id,
                "approval_id": approval_id,
                "approval_digest": issued["approval_digest"],
                "approval": None,
                "subject_user_id": principal["subject_user_id"],
                "client_id": principal["client_id"],
                "credential_binding_id": principal["credential_binding_id"],
                "grant_id": grant["grant_id"],
                "interaction_ref": str(issued["interaction_ref"]),
                "revocation_reason": reason,
                "occurred_at": timepoint(current.isoformat().replace("+00:00", "Z")),
            }
            self._append_unlocked(revoke_row)

    def verify(self, approval: dict[str, Any], principal: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(approval, dict):
            return {"verified": False, "verification_ref": None}
        try:
            self._validate_identity(principal, grant)
        except ApprovalRejected:
            return {"verified": False, "verification_ref": None}
        approval_id = str(approval.get("approval_id") or "")
        approval_digest = digest(approval)
        rows = self.read_journal()
        issued = next(
            (
                row for row in rows
                if row.get("event_type") == "approval_issued"
                and row.get("authority_id") == self.authority_id
                and row.get("approval_id") == approval_id
            ),
            None,
        )
        if issued is None or issued.get("approval_digest") != approval_digest:
            return {"verified": False, "verification_ref": None}
        if canonical_json(issued.get("approval")) != canonical_json(approval):
            return {"verified": False, "verification_ref": None}
        if str(issued.get("subject_user_id")) != str(principal.get("subject_user_id")):
            return {"verified": False, "verification_ref": None}
        if str(issued.get("client_id")) != str(principal.get("client_id")):
            return {"verified": False, "verification_ref": None}
        if str(issued.get("credential_binding_id")) != str(principal.get("credential_binding_id")):
            return {"verified": False, "verification_ref": None}
        if any(row.get("event_type") == "approval_revoked" and row.get("approval_id") == approval_id for row in rows):
            return {"verified": False, "verification_ref": None}
        return {
            "verified": True,
            "verification_ref": f"approval-verification:{digest([self.authority_id, approval_id, approval_digest])[:24]}",
        }
