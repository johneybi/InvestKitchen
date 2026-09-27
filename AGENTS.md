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

## Repository and Synology roles

- Treat this Git repository and GitHub as the source of truth for deployable
  InvestKitchen product code, contracts, tests, documentation, and deployment
  scripts. A commit or merge is not a deployment.
- Treat a Synology personal development workspace as a separate, private workspace
  for experiments, operator notes, and personal inputs. Keep it outside the
  production `runtime-repo/`, `runtime-data/`, `secrets/`, and backup trees. If it
  has its own Git history, keep it private and do not push personal content to
  this product repository or copy its files into a product commit wholesale.
- Move reusable work from the personal workspace into this repository only after
  removing personal data and host-specific values, adding synthetic examples and
  relevant tests, and reviewing the exact staged diff. Product fixes made on the
  NAS must return here through a branch and review before the next release.
- Deploy an identified product commit or verified image to Synology using the
  documented preflight, cutover, and rollback steps. Never use the live runtime
  checkout or state directories as a scratch development tree.

## GitHub workflow

- Follow `CONTRIBUTING.md` for issue, Project, branch, commit, PR, and release
  conventions. For substantive work, link an issue; use a small branch from the
  current product base, focused commits, and a PR before merging to `main`.
- Keep issue, PR, commit, and Project text free of real account data, secrets,
  private paths, raw logs, and private screenshots. Use redacted or synthetic
  examples. Record verification honestly as local test, deployed, or operationally
  verified; do not imply one from another.
