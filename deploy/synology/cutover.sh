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
DOCKER_TIMEOUT=${INVESTKITCHEN_DOCKER_TIMEOUT_SECONDS:-120}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this cutover with sudo/root so it can access the Docker daemon." >&2
  exit 2
fi
if [ ! -s "$ROOT/secrets/trademind-runtime-api-key" ]; then
  echo "TradeMind runtime API key is missing." >&2
  exit 2
fi
if [ ! -r "$ENV_FILE" ]; then
  echo "TradeMind runtime deploy env is missing." >&2
  exit 2
fi
IMAGE_REF=$(sed -n 's/^TRADEMIND_RUNTIME_IMAGE_REF=//p' "$ENV_FILE" | tail -n 1)
MARKET_PROVIDER=$(sed -n 's/^INVESTKITCHEN_MARKET_PROVIDER=//p' "$ENV_FILE" | tail -n 1)
MARKET_SECRET_HOST_FILE=$(sed -n 's/^INVESTKITCHEN_TOSS_MARKET_SECRET_HOST_FILE=//p' "$ENV_FILE" | tail -n 1)
MARKET_PROVIDER=${MARKET_PROVIDER:-none}
MARKET_SECRET_HOST_FILE=${MARKET_SECRET_HOST_FILE:-$ROOT/secrets/toss-market-readonly.json}
if [ "$MARKET_PROVIDER" = "toss-native-subprocess" ] && [ ! -s "$MARKET_SECRET_HOST_FILE" ]; then
  echo "Toss market provider is enabled but its secret file is missing." >&2
  exit 2
fi
if ! timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" image inspect "$IMAGE_REF" >/dev/null 2>&1; then
  echo "Expected preflighted image is not installed: $IMAGE_REF" >&2
  exit 2
fi

cd "$REPO_ROOT"
timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml up -d --no-build runtime

container_id=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml ps -q runtime)
if [ -z "$container_id" ]; then
  echo "TradeMind runtime container was not created." >&2
  exit 2
fi

for _ in $(seq 1 60); do
  status=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")
  if [ "$status" = "healthy" ]; then
    echo "TradeMind Synology runtime: healthy"
    timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml ps runtime
    exit 0
  fi
  if [ "$status" = "unhealthy" ] || [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
    "$DOCKER_BIN" logs --tail 80 "$container_id" >&2 || true
    echo "TradeMind Synology runtime failed: $status" >&2
    exit 2
  fi
  sleep 2
done

"$DOCKER_BIN" logs --tail 80 "$container_id" >&2 || true
echo "TradeMind Synology runtime did not become healthy in time." >&2
exit 2
