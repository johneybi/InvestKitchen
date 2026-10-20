# Confirmed updates from Gemini

[한국어](gemini-confirmed-writes.ko.md) · [Issue #5](https://github.com/johneybi/InvestKitchen/issues/5)

The optional HTTP write surface is implemented and tested with synthetic data.
It is not automatically enabled on an existing NAS read connection. Deployment,
OAuth reconnection, and actual Gemini write calls require separate verification.

It exposes `preview_knowledge_update` / `apply_knowledge_update` and
`preview_portfolio_update` / `apply_portfolio_update`. An explicitly enabled
[Plan profile](native-plan-lifecycle.md) also exposes `preview_plan_update` /
`apply_plan_update` for creation, replacement and closure. Knowledge is
instance-wide; Portfolio observations and Plans are restricted to granted IDs.
None of these tools authorizes brokerage execution.

HTTP declarations retain `const` and add explicit types and single-value enums
for fixed literals: Knowledge uses `protocol_version="1.0-draft"` and
`request_type="knowledge.commit"`; Portfolio observations use `request_version=1`.
This keeps these values visible to clients that discard `const` during function
declaration conversion. Canonical validation and independent approval are unchanged.
If an older conversation reports `Transport for env PROD not found in configuration`,
verify a read in a new chat with the actual connector selected. A server schema
fix does not repair Google's conversation bindings. Synthetic tests, deployment,
and actual Gemini preview/approval-link verification are separate results.

For clients that reshape nested function arguments, operators can explicitly
select `--write-input-format json-string`. The default `object` profile and stdio
contracts are unchanged. In this optional HTTP profile the authorized preview
tools advertise a flat `update_json` string. `get_write_contract(preview_tool=...)`
returns the original canonical input schema, only with the same unexpired write
grant and scope as the selected preview. Apply tools and their independent
approval requirements are unchanged. Cached object-format preview calls are
rejected in this opt-in profile with a tool-refresh instruction; clients must
rediscover its declarations. The default object profile remains compatible.

Read the contract, construct its `update` object, and serialize that object into
`update_json`. Preserve the entire source note, empty objects and omitted optional
fields. A client must not expand an empty `current_state` with schema defaults.
For a separately prepared update, also supply its independently computed
`expected_payload_digest`. The server rejects a mismatch before staging; a hash
invented by the model does not prove the original was preserved. A trusted caller
can prepare the envelope with Python's `json` and `hashlib` modules:

```python
canonical = json.dumps(update, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
arguments = {"update_json": canonical,
             "expected_payload_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
```

Omit the digest when it is unavailable; never invent one. JSON parsing rejects
duplicate keys, non-finite numbers, invalid envelopes and oversized input. HTTP
body limits, full canonical validation, portfolio scope, grant expiry and NAS
approval remain authoritative. Synthetic tests do not verify a deployed client's
handling of a particular note. Compare the actual returned digest and changed
fields before presenting its approval link.

HTTP is sessionless for legacy MCP clients as well. Reused or expired transport
IDs do not replace per-request OAuth identity, scope and grant checks. Pending
previews remain server-side and bound to the OAuth identity/client, with the
same independent browser approval and expiry checks. This HTTP profile does not
use server-initiated back-channel requests or stream resumption.

Operators may opt into `--transport-diagnostics-root`, a separate service-owned
0700 directory outside source, configuration, canonical stores and approval
stores. Its 0600 `transport-events.jsonl` is capped at 256 KiB and contains only
fixed tool names, outcome categories, HTTP status, protocol date, presence of a
session header or bearer token and timings. Private authorization categories
distinguish expired tokens, wrong audiences, invalid signatures and other
validation failures; they never include actual claims or appear in HTTP errors.
These categories do not establish why a client lost its refresh-token chain.
It never records arguments, credentials, identities,
approval URLs or raw audit records. Diagnostics default to off and logging
failure cannot authorize or block a write. Compare those events with actual
client attempts before attributing a connection failure to Google or the NAS.

Before the first preview, the operator must identify the separate NAS approval
login account and provide its login details through a private handoff outside
Git and chat. The account must match the server's `browser_username`; Google
OAuth client credentials are not browser login credentials. Do not assume the
user knows a service account or shares its password with a computer or NAS
administrator account. Have the user sign in independently before starting the
preview window. Never log in or approve on their behalf.

1. Select the actual InvestKitchen connector. Check discovery and
   `get_capabilities.remote_write_tools` before offering an update.
2. Read current state and preview the exact update. This does not save anything.
3. Give the user `approval_url`. They must open it themselves, log into the NAS,
   review the complete canonical change, and approve or reject it. The preview
   expires at its returned deadline (five minutes by default). Never operate that
   approval UI for the user.
4. After the user approves there, call the matching apply tool with the returned
   `preview_id` and `confirmation`. Complete any Gemini write confirmation too.
   Report success only for `status=applied` and `read_back.matches=true`.

Use only the newest approval link. A login redirect may return to the older
request embedded in its URL. If the approval page says the request cannot be
found or access is unavailable, verify login readiness, expiry and the matching
server approval record before claiming approval succeeded. Obtain a fresh
preview when required and send one clearly identified replacement link.
After NAS approval, complete Gemini's own write confirmation while the same
preview is still valid; neither confirmation extends its original deadline.

For slower Plan reviews, the operator may set `plan_preview_ttl_seconds` in the
server-owned write config to an integer from 60 through 900 seconds. The default
is 300. This setting applies only to Plan previews; clients cannot select it.
The browser deadline is capped by the canonical candidate and write grant, and
approval does not restart it. The Plan example opts into 900 seconds. Knowledge
and Portfolio previews retain their five-minute limit.

Browser approval alone does not save. Expiry, restart, stale state, rejection, or
a failed apply requires a fresh preview and approval. For an uncertain response,
read authoritative state before retrying. Approval is single-use and cannot be
transferred across identities, tools, or payloads. A model-generated confirmation
string is never human approval evidence.

For a manually created Gemini web skill, combine the read guide's scope,
provenance and freshness rules with the procedure above. No ZIP installation is
required. Replace the read-only skill instructions when adopting the write
profile; a skill itself cannot grant permissions or create approval evidence.
The [Korean guide](gemini-confirmed-writes.ko.md) includes copyable instructions.

If an existing app still requests only read scopes after the server advertises
write scope in its initial OAuth challenge and metadata, reauthorize it. If
unlinking and reconnecting through Google Account still preserves the old scope
request, obtain approval to remove the custom-app registration, then recreate it
with the same MCP URL and existing client credentials. Gemini may reject a
duplicate registration while the original remains. Keep credentials and callback
configuration private.

Verify `investkitchen.write` in NAS consent, the configured read and write tools
in Gemini discovery, and a successful actual read. The base profile has six read
and four preview/apply tools; enabling Plan reads and writes adds two of each.
Consent and discovery do not verify persistence: that requires the exact
user-approved preview, manual NAS approval, apply and read-back sequence above.

Operators must follow the [deployment runbook](../deploy/gemini/confirmed-writes.md).
The old read grant does not authorize enabling live writes.
