#!/usr/bin/env python3
"""Personal-use ChatGPT write bridge for InvestKitchen.

This is a narrow fallback for ChatGPT plans where custom MCP write actions are
not exposed. It is intended to be invoked through the already-connected local
operator bridge (Chat On Steroids), not directly by an untrusted remote client.

The bridge persists opaque pending previews outside Git, requires an exact
digest-bound confirmation phrase, re-resolves authoritative state before apply,
and consumes each preview once. Canonical writes still use the existing native
Knowledge and Portfolio reconciliation stores.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import canonical_json, digest, timepoint  # noqa: E402
from protocol.v1.runtime.native_knowledge_store import (  # noqa: E402
    NativeKnowledgeStore,
    apply_knowledge_commit,
    build_knowledge_preview,
)
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import (  # noqa: E402
    PortfolioCheckpointStore,
    project_from_latest_checkpoint_through_cursor,
)
from protocol.v1.runtime.portfolio_update_service import (  # noqa: E402
    accept_portfolio_update,
    build_portfolio_update_preview,
    new_approval,
)
from protocol.v1.security.trusted_approval import TrustedApprovalStore  # noqa: E402


HANDLE_RE = re.compile(r"^pending:[0-9a-f]{32}$")


class BridgeRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_object(path: Path) -> dict[str, Any]:
    if str(path) == "-":
        value = json.load(sys.stdin)
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BridgeRejected("input_not_object")
    return value


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("pending write failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


class PendingStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.lock_path = self.root / ".lock"
        self.applied_root = self.root / "applied"

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.applied_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.applied_root, 0o700)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self._ensure()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create(self, value: dict[str, Any]) -> str:
        with self.locked():
            handle = f"pending:{uuid.uuid4().hex}"
            path = self.root / f"{handle.removeprefix('pending:')}.json"
            _write_json_exclusive(path, value)
            return handle

    def read(self, handle: str) -> dict[str, Any]:
        if not HANDLE_RE.fullmatch(handle):
            raise BridgeRejected("pending_handle_invalid")
        leaf = f"{handle.removeprefix('pending:')}.json"
        path = self.root / leaf
        if not path.is_file() or path.is_symlink():
            if (self.applied_root / leaf).is_file():
                raise BridgeRejected("pending_already_applied")
            raise BridgeRejected("pending_not_found")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("pending_version") != 1:
            raise BridgeRejected("pending_corrupt")
        return value

    def consume(self, handle: str) -> None:
        if not HANDLE_RE.fullmatch(handle):
            raise BridgeRejected("pending_handle_invalid")
        leaf = f"{handle.removeprefix('pending:')}.json"
        source = self.root / leaf
        target = self.applied_root / leaf
        if not source.is_file():
            raise BridgeRejected("pending_not_found")
        os.replace(source, target)
        fd = os.open(self.applied_root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _personal_identity(*, permissions: list[str], portfolio_scope: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    now = _utc_now()
    issued = timepoint(now.isoformat().replace("+00:00", "Z"))
    expires = timepoint((now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"))
    principal = {
        "subject_user_id": "user:investkitchen-owner",
        "client_id": "client:chat-on-steroids",
        "credential_binding_id": "credential:local-private-bridge",
        "authentication_event_id": f"auth:{uuid.uuid4().hex}",
        "authenticated_at": issued,
        "expires_at": expires,
    }
    grant = {
        "grant_id": f"grant:{uuid.uuid4().hex}",
        "instance_id": "fixture-full-reference",
        "subject_user_id": principal["subject_user_id"],
        "client_id": principal["client_id"],
        "credential_binding_id": principal["credential_binding_id"],
        "permissions": sorted(set(permissions)),
        "portfolio_scope": list(portfolio_scope),
        "tool_allowlist": [],
        "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
        "policy_version": "personal-advisory-bridge-v1",
        "issued_at": issued,
        "expires_at": expires,
    }
    return principal, grant


def preview_knowledge(
    request: dict[str, Any],
    *,
    native_root: Path,
    pending_store: PendingStore,
) -> dict[str, Any]:
    store = NativeKnowledgeStore(native_root)
    preview = build_knowledge_preview(
        request,
        base_generation_id=store.latest_generation_id(),
    )
    mutation = preview["preview"]
    phrase = f"APPROVE KNOWLEDGE UPDATE {str(mutation['payload_digest'])[:12]}"
    pending = {
        "pending_version": 1,
        "kind": "knowledge",
        "created_at": timepoint(),
        "confirmation": phrase,
        "preview": preview,
    }
    handle = pending_store.create(pending)
    payload = mutation["canonical_payload"]
    return {
        "status": "review_required",
        "pending_id": handle,
        "generation_id": payload["generation_id"],
        "payload_digest": mutation["payload_digest"],
        "base_generation_id": mutation.get("base_version"),
        "evidence_count": len(payload.get("evidence") or []),
        "claim_count": len(payload.get("claims") or []),
        "claims": [
            {
                "claim_id": row.get("claim_id"),
                "statement": row.get("statement"),
                "truth_status": row.get("truth_status"),
                "applicability_status": row.get("applicability_status"),
            }
            for row in payload.get("claims") or []
            if isinstance(row, dict)
        ],
        "current_state_summary": (payload.get("current_state") or {}).get("summary"),
        "confirmation": phrase,
    }


def apply_knowledge(
    pending_id: str,
    confirmation: str,
    *,
    native_root: Path,
    approval_root: Path,
    pending_store: PendingStore,
) -> dict[str, Any]:
    with pending_store.locked():
        pending = pending_store.read(pending_id)
        if pending.get("kind") != "knowledge":
            raise BridgeRejected("pending_kind_mismatch")
        if confirmation != pending.get("confirmation"):
            raise BridgeRejected("confirmation_mismatch")
        preview = pending.get("preview")
        if not isinstance(preview, dict):
            raise BridgeRejected("pending_corrupt")
        store = NativeKnowledgeStore(native_root)
        if preview.get("preview", {}).get("base_version") != store.latest_generation_id():
            raise BridgeRejected("preview_stale")
        principal, grant = _personal_identity(
            permissions=["knowledge.commit", "operation.approve"],
            portfolio_scope=[],
        )
        approvals = TrustedApprovalStore(approval_root)
        receipt = approvals.issue(
            preview,
            principal=principal,
            grant=grant,
            interaction_ref=f"chat-explicit:{digest([pending_id, confirmation])[:24]}",
            approval_method="chat_explicit_confirmation",
            ttl_seconds=300,
        )
        result = apply_knowledge_commit(
            preview,
            receipt,
            principal=principal,
            grant=grant,
            approval_verifier=approvals.verify,
            store=store,
        )
        if result.get("replayed") is True:
            raise BridgeRejected("write_replay_detected")
        pending_store.consume(pending_id)
        generation_id = str(result["generation_id"])
        return {
            "status": "applied",
            "pending_id": pending_id,
            "generation_id": generation_id,
            "payload_digest": result["payload_digest"],
            "committed_at": result["committed_at"],
            "read_back": {
                "generation_id": store.latest_generation_id(),
                "matches": store.latest_generation_id() == generation_id,
            },
        }


def preview_portfolio(
    request: dict[str, Any],
    *,
    native_root: Path,
    checkpoint_root: Path,
    pending_store: PendingStore,
) -> dict[str, Any]:
    write_store = NativeWriteStore(native_root)
    checkpoint_store = PortfolioCheckpointStore(checkpoint_root)
    result = build_portfolio_update_preview(
        request,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
    )
    candidate = result["candidate"]
    review = result["review"]
    latest_checkpoint = checkpoint_store.latest(str(candidate["portfolio_id"]))
    pending = {
        "pending_version": 1,
        "kind": "portfolio",
        "created_at": timepoint(),
        "confirmation": review["approval_phrase"],
        "candidate": candidate,
        "latest_checkpoint_id": latest_checkpoint.get("checkpoint_id"),
        "journal_cursor": candidate.get("journal_cursor"),
        "current_portfolio_digest": candidate.get("current_portfolio_digest"),
    }
    handle = pending_store.create(pending)
    return {
        "status": "review_required",
        "pending_id": handle,
        "portfolio_id": candidate["portfolio_id"],
        "candidate_digest": candidate["candidate_digest"],
        "summary": review["summary"],
        "differences": review["differences"],
        "gaps": review["gaps"],
        "confirmation": review["approval_phrase"],
    }


def _portfolio_is_stale(
    pending: dict[str, Any],
    *,
    write_store: NativeWriteStore,
    checkpoint_store: PortfolioCheckpointStore,
) -> bool:
    candidate = pending.get("candidate")
    if not isinstance(candidate, dict):
        return True
    portfolio_id = str(candidate.get("portfolio_id") or "")
    latest = checkpoint_store.latest(portfolio_id)
    if latest.get("checkpoint_id") != pending.get("latest_checkpoint_id"):
        return True
    cursor = pending.get("journal_cursor")
    if not isinstance(cursor, dict):
        return True
    try:
        actual = write_store.journal_cursor(cursor.get("through_commit_id"))
        projection = project_from_latest_checkpoint_through_cursor(
            checkpoint_store,
            write_store,
            portfolio_id=portfolio_id,
            through_commit_id=cursor.get("through_commit_id"),
            generated_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), dict) else None,
        )
    except Exception:
        return True
    current = projection.get("materialization", {}).get("portfolio")
    return actual != cursor or not isinstance(current, dict) or digest(current) != pending.get("current_portfolio_digest")


def apply_portfolio(
    pending_id: str,
    confirmation: str,
    *,
    native_root: Path,
    checkpoint_root: Path,
    pending_store: PendingStore,
) -> dict[str, Any]:
    with pending_store.locked():
        pending = pending_store.read(pending_id)
        if pending.get("kind") != "portfolio":
            raise BridgeRejected("pending_kind_mismatch")
        if confirmation != pending.get("confirmation"):
            raise BridgeRejected("confirmation_mismatch")
        candidate = pending.get("candidate")
        if not isinstance(candidate, dict):
            raise BridgeRejected("pending_corrupt")
        write_store = NativeWriteStore(native_root)
        checkpoint_store = PortfolioCheckpointStore(checkpoint_root)
        if _portfolio_is_stale(pending, write_store=write_store, checkpoint_store=checkpoint_store):
            raise BridgeRejected("preview_stale")
        approval = new_approval(
            candidate,
            approval_ref=f"chat-explicit:{digest([pending_id, confirmation, uuid.uuid4().hex])[:24]}",
        )
        expected_candidate = digest(candidate)
        expected_approval = digest(approval)

        def verifier(candidate_value: dict[str, Any], approval_value: dict[str, Any]) -> dict[str, Any]:
            verified = digest(candidate_value) == expected_candidate and digest(approval_value) == expected_approval
            return {
                "verified": verified,
                "verification_ref": (
                    f"chat-portfolio-approval:{digest([pending_id, expected_approval])[:24]}"
                    if verified else None
                ),
            }

        result = accept_portfolio_update(
            candidate,
            approval,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            approval_verifier=verifier,
        )
        pending_store.consume(pending_id)
        latest = checkpoint_store.latest(str(candidate["portfolio_id"]))
        return {
            "status": "applied",
            "pending_id": pending_id,
            "portfolio_id": candidate["portfolio_id"],
            "candidate_digest": result["candidate_digest"],
            "checkpoint_id": result["checkpoint_id"],
            "accepted_at": result["accepted_at"],
            "read_back": {
                "checkpoint_id": latest.get("checkpoint_id"),
                "matches": latest.get("checkpoint_id") == result["checkpoint_id"],
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--pending-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    kp = sub.add_parser("knowledge-preview")
    kp.add_argument("--input", type=Path, required=True)
    ka = sub.add_parser("knowledge-apply")
    ka.add_argument("--pending-id", required=True)
    ka.add_argument("--confirmation", required=True)

    pp = sub.add_parser("portfolio-preview")
    pp.add_argument("--input", type=Path, required=True)
    pa = sub.add_parser("portfolio-apply")
    pa.add_argument("--pending-id", required=True)
    pa.add_argument("--confirmation", required=True)

    args = parser.parse_args(argv)
    state_root = args.state_root.expanduser().resolve()
    native_root = state_root / "native-write"
    checkpoint_root = state_root / "portfolio-checkpoints"
    approval_root = state_root / "approvals"
    pending_root = (args.pending_root or (state_root / "advisory-pending")).expanduser().resolve()
    pending = PendingStore(pending_root)

    try:
        if args.command == "knowledge-preview":
            result = preview_knowledge(_read_object(args.input), native_root=native_root, pending_store=pending)
        elif args.command == "knowledge-apply":
            result = apply_knowledge(
                args.pending_id,
                args.confirmation,
                native_root=native_root,
                approval_root=approval_root,
                pending_store=pending,
            )
        elif args.command == "portfolio-preview":
            result = preview_portfolio(
                _read_object(args.input),
                native_root=native_root,
                checkpoint_root=checkpoint_root,
                pending_store=pending,
            )
        else:
            result = apply_portfolio(
                args.pending_id,
                args.confirmation,
                native_root=native_root,
                checkpoint_root=checkpoint_root,
                pending_store=pending,
            )
    except (OSError, ValueError, json.JSONDecodeError, BridgeRejected) as exc:
        code = str(getattr(exc, "code", "advisory_write_rejected") or "advisory_write_rejected")
        sys.stderr.write(f"InvestKitchen advisory write rejected: {code}\n")
        return 2

    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
