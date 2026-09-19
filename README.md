# InvestKitchen

InvestKitchen is a self-hosted investment decision workspace that keeps **portfolio state, source-backed knowledge, policy, decision history, and reflection** behind a strict runtime boundary while AI clients handle conversation, research, comparison, and explanation.

It is not an auto-trading bot. The core idea is to keep facts, external opinions, user policy, decisions, and transactions as different authorities instead of blending them into chat memory.

## What it provides

- **Portfolio authority** — accounts, positions, cash observations, checkpoints, reconciliation, replay verification
- **Knowledge authority** — evidence, claims, provenance, freshness, supersession, bounded search
- **Policy and opinion weighting** — versioned advisory policy plus horizon-aware speaker weighting and reserve handling
- **Decision context** — safe composition of portfolio, policy, knowledge, history, and market capability results
- **History** — durable Decision and Transaction read models
- **Approval boundary** — preview → explicit approval → apply → read-back for bounded mutations
- **Read-only market/account adapters** — provider-neutral runtime contracts with credentials kept outside Git
- **MCP transport** — ChatGPT/Codex-compatible stdio gateway and Secure MCP Tunnel deployment wiring
- **Self-hosted deployment** — Synology-oriented Docker deployment templates

## Product boundary

```text
AI client
  intent · research · reasoning · explanation

InvestKitchen runtime
  state · evidence · calculation · policy · authorization · persistence
```

Core invariants:

- Chat memory is never authoritative portfolio state.
- Advice != Decision != Transaction.
- A provider observation does not automatically authorize a canonical write.
- External opinions remain attributed opinions; they do not become user policy automatically.
- Missing/conflicting identity fails closed instead of being guessed.
- Write operations are scoped, previewed, approval-bound, replay-protected, and read back after apply.
- Real brokerage credentials, holdings, cash balances, personal policy values, journals, and backups stay outside the repository.

## Repository layout

```text
protocol/v1/
  adapters/       capability adapters and context composition
  runtime/        portfolio, knowledge, policy, history and write services
  providers/      read-only provider adapters/workers
  security/       trusted approval and gateway authorization
  transport/      MCP/stdio tool surface
  schemas/        protocol contracts
  tests/          synthetic product tests

deploy/synology/  self-hosted Docker deployment templates
docs/             product and architecture documentation
```

## Quick start

Python 3.11+ is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python3 -m pytest protocol/v1/tests -q
```

All tests and examples are designed to run without real brokerage accounts or personal portfolio data.

## MCP surfaces

Representative read capabilities:

- `portfolio.state`
- `knowledge.current`
- `knowledge.search`
- `policy.current`
- `opinion.weighting.current`
- `opinion.consensus`
- `decision.context`
- `decision.history`
- `transaction.history`
- `market.quote`
- `market.ohlcv`

Bounded write tools are exposed only when the server-owned grant permits them. Examples include Knowledge, Portfolio observation, account synchronization, Policy, and Opinion Weighting preview/apply flows.

Client capability differs by platform. A runtime may therefore expose read-only tools to one client and approved write tools to another while both use the same authoritative state.

## Opinion weighting

InvestKitchen can preserve a versioned weighting policy for attributed source views. The model supports:

- short / medium / long-term anchor weights
- a supplemental speaker pool
- reserve weight that is **not automatically redistributed** when evidence is missing
- per-speaker multipliers
- per-speaker, per-approach multipliers and decision roles

This produces an informational consensus view; it does not authorize an investment action or an order.

## Self-hosting

Synology deployment templates are under [`deploy/synology/`](deploy/synology/README.md). Runtime state, secrets, backups, account bindings, and personal data are mounted from paths outside this repository.

Never commit a real credential or generated personal-data bundle. See [`SECURITY.md`](SECURITY.md) and [`docs/public-data-boundary.md`](docs/public-data-boundary.md).

## Documentation

- [`docs/product-plan.md`](docs/product-plan.md) — product definition and decision loops
- [`docs/architecture.md`](docs/architecture.md) — runtime/client authority boundaries
- [`docs/native-knowledge-write.md`](docs/native-knowledge-write.md) — native Knowledge write model
- [`docs/personal-advisory-write-path.md`](docs/personal-advisory-write-path.md) — bounded advisory write architecture
- [`docs/codex-client-usage.md`](docs/codex-client-usage.md) — write-capable MCP client model
- [`docs/chatgpt-app-product-opportunity.md`](docs/chatgpt-app-product-opportunity.md) — possible public ChatGPT app direction
- [`protocol/v1/README.md`](protocol/v1/README.md) — protocol reference

## Scope

InvestKitchen intentionally does **not** provide automatic real-money order execution. The reference implementation focuses on information, state, analysis context, explicit user policy, and controlled data mutation.

## License

No open-source license has been selected yet. Until a license is added, normal copyright rules apply.
