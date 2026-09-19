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
ARCHIVE=${1:-$ROOT/runtime-images/trademind-runtime.tar}
DOCKER_TIMEOUT=${INVESTKITCHEN_DOCKER_TIMEOUT_SECONDS:-120}
DOCKER_LOAD_TIMEOUT=${INVESTKITCHEN_DOCKER_LOAD_TIMEOUT_SECONDS:-300}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer with sudo/root so it can access the Docker daemon." >&2
  exit 2
fi
if [ ! -r "$ENV_FILE" ]; then
  echo "TradeMind runtime deploy env is missing: $ENV_FILE" >&2
  exit 2
fi
if [ ! -r "$ARCHIVE" ]; then
  echo "TradeMind runtime image archive is missing: $ARCHIVE" >&2
  exit 2
fi
if [ ! -r "$ROOT/runtime-data/personal/personal-data-manifest.json" ]; then
  echo "TradeMind personal-data manifest is missing." >&2
  exit 2
fi

IMAGE_REF=$(sed -n 's/^TRADEMIND_RUNTIME_IMAGE_REF=//p' "$ENV_FILE" | tail -n 1)
RUNTIME_UID=$(sed -n 's/^TRADEMIND_RUNTIME_UID=//p' "$ENV_FILE" | tail -n 1)
RUNTIME_GID=$(sed -n 's/^TRADEMIND_RUNTIME_GID=//p' "$ENV_FILE" | tail -n 1)
PORTFOLIO_AUTHORITY=$(sed -n 's/^INVESTKITCHEN_PORTFOLIO_AUTHORITY=//p' "$ENV_FILE" | tail -n 1)
MARKET_PROVIDER=$(sed -n 's/^INVESTKITCHEN_MARKET_PROVIDER=//p' "$ENV_FILE" | tail -n 1)
MARKET_SECRET_HOST_FILE=$(sed -n 's/^INVESTKITCHEN_TOSS_MARKET_SECRET_HOST_FILE=//p' "$ENV_FILE" | tail -n 1)
RUNTIME_UID=${RUNTIME_UID:-1000}
RUNTIME_GID=${RUNTIME_GID:-1000}
PORTFOLIO_AUTHORITY=${PORTFOLIO_AUTHORITY:-personal-data}
MARKET_PROVIDER=${MARKET_PROVIDER:-none}
MARKET_SECRET_HOST_FILE=${MARKET_SECRET_HOST_FILE:-$ROOT/secrets/toss-market-readonly.json}
if [ -z "$IMAGE_REF" ]; then
  echo "TRADEMIND_RUNTIME_IMAGE_REF is missing from $ENV_FILE" >&2
  exit 2
fi

# Load the exact prebuilt image; production Synology does not build it.
LOAD_OUT=$(mktemp /tmp/trademind-docker-load.XXXXXX)
PREFLIGHT_OUT=$(mktemp /tmp/trademind-mcp-preflight.XXXXXX)
trap 'rm -f "$LOAD_OUT" "$PREFLIGHT_OUT"' EXIT HUP INT TERM
timeout "${DOCKER_LOAD_TIMEOUT}s" "$DOCKER_BIN" load -i "$ARCHIVE" >"$LOAD_OUT"
if ! timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" image inspect "$IMAGE_REF" >/dev/null 2>&1; then
  cat "$LOAD_OUT" >&2 || true
  echo "Loaded archive did not provide expected image: $IMAGE_REF" >&2
  exit 2
fi

PLATFORM=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" image inspect --format '{{.Architecture}}/{{.Os}}' "$IMAGE_REF")
if [ "$PLATFORM" != "amd64/linux" ]; then
  echo "Unexpected image platform: $PLATFORM" >&2
  exit 2
fi

VERSION=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" run --rm --entrypoint /usr/bin/tunnel-client-runtime "$IMAGE_REF" --version)
case "$VERSION" in
  0.0.14*) ;;
  *) echo "Unexpected tunnel-client-runtime version: $VERSION" >&2; exit 2 ;;
esac

MARKET_ARGS="--market-provider none"
MARKET_MOUNT_ARGS=""
case "$MARKET_PROVIDER" in
  none)
    ;;
  toss-native-subprocess)
    if [ ! -s "$MARKET_SECRET_HOST_FILE" ]; then
      echo "Toss market provider is enabled but its secret file is missing." >&2
      exit 2
    fi
    MARKET_ARGS="--market-provider toss-native-subprocess --market-secret-file /run/secrets/toss_market_readonly"
    MARKET_MOUNT_ARGS="-v $MARKET_SECRET_HOST_FILE:/run/secrets/toss_market_readonly:ro"
    ;;
  *)
    echo "Unsupported INVESTKITCHEN_MARKET_PROVIDER: $MARKET_PROVIDER" >&2
    exit 2
    ;;
esac

REVISION=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE_REF")
case "$IMAGE_REF" in
  *"$REVISION"*) ;;
  *)
    echo "Image revision label does not match image tag: $REVISION vs $IMAGE_REF" >&2
    exit 2
    ;;
esac

cd "$REPO_ROOT"
timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml config --quiet

CHECKPOINT_ARGS=""
case "$PORTFOLIO_AUTHORITY" in
  personal-data)
    ;;
  checkpoint)
    if [ ! -s "$ROOT/runtime-data/state/portfolio-checkpoints/checkpoints.jsonl" ]; then
      echo "Portfolio checkpoint authority is enabled but no checkpoint journal exists." >&2
      exit 2
    fi
    CHECKPOINT_ARGS="--portfolio-checkpoint-root /var/lib/trademind/state/portfolio-checkpoints"
    ;;
  *)
    echo "Unsupported INVESTKITCHEN_PORTFOLIO_AUTHORITY: $PORTFOLIO_AUTHORITY" >&2
    exit 2
    ;;
esac

# Validate the exact image against the mounted native personal-data store without
# contacting OpenAI or reading the runtime API key.
timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" run --rm \
  --user "$RUNTIME_UID:$RUNTIME_GID" \
  --entrypoint python3 \
  -v "$ROOT/runtime-data/personal:/var/lib/trademind/personal:ro" \
  -v "$ROOT/runtime-data/state:/var/lib/trademind/state" \
  $MARKET_MOUNT_ARGS \
  "$IMAGE_REF" \
  /app/protocol/v1/deployment/secure_tunnel.py preflight \
  --runtime-root /app \
  --personal-data-root /var/lib/trademind/personal \
  --native-store-root /var/lib/trademind/state/native-write \
  --advisory-state-root /var/lib/trademind/state/advisory-state \
  --historical-decision-root /var/lib/trademind/state/historical-decisions \
  --historical-transaction-root /var/lib/trademind/state/historical-transactions \
  $CHECKPOINT_ARGS \
  --manifest /app/protocol/v1/fixtures/full-reference.instance.json \
  $MARKET_ARGS \
  >"$PREFLIGHT_OUT"

grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$PREFLIGHT_OUT" || {
  cat "$PREFLIGHT_OUT" >&2
  echo "MCP preflight failed." >&2
  exit 2
}
for tool in get_capabilities get_portfolio_state get_open_items get_current_policy get_active_plans get_current_knowledge search_knowledge build_decision_context start_reflection get_decision_history get_transactions; do
  grep -q "\"$tool\"" "$PREFLIGHT_OUT" || {
    cat "$PREFLIGHT_OUT" >&2
    echo "MCP preflight missing tool: $tool" >&2
    exit 2
  }
done
if [ "$MARKET_PROVIDER" != "none" ]; then
  for tool in get_market_quote get_market_ohlcv; do
    grep -q "\"$tool\"" "$PREFLIGHT_OUT" || {
      cat "$PREFLIGHT_OUT" >&2
      echo "MCP preflight missing market tool: $tool" >&2
      exit 2
    }
  done
fi
echo "TradeMind image install preflight: PASS"

echo "image=$IMAGE_REF"
echo "platform=$PLATFORM"
echo "revision=$REVISION"
echo "tunnel_runtime=0.0.14"
echo "portfolio_authority=$PORTFOLIO_AUTHORITY"
echo "market_provider=$MARKET_PROVIDER"
