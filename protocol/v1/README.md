# InvestKitchen Protocol v1 draft — TradeMind compatibility namespace

This directory contains the machine-readable v1 contracts plus the deployable
InvestKitchen reference runtime. Existing `TradeMind` schema IDs, environment
variables, and internal identifiers are compatibility namespace and do not name
the current product. Legacy portfolio/knowledge sources are optional
external compatibility inputs; they do not live in this repository.

The compatibility adapters do **not** migrate or rewrite the supplied legacy
workspace. Native Decision/Transaction state is stored only under an explicit
external state root.

## Contracts

- `schemas/common.schema.json` — shared IDs, time points, money, provenance, and gaps.
- `schemas/capability-discovery.schema.json` — client-visible installed/readiness/exposure state.
- `schemas/capability-result.schema.json` — standalone Capability Result Envelope used by adapters and the Context Composer.
- `schemas/portfolio.schema.json` — Portfolio / Account / Position / Cash / Transaction / Policy projection.
- `schemas/portfolio-materialization.schema.json` — explicit checkpoint + transaction delta에서 만든 projected Portfolio materialization result.
- `schemas/portfolio-checkpoint.schema.json` — persisted Portfolio snapshot + native write-journal cursor/prefix binding.
- `schemas/portfolio-authority-projection.schema.json` — checkpoint 이후 canonical Transaction delta와 materialization을 묶은 authority projection.
- `schemas/portfolio-reconciliation.schema.json` — observed Portfolio와 projected current state를 비교한 review-required reconciliation candidate.
- `schemas/portfolio-reconciliation-acceptance.schema.json` — exact candidate digest에 대한 verified acceptance 결과.
- `schemas/evidence-claim.schema.json` — Evidence / Claim / committed Knowledge generation projection.
- `schemas/decision-context.schema.json` — DecisionRequest, DecisionContext, Assessment, and client/diagnostic split.
- `schemas/operation.schema.json` — operation / preview / approval / receipt semantics.
- `schemas/extension-manifest.schema.json` — Extension capability, permission, network, secret, storage, and background declaration.
- `schemas/instance-manifest.schema.json` — assembled instance capabilities and client exposure.
- `schemas/reflection.schema.json` — PRE/POST Reflection session and method contract. Starting a session is distinct from persisting a Reflection record.
- `schemas/remote-gateway.schema.json` — authenticated principal, access grant,
  replay-bounded wire request, authorization decision, rate-limit signal,
  minimal audit event, and remote response contracts.

## First compatibility adapters

`adapters/` is the executable contract boundary. Legacy readers receive an
explicit external workspace instead of assuming product code and personal data
share one repository.

- `portfolio_legacy.py` — exposes current account records as `portfolio.state`.
- `market_legacy.py` — normalizes legacy provider results as `market.*` without performing network calls in contract tests.
- `knowledge_legacy.py` — exposes committed `knowledge.current` plus bounded deterministic `knowledge.search`.
- `context_composer.py` — combines Capability Result Envelopes into a bounded DecisionContext and performs no investment judgment.
- `reflection_adapter.py` — starts PRE/POST reflection with Portfolio/Decision context remaining optional.
- `capability_registry.py` — projects installed/readiness/client-exposure state from an Instance manifest.
- `gateway_facade.py` — stable read-only client surface for capability discovery,
  Portfolio/Knowledge/Market reads, DecisionContext composition, and optional
  Reflection start. It deliberately has no concrete market-provider import.

The Portfolio/Knowledge compatibility readers are declared as trusted
`system_adapter` fixtures because they temporarily need direct access to the
legacy canonical store. Ordinary provider/analysis/reflection Extensions are
schema-blocked from declaring direct canonical storage access.

## Native Portfolio materialization

`runtime/portfolio_materializer.py` implements the first deterministic bridge
from an explicit base Portfolio checkpoint plus canonical Transaction delta to a
projected current Portfolio. It is a pure projection kernel: it does not mutate
PersonalDataStore, NativeWriteStore, legacy files, or the Synology runtime state.

Materialization v1 deliberately refuses to guess:

```text
- asset identity must match exactly
- base position quantity must be confirmed before applying a trade
- oversell blocks unless the account is explicitly allowed to short
- FX is never synthesized
- cash changes only when an explicit cash basis row is declared
- missing trade amount/price leaves cash unchanged and emits a gap
- correction/corporate-action/other transactions block until semantics exist
- changed position cost basis / average cost are omitted rather than carried forward incorrectly
```

The result carries the checkpoint reference, base/delta digests, deterministic
ordering contract, applied/skipped transaction IDs, projected Portfolio, and
bounded gaps. This is not yet the production Portfolio authority; checkpoint
selection, reconciliation ingestion, and authority cutover remain separate
runtime work.

`runtime/portfolio_checkpoint.py` adds the persistent checkpoint/delta bridge.
A checkpoint stores the full base Portfolio snapshot, its digest, an explicit
snapshot effective time, the native write-journal cursor included by that
snapshot, and a digest/count of the journal prefix through that cursor. Runtime
projection revalidates that prefix before selecting canonical Transaction
resource commits after the cursor. Decision commits and deduplicated operation
events are not Portfolio deltas.

The reference runtime can opt in to this authority path with
`--portfolio-checkpoint-root` together with `--native-store-root`. When enabled,
`portfolio.state` is served from the latest valid checkpoint plus later native
Transactions. Without the flag, the current personal-data Portfolio handler is
unchanged. The deployed Synology profile has not enabled this cutover yet.

The checkpoint journal is now included in backup format v2 together with native
write and approval journals. Verification replays the checkpoint→write cursor
relationship against the backed-up write journal, and restore revalidates the
same linkage before publishing the target state. Legacy format v1 snapshots
remain verifiable/restorable for rollback compatibility.

`runtime/portfolio_reconciliation.py` adds the observation/reconciliation bridge.
It never persists an observation directly. Instead it builds a deterministic
review candidate containing structured account/position/cash differences, gaps,
the reconciled Portfolio snapshot, and a checkpoint draft. Complete holdings or
cash observations may remove omitted rows; partial/unknown observations retain
unseen current state and emit explicit gaps rather than treating absence as zero.

Observed Transaction or Policy rows are not silently promoted through this path.
Canonical Transaction ingestion and Policy mutation remain separate authorities.
The checkpoint cursor is bounded to the journal prefix that can be safely treated
as included by the observation snapshot time; later events remain delta. Date-only
observations do not claim same-day journal events because the ordering is
ambiguous.

Persisting a reconciliation requires an exact candidate digest plus a server-side
acceptance verifier. Accepted checkpoints retain both the reconciliation candidate
reference and the acceptance verification reference in their durable identity.
`deployment/portfolio_reconciliation_cli.py` is the current local operator surface.
`review` is read-only and prints only candidate metadata, structured differences,
and gaps rather than the full reconciled Portfolio/checkpoint draft. `accept`
requires an interactive TTY and the exact `ACCEPT <digest-prefix>` phrase; the
acceptance object is created inside that process and is not accepted from a
caller-supplied JSON file. The in-process verifier is single-use and binds the
exact candidate and acceptance, and the resulting verification ref is persisted
in the accepted checkpoint. This remains a local operator workflow and is not a
ChatGPT write surface. The production Synology `portfolio.state` read path has
since been cut over to the checkpoint/materializer authority.

Reconciliation candidate files contain private Portfolio state and must stay
outside Git. The repository ignores the conventional `reconciliation-candidates/`
directory and `*.reconciliation-candidate.json` files as a secondary safeguard.

```bash
python3 protocol/v1/deployment/portfolio_reconciliation_cli.py review \
  --candidate /secure/runtime/reconciliation-candidate.json

python3 protocol/v1/deployment/portfolio_reconciliation_cli.py accept \
  --candidate /secure/runtime/reconciliation-candidate.json \
  --native-store-root /secure/runtime/native-write \
  --checkpoint-root /secure/runtime/portfolio-checkpoints
```

`runtime/portfolio_shadow.py` and `deployment/portfolio_shadow_cli.py` provide a
privacy-safe read-only shadow path for production-equivalent data. The runner
copies only the native write journal into a temporary workspace, creates ephemeral
checkpoints there, and never creates lock files or state under the source roots.
Its report contains opaque Portfolio refs, state digests, delta/gap counts, and
account/position/cash difference counts; raw account ids, symbols, quantities,
cash values, or local paths are not emitted.

The first real-data shadow run used a personal-data manifest whose SHA-256 matched
the currently deployed Synology bundle and a Synology native-write state with no
journal yet. Every configured Portfolio reproduced exactly through the new
checkpoint/materializer path with zero account/position/cash differences, zero
blocks, and zero materialization gaps. This validates lossless seeding on the
current production snapshot; production transaction-delta replay remains to be
rechecked once native Transactions actually exist.

Production cutover is now complete. Both configured Portfolio scopes were
verified through the live ChatGPT connector with `status=ok`,
`authority=derived_calculation`, checkpoint provenance, and zero returned gaps.
The deployed runtime image is source commit
`7f09fd1b538d8adaaa47736fdddbd53bd0f6e270`.

`runtime/portfolio_authority_seed.py` and
`deployment/portfolio_authority_cli.py` provide the one-time production seed
boundary. Seeding is refused unless the privacy-safe shadow run is exact for every
configured Portfolio, the personal-data snapshot still matches the checkpoint
base digest, and no native Transaction delta appeared between the source snapshot
and cutover verification. Initial cash-delta bases are deliberately conservative:
only an unambiguous `nominal_balance` row may become a cash basis. Legacy
`provider_specific`, `unknown`, or displayed-settlement rows remain snapshot facts
but are not silently promoted into execution-adjusted ledgers.

The Synology profile supports `personal-data` as the rollback mode. Production now
uses `INVESTKITCHEN_PORTFOLIO_AUTHORITY=checkpoint`, which activates the
materialized Portfolio handler after a verified checkpoint journal exists. The deployment flow installs the exact
preflighted image, seeds or re-verifies checkpoints, creates a v2 pre-cutover
backup, flips the authority flag, and waits for health. A failed health transition
restores the previous env automatically; `portfolio-authority-rollback.sh` also
returns the same image to personal-data authority without deleting checkpoints.

`runtime/portfolio_replay_verify.py` and
`deployment/portfolio_replay_verify_cli.py` provide the post-cutover replay check.
They read copies of the Portfolio checkpoint and native-write journals only, verify
checkpoint-to-write prefix integrity, replay canonical Transactions after each
latest checkpoint, and report only opaque Portfolio refs, state digests, change
counts, and materialization gap codes. No raw account, symbol, quantity, or cash
value is included in the report. A production journal with no native Transactions
returns `waiting_for_native_transaction`; this is not treated as an error. The
current Synology production state has two valid checkpoints, zero native-write
events, zero replay blocks/gaps, and is therefore waiting for the first real native
Transaction before the production delta path can be observed end-to-end.

## Personal Session Authority handoff

InvestKitchen now also owns the personal automated-trading session authority handoff.
The wire contract remains the existing `SessionBrief` v1 /
`SessionBriefAmendment` v1 / `MaterialContext` v1 exchange so trading-runtime
candidate, strategy, risk, and execution semantics do not change. The native
producer identity is `investkitchen / session-authority-v1`; the historical
`trademind-session-exchange` package name is retained only as a compatibility
protocol identifier.

The personal source-registration path is deliberately smaller than the legacy
registration framework. ChatGPT performs semantic extraction from the user's raw
material; InvestKitchen then binds the exact raw bytes to the structured bundle,
validates session authority/provenance, stores the source privately outside Git,
and publishes the immutable neutral exchange. `source_registration.py` requires
the raw SHA-256 to match the SessionBrief source document, requires
`SESSION_TRADING_INPUT` authority for the exact session date, and fails closed if
candidate/mapping/ground/amendment/context provenance references an unregistered
document. Exact registration replay is idempotent; conflicting document/source or
bundle bindings are rejected.

The 2026-09-17 migration case was exercised end to end with one raw checkpoint
source. Legacy and InvestKitchen exchanges both produced 21 ordered actionable
symbols plus one context-only unresolved idea and one MaterialContext event. The
normal trading-runtime consumer reported zero rejects/errors, all 21
`candidate_authorized()` and `material_context()` results matched, and the
registered InvestKitchen publish was byte-for-byte identical to the active
InvestKitchen exchange. The legacy exchange remains only as a rollback/golden
oracle.

## Personal advisory writes

The immediate personal advisory workflow has two bounded native write paths.
`runtime/portfolio_update_service.py` turns a full Portfolio observation or a
narrow existing position/cash change into the existing reconciliation contract.
Preview is read-only; persistence still requires exact digest-bound trusted local
approval and produces an accepted Portfolio checkpoint. Narrow no-op updates are
rejected instead of producing observation-only checkpoint churn.

`runtime/native_knowledge_store.py` is an append-only Knowledge overlay. A
`knowledge.commit` request contains a current-state patch plus structured Evidence
and Claims. It validates provenance, semantic fidelity, truth/applicability state,
Evidence references, and time bounds before a preview can be approved. Reads keep
the immutable personal-data bundle as the baseline and overlay committed native
state/Claims; `knowledge.search` exposes canonical Claims from the merged view.

The runtime now also implements four bounded connector actions:
`preview_knowledge_update`, `apply_knowledge_update`,
`preview_portfolio_update`, and `apply_portfolio_update`. Preview actions are
read-only. Apply actions are mutating, confirmation-required, resolve only a
server-owned prior preview, reject stale state, and consume the preview once.
No Decision, Transaction, order, execution, or market-data write action is
exposed.

The connector actions are registered only when the runtime has an explicit
server-side write principal/grant and approval store. Client arguments never
carry trusted identity, grant, ApprovalReceipt, or canonical prior-preview JSON.
Knowledge apply uses `TrustedApprovalStore`; Portfolio apply rechecks the latest
checkpoint, write cursor, and current Portfolio digest before persistence.

On ChatGPT plans that do not surface custom MCP write actions, the same domain
services remain usable through `deployment/advisory_write_bridge.py`, normally
invoked by the private Synology `deploy/synology/advisory-write.sh` wrapper via
the connected local operator tool. That fallback keeps opaque pending previews
private, requires the exact confirmation phrase, stale-checks before apply, and
verifies canonical read-back before reporting success.

The current personal production deployment intentionally leaves connector writes
disabled in the running MCP container because the user's ChatGPT Pro custom MCP
surface is read/fetch-only. The host fallback is deployed separately from the
active runtime image: the healthy container remains at source `66ccc565...`, while
the pinned host bridge source is `ee2be237...`. This avoids an unnecessary runtime
restart and keeps the MCP write implementation dormant until the platform can
surface confirmed write actions.

The first production native Knowledge write was exercised on 2026-09-17 with a
user-provided analyst pre-open transcript. One Evidence record and six canonical
Claims were committed as generation
`knowledge-generation:2026-09-17-hachangwan-preopen-1`, then independently read
back through `knowledge.current` and `knowledge.search`. Portfolio update has been
smoke-tested on a restored production snapshot, but the live Portfolio has not yet
received a post-cutover observation write.

Operator surfaces:

```bash
python3 protocol/v1/deployment/session_source_registration_cli.py register \
  --source /private/source.md \
  --bundle /private/session-bundle.json \
  --document-id <document-id> \
  --registration-root /private/investkitchen/session-source-registrations

python3 protocol/v1/deployment/session_source_registration_cli.py publish \
  --bundle /private/session-bundle.json \
  --registration-root /private/investkitchen/session-source-registrations \
  --exchange-root /private/investkitchen/session-exchange
```

## Read-only projection

Generate current compatibility fixtures:

```bash
python3 protocol/v1/tools/project_legacy_records.py \
  --workspace /path/to/legacy-workspace \
  --portfolio-id portfolio-id-1 \
  --portfolio-id portfolio-id-2 \
  --output-dir /tmp/trademind-projections
```

`protocol/v1/fixtures/generated/` is gitignored because real compatibility
projections may contain private portfolio or knowledge data.

Validate schemas and fixtures:

```bash
python3 -m pytest protocol/v1/tests -q
```

The projector may only read current canonical records and write to the explicitly supplied fixture directory. Unknown semantics must remain explicit migration gaps instead of being guessed.

This is a draft compatibility boundary, not yet a stable public API.

## Read-only Gateway facade

The first client-facing semantic surface is intentionally transport-neutral.
`ReadOnlyGatewayFacade` is not yet an HTTP or MCP server; a later transport can
wrap these methods without exposing legacy storage/provider implementation:

```text
get_capabilities
get_portfolio_state
get_current_knowledge
search_knowledge
get_market_quote
get_market_ohlcv
build_decision_context
get_decision_history
get_transactions
start_reflection
```

Market execution is injected behind stable capability IDs. The Gateway does not
import a concrete broker SDK or provider module. Public projections strip local
paths, legacy adapter identities, provider brands, and runtime metrics while
preserving capability status, gaps, freshness, authority, and safe provenance.

## Authenticated remote Gateway contract

`security/remote_gateway.py` and
`transport/authenticated_remote_contract.py` define the security boundary that a
future HTTP/MCP transport must satisfy. They do **not** choose or implement an
authentication technology.

The client wire request contains only:

```text
request_id
instance_id
method / params
issued_at / expires_at
nonce
```

It cannot declare `subject_user_id`, `client_id`, permissions, grant identity,
or credential binding. Those values are supplied server-side only after an
outer transport has authenticated the caller.

The server-side `AccessGrant` binds:

```text
instance
user subject
client
credential binding
permissions
portfolio scope
tool allowlist
request TTL / allowed future clock skew
policy version
expiry
```

Authorization checks nested `build_decision_context` capabilities as well as the
outer tool permission. One request may not silently mix different portfolio
scopes. Nonces are one-use within the replay guard, and audit records contain
only identity references, permission/scope decisions, request digest, reason,
and result status — not raw tool arguments or portfolio/reflection payloads.

`remote-gateway.contract.json` is the current hand-authored security fixture.
The in-memory replay guard and authenticated harness exist only to validate the
contract; durable nonce storage, credential format, TLS/tunnel, HTTP/MCP, and
rate-limit implementation remain deployment decisions.

## Chosen ChatGPT reference connection

OpenAI's official documentation was re-checked on 2026-09-15. The current
reference decision is:

```text
InvestKitchen domain protocol (TradeMind compatibility namespace): transport-neutral
ChatGPT integration protocol: MCP
private/self-hosted ChatGPT connection: Secure MCP Tunnel
public plugin distribution: separate future deployment path
```

Secure MCP Tunnel keeps the private MCP server behind the user's network
boundary and uses `tunnel-client` to make an outbound HTTPS connection to
OpenAI. The tunnel can forward to a private MCP server over stdio or HTTP. It is
not a public-plugin distribution mechanism; public plugins require a stable,
publicly reachable HTTPS MCP endpoint.

`transport/stdio_rpc.py` remains a local smoke harness and is not itself an MCP
server. The next transport implementation is a read-only MCP adapter that maps
the existing machine-readable tool catalog to MCP discovery/calls and delegates
execution back to `ReadOnlyGatewayFacade`.

That adapter now exists at `transport/mcp_stdio.py`. It targets MCP protocol
revision `2026-07-28`, whose core is stateless: there is no
`initialize`/`initialized` handshake. Each request carries the protocol revision
and client capabilities in `params._meta`, and clients can probe the server with
`server/discover`.

The adapter currently implements only:

```text
server/discover
tools/list
tools/call
```

It maps `tool_catalog.json` to deterministic MCP tool definitions, marks the
surface read-only, keeps tool-list caching private, returns both
`structuredContent` and a JSON TextContent fallback, and filters tools whose
bound Runtime capability is not actually usable. Local implementation paths and
compatibility identities remain stripped by the Gateway projection.

The current market binding is also self-contained inside InvestKitchen.
`providers/toss_readonly_worker.py` runs as a scrubbed one-shot subprocess for
`market.quote` and `market.ohlcv`; it no longer imports a legacy workspace.
Credentials are delivered only by secret-file path. The worker uses fixed
read-only Toss endpoints, validates bounded symbols/interval/count, disables
redirects, caps response size, and exposes no account/order/execution/write
method. The parent Runtime keeps a small process-memory cache (3 seconds for
quotes, 15 seconds for OHLCV); cache hits are surfaced as
`source_mode=cache` / `freshness=cached` rather than as fresh network reads.

`TradeMindMCPServer` also has explicit server-side tool visibility and
authorization hooks. The local single-user smoke leaves them permissive, but a
future OAuth/remote transport can inject a Principal/Grant-bound policy without
treating MCP `clientInfo` or other client-supplied metadata as authentication
authority.

Local MCP smoke:

```bash
python3 protocol/v1/transport/mcp_stdio.py <<'EOF'
{"jsonrpc":"2.0","id":"d","method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"local-smoke","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}
EOF
```

## Secure MCP Tunnel reference wiring

The private self-host reference wiring lives under `deployment/`:

```text
secure_tunnel.py
tunnel_stdio_launcher.py
self-hosted-readonly.wiring.json
```

The process boundary is intentionally:

```text
tunnel-client
  has CONTROL_PLANE_API_KEY
        ↓ --mcp-command
tunnel_stdio_launcher.py
  replaces the environment with a small allowlist
        ↓ exec
mcp_stdio.py
  does not receive CONTROL_PLANE_API_KEY
        ↓ optional market capability
toss_readonly_worker.py
  receives a secret-file path, not secret values in argv/env
```

The market worker is a compatibility process boundary, **not** the final
third-party Extension sandbox. It reduces accidental credential inheritance but
does not claim OS-level filesystem/network isolation.

Local tunnel-child preflight (no OpenAI connection required):

```bash
python3 protocol/v1/deployment/secure_tunnel.py preflight
```

To include legacy Portfolio/Knowledge reads, pass the external compatibility
source explicitly:

```bash
python3 protocol/v1/deployment/secure_tunnel.py preflight \
  --legacy-workspace /path/to/trademind-framework
```

To preflight the optional current Toss read binding, supply a secret file from a
runtime secret mount outside the repository:

```json
{"client_id":"...","client_secret":"..."}
```

```bash
python3 protocol/v1/deployment/secure_tunnel.py preflight \
  --legacy-workspace /path/to/trademind-framework \
  --market-provider toss-readonly-subprocess \
  --market-secret-file /run/secrets/trademind-toss.json
```

The helper can render the official tunnel-client stdio profile commands without
reading or printing the runtime API key:

```bash
python3 protocol/v1/deployment/secure_tunnel.py render \
  --tunnel-id tunnel_xxx \
  --profile trademind-readonly
```

`CONTROL_PLANE_API_KEY` belongs to the `tunnel-client` process environment. Do
not put its value in `--mcp-command`, the InvestKitchen instance manifest, or repository
configuration.

The platform facts above are external dependencies and must be revalidated
before a release. Current official references:

- https://developers.openai.com/api/docs/guides/developer-mode
- https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://developers.openai.com/plugins/build/auth

## First write contract

The first write boundary is intentionally limited to Decision and Transaction
records.  It is a contract/preview implementation only; the current MCP app
remains read-only.

```text
schemas/decision-record.schema.json
schemas/write-request.schema.json
schemas/mutation-preview.schema.json
runtime/write_preview.py
```

`transaction.record` separates evidence that an execution actually occurred
(`occurrence_attestation`) from authorization to mutate canonical state
(`ApprovalReceipt`).  A user statement such as "I bought 10 shares" can be
execution evidence, but it is not by itself a reusable database-write approval.

All previews remain:

```text
operation.state = awaiting_approval
approval = null
apply_state = not_applied
```

The preview builder hashes the exact canonical payload and performs no
filesystem/database mutation.  Caller-supplied booleans such as
`user_confirmed=true` are not part of the write request contract.

As of 2026-09-15, the OpenAI Developer Mode documentation states that Pro can
connect read/fetch MCP servers while full write/modify MCP support is currently
available to Business and Enterprise/Edu in beta.  For that reason this source
tree does not expose a hidden canonical-write path through the current Pro MCP
deployment.  Revalidate this product constraint before enabling write tools.

Contract validation:

```bash
python3 -m pytest protocol/v1/tests/test_write_contract.py -q

python3 -m pytest protocol/v1/tests -q
```

## Native write reference store

`runtime/native_write_store.py` implements the first server-side apply boundary
without exposing any new MCP write tool.  The caller must provide an explicit
store root; there is no default path into `accounts/` or another legacy
canonical source.

The reference backend is append-only JSONL with a lock and `fsync`.  It is a
contract/runtime reference, not a final database choice.  A successful commit
stores one canonical Decision or Transaction together with its OperationReceipt
and bounded audit metadata.

`apply_mutation()` fails closed unless a server-side `approval_verifier` confirms
the ApprovalReceipt and the receipt still matches the exact preview id, payload
digest, target, base version, authenticated user/client, permissions, portfolio
scope, and expiry constraints.

Transaction retries also preserve the stronger domain rule: a repeated known
provider/source execution identity cannot create a duplicate canonical
Transaction.  A conflicting payload for the same execution identity is blocked
instead of overwritten.

```bash
python3 -m pytest protocol/v1/tests/test_native_write_store.py -q
```

The MCP tool catalog remains read-only; the native apply API is not reachable
through the current ChatGPT Pro tunnel deployment.

## Trusted approval + native history reference

`security/trusted_approval.py` adds a server-owned, append-only approval journal.
An ApprovalReceipt is verified only when its exact digest exists as an active
issuance record in that journal and its user/client/credential binding still
matches the server-side Principal/Grant. A caller-created JSON object that merely
looks like an ApprovalReceipt does not verify. Revocation is append-only as well.

Approval issuance has a distinct `operation.approve` permission in addition to
the domain action permission (`decision.create` or `transaction.record`) and the
portfolio-scope check. Receipt TTL is bounded by the preview and current grant
expiry.

`deployment/local_approval_cli.py` is the first concrete trusted interaction
surface. It runs locally, requires an interactive TTY, displays the exact target,
effect, warnings, and payload digest, and issues a receipt only after the operator
types `APPROVE <digest-prefix>`. It does not apply the mutation and is not an MCP
tool. Principal and Grant files are server-side inputs to this local operator
surface, not client tool arguments.

`adapters/native_history.py` projects the native write journal as bounded
`decision.history` and `transaction.history` Capability Results. Raw write
journal events, approval evidence, credential binding, and write-audit metadata
are not returned in the history projection.

History tools are optional runtime bindings:

```text
get_decision_history
get_transactions
```

The full-reference manifest declares them, but the Gateway marks them
`runtime_handler_missing` unless an explicit native store root is attached.
The current Synology production composition attaches the native state root, so
runtime preflight reports both history capabilities ready. The currently loaded
ChatGPT connector metadata in the verified client session still presents the
original six direct tool functions; runtime readiness and client tool exposure
are tracked separately. Supplying `--native-store-root <path>` makes the two
history tools discoverable at the MCP runtime without enabling any write tool.

Validation:

```bash
python3 -m pytest protocol/v1/tests/test_trusted_approval_and_history.py -q

python3 -m pytest protocol/v1/tests -q
```

## Persistent storage, backup, and restore contract

The first persistent layout is host-neutral and machine-readable:

```text
deployment/self-hosted-storage.layout.json
schemas/storage-layout.schema.json
schemas/backup-manifest.schema.json
runtime/storage_recovery.py
deployment/storage_recovery_cli.py
```

Storage layout v2 keeps the existing three-journal Decision/Transaction/Portfolio
consistency contract and now snapshots the native Knowledge journal alongside it:

```text
<state-root>/
  native-write/write-journal.jsonl
  native-write/knowledge-journal.jsonl
  approvals/approval-journal.jsonl
  portfolio-checkpoints/checkpoints.jsonl
```

Lock files are runtime coordination artifacts and are not copied into backups.
Backup format v2 acquires the existing three journal locks plus the native
Knowledge journal lock before reading any state, validates JSONL plus
approval→write, approval→Knowledge, and checkpoint→write-cursor linkage, writes a
SHA-256 manifest, fsyncs the snapshot, and then atomically publishes the completed
snapshot directory. A backup is not reported successful until the completed
snapshot is verified again.

The verifier and restore path remain backward-compatible with format v1 snapshots
that contain only native-write and approval journals, and with pre-Knowledge v2
snapshots containing the original three journals. New backups are always v2 and
include `native-write/knowledge-journal.jsonl` even when it is empty.

The first real Synology v2 restore drill completed on the production NAS filesystem.
The live state was backed up, verified, restored into a separate staging root, and
all three restored journal hashes matched the snapshot before staging was removed.
Because the live journals were still empty at that point, a second NAS-local
synthetic drill seeded exactly one write, one trusted approval, and one Portfolio
checkpoint; v2 backup/restore then reloaded all three records and successfully
projected the restored checkpoint. Synthetic drill data was removed afterwards.
The validated live backup snapshot was retained under the configured backup root.

Restore is fail-closed: the backup must match the expected logical
`instance_id`, all file hashes and record counts must match, journal invariants
must pass, every committed write must link to an issued trusted approval, and
every Portfolio checkpoint cursor/prefix digest must match the backed-up native
write journal.
Restore first materializes and validates a staging directory and refuses to
overwrite a non-empty target state root.

```bash
python3 protocol/v1/deployment/storage_recovery_cli.py backup \
  --state-root "$TRADEMIND_V1_STATE_ROOT" \
  --backup-root "$TRADEMIND_V1_BACKUP_ROOT" \
  --instance-id '<logical-instance-id>'

python3 protocol/v1/deployment/storage_recovery_cli.py verify \
  --snapshot '<snapshot-dir>' \
  --instance-id '<logical-instance-id>'

python3 protocol/v1/deployment/storage_recovery_cli.py restore \
  --snapshot '<snapshot-dir>' \
  --target-state-root '<empty-staging-root>' \
  --instance-id '<logical-instance-id>'
```

The manifest hashes provide corruption/tamper detection relative to the
manifest; they are not a signature against an attacker who can rewrite both the
snapshot and manifest. Operational backups therefore still need protected or
off-host copies. Automatic pruning is intentionally disabled in this first
contract so a runtime cannot silently delete the last known-good backup.

The current test suite performs synthetic v2 restores including a persisted
Portfolio checkpoint and a native Knowledge generation, and also verifies that
pre-Knowledge v2 and legacy v1 snapshots remain restorable. A real Synology
restore drill for the Knowledge-extended snapshot is still an operational follow-up.

Validation:

```bash
python3 -m pytest protocol/v1/tests/test_storage_recovery.py -q

python3 -m pytest protocol/v1/tests -q
```

For macOS development, the official `openai/tunnel-client` repository currently
documents Homebrew as the supported install path:

```bash
brew install openai/tools/tunnel-client
tunnel-client --version
tunnel-client help quickstart
```

On the current Apple Silicon development machine, `tunnel-client 0.0.14` was
validated successfully. The production reference has since been cut over to a
Synology `linux/amd64` container using the official OpenAI
`tunnel-client-runtime v0.0.14` release archive with its published SHA-256.
The exact deployment image was verified on the builder, transferred to the NAS,
loaded without a NAS-side production build, reported healthy, and answered real
ChatGPT capability and native Knowledge calls after the Mac runtime was stopped.

`deployment/secure_tunnel.py render` validates the installed client's current
tunnel ID shape (`tunnel_` followed by exactly 32 lowercase hexadecimal
characters) before rendering `init / doctor / run` commands.

## Local stdio transport smoke

`transport/stdio_rpc.py` is a local line-delimited JSON harness. It is not an
HTTP endpoint, remote authentication boundary, or MCP server. The transport has
two methods only:

```text
tools.list
tools.call
```

`transport/tool_catalog.json` is the machine-readable read-only tool catalog.
Each line on stdin produces exactly one bounded JSON response on stdout.

Example:

```bash
python3 protocol/v1/transport/stdio_rpc.py <<'EOF'
{"request_id":"caps","method":"tools.call","params":{"name":"get_capabilities","arguments":{}}}
{"request_id":"knowledge","method":"tools.call","params":{"name":"search_knowledge","arguments":{"query":"SK하이닉스","limit":1}}}
EOF
```

The default local harness intentionally has no concrete market handler attached.
Capability discovery therefore reports `market.quote` / `market.ohlcv` as
`ready=false` with `runtime_handler_missing`, and a direct market call returns a
bounded `unavailable` Capability Result instead of bypassing the Gateway.
