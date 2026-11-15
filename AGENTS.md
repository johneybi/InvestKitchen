# InvestKitchen repository rules

- InvestKitchen is the product name. `TradeMind` / `trademind-*` names that remain
  in protocol IDs, environment variables, image names, or Synology paths are
  compatibility identifiers and must not be presented as the current product name.
- Product code and user data are separate boundaries. Never commit real account,
  portfolio, transaction, reflection, decision, credential, secret, or backup data.
- `protocol/v1/fixtures/generated/` is always local-only because migration
  projections may contain private data.
- Treat `--legacy-workspace` as a read-only migration/compatibility input. Do not
  mutate it from runtime adapters or tests, and do not make production Portfolio
  or Knowledge depend on it again.
- Keep Advice, Decision, Transaction, ApprovalReceipt, and execution distinct.
- Write operations must fail closed without server-side authorization and trusted
  approval verification.
- Do not expose local paths, provider secrets, or raw audit journals through MCP.
- Keep planned, implemented, tested, deployed, and operationally verified states
  distinct in code reviews and documentation.
- Deployment changes must preserve repository / personal-data / state / secret /
  backup separation.
- Do not rename operational `trademind-*` compatibility identifiers as part of an
  unrelated feature. Namespace migration requires its own rollback-safe change.

## Client instructions and skill maintenance

- Before changing client-facing tool behavior, workflow methods, approval flows,
  project instructions, or product skills, read and apply
  `skills/investkitchen-instruction-dev/SKILL.md` and its linked development guide.
  This is developer guidance, not a skill to upload to an investment chat.
- Treat server behavior, shared instructions, both client delivery profiles, and
  observable evaluation cases as one change contract. Record unaffected surfaces
  with a reason instead of editing them mechanically.
- Maintain canonical sources and rule ownership. Do not fix only a live prompt or
  generated package; return an authorized live correction to the product source.
- Record source version, installed version, validation, and operational results
  separately. A skill file or passing parser check does not prove client behavior.
