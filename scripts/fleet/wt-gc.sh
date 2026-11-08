#!/usr/bin/env bash
# Squash-aware worktree GC with worktrunk (skfleet binaries: wt).
#
# Usage: wt-gc.sh [--apply] [--no-fetch] REPO...
#        wt-gc.sh [--apply] [--no-fetch] --discover ROOT...   (find main repos that have linked worktrees)
#
# Dry run by default. Removes only worktrees that are:
#   integrated into the default branch (worktrunk: tree match / simulated merge / patch-id),
#   clean, not the main worktree, not locked (wt refuses locked ones itself, e.g. live
#   Claude subagents), and not the cwd of any running process.
# Also prunes metadata for worktrees whose directory is already gone, EXCEPT entries under
# /mnt (a network mount that is merely offline would read as "gone"): those are locked with
# a reason instead, for a human to look at.
# --no-fetch skips `git fetch`. That is the safe direction: a stale default branch can only
# make FEWER worktrees read as integrated. Use it on loaded hosts.
#
# Worker hosts whose workspaces are custody evidence need an explicit operator decision
# before --apply (chiap08: Chef approved 2026-11-07).
set -uo pipefail
APPLY=0 FETCH=1 DISCOVER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --no-fetch) FETCH=0 ;;
    --discover) DISCOVER=1 ;;
    *) break ;;
  esac
  shift
done
WT=${WT:-$HOME/.local/bin/wt}
tmpf=$(mktemp)
trap 'rm -f "$tmpf"' EXIT

repos=("$@")
if [ $DISCOVER = 1 ]; then
  mapfile -t repos < <(
    for root in "$@"; do
      find "$root" -maxdepth 5 -name .git -type f -not -path '*/node_modules/*' 2>/dev/null
    done | while read -r f; do
      g=$(sed -n 's/^gitdir: //p' "$f"); echo "${g%/.git/worktrees/*}"
    done | sort -u | grep -v '^/mnt/'
  )
fi

# Snapshot of every process cwd (reading /proc can block on D-state processes: cap each read).
busy=$(for p in /proc/[0-9]*; do timeout 1 readlink "$p/cwd" 2>/dev/null; done | sort -u)

total_rm=0 total_prune=0
for repo in "${repos[@]}"; do
  [ -d "$repo/.git" ] || continue
  cd "$repo" || continue
  # Dead entries: prune, but lock (do not prune) anything under /mnt.
  dead=$(git worktree list --porcelain | awk '/^worktree /{p=substr($0,10)} /^prunable/{print p}')
  np=0
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    case "$p" in
      /mnt/*) [ $APPLY = 1 ] && git worktree lock --reason "wt-gc: missing under /mnt, check the mount" "$p" 2>/dev/null ;;
      *) np=$((np + 1)) ;;
    esac
  done <<<"$dead"
  [ $APPLY = 1 ] && [ "$np" -gt 0 ] && git worktree prune
  [ $FETCH = 1 ] && timeout 120 git fetch -q origin 2>/dev/null
  NO_COLOR=1 timeout 600 "$WT" list --format=json 2>/dev/null | python3 -I -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except ValueError:
    items = []
for i in items:
    w = i.get("worktree") or {}
    if not w or w.get("main") or not i.get("branch"):
        continue
    ch = w.get("changes") or {}
    dirty = any(v for k, v in ch.items() if k != "diff")
    st = (i.get("display") or {}).get("state", "")
    print(i["branch"], w["path"], st, "dirty" if dirty else "clean", sep="\t")
' >"$tmpf"
  rm_n=0 keep=""
  while IFS=$'\t' read -r br path st cl; do
    if [ "$st" = integrated ] && [ "$cl" = clean ] \
      && ! grep -qxF -e "$path" <<<"$busy" && ! grep -qF -e "$path/" <<<"$busy"; then
      rm_n=$((rm_n + 1))
      if [ $APPLY = 1 ]; then
        timeout 300 "$WT" remove "$br" -y --foreground >/dev/null 2>&1 || echo "  WARN remove refused/failed: $br"
      else
        echo "  would remove: $br ($path)"
      fi
    else
      keep="$keep  keep: $br [$st,$cl]\n"
    fi
  done <"$tmpf"
  total_rm=$((total_rm + rm_n)) total_prune=$((total_prune + np))
  echo "$repo: pruned-missing=$np removed-integrated=$rm_n"
  if [ -n "$keep" ]; then printf "%b" "$keep"; fi
done
echo "TOTAL: pruned-missing=$total_prune removed-integrated=$total_rm apply=$APPLY"
