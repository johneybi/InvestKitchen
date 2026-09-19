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
  echo "Run this cutover with sudo/root so it can use the Docker daemon." >&2
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
[ -s "$ROOT/runtime-data/state/portfolio-checkpoints/checkpoints.jsonl" ] || {
  echo "Portfolio checkpoint journal is missing; seed before cutover." >&2
  exit 2
}

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
  echo "Portfolio authority verification failed before cutover." >&2
  exit 2
}

echo "Creating pre-cutover v2 backup..."
timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" run --rm \
  --user "$RUNTIME_UID:$RUNTIME_GID" \
  --entrypoint python3 \
  -v "$ROOT/runtime-data/state:/var/lib/trademind/state" \
  -v "$ROOT/runtime-data/backups:/var/lib/trademind/backups" \
  "$IMAGE_REF" \
  /app/protocol/v1/deployment/storage_recovery_cli.py backup \
  --state-root /var/lib/trademind/state \
  --backup-root /var/lib/trademind/backups \
  --instance-id fixture-full-reference

stamp=$(date -u +%Y%m%dT%H%M%SZ)
ENV_BACKUP="$ENV_FILE.pre-portfolio-authority-$stamp"
cp -p "$ENV_FILE" "$ENV_BACKUP"

set_authority() {
  value=$1
  owner=$(stat -c '%u:%g' "$ENV_FILE")
  mode=$(stat -c '%a' "$ENV_FILE")
  tmp=$(mktemp "$ENV_FILE.tmp.XXXXXX")
  awk -v value="$value" '
    BEGIN { found=0 }
    /^INVESTKITCHEN_PORTFOLIO_AUTHORITY=/ { print "INVESTKITCHEN_PORTFOLIO_AUTHORITY=" value; found=1; next }
    { print }
    END { if (!found) print "INVESTKITCHEN_PORTFOLIO_AUTHORITY=" value }
  ' "$ENV_FILE" > "$tmp"
  chown "$owner" "$tmp"
  chmod "$mode" "$tmp"
  mv "$tmp" "$ENV_FILE"
}

wait_healthy() {
  cd "$REPO_ROOT"
  container_id=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml ps -q runtime)
  [ -n "$container_id" ] || return 1
  for _ in $(seq 1 60); do
    status=$(timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")
    [ "$status" = "healthy" ] && return 0
    case "$status" in unhealthy|exited|dead) return 1 ;; esac
    sleep 2
  done
  return 1
}

set_authority checkpoint
cd "$REPO_ROOT"
if ! timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml up -d --no-build runtime || ! wait_healthy; then
  echo "Checkpoint authority cutover failed; rolling back to personal-data authority." >&2
  cp -p "$ENV_BACKUP" "$ENV_FILE"
  timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml up -d --no-build runtime || true
  wait_healthy || true
  exit 2
fi

echo "InvestKitchen Portfolio authority cutover: healthy"
echo "rollback_env=$ENV_BACKUP"
timeout "${DOCKER_TIMEOUT}s" "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f deploy/synology/compose.yml ps runtime
