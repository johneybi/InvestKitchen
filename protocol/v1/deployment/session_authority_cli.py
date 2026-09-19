#!/usr/bin/env python3
"""Personal-use CLI for publishing InvestKitchen session contract bundles.

The caller (normally ChatGPT in the user's local workflow) is responsible for
turning source material into frozen SessionBrief/Amendment/MaterialContext
semantic payloads. This CLI only validates, binds stream identities, and
publishes the immutable neutral exchange consumed by trading-runtime.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.session_authority import SessionAuthorityProducer  # noqa: E402
from protocol.v1.session_authority.contracts import SessionAuthorityContractError  # noqa: E402


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("session authority bundle is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("session authority bundle must be an object")
    return value


def _bundle(value: dict[str, Any]) -> tuple[SessionAuthorityProducer, str]:
    allowed = {"bundle_version", "session_date", "published_at", "events"}
    if set(value) != allowed or value.get("bundle_version") != 1:
        raise ValueError("session authority bundle fields/version are invalid")
    session_date = value.get("session_date")
    published_at = value.get("published_at")
    events = value.get("events")
    if not isinstance(session_date, str) or not session_date:
        raise ValueError("session authority bundle session_date is required")
    if not isinstance(published_at, str) or not published_at:
        raise ValueError("session authority bundle published_at is required")
    if not isinstance(events, list) or not events:
        raise ValueError("session authority bundle events are required")

    producer = SessionAuthorityProducer(session_date)
    for offset, row in enumerate(events):
        if not isinstance(row, dict):
            raise ValueError(f"events[{offset}] must be an object")
        if set(row) != {"event_type", "occurred_at", "effective_at", "payload"}:
            raise ValueError(f"events[{offset}] fields are invalid")
        event_type = row.get("event_type")
        occurred_at = row.get("occurred_at")
        effective_at = row.get("effective_at")
        payload = row.get("payload")
        if event_type not in {"SessionBrief", "SessionBriefAmendment", "MaterialContext"}:
            raise ValueError(f"events[{offset}] event_type is unsupported")
        if not isinstance(occurred_at, str) or not occurred_at:
            raise ValueError(f"events[{offset}] occurred_at is required")
        if effective_at is not None and (not isinstance(effective_at, str) or not effective_at):
            raise ValueError(f"events[{offset}] effective_at is invalid")
        if not isinstance(payload, dict):
            raise ValueError(f"events[{offset}] payload must be an object")

        if event_type == "SessionBrief":
            producer.append_brief(payload, occurred_at=occurred_at, effective_at=effective_at)
            continue
        if event_type == "MaterialContext":
            producer.append_context(payload, occurred_at=occurred_at, effective_at=effective_at)
            continue

        # Personal authoring ergonomics: callers may omit chain-generated
        # identity fields from amendment payloads. They are filled from the
        # already validated local authority chain, never trusted from prose.
        if not producer.events:
            raise ValueError("SessionBriefAmendment requires a prior SessionBrief")
        bound = copy.deepcopy(payload)
        base = producer.events[0]
        previous = producer.events[-1]
        bound.setdefault("base_brief_id", base.manifest["event_id"])
        bound.setdefault("base_brief_hash", base.manifest["event_hash"])
        bound.setdefault(
            "previous_authority_event",
            {"event_id": previous.manifest["event_id"], "event_hash": previous.manifest["event_hash"]},
        )
        producer.append_amendment(bound, occurred_at=occurred_at, effective_at=effective_at)
    return producer, published_at


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "publish"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--exchange-root", type=Path)
    args = parser.parse_args()
    try:
        producer, published_at = _bundle(_read_object(args.bundle))
        state = producer.authority_state()
        if args.command == "validate":
            result = {
                "ok": True,
                "status": "valid",
                "session_date": state["session_date"],
                "event_count": len(producer.all_events),
                "record_count": len(state["records"]),
            }
        else:
            if args.exchange_root is None:
                parser.error("--exchange-root is required for publish")
            result = producer.publish_exchange(args.exchange_root, published_at=published_at)
    except (OSError, ValueError, SessionAuthorityContractError) as exc:
        sys.stderr.write(f"InvestKitchen session authority failed: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
