#!/usr/bin/env python3
"""Local operator review/acceptance surface for Portfolio reconciliation.

`review` is read-only and may run without a TTY. `accept` requires an interactive
TTY and an exact digest-prefix confirmation. The acceptance object is created
inside this trusted local process; the command does not accept a caller-supplied
acceptance JSON file.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import canonical_json, digest, timepoint  # noqa: E402
from protocol.v1.runtime.native_write_store import NativeWriteStore  # noqa: E402
from protocol.v1.runtime.portfolio_checkpoint import PortfolioCheckpointStore  # noqa: E402
from protocol.v1.runtime.portfolio_reconciliation import (  # noqa: E402
    ReconciliationError,
    accept_reconciliation_candidate,
    validate_reconciliation_candidate,
)


class OperatorCancelled(RuntimeError):
    pass


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def reconciliation_review_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return only the review-relevant surface, never the full Portfolio snapshot."""

    validate_reconciliation_candidate(candidate)
    summary = candidate.get("summary") if isinstance(candidate.get("summary"), dict) else {}
    differences = candidate.get("differences") if isinstance(candidate.get("differences"), list) else []
    gaps = candidate.get("gaps") if isinstance(candidate.get("gaps"), list) else []
    return {
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "snapshot_effective_at": copy.deepcopy(candidate.get("snapshot_effective_at")),
        "summary": copy.deepcopy(summary),
        "differences": copy.deepcopy(differences),
        "gaps": copy.deepcopy(gaps),
    }


def acceptance_phrase(candidate: dict[str, Any]) -> str:
    validate_reconciliation_candidate(candidate)
    return f"ACCEPT {str(candidate['candidate_digest'])[:12]}"


def accept_candidate_with_confirmation(
    candidate: dict[str, Any],
    *,
    confirmation: str,
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    accepted_at: dict[str, Any] | None = None,
    interaction_ref: str | None = None,
) -> dict[str, Any]:
    """Accept exactly one candidate after local operator confirmation.

    The verifier is intentionally in-process and single-use. It only verifies the
    exact candidate/acceptance objects created by this trusted interaction surface.
    """

    validate_reconciliation_candidate(candidate)
    expected_phrase = acceptance_phrase(candidate)
    if confirmation.strip() != expected_phrase:
        raise OperatorCancelled("reconciliation_operator_cancelled")

    accepted = copy.deepcopy(accepted_at or timepoint())
    interaction = interaction_ref or f"local-tty:{uuid.uuid4().hex}"
    acceptance = {
        "candidate_id": candidate["candidate_id"],
        "accepted_candidate_digest": candidate["candidate_digest"],
        "decision": "accept",
        "accepted_at": accepted,
    }
    expected_candidate_json = canonical_json(candidate)
    expected_acceptance_json = canonical_json(acceptance)
    expected_acceptance_digest = digest(acceptance)

    def verify(candidate_value: dict[str, Any], acceptance_value: dict[str, Any]) -> dict[str, Any]:
        if canonical_json(candidate_value) != expected_candidate_json:
            return {"verified": False, "verification_ref": None}
        if canonical_json(acceptance_value) != expected_acceptance_json:
            return {"verified": False, "verification_ref": None}
        return {
            "verified": True,
            "verification_ref": (
                "reconciliation-verification:"
                + digest([
                    interaction,
                    candidate["candidate_id"],
                    candidate["candidate_digest"],
                    expected_acceptance_digest,
                ])[:24]
            ),
        }

    return accept_reconciliation_candidate(
        candidate,
        acceptance,
        checkpoint_store=checkpoint_store,
        write_store=write_store,
        acceptance_verifier=verify,
    )


def _print_review(candidate: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(reconciliation_review_summary(candidate), ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review")
    review.add_argument("--candidate", type=Path, required=True)

    accept = sub.add_parser("accept")
    accept.add_argument("--candidate", type=Path, required=True)
    accept.add_argument("--native-store-root", type=Path, required=True)
    accept.add_argument("--checkpoint-root", type=Path, required=True)

    args = parser.parse_args()
    try:
        candidate = _read_object(args.candidate)
        validate_reconciliation_candidate(candidate)
    except (OSError, ValueError, json.JSONDecodeError, ReconciliationError):
        sys.stderr.write("InvestKitchen reconciliation input error\n")
        return 2

    if args.command == "review":
        _print_review(candidate)
        return 0

    if not sys.stdin.isatty():
        sys.stderr.write("InvestKitchen reconciliation acceptance requires an interactive TTY\n")
        return 2

    _print_review(candidate)
    phrase = acceptance_phrase(candidate)
    sys.stdout.write(f"Type exactly: {phrase}\n")
    sys.stdout.flush()
    confirmation = input("> ").strip()
    if confirmation != phrase:
        sys.stderr.write("Reconciliation acceptance cancelled\n")
        return 1

    try:
        result = accept_candidate_with_confirmation(
            candidate,
            confirmation=confirmation,
            checkpoint_store=PortfolioCheckpointStore(args.checkpoint_root),
            write_store=NativeWriteStore(args.native_store_root),
        )
    except (OperatorCancelled, ReconciliationError, OSError, ValueError):
        sys.stderr.write("InvestKitchen reconciliation acceptance rejected\n")
        return 2

    # Acceptance output is deliberately bounded. The full candidate and Portfolio
    # values were review-only and are not repeated in the result/audit surface.
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
