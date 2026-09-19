#!/usr/bin/env python3
"""Backup, verify, and restore the InvestKitchen Protocol v1 reference state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.runtime.storage_recovery import (  # noqa: E402
    BackupError,
    RestoreRefused,
    StorageLayout,
    create_backup,
    restore_backup,
    verify_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup")
    backup.add_argument("--state-root", type=Path, required=True)
    backup.add_argument("--backup-root", type=Path, required=True)
    backup.add_argument("--instance-id", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--snapshot", type=Path, required=True)
    verify.add_argument("--instance-id")

    restore = sub.add_parser("restore")
    restore.add_argument("--snapshot", type=Path, required=True)
    restore.add_argument("--target-state-root", type=Path, required=True)
    restore.add_argument("--instance-id", required=True)

    args = parser.parse_args()
    try:
        if args.command == "backup":
            snapshot = create_backup(
                StorageLayout(
                    state_root=args.state_root,
                    backup_root=args.backup_root,
                    instance_id=args.instance_id,
                )
            )
            result = verify_backup(snapshot, expected_instance_id=args.instance_id)
            result["snapshot"] = str(snapshot)
        elif args.command == "verify":
            result = verify_backup(args.snapshot, expected_instance_id=args.instance_id)
        else:
            result = restore_backup(
                args.snapshot,
                args.target_state_root,
                expected_instance_id=args.instance_id,
            )
    except (BackupError, RestoreRefused, OSError, ValueError):
        sys.stderr.write("InvestKitchen storage recovery operation failed\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
