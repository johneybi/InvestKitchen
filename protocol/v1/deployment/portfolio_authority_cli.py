#!/usr/bin/env python3
"""Seed or verify InvestKitchen native Portfolio authority checkpoints."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.runtime.portfolio_authority_seed import (  # noqa: E402
    PortfolioAuthoritySeedError,
    seed_initial_portfolio_authority,
    verify_seeded_portfolio_authority,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seed", "verify"))
    parser.add_argument("--personal-data-root", type=Path, required=True)
    parser.add_argument("--native-store-root", type=Path, required=True)
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "seed":
            result = seed_initial_portfolio_authority(
                personal_data_root=args.personal_data_root,
                native_store_root=args.native_store_root,
                checkpoint_root=args.checkpoint_root,
            )
        else:
            result = verify_seeded_portfolio_authority(
                personal_data_root=args.personal_data_root,
                native_store_root=args.native_store_root,
                checkpoint_root=args.checkpoint_root,
            )
    except (PortfolioAuthoritySeedError, OSError, ValueError):
        sys.stderr.write("InvestKitchen Portfolio authority operation failed\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
