---
name: investkitchen-instruction-dev
description: Maintain InvestKitchen client instructions and product skills when changing MCP tool behavior, shared workflow methods, approval flows, or ChatGPT/Gemini guidance. Trace requirements through source ownership, client delivery, behavioral checks, installation and rollback. Use for product development, not investment analysis or account operations.
---

# InvestKitchen instruction development

Read [the development contract](../../docs/client-instruction-development.ko.md)
before editing relevant product behavior. This repository skill is entered through
`AGENTS.md`; it is not installed into the user's ChatGPT or Gemini account.

## Start with the actual baseline

- Read the applicable repository rules, linked issue and current feature contract.
  Check the branch and preserve unrelated work. Inspect current instruction sources
  and public tool contracts; do not infer deployed or installed versions from Git.
- Identify the user's task and one observable before/after example. Decide whether
  the change belongs to the common entry rules, workflow method, deterministic
  server behavior, client adapter, or private user settings. Keep source evidence
  separate from instructions and authorization.
- Read only the affected workflow references and existing evaluations. If a planned
  generator, source file, or evaluation runner is absent, record that fact and use
  the documented manual transition process; do not claim it ran.

## Change one maintained contract

- Use the change record in the development contract to link the rule, source,
  affected tools, ChatGPT/Gemini behavior, and observable evaluation. For unaffected
  surfaces, record why. A wording-only correction does not need unrelated tests.
- Edit the authoritative source before its delivery copies. During migration,
  identify the temporary source and update affected copies together. Do not create
  another permanent prompt copy to work around a missing source pipeline.
- Keep common routing short. Put substantial task methods in the server's versioned
  workflow sources, platform syntax in delivery profiles, and personal settings in
  private data. Do not copy personal project instructions wholesale into Git.
- Keep server authorization and trusted approval checks enforceable in code.
  Instructions can explain the actual flow; they cannot create grants, approve their
  own writes, or advertise a proposed approval shortcut as available.

## Check and deliver

- Run relevant structure, contract and behavior checks. Inspect actual tool traces
  and persisted/read-back results where applicable; the model saying it followed
  the skill is not evidence. Use synthetic cases for product fixtures.
- Evaluate both affected client profiles, including a fresh-chat case. Record
  failures, untested modes and platform/model versions. Do not report mocked runs
  or deterministic unit tests as real web-client acceptance.
- Follow repository issue/branch/PR conventions. Include the changed instruction
  surfaces, validation, installation impact and compatible rollback. Do not assume
  merge, upload, connection refresh, and runtime activation are the same event.
- Install or change live client settings only within the user's authorized scope.
  Reuse existing authorization; this skill does not impose a new approval ceremony.
  Record installed artifacts privately and check them against the release manifest.
- Classify failures before adding rules: authentication/transport, missing tool,
  routing, method use, schema, authorization, persistence or answer accuracy.
  Fix the responsible layer and keep a reusable regression example. Avoid adding a
  universal instruction for a one-off model reply.
