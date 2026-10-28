# Manual account observations

Tracks [issue #28](https://github.com/johneybi/InvestKitchen/issues/28).

Use `preview_portfolio_update` for an account maintained from a broker screen,
statement or user observation, including IRP and pension accounts without a
broker API binding. `preview_account_sync` requires a registered provider binding
and is not a fallback for these accounts.

Read `get_portfolio_state` to identify the existing account, position and cash
rows. For a partial screen update, send only the fields actually observed through
`changes.position_observations` and `changes.cash_amounts`. Each position uses
`account_id` plus exactly one of `symbol` or `position_id`. Public IDs returned by
the read API work as selectors; the server resolves them within the current
Portfolio and rejects ambiguous IDs, cross-account positions and duplicate edits.
Existing symbol-less funds can use `position_id`. New holdings still require the
existing full `observed_portfolio` reconciliation workflow.

Synthetic request example:

```json
{
  "request_version": 1,
  "portfolio_id": "portfolio-example",
  "snapshot_effective_at": {"value": "2026-10-28", "precision": "date_only"},
  "source_ref": "user-observation:statement:example",
  "changes": {
    "position_observations": [{
      "account_id": "account-example",
      "symbol": "EXAMPLE",
      "cost_basis_values": [{"amount": 1003, "currency": "KRW", "value_basis": "observed"}],
      "avg_cost_values": [{"amount": 100.25, "currency": "KRW", "value_basis": "observed"}],
      "valuation": {
        "currency": "KRW", "value_basis": "observed",
        "market_value": 900, "profit_loss": -103, "profit_loss_rate": -0.1027
      }
    }],
    "cash_amounts": [{"cash_id": "cash-example", "amount": 820}]
  }
}
```

Average purchase price and principal are independent **observed** screen values.
Never replace either with a calculated value to force arithmetic agreement.
Amounts retain the asset currency; return rates are fractions (`0.1` = 10%).
Missing numbers stay omitted. Supplied money and valuation objects receive the
snapshot time and source evidence. A partial valuation replaces the previous
valuation object, preventing old prices from acquiring a new timestamp. With
unchanged quantity, omitted cost fields retain their original nested timestamps.
Changing quantity in `position_observations` clears old costs and valuation unless
replacement observations are supplied. Cash keeps its original currency, kind
and label; a cash-like valuation is not reclassified as available buying power.

Omit `created_at` to use one server timestamp throughout candidate construction
and current-state digest calculation. The observation time must reflect the
source's actual precision. The timestamp fix does not extend preview expiry:
apply still needs the exact candidate confirmation, server authorization and
trusted approval. A changed checkpoint or journal, tampered candidate, expired
preview or replay remains blocked. Manual account approval is separate from any
standing automatic broker observation consent. These writes record Portfolio
observations and do not create trades, orders or realized profit.

## Validation and rollout

Synthetic tests cover preview → confirmed apply → persisted read-back without a
broker binding or caller-supplied creation time, independently observed values,
public-ID round trips, partial updates, invalid observations and scope/collision
rejection. Existing approval, replay and genuine stale-state tests remain in
force. Implementation and test results do not establish production deployment;
deploy a reviewed product patch while retaining operational compatibility code,
separate personal data, signed standing policies and rollback backups.
