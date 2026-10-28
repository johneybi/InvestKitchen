# Current brokerage observations and automatic refresh

Tracks [issue #25](https://github.com/johneybi/InvestKitchen/issues/25).
This feature combines current reads and optional automatic reflection. They can
be enabled together; users do not have to choose between them.

## User flow

- For a current linked account, use `get_account_state(portfolio_id, account_id)`.
  It reads the server-bound broker account without a sync preview, write grant,
  confirmation token or approval deadline. Quantity, average purchase price,
  purchase amount, holding price, market value, profit/loss and available cash
  include their source time, currency and completeness. Missing values stay absent.
- Ordinary reads use a short cache and automatically fetch when it expires.
  `refresh: true` requests an immediate broker read. A holding's price remains a
  broker snapshot observation; use `market.quote` for a separate current quote.
- After one explicit operator consent for a particular portfolio/account binding,
  a background worker saves the latest observation and `get_portfolio_state`
  incorporates it automatically. New and removed holdings are reflected only
  when the broker reports complete holdings. Other accounts remain independent.
- Disabling that consent stops background reflection. Ordinary authorized broker
  reads still work. No per-refresh chat approval is needed while consent is active.

Automatic reflection updates a durable **latest observation and current Portfolio
view**. It does not rewrite canonical checkpoints, invent trades, or change
Transaction, Decision, Knowledge, advisory Policy, approval or execution history.
Explicit checkpoint reconciliation continues to use the existing approved
preview/apply flow. Responses identify automatic overlays and include
`canonical_checkpoint_changed: false` so consumers can distinguish these sources.

## Server load and failure behavior

The operator examples use a 60-second shared cache and a five-minute background
interval. A standalone live reader uses a 30-second memory cache by default.
The background worker skips a scope recently refreshed by a user. Independent
processes share a private per-account file lock, so concurrent ordinary requests
do not each call the provider. Explicit forced refresh bypasses cache reuse.

Each scope keeps one signed latest JSON snapshot and one lock file, with no
per-refresh journal growth. Background intervals can range from five minutes to
one day; shared cache TTLs range from 30 seconds to five minutes. Existing broker
timeouts still apply. The timer runs once every five minutes and the policy can
skip ticks for accounts configured with longer intervals.

When a refresh fails, the prior observation retains its original timestamps and
is explicitly marked stale for at most 15 minutes. After that, current data is
unavailable and the Portfolio view retains saved checkpoint facts with a warning.
A failed refresh with a stored observation shares a 30-second retry backoff;
manual forced refresh can retry sooner. Invalid broker identity, currency or
payload is rejected rather than presented as a successful refresh. Incomplete
holdings never clear missing positions, and partial cash does not remove another
cash basis or currency. Failed provider details and private account selectors
are not included in MCP results.

## Configuration and one-time consent

Keep product sources, canonical state, latest observations, consent configuration,
signing key, provider credentials and backups separate. All example paths below
are operator-supplied placeholders; real values must stay outside Git.

The stdio and HTTP launchers accept `--account-binding-file` and the existing
read-only provider secret options. Without a configured reader, the new tool is
hidden. With a reader, `portfolio.read` and the existing portfolio scope authorize
broker reads; they do not authorize canonical writes or enabling automatic mode.

Prepare the following private paths, then run from an authenticated operator TTY:

```sh
python protocol/v1/deployment/account_observation_cli.py authorize \
  --runtime-root PRODUCT_ROOT --account-binding-file PRIVATE_BINDINGS \
  --observation-root PRIVATE_OBSERVATIONS --policy-file PRIVATE_CONSENT \
  --key-file PRIVATE_SIGNING_KEY --portfolio-id PORTFOLIO_ID --account-id ACCOUNT_ID \
  --interval-seconds 300 --cache-ttl-seconds 60
```

The command prints the exact scope and asks for `ENABLE ACCOUNT OBSERVATIONS`.
It requires a real interactive TTY; there is no MCP auto-approve flag or
caller-supplied approval attestation. Consent binds to the full server-owned
provider/account/credential selection, and is checked on every cache read/write
and overlay. A binding change requires a new consent. The signed policy can
approve multiple accounts independently. Signatures protect accidental or
unauthorized file edits; they do not protect against compromise of the local
operator account that owns the signing key.

Add these three options to each configured gateway that should share current
observations:

```text
--account-observation-root PRIVATE_OBSERVATIONS
--account-observation-policy-file PRIVATE_CONSENT
--account-observation-key-file PRIVATE_SIGNING_KEY
```

Install the [service example](../deploy/synology/account-observations.service.example)
and [timer example](../deploy/synology/account-observations.timer.example) only
after replacing placeholders, reviewing credential access and validating the
identified product release. The worker receives read-only broker credentials and
observation storage access, with no canonical checkpoint or approval writer.
Do not loosen an HTTP service's secret isolation just to copy these examples.

The CLI actions `status`, `tick` and `disable` use the same private path arguments.
`tick` also needs the appropriate provider secret option. `disable` revokes all
scopes in that policy immediately without deleting canonical records. Add
`--portfolio-id PORTFOLIO_ID --account-id ACCOUNT_ID` to `disable` to revoke only
that account. No remote model tool can edit or re-enable this policy.

## Verification and rollout

Implementation and synthetic local validation do not establish deployment.
Deploy the reviewed commit with the documented Synology preflight, backup,
cutover and rollback process. Verify existing tool/grant compatibility, a fresh
broker read, shared-cache reuse, timer cadence, Portfolio read-back and revocation
without calling real account-sync apply. Record live evidence privately and only
sanitized outcomes in the public issue or PR.

Rollback stops/disables the observation timer and restores the previous gateway
launch configuration and product source. Canonical checkpoints need no migration
or restoration for this feature because observation refresh never changes them.
Retain private latest snapshots and consent files for inspection; do not copy
them into the product repository.
