#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.runtime.personal_data_migration import MigrationError, export_legacy_bundle, install_bundle  # noqa: E402
from protocol.v1.runtime.personal_data_store import PersonalDataError, PersonalDataStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Export, verify, and install TradeMind personal runtime data.")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export-legacy")
    export.add_argument("--runtime-root", type=Path, default=ROOT)
    export.add_argument("--legacy-workspace", type=Path, required=True)
    export.add_argument("--output-dir", type=Path, required=True)
    export.add_argument("--portfolio-id", action="append", dest="portfolio_ids", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)

    install = sub.add_parser("install")
    install.add_argument("--bundle", type=Path, required=True)
    install.add_argument("--target-root", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "export-legacy":
            path = export_legacy_bundle(
                runtime_root=args.runtime_root,
                legacy_workspace=args.legacy_workspace,
                output_dir=args.output_dir,
                portfolio_ids=list(args.portfolio_ids),
            )
            result = PersonalDataStore(path).verify()
            result["bundle_root"] = str(path)
        elif args.command == "verify":
            result = PersonalDataStore(args.bundle).verify()
        else:
            result = install_bundle(args.bundle, args.target_root)
    except (MigrationError, PersonalDataError, OSError, ValueError) as exc:
        sys.stderr.write(f"TradeMind personal data error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
