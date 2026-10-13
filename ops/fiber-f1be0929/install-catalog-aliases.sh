#!/usr/bin/env bash
# One-time, additive migration. Never print or transfer credential values.
set -euo pipefail
umask 077
catalog=${1:?catalog path required}
descriptors=${2:?nonsecret descriptor path required}
filter=${3:?jq filter path required}
[[ -f "$catalog" && ! -L "$catalog" ]] || exit 78
exec 9>"${catalog}.fiber.lock"
flock -n 9 || exit 75
before=$(sha256sum "$catalog" | cut -d ' ' -f1)
candidate=$(mktemp "${catalog}.fiber-candidate.XXXXXX")
trap 'rm -f -- "$candidate"' EXIT
jq --slurpfile aliases "$descriptors" -f "$filter" "$catalog" > "$candidate"
# Compare parsed values so a repeated migration is a true no-op.
if cmp -s <(jq -S . "$catalog") <(jq -S . "$candidate"); then
    printf 'unchanged sha256=%s\n' "$before"
    exit 0
fi
[[ $(sha256sum "$catalog" | cut -d ' ' -f1) == "$before" ]] || exit 75
backup=$(mktemp "${catalog}.fiber-backup.XXXXXX")
cp --preserve=mode,timestamps -- "$catalog" "$backup"
chmod 600 "$backup" "$candidate"
[[ $(sha256sum "$backup" | cut -d ' ' -f1) == "$before" ]] || exit 75
[[ $(sha256sum "$catalog" | cut -d ' ' -f1) == "$before" ]] || exit 75
mv -- "$candidate" "$catalog"
printf 'installed before=%s after=%s backup=%s\n' "$before" "$(sha256sum "$catalog" | cut -d ' ' -f1)" "$backup"
