# Public repository data boundary

This repository is the **product source tree**, not a backup of any user's InvestKitchen instance.

## Public/product material

The public repository may contain:

- protocol schemas and capability contracts
- runtime/store implementations
- provider-neutral account/market adapter code
- synthetic fixtures and tests
- example deployment configuration
- product/architecture documentation

## Private/runtime material

A deployed instance keeps these outside Git:

```text
personal-data/
state/
backups/
secrets/
provider account bindings
user policy configuration
opinion-weighting configuration
```

Private migration reports and one-off production diagnostics are also intentionally excluded from the public tree.

## Synthetic tests

Tests must use invented portfolio IDs, account IDs, quantities, policy values, and source identities. A fixture should demonstrate a contract, not reproduce a real user's state.

## Compatibility identifiers

Some `TradeMind` / `trademind-*` names remain in protocol IDs, environment variables, image names, and deployment paths for compatibility. They are implementation identifiers, not user data and not the current product name.
