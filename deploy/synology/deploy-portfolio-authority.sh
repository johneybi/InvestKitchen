#!/bin/sh
set -eu

ROOT=${TRADEMIND_SYNOLOGY_ROOT:-/volume1/docker/trademind}
REPO_ROOT=${TRADEMIND_RUNTIME_REPO_ROOT:-$ROOT/runtime-repo}
ARCHIVE=${1:-$ROOT/runtime-images/trademind-runtime.tar}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this deployment with sudo/root." >&2
  exit 2
fi

cd "$REPO_ROOT"
./deploy/synology/install-image.sh "$ARCHIVE"
./deploy/synology/seed-portfolio-authority.sh
./deploy/synology/portfolio-authority-cutover.sh
