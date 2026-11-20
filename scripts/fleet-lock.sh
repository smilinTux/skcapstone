#!/usr/bin/env bash
# Regenerate deploy/fleet-constraints.txt, the fleet's fully resolved lock.
#
#   scripts/fleet-lock.sh            rewrite the lock, keeping every existing pin
#                                    unless an input forces a change
#   scripts/fleet-lock.sh --check    exit 1 if the lock is stale for its inputs
#   scripts/fleet-lock.sh --upgrade-package NAME [...]   move NAME to its newest
#                                    version the inputs allow
#   scripts/fleet-lock.sh --upgrade  move everything (what Renovate's weekly
#                                    lock maintenance does)
#
# Inputs: deploy/fleet-self.in (skcapstone's own '.[fleet-qualify]'),
# deploy/fleet-requirements.in (SK pins) and deploy/fleet-tools.in (third-party
# tools in the same venv). The lock is universal: one file, with environment
# markers, for every fleet Python (3.12 and 3.14).
#
# The command below is the one uv writes into the lock's header, and the one
# Renovate's pip-compile manager reruns, so a Renovate PR and a hand run produce
# the same file. Change it in both places or not at all.
set -euo pipefail
cd "$(dirname "$0")/.."
LOCK=deploy/fleet-constraints.txt
CMD=(uv pip compile --universal --python-version 3.12 --no-annotate
  --no-emit-package skcapstone
  deploy/fleet-self.in deploy/fleet-requirements.in deploy/fleet-tools.in)

extra=()
check=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) check=1 ;;
    --upgrade) extra+=(--upgrade) ;;
    --upgrade-package) extra+=(--upgrade-package "$2"); shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# The current lock seeds the resolver (uv reads an existing -o file as
# preferences), so unrelated pins never move.
cp "$LOCK" "$tmp/lock.txt"
"${CMD[@]}" --quiet -o "$tmp/lock.txt" ${extra[@]+"${extra[@]}"}
# uv names the output path in its header; keep it the repo path.
sed -i "s#-o $tmp/lock.txt#-o $LOCK#" "$tmp/lock.txt"

if [ "$check" = 1 ]; then
  if ! diff -u "$LOCK" "$tmp/lock.txt"; then
    echo "fleet lock is stale: run scripts/fleet-lock.sh and commit the result" >&2
    exit 1
  fi
  echo "fleet lock is current"
  exit 0
fi
cp "$tmp/lock.txt" "$LOCK"
echo "wrote $LOCK ($(grep -c '==' "$LOCK") pins)"
