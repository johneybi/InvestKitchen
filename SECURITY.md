# Security and private-data boundary

InvestKitchen is designed around a strict separation between product code and user-owned runtime data.

## Never commit

Do not commit any of the following:

- brokerage account numbers or provider account identifiers
- API keys, OAuth credentials, client secrets, access/refresh tokens, tunnel credentials
- real holdings, quantities, cash balances, transaction exports, decision journals, reflection records
- personal Policy or Opinion Weighting configuration
- account-binding files that map internal portfolio/account IDs to real provider accounts
- generated personal-data bundles, runtime state, approval journals, backups, or production logs

The repository `.gitignore` blocks common runtime/state/secret paths, but ignore rules are not a substitute for review.

## Expected deployment model

Secrets and user data are mounted into the runtime from external storage. Example files under `deploy/synology/` contain schema-shaped placeholders only.

## Reporting

If you discover a security issue, avoid publishing credentials or private runtime data in a public issue. Contact the repository owner privately first.
