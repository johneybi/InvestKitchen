#!/usr/bin/env python3
"""Render and locally preflight the Secure MCP Tunnel stdio wiring.

This helper never reads CONTROL_PLANE_API_KEY and never invokes tunnel-client.
It only builds the documented tunnel-client command and verifies the private
MCP child command locally.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
MCP_VERSION = "2026-07-28"
_TUNNEL_ID_RE = re.compile(r"^tunnel_[0-9a-f]{32}$")


def launcher_argv(
    *,
    runtime_root: Path,
    manifest: Path,
    legacy_workspace: Path | None = None,
    personal_data_root: Path | None = None,
    market_provider: str = "none",
    market_secret_file: Path | None = None,
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
    launcher = runtime_root / "protocol" / "v1" / "deployment" / "tunnel_stdio_launcher.py"
    argv = [
        sys.executable,
        str(launcher),
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


def tunnel_init_argv(*, tunnel_id: str, profile: str, mcp_command: str) -> list[str]:
    if not _TUNNEL_ID_RE.fullmatch(tunnel_id):
        raise ValueError("tunnel_id must match tunnel_<32 lowercase hexadecimal characters>")
    return [
        "tunnel-client",
        "init",
        "--sample",
        "sample_mcp_stdio_local",
        "--profile",
        profile,
        "--tunnel-id",
        tunnel_id,
        "--mcp-command",
        mcp_command,
    ]


def _mcp_meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": MCP_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "trademind-tunnel-preflight", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _request(request_id: str, method: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {"_meta": _mcp_meta()},
    }


def run_preflight(command: list[str], *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    payload = "\n".join(
        json.dumps(row, ensure_ascii=False)
        for row in (_request("discover", "server/discover"), _request("tools", "tools/list"))
    ) + "\n"
    completed = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        return {"ok": False, "reason": "mcp_process_failed", "returncode": completed.returncode}
    try:
        rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return {"ok": False, "reason": "invalid_mcp_output"}
    if len(rows) != 2 or any("error" in row for row in rows):
        return {"ok": False, "reason": "mcp_preflight_failed"}
    tools = [str(tool.get("name")) for tool in rows[1].get("result", {}).get("tools", [])]
    return {
        "ok": True,
        "supported_versions": rows[0].get("result", {}).get("supportedVersions", []),
        "tools": tools,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "render"))
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    parser.add_argument("--legacy-workspace", type=Path)
    parser.add_argument("--personal-data-root", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "protocol" / "v1" / "fixtures" / "full-reference.instance.json",
    )
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
    parser.add_argument("--profile", default="trademind-readonly")
    parser.add_argument("--tunnel-id")
    args = parser.parse_args()

    runtime_root = args.runtime_root.resolve()
    legacy_workspace = args.legacy_workspace.resolve() if args.legacy_workspace else None
    personal_data_root = args.personal_data_root.expanduser().resolve() if args.personal_data_root else None
    manifest = args.manifest.resolve()
    secret_file = args.market_secret_file.resolve() if args.market_secret_file else None
    account_binding_file = args.account_binding_file.resolve() if args.account_binding_file else None
    toss_account_secret_file = args.toss_account_secret_file.resolve() if args.toss_account_secret_file else None
    nhplug_account_secret_file = args.nhplug_account_secret_file.resolve() if args.nhplug_account_secret_file else None
    native_store_root = args.native_store_root.resolve() if args.native_store_root else None
    advisory_state_root = args.advisory_state_root.resolve() if args.advisory_state_root else None
    historical_decision_root = (
        args.historical_decision_root.resolve() if args.historical_decision_root else None
    )
    historical_transaction_root = (
        args.historical_transaction_root.resolve() if args.historical_transaction_root else None
    )
    portfolio_checkpoint_root = args.portfolio_checkpoint_root.resolve() if args.portfolio_checkpoint_root else None
    approval_store_root = args.approval_store_root.resolve() if args.approval_store_root else None
    write_principal = args.write_principal.resolve() if args.write_principal else None
    write_grant = args.write_grant.resolve() if args.write_grant else None
    if args.market_provider != "none" and secret_file is None:
        parser.error("--market-secret-file is required for the selected market provider")
    if (toss_account_secret_file is not None or nhplug_account_secret_file is not None) and account_binding_file is None:
        parser.error("--account-binding-file is required for account provider secrets")
    if args.account_sync_max_age_seconds < 0:
        parser.error("--account-sync-max-age-seconds must be nonnegative")
    command = launcher_argv(
        runtime_root=runtime_root,
        manifest=manifest,
        legacy_workspace=legacy_workspace,
        personal_data_root=personal_data_root,
        market_provider=args.market_provider,
        market_secret_file=secret_file,
        account_binding_file=account_binding_file,
        toss_account_secret_file=toss_account_secret_file,
        nhplug_account_secret_file=nhplug_account_secret_file,
        account_sync_max_age_seconds=args.account_sync_max_age_seconds,
        native_store_root=native_store_root,
        advisory_state_root=advisory_state_root,
        historical_decision_root=historical_decision_root,
        historical_transaction_root=historical_transaction_root,
        portfolio_checkpoint_root=portfolio_checkpoint_root,
        approval_store_root=approval_store_root,
        write_principal=write_principal,
        write_grant=write_grant,
    )

    if args.command == "preflight":
        print(json.dumps(run_preflight(command), ensure_ascii=False, indent=2))
        return 0

    if not args.tunnel_id:
        parser.error("--tunnel-id is required for render")
    mcp_command = shlex.join(command)
    try:
        init = tunnel_init_argv(tunnel_id=args.tunnel_id, profile=args.profile, mcp_command=mcp_command)
    except ValueError as exc:
        parser.error(str(exc))
    print(shlex.join(init))
    print(shlex.join(["tunnel-client", "doctor", "--profile", args.profile, "--explain"]))
    print(shlex.join(["tunnel-client", "run", "--profile", args.profile]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
