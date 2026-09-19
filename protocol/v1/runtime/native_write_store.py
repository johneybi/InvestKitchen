from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from protocol.v1.adapters.common import PROTOCOL_VERSION, canonical_json, digest, timepoint


ApprovalVerifier = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]

_WRITE_PERMISSION = {
    "decision.create": "decision.create",
    "transaction.record": "transaction.record",
    "knowledge.commit": "knowledge.commit",
}

_EXECUTION_BASIS = {
    "explicit_user_statement": "explicit_user_confirmation",
    "provider_execution": "provider_fill",
    "official_notice": "official_notice",
    "balance_reconciliation": "balance_reconciliation",
    "correction_evidence": "derived_correction",
}


class ApplyRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StoreConflict(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StoreCorrupt(RuntimeError):
    pass


@dataclass(frozen=True)
class ApplyValidation:
    allowed: bool
    reason_code: str
    required_permission: str | None
    portfolio_id: str | None
    approval_verification_ref: str | None


def _parse_timepoint(value: Any) -> datetime:
    raw = value.get("value") if isinstance(value, dict) else None
    if not isinstance(raw, str):
        raise ValueError("invalid timepoint")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timepoint must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _resource_id(resource_type: str, operation_id: str, payload_digest: str) -> str:
    return f"{resource_type}:{digest([operation_id, payload_digest])[:24]}"


def _approval_result(
    approval_verifier: ApprovalVerifier | None,
    approval: dict[str, Any],
    principal: dict[str, Any],
    grant: dict[str, Any],
) -> tuple[bool, str | None]:
    if approval_verifier is None:
        return False, None
    try:
        result = approval_verifier(approval, principal, grant)
    except Exception:
        return False, None
    if not isinstance(result, dict) or result.get("verified") is not True:
        return False, None
    verification_ref = result.get("verification_ref")
    if not isinstance(verification_ref, str) or not verification_ref:
        return False, None
    return True, verification_ref


def validate_apply(
    preview: dict[str, Any],
    approval: dict[str, Any],
    *,
    principal: dict[str, Any],
    grant: dict[str, Any],
    approval_verifier: ApprovalVerifier | None,
    now: datetime | None = None,
    current_base_version: str | None = None,
) -> ApplyValidation:
    """Validate an apply using only server-side identity/grant/approval verification.

    The approval object is never trusted merely because a caller supplied it.
    A server-side verifier must establish that it came from a trusted approval
    surface before any canonical mutation is eligible to commit.
    """

    current = _utc_now(now)
    required_permission: str | None = None
    portfolio_id: str | None = None
    verification_ref: str | None = None
    try:
        if not isinstance(preview, dict) or not isinstance(approval, dict):
            raise ApplyRejected("malformed_apply")
        operation = preview.get("operation")
        mutation = preview.get("preview")
        if not isinstance(operation, dict) or not isinstance(mutation, dict):
            raise ApplyRejected("malformed_preview")
        if preview.get("approval_required") is not True or preview.get("apply_state") != "not_applied":
            raise ApplyRejected("malformed_preview")
        if operation.get("state") != "awaiting_approval":
            raise ApplyRejected("operation_not_awaiting_approval")

        action = operation.get("action")
        if not isinstance(action, str) or action not in _WRITE_PERMISSION:
            raise ApplyRejected("unsupported_action")
        required_permission = _WRITE_PERMISSION[action]
        payload = mutation.get("canonical_payload")
        if not isinstance(payload, dict) or payload.get("request_type") != action:
            raise ApplyRejected("payload_action_mismatch")
        if mutation.get("action") != action:
            raise ApplyRejected("payload_action_mismatch")
        expected_digest = digest(payload)
        if mutation.get("payload_digest") != expected_digest:
            raise ApplyRejected("payload_digest_invalid")

        operation_target = operation.get("target")
        mutation_target = mutation.get("target")
        approval_target = approval.get("target")
        if not isinstance(operation_target, dict) or not isinstance(mutation_target, dict) or not isinstance(approval_target, dict):
            raise ApplyRejected("target_mismatch")
        if canonical_json(operation_target) != canonical_json(mutation_target):
            raise ApplyRejected("target_mismatch")
        if canonical_json(approval_target) != canonical_json(mutation_target):
            raise ApplyRejected("approval_target_mismatch")

        verified, verification_ref = _approval_result(approval_verifier, approval, principal, grant)
        if not verified:
            raise ApplyRejected("approval_unverified")
        if approval.get("preview_id") != mutation.get("preview_id"):
            raise ApplyRejected("approval_preview_mismatch")
        if approval.get("approved_payload_digest") != expected_digest:
            raise ApplyRejected("approval_payload_mismatch")
        if approval.get("base_version") != mutation.get("base_version"):
            raise ApplyRejected("approval_base_version_mismatch")

        if str(approval.get("authenticated_user_id") or "") != str(principal.get("subject_user_id") or ""):
            raise ApplyRejected("approval_identity_mismatch")
        if str(approval.get("client_id") or "") != str(principal.get("client_id") or ""):
            raise ApplyRejected("approval_identity_mismatch")
        for field in ("subject_user_id", "client_id", "credential_binding_id"):
            if str(principal.get(field) or "") != str(grant.get(field) or ""):
                raise ApplyRejected("principal_grant_mismatch")

        if _parse_timepoint(principal["expires_at"]) <= current:
            raise ApplyRejected("principal_expired")
        if _parse_timepoint(grant["expires_at"]) <= current:
            raise ApplyRejected("grant_expired")
        if _parse_timepoint(mutation["expires_at"]) <= current:
            raise ApplyRejected("preview_expired")
        approved_at = _parse_timepoint(approval["approved_at"])
        approval_expires = _parse_timepoint(approval["expires_at"])
        if approved_at > current or approval_expires <= current or approval_expires <= approved_at:
            raise ApplyRejected("approval_expired")

        base_version = mutation.get("base_version")
        if base_version is not None and current_base_version != base_version:
            raise ApplyRejected("base_version_conflict")

        permissions = {str(value) for value in grant.get("permissions") or []}
        if required_permission not in permissions:
            raise ApplyRejected("permission_denied")
        portfolio_value = mutation_target.get("portfolio_id")
        if portfolio_value is not None:
            portfolio_id = str(portfolio_value)
            portfolio_scope = {str(value) for value in grant.get("portfolio_scope") or []}
            if portfolio_id not in portfolio_scope:
                raise ApplyRejected("portfolio_scope_denied")

    except (KeyError, TypeError, ValueError, ApplyRejected) as exc:
        code = exc.code if isinstance(exc, ApplyRejected) else "malformed_apply"
        return ApplyValidation(False, code, required_permission, portfolio_id, verification_ref)

    return ApplyValidation(True, "allowed", required_permission, portfolio_id, verification_ref)


def _decision_record(payload: dict[str, Any], *, operation_id: str, payload_digest: str, recorded_at: dict[str, Any]) -> dict[str, Any]:
    authority = (
        "explicit_user_decision"
        if payload["authority_basis"] == "explicit_user_decision"
        else "user_approved_assistant_draft"
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "decision_id": _resource_id("decision", operation_id, payload_digest),
        "portfolio_id": payload.get("portfolio_id"),
        "account_ids": list(payload.get("account_ids") or []),
        "subject_refs": list(payload.get("subject_refs") or []),
        "statement": payload["statement"],
        "action_intent": payload["action_intent"],
        "conditions": list(payload.get("conditions") or []),
        "invalidation_conditions": list(payload.get("invalidation_conditions") or []),
        "rationale_summary": payload.get("rationale_summary"),
        "source_context_ref": payload.get("source_context_ref"),
        "authority": authority,
        "status": "active",
        "supersedes": None,
        "decided_at": payload["decided_at"],
        "recorded_at": recorded_at,
    }


def _transaction_record(payload: dict[str, Any], *, operation_id: str, payload_digest: str, recorded_at: dict[str, Any]) -> dict[str, Any]:
    attestation = dict(payload["occurrence_attestation"])
    attestation_ref = f"attestation:{digest(attestation)[:24]}"
    lineage = None
    if payload.get("correction_of") is not None:
        lineage = {"corrects_transaction_id": payload["correction_of"]}
    record: dict[str, Any] = {
        "transaction_id": _resource_id("transaction", operation_id, payload_digest),
        "portfolio_id": payload["portfolio_id"],
        "account_id": payload["account_id"],
        "transaction_type": payload["transaction_type"],
        "side": payload.get("side"),
        "quantity": payload.get("quantity"),
        "price": payload.get("price"),
        "amount": payload.get("amount"),
        "effective_at": payload["effective_at"],
        "recorded_at": recorded_at,
        "occurrence_status": "confirmed",
        "detail_status": payload["detail_status"],
        "execution_basis": _EXECUTION_BASIS[attestation["attestation_type"]],
        "source_type": attestation["attestation_type"],
        "source_evidence": [attestation_ref],
        "provider_execution_id": payload.get("provider_execution_id"),
        "transfer_group_id": payload.get("transfer_group_id"),
        "lineage": lineage,
    }
    if payload.get("asset") is not None:
        record["asset"] = payload["asset"]
    return record


def _transaction_identity(payload: dict[str, Any]) -> str | None:
    provider_execution_id = payload.get("provider_execution_id")
    if provider_execution_id:
        return "provider:" + digest([
            payload.get("portfolio_id"),
            payload.get("account_id"),
            payload.get("transaction_type"),
            provider_execution_id,
        ])
    attestation = payload.get("occurrence_attestation") if isinstance(payload.get("occurrence_attestation"), dict) else {}
    source_ref = attestation.get("source_ref")
    if source_ref:
        return "source:" + digest([
            payload.get("portfolio_id"),
            payload.get("account_id"),
            payload.get("transaction_type"),
            source_ref,
        ])
    statement_digest = attestation.get("statement_digest")
    if statement_digest:
        return "statement:" + digest([
            payload.get("portfolio_id"),
            payload.get("account_id"),
            payload.get("transaction_type"),
            statement_digest,
        ])
    return None


class NativeWriteStore:
    """Append-only reference store for canonical Decision/Transaction commits.

    The caller chooses the root.  There is intentionally no default pointing at
    existing account records, so constructing the store cannot mutate legacy
    canonical sources.  The JSONL backend is a reference persistence mechanism,
    not a final database selection.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.journal_path = self.root / "write-journal.jsonl"
        self.lock_path = self.root / ".write-journal.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with os.fdopen(lock_fd, "r+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            raise

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StoreCorrupt(f"invalid journal line {line_number}") from exc
                if not isinstance(value, dict):
                    raise StoreCorrupt(f"invalid journal line {line_number}")
                entries.append(value)
        return entries

    def read_journal(self) -> list[dict[str, Any]]:
        with self._locked(exclusive=False):
            return self._read_unlocked()

    @staticmethod
    def _journal_commit_index(entries: list[dict[str, Any]]) -> dict[str, int]:
        index: dict[str, int] = {}
        for offset, entry in enumerate(entries):
            commit_id = str(entry.get("commit_id") or "")
            if not commit_id or commit_id in index:
                raise StoreCorrupt("duplicate or missing commit id")
            index[commit_id] = offset
        return index

    def journal_cursor(self, through_commit_id: str | None) -> dict[str, Any]:
        """Return a stable digest/cursor for the journal prefix through one commit.

        `through_commit_id=None` means the checkpoint predates all native-write
        events and therefore covers an empty journal prefix.
        """

        entries = self.read_journal()
        commit_index = self._journal_commit_index(entries)
        if through_commit_id is None:
            prefix: list[dict[str, Any]] = []
        else:
            if through_commit_id not in commit_index:
                raise StoreConflict("journal_cursor_not_found")
            prefix = entries[: commit_index[through_commit_id] + 1]
        return {
            "through_commit_id": through_commit_id,
            "event_count": len(prefix),
            "prefix_digest": digest(prefix),
        }

    def transactions_after_cursor(
        self,
        *,
        portfolio_id: str,
        through_commit_id: str | None,
    ) -> dict[str, Any]:
        """Return canonical Transaction resources committed after a journal cursor.

        Decision commits and operation-deduplicated journal entries are not
        transaction deltas. The result preserves append order; the Portfolio
        materializer later applies its explicit effective-time ordering rule.
        """

        entries = self.read_journal()
        commit_index = self._journal_commit_index(entries)
        if through_commit_id is None:
            start = 0
        else:
            if through_commit_id not in commit_index:
                raise StoreConflict("journal_cursor_not_found")
            start = commit_index[through_commit_id] + 1

        transactions: list[dict[str, Any]] = []
        transaction_commit_ids: list[str] = []
        for entry in entries[start:]:
            if entry.get("event_type") != "resource_commit" or entry.get("resource_type") != "transaction":
                continue
            resource = entry.get("resource")
            if not isinstance(resource, dict):
                raise StoreCorrupt("resource commit missing resource")
            transaction_id = str(resource.get("transaction_id") or "")
            if not transaction_id or str(entry.get("resource_ref") or "") != transaction_id:
                raise StoreCorrupt("transaction resource identity mismatch")
            resource_digest = entry.get("resource_digest")
            if resource_digest is not None and resource_digest != digest(resource):
                raise StoreCorrupt("transaction resource digest mismatch")
            if resource.get("portfolio_id") != portfolio_id:
                continue
            transactions.append(resource)
            transaction_commit_ids.append(str(entry.get("commit_id") or ""))

        tail_commit_id = str(entries[-1].get("commit_id") or "") if entries else None
        if tail_commit_id == "":
            tail_commit_id = None
        return {
            "from_commit_id": through_commit_id,
            "through_commit_id": tail_commit_id,
            "journal_event_count": len(entries),
            "transaction_commit_ids": transaction_commit_ids,
            "transactions": transactions,
        }

    def transactions_between_cursors(
        self,
        *,
        portfolio_id: str,
        after_commit_id: str | None,
        through_commit_id: str | None,
    ) -> dict[str, Any]:
        """Return canonical Transaction commits in one explicit journal prefix range.

        `through_commit_id=None` denotes the empty prefix. Therefore it is valid
        only when `after_commit_id` is also None, in which case the range is empty.
        """

        entries = self.read_journal()
        commit_index = self._journal_commit_index(entries)
        if after_commit_id is None:
            start = 0
        else:
            if after_commit_id not in commit_index:
                raise StoreConflict("journal_cursor_not_found")
            start = commit_index[after_commit_id] + 1

        if through_commit_id is None:
            if after_commit_id is not None:
                raise StoreConflict("journal_cursor_regression")
            end = 0
        else:
            if through_commit_id not in commit_index:
                raise StoreConflict("journal_cursor_not_found")
            end = commit_index[through_commit_id] + 1
        if end < start:
            raise StoreConflict("journal_cursor_regression")

        transactions: list[dict[str, Any]] = []
        transaction_commit_ids: list[str] = []
        for entry in entries[start:end]:
            if entry.get("event_type") != "resource_commit" or entry.get("resource_type") != "transaction":
                continue
            resource = entry.get("resource")
            if not isinstance(resource, dict):
                raise StoreCorrupt("resource commit missing resource")
            transaction_id = str(resource.get("transaction_id") or "")
            if not transaction_id or str(entry.get("resource_ref") or "") != transaction_id:
                raise StoreCorrupt("transaction resource identity mismatch")
            resource_digest = entry.get("resource_digest")
            if resource_digest is not None and resource_digest != digest(resource):
                raise StoreCorrupt("transaction resource digest mismatch")
            if resource.get("portfolio_id") != portfolio_id:
                continue
            transactions.append(resource)
            transaction_commit_ids.append(str(entry.get("commit_id") or ""))

        return {
            "from_commit_id": after_commit_id,
            "through_commit_id": through_commit_id,
            "journal_event_count": end,
            "transaction_commit_ids": transaction_commit_ids,
            "transactions": transactions,
        }

    def list_resources(self, resource_type: str) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in self.read_journal():
            if entry.get("event_type") != "resource_commit" or entry.get("resource_type") != resource_type:
                continue
            resource = entry.get("resource")
            if not isinstance(resource, dict):
                raise StoreCorrupt("resource commit missing resource")
            resource_id = str(entry.get("resource_ref") or "")
            if not resource_id or resource_id in seen:
                raise StoreCorrupt("duplicate or missing canonical resource id")
            seen.add(resource_id)
            resources.append(resource)
        return resources

    def _append_unlocked(self, entry: dict[str, Any]) -> None:
        encoded = (canonical_json(entry) + "\n").encode("utf-8")
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _resource_for_ref(entries: list[dict[str, Any]], resource_ref: str) -> dict[str, Any]:
        for entry in entries:
            if entry.get("event_type") == "resource_commit" and entry.get("resource_ref") == resource_ref:
                resource = entry.get("resource")
                if isinstance(resource, dict):
                    return resource
        raise StoreCorrupt("journal references a missing canonical resource")

    def commit_operation(
        self,
        *,
        preview: dict[str, Any],
        resource: dict[str, Any],
        principal: dict[str, Any],
        validation: ApplyValidation,
        committed_at: datetime,
    ) -> dict[str, Any]:
        operation = preview["operation"]
        mutation = preview["preview"]
        action = str(operation["action"])
        idempotency_key = str(operation["idempotency_key"])
        payload_digest = str(mutation["payload_digest"])
        resource_type = str(preview["resource_type"])
        transaction_identity = _transaction_identity(mutation["canonical_payload"]) if resource_type == "transaction" else None

        with self._locked(exclusive=True):
            entries = self._read_unlocked()
            for entry in entries:
                if entry.get("idempotency_key") != idempotency_key:
                    continue
                if entry.get("action") != action or entry.get("payload_digest") != payload_digest:
                    raise StoreConflict("idempotency_conflict")
                resource_ref = str(entry.get("resource_ref") or "")
                existing_resource = self._resource_for_ref(entries, resource_ref)
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "resource_type": resource_type,
                    "resource": existing_resource,
                    "receipt": entry["receipt"],
                    "replayed": True,
                    "dedupe_basis": "idempotency",
                }

            duplicate_entry: dict[str, Any] | None = None
            if transaction_identity is not None:
                for entry in entries:
                    if entry.get("transaction_identity") == transaction_identity:
                        duplicate_entry = entry
                        break
            if duplicate_entry is not None:
                if duplicate_entry.get("payload_digest") != payload_digest:
                    raise StoreConflict("transaction_identity_conflict")
                resource_ref = str(duplicate_entry.get("resource_ref") or "")
                existing_resource = self._resource_for_ref(entries, resource_ref)
                commit_id = f"commit:{uuid.uuid4().hex}"
                audit_ref = f"audit:write:{digest([operation['operation_id'], payload_digest, commit_id])[:24]}"
                receipt = self._receipt(
                    operation=operation,
                    resource_ref=resource_ref,
                    applied_version=str(duplicate_entry.get("commit_id") or ""),
                    completed_at=committed_at,
                    audit_ref=audit_ref,
                    effect_scope="existing_transaction_reused",
                )
                entry = self._journal_entry(
                    event_type="operation_deduplicated",
                    commit_id=commit_id,
                    preview=preview,
                    resource_ref=resource_ref,
                    resource=None,
                    receipt=receipt,
                    principal=principal,
                    validation=validation,
                    committed_at=committed_at,
                    transaction_identity=transaction_identity,
                )
                self._append_unlocked(entry)
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "resource_type": resource_type,
                    "resource": existing_resource,
                    "receipt": receipt,
                    "replayed": True,
                    "dedupe_basis": "transaction_identity",
                }

            commit_id = f"commit:{uuid.uuid4().hex}"
            resource_ref = str(resource.get("decision_id") or resource.get("transaction_id") or "")
            if not resource_ref:
                raise StoreConflict("resource_identity_missing")
            audit_ref = f"audit:write:{digest([operation['operation_id'], payload_digest, commit_id])[:24]}"
            receipt = self._receipt(
                operation=operation,
                resource_ref=resource_ref,
                applied_version=commit_id,
                completed_at=committed_at,
                audit_ref=audit_ref,
                effect_scope="decision_record_appended" if resource_type == "decision" else "transaction_record_appended",
            )
            entry = self._journal_entry(
                event_type="resource_commit",
                commit_id=commit_id,
                preview=preview,
                resource_ref=resource_ref,
                resource=resource,
                receipt=receipt,
                principal=principal,
                validation=validation,
                committed_at=committed_at,
                transaction_identity=transaction_identity,
            )
            self._append_unlocked(entry)
            return {
                "protocol_version": PROTOCOL_VERSION,
                "resource_type": resource_type,
                "resource": resource,
                "receipt": receipt,
                "replayed": False,
                "dedupe_basis": "none",
            }

    @staticmethod
    def _receipt(
        *,
        operation: dict[str, Any],
        resource_ref: str,
        applied_version: str,
        completed_at: datetime,
        audit_ref: str,
        effect_scope: str,
    ) -> dict[str, Any]:
        return {
            "operation_id": operation["operation_id"],
            "action": operation["action"],
            "target": operation["target"],
            "state": "completed",
            "applied_version": applied_version,
            "result_ref": resource_ref,
            "result_claim": "supported",
            "effect_scope": effect_scope,
            "target_lifecycle_state_ref": None,
            "completed_at": timepoint(completed_at.isoformat().replace("+00:00", "Z")),
            "failure_code": None,
            "audit_ref": audit_ref,
        }

    @staticmethod
    def _journal_entry(
        *,
        event_type: str,
        commit_id: str,
        preview: dict[str, Any],
        resource_ref: str,
        resource: dict[str, Any] | None,
        receipt: dict[str, Any],
        principal: dict[str, Any],
        validation: ApplyValidation,
        committed_at: datetime,
        transaction_identity: str | None,
    ) -> dict[str, Any]:
        operation = preview["operation"]
        mutation = preview["preview"]
        audit = {
            "audit_event_id": receipt["audit_ref"],
            "operation_id": operation["operation_id"],
            "subject_user_id": principal["subject_user_id"],
            "client_id": principal["client_id"],
            "action": operation["action"],
            "target": operation["target"],
            "permission": validation.required_permission,
            "payload_digest": mutation["payload_digest"],
            "approval_verification_ref": validation.approval_verification_ref,
            "authorization": "allowed",
            "occurred_at": timepoint(committed_at.isoformat().replace("+00:00", "Z")),
        }
        entry: dict[str, Any] = {
            "journal_version": 1,
            "event_type": event_type,
            "commit_id": commit_id,
            "idempotency_key": operation["idempotency_key"],
            "operation_id": operation["operation_id"],
            "action": operation["action"],
            "payload_digest": mutation["payload_digest"],
            "resource_type": preview["resource_type"],
            "resource_ref": resource_ref,
            "resource_digest": digest(resource) if resource is not None else None,
            "receipt": receipt,
            "audit": audit,
            "transaction_identity": transaction_identity,
            "committed_at": timepoint(committed_at.isoformat().replace("+00:00", "Z")),
        }
        if resource is not None:
            entry["resource"] = resource
        return entry


def apply_mutation(
    preview: dict[str, Any],
    approval: dict[str, Any],
    *,
    principal: dict[str, Any],
    grant: dict[str, Any],
    approval_verifier: ApprovalVerifier | None,
    store: NativeWriteStore,
    now: datetime | None = None,
    current_base_version: str | None = None,
) -> dict[str, Any]:
    current = _utc_now(now)
    validation = validate_apply(
        preview,
        approval,
        principal=principal,
        grant=grant,
        approval_verifier=approval_verifier,
        now=current,
        current_base_version=current_base_version,
    )
    if not validation.allowed:
        raise ApplyRejected(validation.reason_code)

    mutation = preview["preview"]
    payload = mutation["canonical_payload"]
    operation = preview["operation"]
    recorded_at = timepoint(current.isoformat().replace("+00:00", "Z"))
    if preview["resource_type"] == "decision":
        resource = _decision_record(
            payload,
            operation_id=operation["operation_id"],
            payload_digest=mutation["payload_digest"],
            recorded_at=recorded_at,
        )
    elif preview["resource_type"] == "transaction":
        resource = _transaction_record(
            payload,
            operation_id=operation["operation_id"],
            payload_digest=mutation["payload_digest"],
            recorded_at=recorded_at,
        )
    else:
        raise ApplyRejected("unsupported_resource_type")

    return store.commit_operation(
        preview=preview,
        resource=resource,
        principal=principal,
        validation=validation,
        committed_at=current,
    )
