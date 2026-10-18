# Native advisory Plan lifecycle

Issue: [#9](https://github.com/johneybi/InvestKitchen/issues/9).

Implementation and synthetic local tests are separate from release, deployment
and operational verification in GPT/Gemini. This feature does not itself deploy
or replace any personal Plan.

## Client contract

`get_active_plans(portfolio_id)` reads the authoritative active Plan.
`get_plan_history(portfolio_id, plan_id?, limit=50)` returns bounded versions and
curated lifecycle events. Internal receipts and raw history journals are never
returned. `limit` is at most 100; `truncated`, `total_plans`, and `total_events`
describe the bounded result.

`preview_plan_update({update})` accepts one of three operations:

- `create`: create the first active Plan, or a new one after the prior Plan closed.
- `replace`: require the exact active `plan_id`, retain it as `superseded`, and
  create a new active version linked by `supersedes` / `superseded_by`.
- `close`: require the exact active `plan_id` and retain it as `closed`.

One Plan may be active per Portfolio. A Plan is conditional advisory information:
`order_authorized=false`, `automatic_execution=false`, and current Portfolio and
Market revalidation remain required. Saving a Plan does not validate every
account or record a Decision, Transaction, trade approval, or execution.

Synthetic creation request:

```json
{
  "update": {
    "operation": "create",
    "portfolio_id": "portfolio-alpha",
    "source_ref": "user-statement:synthetic-board",
    "reason": "Adopt the reviewed conditional board",
    "plan": {
      "title": "Synthetic advisory board",
      "objective": "Review current observations before a separate decision",
      "stages": [{
        "stage_id": "revalidate",
        "conditions": ["Review freshness and gaps for every relevant account"],
        "actions": [{
          "account_id": "account-alpha",
          "symbol": "005930",
          "description": "Evaluate an allocation after current Portfolio and Market review",
          "invalidation": ["Required observations are missing or stale"]
        }]
      }]
    }
  }
}
```

For replacement, use `operation: "replace"` and the currently active `plan_id`,
with complete new `plan` content. Closure supplies `operation: "close"`,
`portfolio_id`, `plan_id`, `source_ref`, and `reason`, with no `plan` field.
The canonical request schema is `protocol/v1/schemas/plan-update.schema.json`.

The preview includes the complete candidate, candidate digest, exact confirmation
phrase, and an explicit deadline (five minutes by default). Display its content
and effect to the user, resolve any intended account validation, and obtain explicit approval of that
candidate. Then call `apply_plan_update({preview_id, confirmation})` with the exact
returned phrase. Do not automatically echo the phrase as approval. If the
candidate must change, preview again and obtain a new approval.

The server-owned HTTP write config may opt into `plan_preview_ttl_seconds`
(60–900 seconds, default 300) to allow for model response time and independent
human login. This is never a tool argument. The canonical candidate digest binds
the original expiry, the browser link cannot outlive that candidate or its grant,
and neither NAS nor Gemini confirmation extends the deadline. Other write
families and the trusted receipt TTL retain their existing limits.

The server checks authenticated identity, permission, Portfolio scope, expiry,
confirmation, and an issued, unrevoked, payload-bound trusted approval receipt.
It rejects stale previews and publishes exactly the staged candidate. Apply is
single-use, including rejected confirmation attempts; process restart also
invalidates pending previews. A new preview requires new approval. Gemini additionally requires the independent browser review described below.

Success contains `read_back.matches=true` and the resulting active Plan IDs after
reading and validating the stored projection through a fresh store instance.
Clients should subsequently read `get_active_plans` / `get_plan_history`. If a
write or read-back reports an error, inspect current state before preparing
another change; persistence may have completed before a read-back error.

## Persistence and compatibility

The existing imported `plans.json` and `manifest.json` remain immutable. Reads
use them until the first approved native mutation. That mutation atomically
publishes `advisory-state/plans/native-state.json`, containing all Plan versions
and the full, untruncated, hash-linked change history. Subsequent reads use this
native state. An empty configured Plan store can be initialized by approved
creation without importing a legacy workspace.

An exclusive store lock and compare-and-swap against the complete preview base
prevent concurrent clients from overwriting each other. One atomic replacement
publishes the projection and history together, with file and directory fsync.
Read-only clients do not create Plan files or locks. Corrupt native state fails
closed rather than falling back to an older imported Plan.

Keep this state and approval evidence outside the product checkout and backups
outside both. Back up the complete authoritative Plan directory and approval
directory. Do not copy personal Plan content into product issues or PRs.

## GPT / stdio deployment

The ChatGPT/Codex stdio grant and container launchers accept
`INVESTKITCHEN_PLAN_WRITES=enabled`; its default is `disabled`. This adds only
`plan.update`, `operation.approve`, and the two Plan write tools to the generated
write grant. Existing principal verification and Portfolio scope still apply.
Custom launchers must supply those same server-owned permissions, allowed tool
names, advisory-state root, and trusted approval store.

Deploy a reviewed, identified product commit using the Synology release
preflight/cutover/rollback procedure. Enable the flag in private operator
configuration, restart the relevant MCP process, refresh client discovery, and
verify visibility and the full lifecycle with synthetic data first.

## Gemini / authenticated HTTP deployment

The default HTTP profile remains read-only. The existing confirmed-write profile
is extended with Plan updates; its independent NAS browser review remains
mandatory. Plan changes additionally require all of:

1. The OAuth read grant must include `plan.read` and explicit Portfolio scope.
   See `deploy/gemini/oauth-resource.plan.example.json`.
2. The separate, expiring `--write-config` grant must match that OAuth subject
   and client, identify the browser username, and include `plan.update` within
   the same Portfolio scope. See `deploy/gemini/plan-write-config.example.json`.
   This operator permission is separate from OAuth scopes and model input.
3. A signed, unexpired resource-bound bearer token must have
   `investkitchen.read` and the existing `investkitchen.write` scope. The optional
   write profile advertises the scope through both resource metadata and the
   initial OAuth challenge. A scope never creates a server permission.
4. `--advisory-plan-root` must point at the same authoritative Plan directory
   used by GPT. `--approval-store-root` must be a separate private directory for
   trusted receipts and browser evidence. Other authoritative stores retain
   their existing access boundaries.

Preview returns `approval_url` and
`approval_state=awaiting_authenticated_browser_approval`. The user opens that
URL, logs in independently, reviews the exact canonical candidate, and approves
or rejects it. The browser verifies the configured identity through the trusted
loopback Authelia CookieSession service, with same-origin and CSRF checks.
Model-supplied confirmation text alone cannot approve or persist the change.
Only after browser approval may the same OAuth identity apply that exact preview.
The service records browser evidence before accepting approval, then the native
Plan service issues and verifies its payload-bound trusted receipt at apply.

For Compose, `deploy/gemini/compose.plan.yml` is an opt-in overlay; the launcher
uses `INVESTKITCHEN_PLAN_WRITES=enabled` to load
`/run/operator/plan-write-config.json`. Mount that private configuration in the
existing operator directory. Set `GEMINI_PLAN_STORE_ROOT` to the authoritative
shared `advisory-state/plans` directory and `GEMINI_PLAN_APPROVAL_ROOT` to the
private approvals directory outside Git. Only the Plan directory and approval
evidence gain writable mounts; base Portfolio/Knowledge/native-journal mounts
remain read-only. Verify process ownership supports the shared lock and files.
A separate synthetic Plan directory is suitable for tests but is not the
production authority.

Use the existing confirmed-write ingress and exact `/approve/{ticket}` browser
authentication routes. Deploying only an MCP endpoint without those routes does
not enable usable approval. Follow `deploy/gemini/README.md` for authentication,
preflight, cutover and rollback; existing read tokens keep their permitted reads.
No additional Knowledge, Portfolio, account-sync, or execution permissions are
granted by the Plan-only example.

## Verification and rollback

For live client verification without changing real Plans, an operator may supply
`--plan-verification-root` and `--plan-verification-id verification:<name>`.
This namespace uses a separate synthetic store; both reads and writes route to
that store, and the authoritative Plan directory remains untouched. The root
must be separate from source, personal data, native journals, approvals and the
real Plan store. Explicit grants must include this namespace, and Gemini still
requires independent browser approval. Remove the temporary namespace and grants
after verification. A real Portfolio ID cannot be used as the test namespace.

An older stdio runtime with additional installed features may use
`protocol/v1/deployment/plan_stdio_extension.py` instead of replacing its full
runtime. Supply the identified `--base-runtime`, the exact
`--base-transport-digest`, and the older stdio arguments; explicitly add
`--enable-plan-writes` when enabling Plan authority. The extension uses this
release's native Plan service and preserves existing tools, handlers and
authorization hooks without editing the older runtime. It refuses a mismatched
transport digest or a Plan service shadowed by another source. Record both
source identities in the operator deployment manifest and retain the old
launcher for rollback.

Run `python -m pytest protocol/v1/tests -q` with the development dependencies.
Synthetic coverage includes both transports, lifecycle/history/read-back,
authorization/scope, tampering/revocation/expiry/replay, concurrent writes,
publication failures, corrupt state, and read-only compatibility.

Before deployment, preserve a consistent backup of Plan and approval state and
record the source commit/image. For a systemd deployment, inspect service
dependencies before stopping MCP. An ingress unit with `Requires=` on MCP stops
with it and is not automatically restarted by restarting MCP alone. Both
cutover and rollback must start MCP and then its ingress, verify the protected
public endpoint, and preserve the original rollback snapshot on retries. After
deployment, inspect tool discovery in each actual client, prove an out-of-scope request fails, and verify approved
synthetic create/replace/close and re-query. Record actual client verification
separately from local HTTP/stdio tests.

To disable writes, remove the GPT flag and HTTP Plan permission/scope or approval
store option, then restart the processes. Preserve `native-state.json` and keep a
reader version that understands native state. Rolling back to a reader that only
understands imported JSON would show an obsolete Plan; never do that after a
native write. If an old image must be restored, stop both Plan writers and
restore the matching complete pre-cutover Plan/approval backup together under
the documented operator rollback procedure.
