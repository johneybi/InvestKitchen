#!/bin/sh
set -eu
set -- python -m protocol.v1.transport.mcp_http \
  --oauth-config /run/operator/oauth-resource.json \
  --public-jwks /run/operator/public-jwks.json \
  --manifest /app/protocol/v1/fixtures/full-reference.instance.json \
  --personal-data-root /var/lib/investkitchen/personal \
  --native-store-root /var/lib/investkitchen/state/native-write
case "${INVESTKITCHEN_PLAN_WRITES:-disabled}" in
  disabled) ;;
  enabled)
    set -- "$@" --advisory-plan-root /var/lib/investkitchen/plans \
      --write-config /run/operator/plan-write-config.json \
      --approval-store-root /var/lib/investkitchen/plan-approvals
    ;;
  *) echo "InvestKitchen HTTP MCP Plan configuration error" >&2; exit 2 ;;
esac
case "${INVESTKITCHEN_PORTFOLIO_AUTHORITY:-checkpoint}" in
  checkpoint)
    set -- "$@" --portfolio-checkpoint-root /var/lib/investkitchen/state/portfolio-checkpoints
    ;;
  personal-data) ;;
  *) echo "InvestKitchen HTTP MCP authority configuration error" >&2; exit 2 ;;
esac
case "${INVESTKITCHEN_MARKET_PROVIDER:-none}" in
  none) ;;
  toss-native-subprocess)
    set -- "$@" --market-provider toss-native-subprocess --market-secret-file /run/market/credential.json
    ;;
  *) echo "InvestKitchen HTTP MCP market configuration error" >&2; exit 2 ;;
esac
if [ "${INVESTKITCHEN_PREFLIGHT_ONLY:-0}" = 1 ]; then
  set -- "$@" --check-config
fi
exec "$@"
