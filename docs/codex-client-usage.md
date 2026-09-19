# Codex client usage — personal InvestKitchen

Codex is a second private MCP client for InvestKitchen. ChatGPT remains the
primary conversational/advice surface. Codex adds the bounded operator path that
the personal ChatGPT Pro custom MCP surface cannot currently expose.

## Client contract

```text
ChatGPT Pro
  → OpenAI Secure MCP Tunnel
  → InvestKitchen read surface

Codex
  → SSH stdio
  → InvestKitchen read surface
  → shared market.quote / market.ohlcv read surface
  → Knowledge / Portfolio observation preview + apply
```

Both clients read the same authoritative Synology state. Codex does not own a
second Portfolio or Knowledge database.

Portfolio scope must be discovered from InvestKitchen, not guessed. When
`list_portfolios` is available, Codex should call it before a Portfolio request
whose scope is not explicit. In the current personal deployment it returns
`portfolio-a` and `portfolio-b`. A fabricated identifier such as `portfolio-alpha`, `default`,
or `main` is never a valid fallback.

When the Synology Toss read-only market provider is enabled, both ChatGPT and
Codex also use the same `market.quote` and `market.ohlcv` capability. The native
provider uses Toss's unified security-symbol namespace, including Korean numeric
symbols, Korean alphanumeric ETF identifiers, and U.S. tickers. Quote responses
that contain USD instruments also include a synchronized `USD/KRW` exchange-rate
observation when available; missing FX makes the result partial rather than
causing USD positions to be silently converted with another source. Market reads
do not add any permission to the Codex write grant and do not expose order,
execution, or market-data mutation tools.

The Codex write grant is intentionally limited to:

- `knowledge.commit`
- `portfolio.update`
- `operation.approve`

The only mutating MCP actions are:

- `apply_knowledge_update`
- `apply_portfolio_update`

Preview actions are non-mutating. Decision, Transaction, order, execution, and
market-data writes are not exposed by this client.

## Codex protocol compatibility

The InvestKitchen stdio adapter targets MCP `2026-07-28`. Codex CLI `0.154.0`
can use that mode when the under-development modern MCP feature is enabled and
the server config opts in explicitly:

```toml
[features]
mcp_2026_07_28 = true

[mcp_servers.investkitchen]
command = "ssh"
args = [
  "-T",
  "-o", "BatchMode=yes",
  "-o", "ClearAllForwardings=yes",
  "-o", "LogLevel=ERROR",
  "user@nas",
  "/volume1/docker/trademind/codex-mcp.sh",
]
env = { CODEX_MCP_PROTOCOL_VERSION = "2026-07-28" }
default_tools_approval_mode = "writes"
```

`default_tools_approval_mode = "writes"` is important. The server-generated
digest-bound confirmation phrase is an integrity binding, not by itself proof
that a human approved the mutation: a model can repeat text. Codex should show
its own write-tool approval UI for the apply actions. That UI is a client-side
execution gate; InvestKitchen does not receive a cryptographic attestation of the
human click. Independently, the server checks the exact prior preview,
confirmation phrase, current state, scope, expiry, staleness, and replay status.

If Codex drops the MCP process or reconnects between preview and apply, the
process-local pending preview is intentionally lost and apply fails closed. Build
a new preview instead of reconstructing it client-side.

## Synology launcher

`deploy/synology/codex-mcp.sh` runs outside the ChatGPT Tunnel container. In the
current personal deployment, the existing private SSH account is a trusted
single-user operator boundary; it is not yet a Codex-specific credential proof.
The launcher:

- derives Portfolio scope from the private personal-data manifest;
- creates a short-lived server-owned Codex Principal/Grant;
- keeps authority fields out of MCP tool arguments;
- uses the existing native Knowledge, write journal, checkpoint, and approval stores;
- requires checkpoint Portfolio authority;
- scrubs the remote process environment before starting `mcp_stdio.py`;
- never needs Docker or `sudo`.

For a hardened permanent setup, use a dedicated SSH key constrained to this
launcher (forced command; no PTY, forwarding, agent forwarding, or X11). The
personal deployment may first validate the flow with the existing private NAS
account, but the key-specific forced-command boundary is the desired steady
state.

## Operating flow

Portfolio/current-market reads should use InvestKitchen first:

```text
unknown Portfolio scope
→ list_portfolios
→ portfolio.state for the explicit/server-listed scope
→ market.quote / market.ohlcv when needed
→ calculation / analysis
→ legacy analyze-live-markets components only for missing analytical layers
```

The `analyze-live-markets` skill is an analysis/routing layer after authoritative
MCP facts are collected. It must not replace InvestKitchen Portfolio/Market reads
with old local `load_account_records` / Toss fetcher paths while those MCP
capabilities are usable. A stale Codex thread may cache an older MCP tool list;
start a fresh Codex thread after a server tool-surface deployment before declaring
a newly deployed capability unavailable.

Knowledge:

```text
source material
→ preview_knowledge_update
→ review
→ Codex write approval UI
→ apply_knowledge_update
→ knowledge.current / knowledge.search read-back
```

Portfolio observation:

```text
observed holdings/cash change
→ current portfolio.state
→ preview_portfolio_update
→ reconciliation review
→ Codex write approval UI
→ apply_portfolio_update
→ portfolio.state read-back
```

Do not describe a write as complete until the apply result and authoritative
read-back both succeed.

## Other MCP clients

Google Antigravity currently supports custom MCP servers and permission-gated MCP
tool execution, so it is a plausible third client. It is not part of the current
production scope. Add it only after the Codex path is stable and reuse this same
server-owned preview/apply and client-specific grant model; do not add an
Antigravity-specific mutation shortcut.
