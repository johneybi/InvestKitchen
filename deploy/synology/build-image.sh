#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
COMMIT=${TRADEMIND_SOURCE_COMMIT:-$(git -C "$ROOT" rev-parse HEAD)}
IMAGE_REF=${TRADEMIND_RUNTIME_IMAGE_REF:-trademind-runtime:synology-$COMMIT}
OUT_DIR=${TRADEMIND_IMAGE_OUTPUT_DIR:-$ROOT/dist}
ARCHIVE=${TRADEMIND_IMAGE_ARCHIVE:-$OUT_DIR/trademind-runtime-$COMMIT.tar}

mkdir -p "$OUT_DIR"
rm -f "$ARCHIVE" "$ARCHIVE.sha256"

# Produce the deployment artifact directly as a single-platform Docker archive.
# Provenance/SBOM attestations are disabled here because DSM Docker 24's older
# BuildKit/OCI stack does not understand newer empty-config attestation media
# types. The OpenAI runtime ZIP itself is still verified in the Dockerfile by
# the release SHA-256 sidecar value.
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --sbom=false \
  --build-arg TRADEMIND_SOURCE_COMMIT="$COMMIT" \
  -f "$ROOT/deploy/synology/Dockerfile" \
  -t "$IMAGE_REF" \
  --output="type=docker,dest=$ARCHIVE" \
  "$ROOT"

# Validate exactly what will be transferred to Synology by loading the archive
# back into Docker and inspecting/running that loaded image.
docker load -i "$ARCHIVE" >/dev/null

PLATFORM=$(docker image inspect --format '{{.Architecture}}/{{.Os}}' "$IMAGE_REF")
[ "$PLATFORM" = "amd64/linux" ] || {
  echo "Unexpected image platform: $PLATFORM" >&2
  exit 2
}

REVISION=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE_REF")
[ "$REVISION" = "$COMMIT" ] || {
  echo "Image revision mismatch: $REVISION vs $COMMIT" >&2
  exit 2
}

VERSION=$(docker run --rm --entrypoint /usr/bin/tunnel-client-runtime "$IMAGE_REF" --version)
case "$VERSION" in
  0.0.14*) ;;
  *) echo "Unexpected tunnel-client-runtime version: $VERSION" >&2; exit 2 ;;
esac

# The archive index must contain exactly one linux/amd64 Docker manifest and no
# attestation entries. This is intentionally checked without external jq.
INDEX_JSON=$(tar -xOf "$ARCHIVE" index.json)
MANIFEST_COUNT=$(printf '%s' "$INDEX_JSON" | grep -o '"mediaType":"application/vnd.docker.distribution.manifest.v2+json"' | wc -l | tr -d ' ')
EMPTY_CONFIG_COUNT=$(printf '%s' "$INDEX_JSON" | grep -o 'application/vnd.oci.empty.v1+json' | wc -l | tr -d ' ')
[ "$MANIFEST_COUNT" = "1" ] || {
  echo "Unexpected archive manifest count: $MANIFEST_COUNT" >&2
  exit 2
}
[ "$EMPTY_CONFIG_COUNT" = "0" ] || {
  echo "Archive contains unsupported OCI attestation entries." >&2
  exit 2
}

shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"

echo "TradeMind Synology image build: PASS"
echo "image=$IMAGE_REF"
echo "platform=$PLATFORM"
echo "revision=$REVISION"
echo "archive=$ARCHIVE"
echo "sha256_file=$ARCHIVE.sha256"
