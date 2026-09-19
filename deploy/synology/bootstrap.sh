#!/bin/sh
set -eu

ROOT=${TRADEMIND_SYNOLOGY_ROOT:-/volume1/docker/trademind}
RUNTIME_UID=${TRADEMIND_RUNTIME_UID:-$(id -u)}
RUNTIME_GID=${TRADEMIND_RUNTIME_GID:-$(id -g)}

mkdir -p \
  "$ROOT/runtime-data/personal" \
  "$ROOT/runtime-data/state/native-write" \
  "$ROOT/runtime-data/state/approvals" \
  "$ROOT/runtime-data/state/portfolio-checkpoints" \
  "$ROOT/runtime-data/backups" \
  "$ROOT/secrets"

for path in \
  "$ROOT/runtime-data" \
  "$ROOT/runtime-data/personal" \
  "$ROOT/runtime-data/state" \
  "$ROOT/runtime-data/state/native-write" \
  "$ROOT/runtime-data/state/approvals" \
  "$ROOT/runtime-data/state/portfolio-checkpoints" \
  "$ROOT/runtime-data/backups"; do
  chmod 0700 "$path"
  if [ "$(id -u)" -eq 0 ]; then
    chown "$RUNTIME_UID:$RUNTIME_GID" "$path"
  fi
done

echo "TradeMind Synology runtime directories prepared under $ROOT for uid:gid $RUNTIME_UID:$RUNTIME_GID"
