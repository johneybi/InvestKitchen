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
DOCKER_TIMEOUT=${INVESTKITCHEN_DOCKER_TIMEOUT_SECONDS:-120}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this seed with sudo/root so it can use the Docker daemon." >&2
  exit 2
fi
if [ ! -r "$ENV_FILE" ]; then
  echo "InvestKitchen runtime deploy env is missing: $ENV_FILE" >&2
  exit 2
fi

IMAGE_REF=$(sed -n 's/^TRADEMIND_RUNTIME_IMAGE_REF=//p' "$ENV_FILE" | tail -n 1)
RUNTIME_UID=$(sed -n 's/^TRADEMIND_RUNTIME_UID=//p' "$ENV_FILE" | tail -n 1)
RUNTIME_GID=$(sed -n 's/^TRADEMIND_RUNTIME_GID=//p' "$ENV_FILE" | tail -n 1)
RUNTIME_UID=${RUNTIME_UID:-1000}
RUNTIME_GID=${RUNTIME_GID:-1000}
[ -n "$IMAGE_REF" ] || { echo "TRADEMIND_RUNTIME_IMAGE_REF is missing." >&2; exit 2; }
timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" image inspect "$IMAGE_REF" >/dev/null 2>&1 || {
  echo "Expected image is not installed: $IMAGE_REF" >&2
  exit 2
}

CHECKPOINT_ROOT="$ROOT/runtime-data/state/portfolio-checkpoints"
mkdir -p "$CHECKPOINT_ROOT"
chown "$RUNTIME_UID:$RUNTIME_GID" "$CHECKPOINT_ROOT"
chmod 0700 "$CHECKPOINT_ROOT"

if [ -s "$CHECKPOINT_ROOT/checkpoints.jsonl" ]; then
  echo "Existing Portfolio checkpoint seed found; verifying before reuse."
else
  timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" run --rm \
    --user "$RUNTIME_UID:$RUNTIME_GID" \
    --entrypoint python3 \
    -v "$ROOT/runtime-data/personal:/var/lib/trademind/personal:ro" \
    -v "$ROOT/runtime-data/state:/var/lib/trademind/state" \
    "$IMAGE_REF" \
    /app/protocol/v1/deployment/portfolio_authority_cli.py seed \
    --personal-data-root /var/lib/trademind/personal \
    --native-store-root /var/lib/trademind/state/native-write \
    --checkpoint-root /var/lib/trademind/state/portfolio-checkpoints
fi

if ! VERIFY=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" run --rm \
  --user "$RUNTIME_UID:$RUNTIME_GID" \
  --entrypoint python3 \
  -v "$ROOT/runtime-data/personal:/var/lib/trademind/personal:ro" \
  -v "$ROOT/runtime-data/state:/var/lib/trademind/state" \
  "$IMAGE_REF" \
  /app/protocol/v1/deployment/portfolio_authority_cli.py verify \
  --personal-data-root /var/lib/trademind/personal \
  --native-store-root /var/lib/trademind/state/native-write \
  --checkpoint-root /var/lib/trademind/state/portfolio-checkpoints); then
  echo "Portfolio authority verification command timed out or failed." >&2
  exit 2
fi

printf '%s\n' "$VERIFY"
printf '%s' "$VERIFY" | grep -q '"ready_for_cutover":true' || {
  echo "Portfolio authority seed verification failed." >&2
  exit 2
}
echo "InvestKitchen Portfolio authority seed: PASS"
