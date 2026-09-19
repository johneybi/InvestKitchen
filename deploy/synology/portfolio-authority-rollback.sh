#!/bin/sh
set -eu

ROOT=${TRADEMIND_SYNOLOGY_ROOT:-/volume1/docker/trademind}
ENV_FILE=${TRADEMIND_RUNTIME_ENV_FILE:-$ROOT/runtime-deploy.env}
REPO_ROOT=${TRADEMIND_RUNTIME_REPO_ROOT:-$ROOT/runtime-repo}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this rollback with sudo/root so it can use the Docker daemon." >&2
  exit 2
fi

owner=$(stat -c '%u:%g' "$ENV_FILE")
mode=$(stat -c '%a' "$ENV_FILE")
tmp=$(mktemp "$ENV_FILE.tmp.XXXXXX")
awk '
  BEGIN { found=0 }
  /^INVESTKITCHEN_PORTFOLIO_AUTHORITY=/ { print "INVESTKITCHEN_PORTFOLIO_AUTHORITY=personal-data"; found=1; next }
  { print }
  END { if (!found) print "INVESTKITCHEN_PORTFOLIO_AUTHORITY=personal-data" }
' "$ENV_FILE" > "$tmp"
chown "$owner" "$tmp"
chmod "$mode" "$tmp"
mv "$tmp" "$ENV_FILE"

cd "$REPO_ROOT"
exec ./deploy/synology/cutover.sh
