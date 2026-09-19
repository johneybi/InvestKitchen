#!/bin/sh
set -eu

# Private single-user Codex MCP launcher for the Synology-hosted InvestKitchen
# runtime. The SSH session is the outer authentication boundary; this launcher
# creates a short-lived server-owned Principal/Grant and never accepts identity,
# scope, or approval authority from MCP arguments.

ROOT=${TRADEMIND_SYNOLOGY_ROOT:-/volume1/docker/trademind}
REPO_ROOT=${TRADEMIND_RUNTIME_REPO_ROOT:-$ROOT/runtime-repo}
PERSONAL_DATA_ROOT=${TRADEMIND_PERSONAL_DATA_ROOT:-$ROOT/runtime-data/personal}
STATE_ROOT=${TRADEMIND_STATE_ROOT:-$ROOT/runtime-data/state}
PYTHON_BIN=${INVESTKITCHEN_PYTHON_BIN:-/var/packages/python310/target/bin/python3}
MANIFEST=${INVESTKITCHEN_INSTANCE_MANIFEST:-$REPO_ROOT/protocol/v1/fixtures/full-reference.instance.json}
ENV_FILE=${TRADEMIND_RUNTIME_ENV_FILE:-$ROOT/runtime-deploy.env}
MARKET_PROVIDER=${INVESTKITCHEN_MARKET_PROVIDER:-}
MARKET_SECRET_FILE=${INVESTKITCHEN_MARKET_SECRET_FILE:-}
CONNECTOR_WRITES=${INVESTKITCHEN_CONNECTOR_WRITES:-}
POLICY_WRITES=${INVESTKITCHEN_POLICY_WRITES:-}
OPINION_WRITES=${INVESTKITCHEN_OPINION_WRITES:-}
ACCOUNT_SYNC=${INVESTKITCHEN_ACCOUNT_SYNC:-}
ACCOUNT_BINDING_FILE=${INVESTKITCHEN_ACCOUNT_BINDING_HOST_FILE:-}
TOSS_ACCOUNT_SECRET_FILE=${INVESTKITCHEN_TOSS_ACCOUNT_SECRET_HOST_FILE:-}
NHPLUG_ACCOUNT_SECRET_FILE=${INVESTKITCHEN_NHPLUG_ACCOUNT_SECRET_HOST_FILE:-}
ACCOUNT_SYNC_MAX_AGE_SECONDS=${INVESTKITCHEN_ACCOUNT_SYNC_MAX_AGE_SECONDS:-300}

if [ -r "$ENV_FILE" ]; then
  if [ -z "$MARKET_PROVIDER" ]; then
    MARKET_PROVIDER=$(sed -n 's/^INVESTKITCHEN_MARKET_PROVIDER=//p' "$ENV_FILE" | tail -n 1)
  fi
  if [ -z "$MARKET_SECRET_FILE" ]; then
    MARKET_SECRET_FILE=$(sed -n 's/^INVESTKITCHEN_TOSS_MARKET_SECRET_HOST_FILE=//p' "$ENV_FILE" | tail -n 1)
  fi
  [ -n "$CONNECTOR_WRITES" ] || CONNECTOR_WRITES=$(sed -n 's/^INVESTKITCHEN_CONNECTOR_WRITES=//p' "$ENV_FILE" | tail -n 1)
  [ -n "$POLICY_WRITES" ] || POLICY_WRITES=$(sed -n 's/^INVESTKITCHEN_POLICY_WRITES=//p' "$ENV_FILE" | tail -n 1)
  [ -n "$OPINION_WRITES" ] || OPINION_WRITES=$(sed -n 's/^INVESTKITCHEN_OPINION_WRITES=//p' "$ENV_FILE" | tail -n 1)
  [ -n "$ACCOUNT_SYNC" ] || ACCOUNT_SYNC=$(sed -n 's/^INVESTKITCHEN_ACCOUNT_SYNC=//p' "$ENV_FILE" | tail -n 1)
  [ -n "$ACCOUNT_BINDING_FILE" ] || ACCOUNT_BINDING_FILE=$(sed -n 's/^INVESTKITCHEN_ACCOUNT_BINDING_HOST_FILE=//p' "$ENV_FILE" | tail -n 1)
  [ -n "$TOSS_ACCOUNT_SECRET_FILE" ] || TOSS_ACCOUNT_SECRET_FILE=$(sed -n 's/^INVESTKITCHEN_TOSS_ACCOUNT_SECRET_HOST_FILE=//p' "$ENV_FILE" | tail -n 1)
  [ -n "$NHPLUG_ACCOUNT_SECRET_FILE" ] || NHPLUG_ACCOUNT_SECRET_FILE=$(sed -n 's/^INVESTKITCHEN_NHPLUG_ACCOUNT_SECRET_HOST_FILE=//p' "$ENV_FILE" | tail -n 1)
fi
MARKET_PROVIDER=${MARKET_PROVIDER:-none}
MARKET_SECRET_FILE=${MARKET_SECRET_FILE:-$ROOT/secrets/toss-market-readonly.json}
CONNECTOR_WRITES=${CONNECTOR_WRITES:-disabled}
POLICY_WRITES=${POLICY_WRITES:-disabled}
OPINION_WRITES=${OPINION_WRITES:-disabled}
ACCOUNT_SYNC=${ACCOUNT_SYNC:-disabled}
ACCOUNT_BINDING_FILE=${ACCOUNT_BINDING_FILE:-$ROOT/secrets/account-bindings.json}
TOSS_ACCOUNT_SECRET_FILE=${TOSS_ACCOUNT_SECRET_FILE:-$ROOT/secrets/toss-account-readonly.json}
NHPLUG_ACCOUNT_SECRET_FILE=${NHPLUG_ACCOUNT_SECRET_FILE:-$ROOT/secrets/nhplug-account-readonly.json}
export INVESTKITCHEN_ACCOUNT_SYNC="$ACCOUNT_SYNC"
export INVESTKITCHEN_CONNECTOR_WRITES="$CONNECTOR_WRITES"
export INVESTKITCHEN_POLICY_WRITES="$POLICY_WRITES"
export INVESTKITCHEN_OPINION_WRITES="$OPINION_WRITES"

for path in \
  "$PERSONAL_DATA_ROOT/personal-data-manifest.json" \
  "$MANIFEST" \
  "$REPO_ROOT/protocol/v1/deployment/tunnel_stdio_launcher.py" \
  "$REPO_ROOT/protocol/v1/transport/mcp_stdio.py"
do
  [ -r "$path" ] || {
    echo "InvestKitchen Codex MCP configuration error: required file missing" >&2
    exit 2
  }
done

[ -x "$PYTHON_BIN" ] || {
  echo "InvestKitchen Codex MCP configuration error: Python runtime missing" >&2
  exit 2
}

mkdir -p "$STATE_ROOT/native-write" "$STATE_ROOT/approvals" "$STATE_ROOT/portfolio-checkpoints" "$STATE_ROOT/advisory-state" "$STATE_ROOT/historical-decisions" "$STATE_ROOT/historical-transactions"

TMP_ROOT=${TMPDIR:-/tmp}
AUTH_DIR=$(mktemp -d "$TMP_ROOT/investkitchen-codex-mcp.XXXXXX")
chmod 700 "$AUTH_DIR"
trap 'rm -rf "$AUTH_DIR"' EXIT HUP INT TERM

WRITE_PRINCIPAL="$AUTH_DIR/principal.json"
WRITE_GRANT="$AUTH_DIR/grant.json"

"$PYTHON_BIN" - \
  "$PERSONAL_DATA_ROOT/personal-data-manifest.json" \
  "$MANIFEST" \
  "$WRITE_PRINCIPAL" \
  "$WRITE_GRANT" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

personal_manifest_path, instance_manifest_path, principal_path, grant_path = map(Path, sys.argv[1:])
personal_manifest = json.loads(personal_manifest_path.read_text(encoding="utf-8"))
instance_manifest = json.loads(instance_manifest_path.read_text(encoding="utf-8"))
portfolio_ids = sorted(str(value) for value in personal_manifest.get("portfolio_ids") or [] if str(value))
if not portfolio_ids:
    raise SystemExit("InvestKitchen Codex MCP configuration error: no Portfolio scope")
instance_id = str(instance_manifest.get("instance_id") or "")
if not instance_id:
    raise SystemExit("InvestKitchen Codex MCP configuration error: instance id missing")

now = datetime.now(timezone.utc)
expires = now + timedelta(hours=12)

def tp(value: datetime) -> dict[str, str]:
    return {
        "value": value.isoformat().replace("+00:00", "Z"),
        "precision": "source_exact",
    }

principal = {
    "subject_user_id": "user:investkitchen-owner",
    "client_id": "client:codex-ssh",
    "credential_binding_id": "credential:investkitchen-codex-ssh",
    "authentication_event_id": "auth:investkitchen-codex-ssh-session",
    "authenticated_at": tp(now),
    "expires_at": tp(expires),
}
env = __import__("os").environ
account_sync = env.get("INVESTKITCHEN_ACCOUNT_SYNC", "disabled") == "enabled"
connector_writes = env.get("INVESTKITCHEN_CONNECTOR_WRITES", "disabled") == "enabled"
policy_writes = env.get("INVESTKITCHEN_POLICY_WRITES", "disabled") == "enabled"
opinion_writes = env.get("INVESTKITCHEN_OPINION_WRITES", "disabled") == "enabled"
permissions = []
allowlist = []
if connector_writes:
    permissions.extend(["knowledge.commit", "portfolio.update", "operation.approve"])
    allowlist.extend([
        "preview_knowledge_update",
        "apply_knowledge_update",
        "preview_portfolio_update",
        "apply_portfolio_update",
    ])
if account_sync:
    permissions.extend(["account.read", "portfolio.update", "operation.approve"])
    allowlist.extend(["preview_account_sync", "apply_account_sync"])
if policy_writes:
    permissions.extend(["policy.update", "operation.approve"])
    allowlist.extend(["preview_policy_update", "apply_policy_update"])
if opinion_writes:
    permissions.extend(["opinion.update", "operation.approve"])
    allowlist.extend(["preview_opinion_weighting_update", "apply_opinion_weighting_update"])
permissions = sorted(set(permissions))
allowlist = list(dict.fromkeys(allowlist))

grant = {
    "grant_id": "grant:investkitchen-codex-advisory-write-v1",
    "instance_id": instance_id,
    "subject_user_id": principal["subject_user_id"],
    "client_id": principal["client_id"],
    "credential_binding_id": principal["credential_binding_id"],
    "permissions": permissions,
    "portfolio_scope": portfolio_ids,
    "tool_allowlist": allowlist,
    "request_policy": {"max_ttl_seconds": 300, "max_future_skew_seconds": 30},
    "policy_version": "codex-advisory-write-v1",
    "issued_at": tp(now),
    "expires_at": tp(expires),
}

for path, value in ((principal_path, principal), (grant_path, grant)):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
PY

# The launcher scrubs the SSH session environment before exec'ing mcp_stdio.py.
# The active production Portfolio authority is checkpoint-based; fail closed if
# the checkpoint journal is absent rather than silently falling back.
[ -s "$STATE_ROOT/portfolio-checkpoints/checkpoints.jsonl" ] || {
  echo "InvestKitchen Codex MCP configuration error: Portfolio checkpoint journal missing" >&2
  exit 2
}

MARKET_ARGS="--market-provider none"
case "$MARKET_PROVIDER" in
  none)
    ;;
  toss-native-subprocess)
    [ -s "$MARKET_SECRET_FILE" ] || {
      echo "InvestKitchen Codex MCP configuration error: Toss market secret missing" >&2
      exit 2
    }
    MARKET_ARGS="--market-provider toss-native-subprocess --market-secret-file $MARKET_SECRET_FILE"
    ;;
  *)
    echo "InvestKitchen Codex MCP configuration error: unsupported market provider" >&2
    exit 2
    ;;
esac

ACCOUNT_ARGS="--account-sync-max-age-seconds $ACCOUNT_SYNC_MAX_AGE_SECONDS"
case "$ACCOUNT_SYNC" in
  disabled)
    ;;
  enabled)
    [ -s "$ACCOUNT_BINDING_FILE" ] || { echo "InvestKitchen Codex MCP configuration error: account binding file missing" >&2; exit 2; }
    ACCOUNT_ARGS="$ACCOUNT_ARGS --account-binding-file $ACCOUNT_BINDING_FILE"
    if [ -s "$TOSS_ACCOUNT_SECRET_FILE" ]; then
      ACCOUNT_ARGS="$ACCOUNT_ARGS --toss-account-secret-file $TOSS_ACCOUNT_SECRET_FILE"
    fi
    if [ -s "$NHPLUG_ACCOUNT_SECRET_FILE" ]; then
      ACCOUNT_ARGS="$ACCOUNT_ARGS --nhplug-account-secret-file $NHPLUG_ACCOUNT_SECRET_FILE"
    fi
    if [ ! -s "$TOSS_ACCOUNT_SECRET_FILE" ] && [ ! -s "$NHPLUG_ACCOUNT_SECRET_FILE" ]; then
      echo "InvestKitchen Codex MCP configuration error: no account provider secret configured" >&2
      exit 2
    fi
    ;;
  *)
    echo "InvestKitchen Codex MCP configuration error: unsupported account sync mode" >&2
    exit 2
    ;;
esac

set +e
"$PYTHON_BIN" "$REPO_ROOT/protocol/v1/deployment/tunnel_stdio_launcher.py" \
  --runtime-root "$REPO_ROOT" \
  --personal-data-root "$PERSONAL_DATA_ROOT" \
  --native-store-root "$STATE_ROOT/native-write" \
  --advisory-state-root "$STATE_ROOT/advisory-state" \
  --historical-decision-root "$STATE_ROOT/historical-decisions" \
  --historical-transaction-root "$STATE_ROOT/historical-transactions" \
  --portfolio-checkpoint-root "$STATE_ROOT/portfolio-checkpoints" \
  --approval-store-root "$STATE_ROOT/approvals" \
  --write-principal "$WRITE_PRINCIPAL" \
  --write-grant "$WRITE_GRANT" \
  --manifest "$MANIFEST" \
  $MARKET_ARGS \
  $ACCOUNT_ARGS
STATUS=$?
set -e
exit "$STATUS"
