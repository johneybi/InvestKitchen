#!/bin/sh
set -eu

resolve_docker_bin() {
  if [ -n "${INVESTKITCHEN_DOCKER_BIN:-}" ]; then
    [ -x "$INVESTKITCHEN_DOCKER_BIN" ] || { echo "Configured Docker binary is not executable: $INVESTKITCHEN_DOCKER_BIN" >&2; exit 2; }
    printf '%s\n' "$INVESTKITCHEN_DOCKER_BIN"
    return
  fi
  for candidate in /usr/local/bin/docker /var/packages/ContainerManager/target/usr/bin/docker /usr/bin/docker; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  echo "Synology Docker CLI was not found in known locations." >&2
  exit 2
}

DOCKER_BIN=$(resolve_docker_bin)

ROOT=${TRADEMIND_SYNOLOGY_ROOT:-/volume1/docker/trademind}
ENV_FILE=${TRADEMIND_RUNTIME_ENV_FILE:-$ROOT/runtime-deploy.env}
REPO_ROOT=${TRADEMIND_RUNTIME_REPO_ROOT:-$ROOT/runtime-repo}

if [ ! -r "$ENV_FILE" ]; then
  echo "TradeMind runtime deploy env is missing: $ENV_FILE" >&2
  exit 2
fi

cd "$REPO_ROOT"
exec "$DOCKER_BIN" compose \
  --env-file "$ENV_FILE" \
  -f deploy/synology/compose.yml \
  "$@"
