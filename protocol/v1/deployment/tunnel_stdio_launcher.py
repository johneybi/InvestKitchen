#!/usr/bin/env python3
"""Minimal Secure MCP Tunnel child launcher with environment scrubbing.

The tunnel client needs CONTROL_PLANE_API_KEY, but the TradeMind MCP subprocess
does not. This launcher is the `--mcp-command` target: it immediately replaces
itself with the MCP adapter under a small allowlisted environment.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def sanitized_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if source is None else source)
    allowed = (
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
    )
    return {key: source[key] for key in allowed if key in source}


def child_argv(
    *,
    runtime_root: Path,
    manifest: Path,
    legacy_workspace: Path | None,
    personal_data_root: Path | None,
    market_provider: str,
    market_secret_file: Path | None,
    account_binding_file: Path | None = None,
    toss_account_secret_file: Path | None = None,
    nhplug_account_secret_file: Path | None = None,
    account_sync_max_age_seconds: int = 300,
    native_store_root: Path | None = None,
    advisory_state_root: Path | None = None,
    historical_decision_root: Path | None = None,
    historical_transaction_root: Path | None = None,
    portfolio_checkpoint_root: Path | None = None,
    approval_store_root: Path | None = None,
    write_principal: Path | None = None,
    write_grant: Path | None = None,
) -> list[str]:
    server = runtime_root / "protocol" / "v1" / "transport" / "mcp_stdio.py"
    argv = [
        sys.executable,
        str(server),
        "--runtime-root",
        str(runtime_root),
        "--manifest",
        str(manifest),
        "--market-provider",
        market_provider,
    ]
    if legacy_workspace is not None:
        argv.extend(["--legacy-workspace", str(legacy_workspace)])
    if personal_data_root is not None:
        argv.extend(["--personal-data-root", str(personal_data_root)])
    if market_secret_file is not None:
        argv.extend(["--market-secret-file", str(market_secret_file)])
    if account_binding_file is not None:
        argv.extend(["--account-binding-file", str(account_binding_file)])
    if toss_account_secret_file is not None:
        argv.extend(["--toss-account-secret-file", str(toss_account_secret_file)])
    if nhplug_account_secret_file is not None:
        argv.extend(["--nhplug-account-secret-file", str(nhplug_account_secret_file)])
    argv.extend(["--account-sync-max-age-seconds", str(account_sync_max_age_seconds)])
    if native_store_root is not None:
        argv.extend(["--native-store-root", str(native_store_root)])
    if advisory_state_root is not None:
        argv.extend(["--advisory-state-root", str(advisory_state_root)])
    if historical_decision_root is not None:
        argv.extend(["--historical-decision-root", str(historical_decision_root)])
    if historical_transaction_root is not None:
        argv.extend(["--historical-transaction-root", str(historical_transaction_root)])
    if portfolio_checkpoint_root is not None:
        argv.extend(["--portfolio-checkpoint-root", str(portfolio_checkpoint_root)])
    if approval_store_root is not None:
        argv.extend(["--approval-store-root", str(approval_store_root)])
    if write_principal is not None:
        argv.extend(["--write-principal", str(write_principal)])
    if write_grant is not None:
        argv.extend(["--write-grant", str(write_grant)])
    return argv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--legacy-workspace", type=Path)
    parser.add_argument("--personal-data-root", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--market-provider",
        choices=("none", "toss-readonly-subprocess", "toss-native-subprocess"),
        default="none",
    )
    parser.add_argument("--market-secret-file", type=Path)
    parser.add_argument("--account-binding-file", type=Path)
    parser.add_argument("--toss-account-secret-file", type=Path)
    parser.add_argument("--nhplug-account-secret-file", type=Path)
    parser.add_argument("--account-sync-max-age-seconds", type=int, default=300)
    parser.add_argument("--native-store-root", type=Path)
    parser.add_argument("--advisory-state-root", type=Path)
    parser.add_argument("--historical-decision-root", type=Path)
    parser.add_argument("--historical-transaction-root", type=Path)
    parser.add_argument("--portfolio-checkpoint-root", type=Path)
    parser.add_argument("--approval-store-root", type=Path)
    parser.add_argument("--write-principal", type=Path)
    parser.add_argument("--write-grant", type=Path)
    args = parser.parse_args()
    runtime_root = args.runtime_root.resolve()
    argv = child_argv(
        runtime_root=runtime_root,
        manifest=args.manifest.resolve(),
        legacy_workspace=args.legacy_workspace.resolve() if args.legacy_workspace else None,
        personal_data_root=args.personal_data_root.expanduser().resolve() if args.personal_data_root else None,
        market_provider=args.market_provider,
        market_secret_file=args.market_secret_file.resolve() if args.market_secret_file else None,
        account_binding_file=args.account_binding_file.resolve() if args.account_binding_file else None,
        toss_account_secret_file=args.toss_account_secret_file.resolve() if args.toss_account_secret_file else None,
        nhplug_account_secret_file=args.nhplug_account_secret_file.resolve() if args.nhplug_account_secret_file else None,
        account_sync_max_age_seconds=args.account_sync_max_age_seconds,
        native_store_root=args.native_store_root.resolve() if args.native_store_root else None,
        advisory_state_root=args.advisory_state_root.resolve() if args.advisory_state_root else None,
        historical_decision_root=(
            args.historical_decision_root.resolve() if args.historical_decision_root else None
        ),
        historical_transaction_root=(
            args.historical_transaction_root.resolve() if args.historical_transaction_root else None
        ),
        portfolio_checkpoint_root=args.portfolio_checkpoint_root.resolve() if args.portfolio_checkpoint_root else None,
        approval_store_root=args.approval_store_root.resolve() if args.approval_store_root else None,
        write_principal=args.write_principal.resolve() if args.write_principal else None,
        write_grant=args.write_grant.resolve() if args.write_grant else None,
    )
    os.execve(sys.executable, argv, sanitized_environment())
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
