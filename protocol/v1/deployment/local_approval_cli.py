#!/usr/bin/env python3
"""Local interactive trusted-approval surface for the reference Runtime.

This command issues an ApprovalReceipt only after an operator confirms the exact
preview digest in an interactive TTY. It does not apply the mutation. Principal
and grant files are server-side inputs and must not come from MCP tool arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.security.trusted_approval import ApprovalRejected, TrustedApprovalStore  # noqa: E402


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def approval_summary(preview: dict[str, Any]) -> dict[str, Any]:
    operation = preview.get("operation") if isinstance(preview.get("operation"), dict) else {}
    mutation = preview.get("preview") if isinstance(preview.get("preview"), dict) else {}
    payload = mutation.get("canonical_payload") if isinstance(mutation.get("canonical_payload"), dict) else {}
    return {
        "action": operation.get("action"),
        "target": mutation.get("target"),
        "payload_digest": mutation.get("payload_digest"),
        "expected_effect": mutation.get("expected_effect"),
        "warnings": mutation.get("warnings") or [],
        "statement": payload.get("statement"),
        "transaction_type": payload.get("transaction_type"),
        "asset": (payload.get("asset") or {}).get("display_name") if isinstance(payload.get("asset"), dict) else None,
        "side": payload.get("side"),
        "quantity": payload.get("quantity"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--principal", type=Path, required=True)
    parser.add_argument("--grant", type=Path, required=True)
    parser.add_argument("--approval-store-root", type=Path, required=True)
    parser.add_argument("--authority-id", default="approval-authority:local-v1")
    args = parser.parse_args()

    if not sys.stdin.isatty():
        sys.stderr.write("TradeMind approval requires an interactive TTY\n")
        return 2

    try:
        preview = _read_object(args.preview)
        principal = _read_object(args.principal)
        grant = _read_object(args.grant)
        summary = approval_summary(preview)
        payload_digest = summary.get("payload_digest")
        if not isinstance(payload_digest, str) or len(payload_digest) != 64:
            raise ValueError("preview payload digest is invalid")
    except (OSError, ValueError, json.JSONDecodeError):
        sys.stderr.write("TradeMind approval input error\n")
        return 2

    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    phrase = f"APPROVE {payload_digest[:12]}"
    sys.stdout.write(f"Type exactly: {phrase}\n")
    sys.stdout.flush()
    if input("> ").strip() != phrase:
        sys.stderr.write("Approval cancelled\n")
        return 1

    store = TrustedApprovalStore(args.approval_store_root, authority_id=args.authority_id)
    try:
        approval = store.issue(
            preview,
            principal=principal,
            grant=grant,
            interaction_ref=f"local-tty:{uuid.uuid4().hex}",
            approval_method="local_tty",
        )
    except ApprovalRejected:
        sys.stderr.write("TradeMind approval rejected\n")
        return 2
    sys.stdout.write(json.dumps(approval, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
