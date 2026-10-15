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

Operators must follow the [deployment runbook](../deploy/gemini/confirmed-writes.md).
The old read grant does not authorize enabling live writes.
