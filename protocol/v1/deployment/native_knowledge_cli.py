#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.runtime.native_knowledge_store import (  # noqa: E402
    KnowledgeRejected,
    KnowledgeStoreConflict,
    NativeKnowledgeStore,
    apply_knowledge_commit,
    build_knowledge_preview,
)
from protocol.v1.runtime.native_write_store import ApplyRejected  # noqa: E402
from protocol.v1.security.trusted_approval import TrustedApprovalStore  # noqa: E402


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or apply one approved native InvestKitchen Knowledge generation.")
    sub = parser.add_subparsers(dest="command", required=True)

    preview = sub.add_parser("preview")
    preview.add_argument("--input", type=Path, required=True)
    preview.add_argument("--native-store-root", type=Path, required=True)

    apply = sub.add_parser("apply")
    apply.add_argument("--preview", type=Path, required=True)
    apply.add_argument("--approval", type=Path, required=True)
    apply.add_argument("--principal", type=Path, required=True)
    apply.add_argument("--grant", type=Path, required=True)
    apply.add_argument("--approval-store-root", type=Path, required=True)
    apply.add_argument("--native-store-root", type=Path, required=True)
    apply.add_argument("--authority-id", default="approval-authority:local-v1")
    args = parser.parse_args(argv)

    try:
        store = NativeKnowledgeStore(args.native_store_root)
        if args.command == "preview":
            result = build_knowledge_preview(
                _read_object(args.input),
                base_generation_id=store.latest_generation_id(),
            )
        else:
            preview_value = _read_object(args.preview)
            approval = _read_object(args.approval)
            principal = _read_object(args.principal)
            grant = _read_object(args.grant)
            approval_store = TrustedApprovalStore(args.approval_store_root, authority_id=args.authority_id)
            result = apply_knowledge_commit(
                preview_value,
                approval,
                principal=principal,
                grant=grant,
                approval_verifier=approval_store.verify,
                store=store,
            )
    except (OSError, ValueError, json.JSONDecodeError, KnowledgeRejected, KnowledgeStoreConflict, ApplyRejected):
        sys.stderr.write("InvestKitchen native Knowledge write rejected\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
