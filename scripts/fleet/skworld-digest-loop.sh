#!/usr/bin/env bash
# SKWorld fleet digest loop. Renders an operations page every INTERVAL seconds.
#
# The previous version lived in /tmp, so it did not survive a reboot, and it
# built the page by shelling out and pasting raw text into <pre> blocks. It also
# surveyed only three of five hosts and its inline renderer threw re.error every
# cycle on a regex bash had mangled. All of that is gone: skworld-digest.py
# collects structured state and renders it, and this only schedules it.
set -u
INTERVAL="${SKWORLD_DIGEST_INTERVAL:-600}"      # 10 minutes
HTML="${SKWORLD_DIGEST_HTML:-/tmp/skworld-fleet-digest.html}"
LOG="${SKWORLD_DIGEST_LOG:-$HOME/.skcapstone/fleet/digest.log}"
DIGEST="$HOME/.local/bin/skworld-digest.py"

mkdir -p "$(dirname "$LOG")"
while true; do
  start=$(date -u +%FT%TZ)
  if out=$(python3 "$DIGEST" "$HTML" 2>&1); then
    printf '%s  ok    %s\n' "$start" "$out" >> "$LOG"
  else
    # Never leave the page stale and silent on failure: the whole reason this
    # was rewritten is that a broken reporter looked like a dead fleet.
    printf '%s  FAIL  %s\n' "$start" "$out" >> "$LOG"
  fi
  # keep the log bounded; this runs every 10 minutes forever
  tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
  sleep "$INTERVAL"
done
