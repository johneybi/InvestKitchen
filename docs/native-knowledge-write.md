# Native Knowledge write — personal advisory path

This is a deliberately small personal-use path for updating InvestKitchen Knowledge without the legacy TradeMind repository.

The immutable `personal_data_root` remains a baseline. New structured Knowledge is appended to `native_store_root/knowledge-journal.jsonl` and `knowledge.current` / `knowledge.search` read a native-over-baseline view. A native claim with the same `claim_id` replaces that baseline claim in the read projection; the journal itself is append-only.

Input is one `knowledge.commit` object conforming to `protocol/v1/schemas/knowledge-write-request.schema.json`. It contains a bounded current-state patch plus Evidence and Claims. Evidence provenance fields, claim provenance/semantic/truth/applicability states, cross-referenced Evidence IDs, and time points are validated before a preview can be created. Native search exposes only Claims whose `registration_state` is `canonical`.

Knowledge writes are not exposed as an MCP tool. The existing read-only MCP surface stays read-only. A commit uses the existing trusted local approval boundary:

```text
structured JSON from ChatGPT
  -> native_knowledge_cli.py preview
  -> local_approval_cli.py (interactive trusted approval)
  -> native_knowledge_cli.py apply
  -> append-only native Knowledge journal
  -> knowledge.current / knowledge.search
```

The required grant permissions are `knowledge.commit` and `operation.approve`. The apply path verifies the ApprovalReceipt against the server-owned `TrustedApprovalStore`, binds it to the exact payload digest and generation target, checks expiry/identity/grant state, and rejects a stale base generation.

This path does not fetch market data, does not run a model, does not change Portfolio state, and does not rebuild the old public registration/generation pipeline. Deployment/cutover is a separate step.
