#!/bin/sh
set -eu

: "${TRADEMIND_TUNNEL_ID:?TRADEMIND_TUNNEL_ID is required}"

CONTROL_PLANE_API_KEY_FILE=${CONTROL_PLANE_API_KEY_FILE:-/run/secrets/control_plane_api_key}
PERSONAL_DATA_ROOT=${TRADEMIND_PERSONAL_DATA_ROOT:-/var/lib/trademind/personal}
STATE_ROOT=${TRADEMIND_STATE_ROOT:-/var/lib/trademind/state}
PORTFOLIO_AUTHORITY=${INVESTKITCHEN_PORTFOLIO_AUTHORITY:-personal-data}
CONNECTOR_WRITES=${INVESTKITCHEN_CONNECTOR_WRITES:-disabled}
POLICY_WRITES=${INVESTKITCHEN_POLICY_WRITES:-disabled}
OPINION_WRITES=${INVESTKITCHEN_OPINION_WRITES:-disabled}
MARKET_PROVIDER=${INVESTKITCHEN_MARKET_PROVIDER:-none}
MARKET_SECRET_FILE=${INVESTKITCHEN_MARKET_SECRET_FILE:-/run/secrets/toss_market_readonly}
ACCOUNT_SYNC=${INVESTKITCHEN_ACCOUNT_SYNC:-disabled}
ACCOUNT_BINDING_FILE=${INVESTKITCHEN_ACCOUNT_BINDING_FILE:-/run/secrets/account_bindings}
TOSS_ACCOUNT_SECRET_FILE=${INVESTKITCHEN_TOSS_ACCOUNT_SECRET_FILE:-/run/secrets/toss_account_readonly}
NHPLUG_ACCOUNT_SECRET_FILE=${INVESTKITCHEN_NHPLUG_ACCOUNT_SECRET_FILE:-/run/secrets/nhplug_account_readonly}
ACCOUNT_SYNC_MAX_AGE_SECONDS=${INVESTKITCHEN_ACCOUNT_SYNC_MAX_AGE_SECONDS:-300}
HEALTH_LISTEN_ADDR=${HEALTH_LISTEN_ADDR:-0.0.0.0:8080}

if [ ! -s "$CONTROL_PLANE_API_KEY_FILE" ]; then
  echo "TradeMind runtime configuration error: control-plane key missing" >&2
  exit 2
fi

if [ ! -s "$PERSONAL_DATA_ROOT/personal-data-manifest.json" ]; then
  echo "TradeMind runtime configuration error: personal data manifest missing" >&2
  exit 2
fi

mkdir -p "$STATE_ROOT/native-write" "$STATE_ROOT/approvals" "$STATE_ROOT/portfolio-checkpoints" "$STATE_ROOT/advisory-state" "$STATE_ROOT/historical-decisions" "$STATE_ROOT/historical-transactions"

CHECKPOINT_ARG=""
case "$PORTFOLIO_AUTHORITY" in
  personal-data)
    ;;
  checkpoint)
    if [ ! -s "$STATE_ROOT/portfolio-checkpoints/checkpoints.jsonl" ]; then
      echo "InvestKitchen runtime configuration error: Portfolio checkpoint journal missing" >&2
      exit 2
    fi
    CHECKPOINT_ARG="--portfolio-checkpoint-root $STATE_ROOT/portfolio-checkpoints"
    ;;
  *)
    echo "InvestKitchen runtime configuration error: unsupported Portfolio authority" >&2
    exit 2
    ;;
esac

WRITE_ARGS=""
case "$CONNECTOR_WRITES" in
  disabled|enabled) ;;
  *)
    echo "InvestKitchen runtime configuration error: unsupported connector write mode" >&2
    exit 2
    ;;
esac
case "$ACCOUNT_SYNC" in
  disabled|enabled) ;;
  *)
    echo "InvestKitchen runtime configuration error: unsupported account sync mode" >&2
    exit 2
    ;;
esac
case "$POLICY_WRITES" in
  disabled|enabled) ;;
  *)
    echo "InvestKitchen runtime configuration error: unsupported policy write mode" >&2
    exit 2
    ;;
esac
case "$OPINION_WRITES" in
  disabled|enabled) ;;
  *)
    echo "InvestKitchen runtime configuration error: unsupported opinion write mode" >&2
    exit 2
    ;;
esac

if [ "$CONNECTOR_WRITES" = "enabled" ] || [ "$ACCOUNT_SYNC" = "enabled" ] || [ "$POLICY_WRITES" = "enabled" ] || [ "$OPINION_WRITES" = "enabled" ]; then
    WRITE_PRINCIPAL=/tmp/investkitchen-write-principal.json
    WRITE_GRANT=/tmp/investkitchen-write-grant.json
    python3 - "$PERSONAL_DATA_ROOT/personal-data-manifest.json" "$WRITE_PRINCIPAL" "$WRITE_GRANT" <<'PY'
import json
import os
import sys
from pathlib import Path

manifest_path, principal_path, grant_path = map(Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
portfolio_ids = sorted(str(value) for value in manifest.get("portfolio_ids") or [] if str(value))
if not portfolio_ids:
    raise SystemExit("InvestKitchen connector write configuration error: no Portfolio scope")

principal = {
    "subject_user_id": "user:investkitchen-owner",
    "client_id": "client:chatgpt-secure-tunnel",
    "credential_binding_id": "credential:investkitchen-private-tunnel",
    "authentication_event_id": "auth:investkitchen-private-runtime",
    "authenticated_at": {"value": "2026-01-01T00:00:00Z", "precision": "source_exact"},
    "expires_at": {"value": "2099-01-01T00:00:00Z", "precision": "source_exact"},
}
permissions = []
allowlist = []
if os.environ.get("INVESTKITCHEN_CONNECTOR_WRITES", "disabled") == "enabled":
    permissions.extend(["knowledge.commit", "portfolio.update", "operation.approve"])
    allowlist.extend([
        "preview_knowledge_update",
        "apply_knowledge_update",
        "preview_portfolio_update",
        "apply_portfolio_update",
    ])
if os.environ.get("INVESTKITCHEN_ACCOUNT_SYNC", "disabled") == "enabled":
    permissions.extend(["account.read", "portfolio.update", "operation.approve"])
    allowlist.extend(["preview_account_sync", "apply_account_sync"])
if os.environ.get("INVESTKITCHEN_POLICY_WRITES", "disabled") == "enabled":
    permissions.extend(["policy.update", "operation.approve"])
    allowlist.extend(["preview_policy_update", "apply_policy_update"])
if os.environ.get("INVESTKITCHEN_OPINION_WRITES", "disabled") == "enabled":
    permissions.extend(["opinion.update", "operation.approve"])
    allowlist.extend(["preview_opinion_weighting_update", "apply_opinion_weighting_update"])
permissions = sorted(set(permissions))
allowlist = list(dict.fromkeys(allowlist))

grant = {
    "grant_id": "grant:investkitchen-personal-write-v1",
    "instance_id": "fixture-full-reference",
    "subject_user_id": principal["subject_user_id"],
    "client_id": principal["client_id"],
    "credential_binding_id": principal["credential_binding_id"],
    "permissions": permissions,
    "portfolio_scope": portfolio_ids,
    "tool_allowlist": allowlist,
    "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
    "policy_version": "personal-write-v1",
    "issued_at": {"value": "2026-01-01T00:00:00Z", "precision": "source_exact"},
    "expires_at": {"value": "2099-01-01T00:00:00Z", "precision": "source_exact"},
}
for path, value in ((principal_path, principal), (grant_path, grant)):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    path.chmod(0o600)
PY
    WRITE_ARGS="--approval-store-root $STATE_ROOT/approvals --write-principal $WRITE_PRINCIPAL --write-grant $WRITE_GRANT"
fi

ACCOUNT_ARGS="--account-sync-max-age-seconds $ACCOUNT_SYNC_MAX_AGE_SECONDS"
case "$ACCOUNT_SYNC" in
  disabled)
    ;;
  enabled)
    [ -s "$ACCOUNT_BINDING_FILE" ] || {
      echo "InvestKitchen runtime configuration error: account binding file missing" >&2
      exit 2
    }
    ACCOUNT_ARGS="$ACCOUNT_ARGS --account-binding-file $ACCOUNT_BINDING_FILE"
    if [ -s "$TOSS_ACCOUNT_SECRET_FILE" ]; then
      ACCOUNT_ARGS="$ACCOUNT_ARGS --toss-account-secret-file $TOSS_ACCOUNT_SECRET_FILE"
    fi
    if [ -s "$NHPLUG_ACCOUNT_SECRET_FILE" ]; then
      ACCOUNT_ARGS="$ACCOUNT_ARGS --nhplug-account-secret-file $NHPLUG_ACCOUNT_SECRET_FILE"
    fi
    if [ ! -s "$TOSS_ACCOUNT_SECRET_FILE" ] && [ ! -s "$NHPLUG_ACCOUNT_SECRET_FILE" ]; then
      echo "InvestKitchen runtime configuration error: no account provider secret configured" >&2
      exit 2
    fi
    ;;
esac

MARKET_ARGS="--market-provider none"
case "$MARKET_PROVIDER" in
  none)
    ;;
  toss-native-subprocess)
    if [ ! -s "$MARKET_SECRET_FILE" ]; then
      echo "InvestKitchen runtime configuration error: Toss market secret missing" >&2
      exit 2
    fi
    MARKET_ARGS="--market-provider toss-native-subprocess --market-secret-file $MARKET_SECRET_FILE"
    ;;
  *)
    echo "InvestKitchen runtime configuration error: unsupported market provider" >&2
    exit 2
    ;;
esac

MCP_COMMAND="python3 /app/protocol/v1/transport/mcp_stdio.py --runtime-root /app --personal-data-root $PERSONAL_DATA_ROOT --native-store-root $STATE_ROOT/native-write --advisory-state-root $STATE_ROOT/advisory-state --historical-decision-root $STATE_ROOT/historical-decisions --historical-transaction-root $STATE_ROOT/historical-transactions $CHECKPOINT_ARG $WRITE_ARGS $MARKET_ARGS $ACCOUNT_ARGS --manifest /app/protocol/v1/fixtures/full-reference.instance.json"

exec /usr/bin/tunnel-client-runtime run \
  --control-plane.tunnel-id "$TRADEMIND_TUNNEL_ID" \
  --control-plane.api-key "file:$CONTROL_PLANE_API_KEY_FILE" \
  --mcp.command "command=$MCP_COMMAND,channel=main" \
  --health.listen-addr "$HEALTH_LISTEN_ADDR" \
  --log.format json \
  --log.level info
