#!/usr/bin/env python3
"""Verify production checkpoint→native-Transaction Portfolio replay without writes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.runtime.portfolio_replay_verify import (  # noqa: E402
    PortfolioReplayVerifyError,
    build_replay_verification_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-store-root", type=Path, required=True)
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_replay_verification_report(
            native_store_root=args.native_store_root,
            checkpoint_root=args.checkpoint_root,
        )
    except (PortfolioReplayVerifyError, OSError, ValueError):
        sys.stderr.write("InvestKitchen Portfolio replay verification failed\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
