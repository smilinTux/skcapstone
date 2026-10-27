#!/usr/bin/env bash
# sync-conflict-guard: count NEW Syncthing conflict copies under the agent
# homes and raise a GTD item (plus an sk-alert) when more than a handful
# appeared in the last day.
#
# Why: per-host runtime state written into the shared agent home forked into
# ~400 conflict copies per chi host per month before anyone noticed
# (2026-10-27, PR #1289). On chi the copies are .stignored, so they stay
# local to the host that made them and nobody else ever sees them. This turns
# a silent pile-up into one item per host per day.
#
# A conflict copy keeps the losing version's mtime, so "new" is measured by
# ctime (the rename that created it), not mtime.
#
# Env: SK_CONFLICT_GUARD_THRESHOLD (default 5), SK_CONFLICT_GUARD_HOURS (24),
#      SK_CONFLICT_GUARD_ROOT (~/.skcapstone/agents), SK_CONFLICT_GUARD_DRY=1
set -uo pipefail

root="${SK_CONFLICT_GUARD_ROOT:-$HOME/.skcapstone/agents}"
threshold="${SK_CONFLICT_GUARD_THRESHOLD:-5}"
hours="${SK_CONFLICT_GUARD_HOURS:-24}"
host="$(hostname -s)"
day="$(date +%F)"
bin="${HOME}/.skenv/bin"

[ -d "$root" ] || { echo "no agent root at $root"; exit 0; }

list="$(timeout 600 find "$root" -name '*.sync-conflict-*' -cmin "-$((hours * 60))" 2>/dev/null)"
rc=$?
if [ "$rc" -ne 0 ]; then
    # A timeout-killed find prints a partial list; do not report it as a count.
    echo "find failed or timed out (rc=$rc); not reporting" >&2
    exit 1
fi
count=$(printf '%s' "$list" | grep -c . || true)
echo "host=$host new_conflicts=$count window=${hours}h threshold=$threshold"
[ "$count" -gt "$threshold" ] || exit 0

# Top paths, conflict stamp stripped, so the item names the writer to fix.
top="$(printf '%s\n' "$list" | sed -E "s#^$root/##; s/\.sync-conflict-[0-9]{8}-[0-9]{6}-([A-Z0-9]{7})/ [\1]/" \
      | sed -E 's/[0-9a-f]{8}-[0-9a-f-]{27,}/<uuid>/g' | sort | uniq -c | sort -rn | head -5 \
      | awk '{$1=$1; print}' | paste -sd ';' -)"
text="Sync conflicts: $count new in ${hours}h under agent homes on $host (threshold $threshold). Top: $top. Find the multi-host writer; archive copies outside the synced tree, never delete. See reference_syncthing_agent_home_conflicts."

if [ "${SK_CONFLICT_GUARD_DRY:-0}" = "1" ]; then
    echo "DRY: $text"
    exit 0
fi
"$bin/skcapstone" gtd capture "$text" --source manual --context @computer \
    --source-ref "sync-conflict-guard:$host:$day" || echo "gtd capture failed" >&2
if [ -x "$bin/sk-alert" ]; then
    "$bin/sk-alert" "$text" >/dev/null 2>&1 || echo "sk-alert failed" >&2
fi
