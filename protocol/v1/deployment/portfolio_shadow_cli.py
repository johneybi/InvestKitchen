#!/usr/bin/env python3
"""Run a privacy-safe InvestKitchen Portfolio authority shadow comparison."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.runtime.portfolio_shadow import PortfolioShadowError, build_shadow_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--personal-data-root", type=Path, required=True)
    parser.add_argument("--native-store-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_shadow_report(
            personal_data_root=args.personal_data_root,
            native_store_root=args.native_store_root,
        )
    except (PortfolioShadowError, OSError, ValueError):
        sys.stderr.write("InvestKitchen portfolio shadow comparison failed\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
