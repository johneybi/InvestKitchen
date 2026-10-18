# Confirmed updates from Gemini

[한국어](gemini-confirmed-writes.ko.md) · [Issue #5](https://github.com/johneybi/InvestKitchen/issues/5)

The optional HTTP write surface is implemented and tested with synthetic data.
It is not automatically enabled on an existing NAS read connection. Deployment,
OAuth reconnection, and actual Gemini write calls require separate verification.

It exposes two native update pairs: `preview_knowledge_update` /
`apply_knowledge_update`, and `preview_portfolio_update` /
`apply_portfolio_update`. Knowledge is instance-wide; portfolio observations are
restricted to explicitly granted IDs. Brokerage execution and other native write
families are outside this adapter's current scope.

1. Select the actual InvestKitchen connector. Check discovery and
   `get_capabilities.remote_write_tools` before offering an update.
2. Read current state and preview the exact update. This does not save anything.
3. Give the user `approval_url`. They must open it themselves, log into the NAS,
   review the complete canonical change, and approve or reject it. The preview
   expires within five minutes. Never operate that approval UI for the user.
4. After the user approves there, call the matching apply tool with the returned
   `preview_id` and `confirmation`. Complete any Gemini write confirmation too.
   Report success only for `status=applied` and `read_back.matches=true`.

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

Verify `investkitchen.write` in NAS consent, six read tools plus four preview/apply
tools in Gemini discovery, and a successful actual read. Consent and discovery do
not verify persistence: that requires the exact user-approved preview, manual NAS
approval, apply and read-back sequence above.

Operators must follow the [deployment runbook](../deploy/gemini/confirmed-writes.md).
The old read grant does not authorize enabling live writes.
