#!/usr/bin/env bash
# Safe worktree GC with worktrunk. Usage: wt-gc.sh [--apply] REPO...
# Part of the worktrunk rollout (skfleet binaries: wt). Locked worktrees (live Claude
# subagents) are refused by wt itself. Do NOT point --apply at worker hosts whose
# workspaces are custody evidence (chiap08) without an explicit operator decision.
# Removes only worktrees that are: integrated into the default branch (squash-aware),
# clean, not the main worktree, and not the cwd of any running process.
# Always prunes metadata for worktrees whose directory is already gone.
set -uo pipefail
APPLY=0; [ "${1:-}" = "--apply" ] && { APPLY=1; shift; }
WT=$HOME/.local/bin/wt
tmpf=$(mktemp); trap 'rm -f "$tmpf"' EXIT
busy=$(for p in /proc/[0-9]*; do readlink "$p/cwd" 2>/dev/null; done | sort -u)
for repo in "$@"; do
  [ -d "$repo/.git" ] || continue
  cd "$repo" || continue
  np=$(git worktree list --porcelain | grep -c '^prunable')
  [ $APPLY = 1 ] && [ "$np" -gt 0 ] && git worktree prune
  git fetch -q origin 2>/dev/null
  NO_COLOR=1 "$WT" list --format=json 2>/dev/null | python3 -I -c '
import json,sys
for i in json.load(sys.stdin).get("items",[]):
    w=i.get("worktree") or {}
    if not w or w.get("main") or not i.get("branch"): continue
    ch=w.get("changes") or {}
    dirty=any(v for k,v in ch.items() if k!="diff")
    st=(i.get("display") or {}).get("state","")
    print(i["branch"], w["path"], st, "dirty" if dirty else "clean", sep="\t")
' > "$tmpf"
  rm_n=0; keep=""
  while IFS=$'\t' read -r br path st cl; do
    if [ "$st" = integrated ] && [ "$cl" = clean ] && ! grep -qxF -e "$path" -e "$path/" <<<"$busy" && ! grep -q "^$path/" <<<"$busy"; then
      rm_n=$((rm_n+1))
      if [ $APPLY = 1 ]; then "$WT" remove "$br" -y --foreground >/dev/null 2>&1 || echo "  WARN remove failed: $br"; else echo "  would remove: $br ($path)"; fi
    else keep="$keep  keep: $br [$st,$cl]\n"; fi
  done < "$tmpf"

  echo "$(basename "$repo"): pruned-missing=$np removed-integrated=$rm_n"
  if [ -n "$keep" ]; then printf "%b" "$keep"; fi
done
