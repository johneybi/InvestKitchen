#!/bin/sh
# Build only an identified product commit; never send an operator workspace.
set -eu
if [ "$#" -ne 1 ]; then
  echo "Usage: deploy/gemini/build-images.sh <product-commit>" >&2
  exit 2
fi
commit=$(git rev-parse --verify "$1^{commit}")
context=$(mktemp -d "${TMPDIR:-/tmp}/investkitchen-gemini-build.XXXXXX")
trap 'rm -rf "$context"' EXIT HUP INT TERM
git archive "$commit" pyproject.toml protocol deploy/gemini/Dockerfile \
  deploy/gemini/edge.Dockerfile deploy/gemini/runtime-entrypoint.sh | tar -xf - -C "$context"
docker build --platform linux/amd64 --build-arg "INVESTKITCHEN_SOURCE_COMMIT=$commit" \
  -f "$context/deploy/gemini/Dockerfile" -t "investkitchen-http:gemini-$commit" "$context"
docker build --platform linux/amd64 --build-arg "INVESTKITCHEN_SOURCE_COMMIT=$commit" \
  -f "$context/deploy/gemini/edge.Dockerfile" -t "investkitchen-gemini-edge:gemini-$commit" "$context"
echo "Built InvestKitchen Gemini trial images for product commit $commit"
