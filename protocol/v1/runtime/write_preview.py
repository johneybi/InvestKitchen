from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


PROTOCOL_VERSION = "1.0-draft"


def _timepoint(value: datetime) -> dict[str, str]:
    return {"value": value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "precision": "source_exact"}


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def _target(request: dict[str, Any]) -> dict[str, Any]:
    target = {"portfolio_id": request.get("portfolio_id")}
    if request.get("account_id") is not None:
        target["account_id"] = request["account_id"]
    return target


def build_mutation_preview(
    request: dict[str, Any],
    *,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    """Build a side-effect-free preview for the first Decision/Transaction write contract.

    This function never persists, applies, or approves a mutation.  In particular,
    transaction occurrence attestation is domain evidence and is not an approval
    receipt for the database mutation.
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    request_type = str(request.get("request_type") or "")
    if request_type not in {"decision.create", "transaction.record"}:
        raise ValueError("unsupported write request type")

    resource_type = "decision" if request_type == "decision.create" else "transaction"
    operation_id = _id("operation")
    preview_id = _id("preview")
    canonical_payload = dict(request)
    payload_digest = _digest(canonical_payload)
    target = _target(request)
    warnings: list[str] = []
    if resource_type == "transaction" and request.get("detail_status") != "complete":
        warnings.append("Transaction detail is not complete; approval must not imply missing execution detail.")

    return {
        "protocol_version": PROTOCOL_VERSION,
        "resource_type": resource_type,
        "operation": {
            "operation_id": operation_id,
            "request_id": _id("request"),
            "idempotency_key": _id("idempotency"),
            "actor": "web-gpt",
            "action": request_type,
            "target": target,
            "requested_at": _timepoint(current),
            "state": "awaiting_approval",
        },
        "preview": {
            "preview_id": preview_id,
            "operation_id": operation_id,
            "action": request_type,
            "target": target,
            "base_version": None,
            "canonical_payload": canonical_payload,
            "payload_digest": payload_digest,
            "expected_effect": (
                "Create a canonical Decision record after authenticated approval."
                if resource_type == "decision"
                else "Record a canonical Transaction event after authenticated approval and domain validation."
            ),
            "warnings": warnings,
            "generated_at": _timepoint(current),
            "expires_at": _timepoint(current + timedelta(seconds=ttl_seconds)),
        },
        "approval_required": True,
        "approval": None,
        "apply_state": "not_applied",
    }
