#!/usr/bin/env python3
"""Local private CLI for Portfolio update preview and explicit acceptance.

`preview` builds a reconciliation candidate from either a full observed Portfolio
or narrow position/cash changes. The private candidate is written only to an
explicit caller-chosen path outside the repository. `accept` requires a TTY and
an exact candidate-digest-bound phrase before the existing reconciliation
machinery persists a new accepted checkpoint.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import canonical_json, digest  # noqa: E402
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import PortfolioCheckpointStore  # noqa: E402
from protocol.v1.runtime.portfolio_update_service import (  # noqa: E402
    PortfolioUpdateError,
    accept_portfolio_update,
    approval_phrase,
    build_portfolio_update_preview,
    new_approval,
    portfolio_update_review,
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def _private_candidate_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    repo = ROOT.resolve()
    if resolved == repo or repo in resolved.parents:
        raise PortfolioUpdateError("portfolio_update_candidate_path_inside_repository")
    parent = resolved.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise PortfolioUpdateError("portfolio_update_candidate_parent_symlink")
    if parent.stat().st_mode & 0o077:
        raise PortfolioUpdateError("portfolio_update_candidate_parent_not_private")
    return resolved


def _write_private_candidate(path: Path, candidate: dict[str, Any]) -> None:
    destination = _private_candidate_path(path)
    raw = (canonical_json(candidate) + "\n").encode("utf-8")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("private candidate write failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _local_approval_verifier(expected_candidate: dict[str, Any], expected_approval: dict[str, Any]):
    candidate_json = canonical_json(expected_candidate)
    approval_json = canonical_json(expected_approval)

    def verify(candidate: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
        if canonical_json(candidate) != candidate_json or canonical_json(approval) != approval_json:
            return {"verified": False, "verification_ref": None}
        return {
            "verified": True,
            "verification_ref": "portfolio-update-verification:" + digest([
                expected_approval["approval_ref"],
                expected_candidate["candidate_id"],
                expected_candidate["candidate_digest"],
                digest(expected_approval),
            ])[:24],
        }

    return verify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    preview = sub.add_parser("preview")
    preview.add_argument("--request", type=Path, required=True)
    preview.add_argument("--native-store-root", type=Path, required=True)
    preview.add_argument("--checkpoint-root", type=Path, required=True)
    preview.add_argument("--candidate-out", type=Path, required=True)

    accept = sub.add_parser("accept")
    accept.add_argument("--candidate", type=Path, required=True)
    accept.add_argument("--native-store-root", type=Path, required=True)
    accept.add_argument("--checkpoint-root", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        write_store = NativeWriteStore(args.native_store_root)
        checkpoint_store = PortfolioCheckpointStore(args.checkpoint_root)
        if args.command == "preview":
            result = build_portfolio_update_preview(
                _read_object(args.request),
                checkpoint_store=checkpoint_store,
                write_store=write_store,
            )
            _write_private_candidate(args.candidate_out, result["candidate"])
            sys.stdout.write(json.dumps(result["review"], ensure_ascii=False, indent=2) + "\n")
            return 0

        candidate = _read_object(args.candidate)
        review = portfolio_update_review(candidate)
    except (OSError, ValueError, json.JSONDecodeError, PortfolioUpdateError) as exc:
        sys.stderr.write(f"InvestKitchen Portfolio update input rejected: {getattr(exc, 'code', 'invalid_input')}\n")
        return 2

    if not sys.stdin.isatty():
        sys.stderr.write("InvestKitchen Portfolio update approval requires an interactive TTY\n")
        return 2
    sys.stdout.write(json.dumps(review, ensure_ascii=False, indent=2) + "\n")
    phrase = approval_phrase(candidate)
    sys.stdout.write(f"Type exactly: {phrase}\n")
    sys.stdout.flush()
    confirmation = input("> ").strip()
    if confirmation != phrase:
        sys.stderr.write("Portfolio update acceptance cancelled\n")
        return 1

    approval = new_approval(candidate, approval_ref=f"local-tty:{uuid.uuid4().hex}")
    approval["confirmation"] = confirmation
    try:
        result = accept_portfolio_update(
            candidate,
            approval,
            checkpoint_store=checkpoint_store,
            write_store=write_store,
            approval_verifier=_local_approval_verifier(candidate, approval),
        )
    except (OSError, ValueError, PortfolioUpdateError) as exc:
        sys.stderr.write(f"InvestKitchen Portfolio update acceptance rejected: {getattr(exc, 'code', 'rejected')}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
