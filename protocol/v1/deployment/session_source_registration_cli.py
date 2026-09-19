#!/usr/bin/env python3
"""Personal-only raw session source registration and guarded exchange publish."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.session_authority.source_registration import (  # noqa: E402
    SessionSourceRegistrationError,
    _read_bundle,
    publish_registered_bundle,
    register_source,
    validate_registered_bundle,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="append one immutable raw source registration")
    register.add_argument("--source", type=Path, required=True)
    register.add_argument("--bundle", type=Path, required=True)
    register.add_argument("--document-id", required=True)
    register.add_argument("--registration-root", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="verify bundle provenance against private registrations")
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--registration-root", type=Path, required=True)

    publish = subparsers.add_parser("publish", help="validate registrations, then publish through native producer")
    publish.add_argument("--bundle", type=Path, required=True)
    publish.add_argument("--registration-root", type=Path, required=True)
    publish.add_argument("--exchange-root", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        bundle = _read_bundle(args.bundle)
        if args.command == "register":
            result = register_source(
                raw_source=args.source,
                bundle=bundle,
                document_id=args.document_id,
                registration_root=args.registration_root,
            )
        elif args.command == "validate":
            _, _, result = validate_registered_bundle(
                bundle=bundle,
                registration_root=args.registration_root,
            )
        else:
            result = publish_registered_bundle(
                bundle=bundle,
                registration_root=args.registration_root,
                exchange_root=args.exchange_root,
            )
    except (OSError, ValueError, SessionSourceRegistrationError) as exc:
        sys.stderr.write(f"InvestKitchen session source registration failed: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
