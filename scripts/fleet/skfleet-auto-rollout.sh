#!/usr/bin/env bash
# skfleet-auto-rollout: deploy merged main to the fleet without a human or a
# chat session holding a loop open.
#
# Why this exists (2026-10-08): rollout ran from a polling loop inside an
# operator chat session. When that loop ended or the session paused, merged
# fixes sat undeployed for hours (#970 waited about nine hours while the fleet
# dispatched nothing), and two overlapping loops once raced each other.
#
# One run: take a lock, fetch origin, and if the deploy checkout is behind
# origin/main, fast-forward it and roll each host in order with
# `skcapstone fleet rollout --apply`. A host whose gate does not pass is
# retried after a readiness refresh; if it still fails the run stops there
# (later hosts keep the previous main) and exits nonzero so the unit's
# OnFailure alert fires. Up-to-date runs are a quiet no-op.
#
# Hand-installed on exactly ONE authority host (see
# the auto-rollout unit files in scripts/fleet/systemd/). Do not enable it on
# more than one host: two deployers race.
set -uo pipefail

REPO="${SKFLEET_AUTO_ROLLOUT_REPO:-$HOME/deploy/skcapstone}"
HOSTS="${SKFLEET_AUTO_ROLLOUT_HOSTS:-chiap08 chiap03 chiap01 chiap02 chiap04}"
SKCAPSTONE="${SKCAPSTONE_BIN:-skcapstone}"
SSH="${SSH_BIN:-ssh}"
SKMAIL="${SKMAIL_BIN:-skmail}"
NOTIFY="${SKFLEET_AUTO_ROLLOUT_NOTIFY:-jarvis lumina-nor}"
LOCK="${SKFLEET_AUTO_ROLLOUT_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/skfleet-auto-rollout.lock}"
ATTEMPTS="${SKFLEET_AUTO_ROLLOUT_ATTEMPTS:-3}"
READY_WAIT="${SKFLEET_AUTO_ROLLOUT_READY_WAIT:-150}"
SYSTEMCTL="${SYSTEMCTL_BIN:-systemctl}"
# Dispatcher units on this host that a deploy must not land in the middle of.
QUIET_UNITS="${SKFLEET_AUTO_ROLLOUT_QUIET_UNITS:-skfleet-seat-cycle.service skfleet-niobe-live.service}"
QUIET_WAIT="${SKFLEET_AUTO_ROLLOUT_QUIET_WAIT:-600}"
# Timers that start those units. The seat cycle re-arms 15s after each run, so
# waiting for a gap is not enough: the next cycle starts mid-rollout. Hold them
# for the length of the run and restart them on every exit path.
HOLD_TIMERS="${SKFLEET_AUTO_ROLLOUT_HOLD_TIMERS:-skfleet-seat-cycle.timer}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

notify() {
  local subject="$1" body="$2" to
  command -v "$SKMAIL" >/dev/null 2>&1 || return 0
  for to in $NOTIFY; do
    "$SKMAIL" send skfleet-auto-rollout "$to" normal "$subject" "$body" >/dev/null 2>&1 || true
  done
}

exec 9>"$LOCK"
if ! flock -n 9; then
  log "another auto-rollout holds $LOCK; skipping"
  exit 0
fi

if ! git -C "$REPO" fetch -q origin main; then
  log "fetch failed; will retry next tick"
  exit 0
fi
# A deploy that lands while the local dispatch cycle runs crashes that cycle
# (exit 70/1), and the drift gate then halts on the failed unit: seen
# 2026-10-09 at 02:32Z and 04:37Z. Wait for the cycle to finish; if it is
# still busy after QUIET_WAIT, skip this tick and let the next one try.
busy_units() {
  local u busy=""
  for u in $QUIET_UNITS; do
    case "$("$SYSTEMCTL" --user is-active "$u" 2>/dev/null)" in
      active|activating|reloading) busy="$busy $u" ;;
    esac
  done
  printf '%s' "${busy# }"
}
head=$(git -C "$REPO" rev-parse HEAD)
target=$(git -C "$REPO" rev-parse origin/main)
if [ "$head" = "$target" ]; then
  exit 0
fi
if ! git -C "$REPO" merge-base --is-ancestor "$head" "$target"; then
  log "deploy checkout $head is not an ancestor of origin/main $target; refusing (fix the checkout by hand)"
  notify "auto-rollout refused: diverged checkout" "deploy checkout $head is not an ancestor of origin/main $target on $(hostname)"
  exit 1
fi
held=""
release_timers() {
  local t
  for t in $held; do
    "$SYSTEMCTL" --user start "$t" >/dev/null 2>&1 || log "could not restart $t; start it by hand"
  done
  held=""
}
trap release_timers EXIT
for t in $HOLD_TIMERS; do
  if [ "$("$SYSTEMCTL" --user is-active "$t" 2>/dev/null)" = active ] \
    && "$SYSTEMCTL" --user stop "$t" >/dev/null 2>&1; then
    held="$held $t"
  fi
done
waited=0
while [ -n "$(busy_units)" ]; do
  if [ "$waited" -ge "$QUIET_WAIT" ]; then
    log "dispatch cycle still busy ($(busy_units)) after ${QUIET_WAIT}s; deferring main ${target:0:8} to the next tick"
    exit 0
  fi
  sleep 10; waited=$((waited + 10))
done
git -C "$REPO" merge -q --ff-only "$target" || { log "fast-forward failed"; exit 1; }
subject=$(git -C "$REPO" log -1 --format=%s "$target")
log "rolling main ${target:0:8} ($subject) from ${head:0:8} to: $HOSTS"

rolled=()
for host in $HOSTS; do
  ok=0
  for attempt in $(seq 1 "$ATTEMPTS"); do
    out=$("$SKCAPSTONE" fleet rollout --node "$host" --repo-root "$REPO" --remote-repo-root "~/deploy/skcapstone" --apply 2>&1 | tail -1)
    log "$host try$attempt: $out"
    if printf '%s' "$out" | grep -q "gate passed"; then ok=1; break; fi
    # A merge during the run makes the host pull a newer main than the
    # manifest this run built, so its gate reports git_sha drift. That is not
    # a fault: stand down quietly and let the next tick roll the newer main.
    if git -C "$REPO" fetch -q origin main && [ "$(git -C "$REPO" rev-parse origin/main)" != "$target" ]; then
      log "main moved to $(git -C "$REPO" rev-parse --short=8 origin/main) during this run; superseded at $host, next tick rolls it"
      exit 0
    fi
    # The gate reads a synced readiness verdict; refresh it and give syncthing a scan.
    "$SSH" -o BatchMode=yes "$host" 'systemctl --user start skfleet-readiness.service; k=$(grep -o "<apikey>[^<]*" ~/.config/syncthing/config.xml ~/.local/state/syncthing/config.xml 2>/dev/null | head -1 | cut -d">" -f2); curl -s -o /dev/null -X POST -H "X-API-Key: $k" "http://127.0.0.1:8384/rest/db/scan?folder=skcapstone&sub=fleet/status/node-$(hostname)/readiness"' >/dev/null 2>&1 || true
    sleep $(( READY_WAIT / ATTEMPTS ))
  done
  if [ "$ok" != 1 ]; then
    log "HALT: $host did not pass its gate after $ATTEMPTS tries; later hosts keep the previous main"
    notify "auto-rollout HALTED at $host" "main ${target:0:8} ($subject) rolled to: ${rolled[*]:-none}; $host failed its gate. Later hosts not touched."
    exit 1
  fi
  "$SSH" -o BatchMode=yes "$host" 'systemctl --user daemon-reload; systemctl --user restart sknoded.service' >/dev/null 2>&1 || log "$host: sknoded restart reported an error"
  rolled+=("$host")
done

log "ROLLED main ${target:0:8} to: ${rolled[*]}"
notify "ROLLED main ${target:0:8}" "$subject -> ${rolled[*]} (gates passed)"
exit 0
