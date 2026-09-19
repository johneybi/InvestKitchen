# Personal advisory write path

This is the smallest personal-use write surface needed for day-to-day advisory
use. It is not a public write API and does not add live-order authority.

## Portfolio observation

Use `protocol/v1/deployment/portfolio_update_cli.py` to preview either a full
Portfolio observation or narrow quantity/cash changes against the latest
checkpoint projection. The preview contains a bounded review plus a private
reconciliation candidate. Acceptance requires an interactive exact digest-bound
phrase and the existing reconciliation verifier before a new checkpoint can be
persisted.

Narrow updates may only identify an existing Position by `(account_id, symbol)`
or an existing Cash row by `cash_id`. Unknown or ambiguous identities fail closed.
Unmentioned state is preserved as partial-observation scope; identical values are
treated as no-effect rather than generating a new checkpoint.

## Knowledge

Use `protocol/v1/deployment/native_knowledge_cli.py` for a structured
`knowledge.commit` request conforming to
`protocol/v1/schemas/knowledge-write-request.schema.json`. The request is previewed,
approved with the existing local trusted approval CLI, then appended to
`native_store_root/knowledge-journal.jsonl`.

The immutable personal-data bundle remains the baseline. `knowledge.current` and
`knowledge.search` merge the latest native current-state patch and canonical native
Claims over that baseline. Evidence/Claim provenance, truth status, applicability,
semantic fidelity, Evidence references, and temporal bounds are validated before
commit.

## Conversation contract

In a normal ChatGPT conversation the intended sequence is:

```text
user supplies facts/material
→ ChatGPT structures the proposed update
→ InvestKitchen produces a preview
→ ChatGPT shows the bounded differences/effect
→ user explicitly approves
→ local trusted approval/apply path persists
→ InvestKitchen read capability is called again
→ advice uses the read-back state, not the proposed input
```

The apply step is deliberately not model-callable through MCP. This preserves the
rule that ChatGPT may prepare and explain a mutation but cannot silently grant its
own approval.
