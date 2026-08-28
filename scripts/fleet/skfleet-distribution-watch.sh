#!/usr/bin/env bash
set -u

hosts=(chiap01 chiap02 chiap03 chiap04 chiap08)
state_dir="$HOME/.skcapstone/fleet"
log_dir="$HOME/.skcapstone/evidence/fleet-distribution-watch"
state_file="$state_dir/distribution-watch.state"
mkdir -p "$state_dir" "$log_dir"

# The same eligible card can appear in every host's POOL line. Keep a set so
# the fleet aggregate describes unique work, not host copies of that work.
declare -A candidate_ids=()
candidate_manifests_missing=0

reset_candidate_inventory() {
  candidate_ids=()
  candidate_manifests_missing=0
}

record_candidate_pool() {
  local pool=$1 manifest cid
  local -a ids=()
  if [[ ! "$pool" =~ (^|[[:space:]])ids=([^[:space:]]+) ]]; then
    candidate_manifests_missing=$((candidate_manifests_missing + 1))
    return
  fi
  manifest=${BASH_REMATCH[2]}
  [[ "$manifest" == - ]] && return
  IFS=',' read -ra ids <<<"$manifest"
  for cid in "${ids[@]}"; do
    [[ "$cid" =~ ^[0-9a-f]{8}$ ]] || continue
    candidate_ids["$cid"]=1
  done
}

sample() {
  local host out workers result pool ready total_workers=0 unavailable=0 details=""
  reset_candidate_inventory
  for host in "${hosts[@]}"; do
    if [[ "$host" == chiap08 ]]; then
      out=$(bash -lc 'workers=$(tmux ls -F "#{session_name}" 2>/dev/null | grep -Ec "^(codex-auto-|glm-auto-)" || true); result=$(systemctl --user show skfleet-rotate.service -p Result --value); pool=$(journalctl --user -u skfleet-rotate.service -n 40 --no-pager -o cat | grep "POOL|" | tail -1); printf "%s|%s|%s\n" "$workers" "$result" "$pool"')
    else
      out=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" 'workers=$(tmux ls -F "#{session_name}" 2>/dev/null | grep -Ec "^(codex-auto-|glm-auto-)" || true); result=$(systemctl --user show skfleet-rotate.service -p Result --value); pool=$(journalctl --user -u skfleet-rotate.service -n 40 --no-pager -o cat | grep "POOL|" | tail -1); printf "%s|%s|%s\n" "$workers" "$result" "$pool"' 2>/dev/null) || out="0|unreachable|"
    fi
    IFS='|' read -r workers result pool <<<"$out"
    [[ "$workers" =~ ^[0-9]+$ ]] || workers=0
    total_workers=$((total_workers + workers))
    if [[ "$result" != success ]]; then
      unavailable=$((unavailable + 1))
    elif [[ "$pool" =~ ready=([0-9]+) ]]; then
      ready=${BASH_REMATCH[1]}
      record_candidate_pool "$pool"
    fi
    details+="$host:workers=$workers,result=$result,pool=${pool:-none};"
  done

  local queue queue_active queue_queued now current previous total_ready
  total_ready=${#candidate_ids[@]}
  queue=$(curl -fsS --max-time 8 http://chiap01:18790/queue 2>/dev/null || true)
  queue_active=$(sed -n 's/.*"totalActive":\([0-9][0-9]*\).*/\1/p' <<<"$queue")
  queue_queued=$(sed -n 's/.*"totalQueued":\([0-9][0-9]*\).*/\1/p' <<<"$queue")
  queue_active=${queue_active:-unavailable}
  queue_queued=${queue_queued:-unavailable}
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  current=up
  (( total_workers == 0 )) && current=zero
  (( unavailable == ${#hosts[@]} )) && current=unavailable
  previous=$(cat "$state_file" 2>/dev/null || true)

  printf '%s|state=%s|workers=%d|workable=%d|candidate_inventory_missing_hosts=%d|unavailable_hosts=%d|queue_active=%s|queue_queued=%s|%s\n' \
    "$now" "$current" "$total_workers" "$total_ready" "$candidate_manifests_missing" "$unavailable" "$queue_active" "$queue_queued" "$details" >> "$log_dir/watch.log"

  if [[ "$current" != "$previous" ]]; then
    if [[ "$current" == zero || "$current" == unavailable ]]; then
      skmail send jarvis lumina urgent FLEET-DISTRIBUTION-DOWN \
        "Fleet watcher transition at $now: state=$current workers=$total_workers unique_workable_cards=$total_ready candidate_inventory_missing_hosts=$candidate_manifests_missing unavailable_hosts=$unavailable queue_active=$queue_active queue_queued=$queue_queued. Details: $details. No restart, rotation trigger, gateway mutation, or mailbox acknowledgment was performed." >/dev/null
    elif [[ -n "$previous" ]]; then
      skmail send jarvis lumina normal FLEET-DISTRIBUTION-RECOVERED \
        "Fleet watcher recovery at $now: workers=$total_workers unique_workable_cards=$total_ready candidate_inventory_missing_hosts=$candidate_manifests_missing unavailable_hosts=$unavailable queue_active=$queue_active queue_queued=$queue_queued. Details: $details." >/dev/null
    fi
    printf '%s\n' "$current" > "$state_file"
  fi
  printf 'state=%s workers=%d workable=%d candidate_inventory_missing_hosts=%d unavailable_hosts=%d queue_active=%s queue_queued=%s\n' \
    "$current" "$total_workers" "$total_ready" "$candidate_manifests_missing" "$unavailable" "$queue_active" "$queue_queued"
}

if [[ "${SKFLEET_DISTRIBUTION_WATCH_LIB_ONLY:-0}" == 1 ]]; then
  return 0 2>/dev/null || exit 0
fi

if [[ "${1:-}" == --once ]]; then
  sample
  exit
fi

while true; do
  sample
  sleep 300
done
