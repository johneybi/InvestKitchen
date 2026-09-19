#!/bin/sh
set -eu

ROOT=${TRADEMIND_SYNOLOGY_ROOT:-/volume1/docker/trademind}
REPO_ROOT=${TRADEMIND_RUNTIME_REPO_ROOT:-$ROOT/runtime-repo}
STATE_ROOT=${TRADEMIND_STATE_ROOT:-$ROOT/runtime-data/state}
PYTHON_BIN=${INVESTKITCHEN_PYTHON_BIN:-/var/packages/python310/target/bin/python3}

[ -x "$PYTHON_BIN" ] || {
  echo "InvestKitchen Python runtime not found: $PYTHON_BIN" >&2
  exit 2
}
[ -r "$REPO_ROOT/protocol/v1/deployment/advisory_write_bridge.py" ] || {
  echo "InvestKitchen advisory write bridge is not deployed." >&2
  exit 2
}

exec "$PYTHON_BIN" \
  "$REPO_ROOT/protocol/v1/deployment/advisory_write_bridge.py" \
  --state-root "$STATE_ROOT" \
  "$@"
