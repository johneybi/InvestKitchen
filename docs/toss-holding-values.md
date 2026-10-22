# Toss holding values

Tracks [issue #20](https://github.com/johneybi/InvestKitchen/issues/20).

The Toss read-only holdings adapter includes provider-reported cost and valuation
observations in the existing account synchronization flow:

`read → validate → reconciliation preview → explicit approval → checkpoint → read-back`

## Fields

Each holding keeps its trading currency. KRW and USD amounts are never combined or
converted by this adapter. Values come from
[`GET /api/v1/holdings`](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json),
using the documented `HoldingsItem`, `MarketValue` and `ProfitLoss` definitions.

| Toss field | AccountSnapshot / Portfolio position field | Meaning |
| --- | --- | --- |
| `averagePurchasePrice` | `avg_cost_values[0]` | Observed average purchase price |
| `marketValue.purchaseAmount` | `cost_basis_values[0]` | Observed purchase amount |
| `lastPrice` | `valuation.last_price` | Price reported with the holding snapshot |
| `marketValue.amount` | `valuation.market_value` | Market value before costs |
| `marketValue.amountAfterCost` | `valuation.market_value_after_cost` | Market value after tax/commission |
| `profitLoss.amount` | `valuation.profit_loss` | Holding profit/loss before costs |
| `profitLoss.amountAfterCost` | `valuation.profit_loss_after_cost` | Holding profit/loss after costs |
| `profitLoss.rate` | `valuation.profit_loss_rate` | Fractional return: `0.1` = 10% |
| `profitLoss.rateAfterCost` | `valuation.profit_loss_rate_after_cost` | Fractional return after costs |

Cost Money objects carry `currency`, `value_basis=observed`, `as_of`, and bounded
`source_evidence`. The `valuation` object carries its own `currency`,
`value_basis=observed`, `observed_at`, and `source_evidence`. Observation time is the
account adapter's snapshot time; it does not assert the last exchange trade time.

The valuation is a stored account observation. Retrieve `market.quote` separately
for a current quote. Holding profit/loss does not represent realized trading
profit and does not create a Transaction. Daily profit/loss and account-wide
totals are outside this change.

## Validation and refresh

- Unchanged quantities still produce a reviewable candidate when valuation or
  cost observations change. New observation times and provenance are retained.
- Valid zero values and signed losses/rates are retained. Non-finite values,
  booleans, malformed values, negative prices/principals, mismatched currencies,
  and mismatched observation times are rejected before a candidate can apply.
- Value fields are additive and optional for older quantity-only adapters. Missing
  values are never inferred from market quotes or transactions. With unchanged
  quantity, a prior value may remain with its original nested timestamp; its
  presence does not establish a fresh provider observation.
- When quantity changes without replacement values, prior costs and valuation
  are removed. Canonical trade replay invalidates prior cost and valuation;
  narrow manual quantity updates also invalidate valuation.
- Existing grants, portfolio/account bindings, trusted approval verification,
  candidate digests, expiry and replay checks remain in force. Preview does not
  write Portfolio state; apply uses the exact staged candidate.

## Release status and rollout

Implemented in the issue #20 branch. Local protocol validation: **411 passed,
8 optional private-legacy integration tests skipped**. Coverage includes synthetic
KRW/USD holdings, zero values, signed losses, valuation-only changes, invalid
observations, approval/tamper rejection, persisted read-back and trade replay.
This is not a deployment or operational verification. Do not copy an unrelated development checkout to
production: deploy a reviewed, identified product commit using the existing
preflight/cutover/rollback process and the existing state/secret separation.

After deployment, obtain a fresh account-sync preview, review quantities and value
fields, obtain explicit approval, then apply and verify authoritative
`portfolio.state` read-back. A code change alone does not refresh existing stored
holdings. Keep live account evidence outside Git and public issue/PR text.

Preserve a private pre-cutover state backup and verify rollback compatibility
before deployment. Older closed-schema readers may reject the added `valuation`
field; older replay code also does not invalidate it after trades. Rollback to a
previous runtime therefore requires the corresponding documented state-recovery
procedure if compatibility checks fail. Retain checkpoint/audit history; do not
delete or truncate journals in place. Stored valuation is not operationally
verified on an older runtime merely because its quantity reads still work.
