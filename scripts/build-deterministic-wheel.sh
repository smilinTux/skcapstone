#!/usr/bin/env bash
set -euo pipefail

repo=$(git rev-parse --show-toplevel)
output=${1:?usage: build-deterministic-wheel.sh OUTPUT_DIRECTORY}
epoch=$(git -C "$repo" show -s --format=%ct HEAD)

mkdir -p "$output"
SOURCE_DATE_EPOCH="$epoch" PYTHONHASHSEED=0 \
  python -m build --wheel --outdir "$output" "$repo"
sha256sum "$output"/*.whl
