#!/usr/bin/env bash
# Verify reported artifacts and board state, not correctness or review approval.
# A verified BLOCKED outcome is explicitly not successful task completion.
set -euo pipefail
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }
[[ $# -ge 2 && $# -le 3 ]] || fail 'usage: verify-card.sh <card-id> <worktree> [base]'
cid=$1
[[ "$cid" =~ ^[0-9a-f]{8}$ ]] || fail 'invalid card id'
wt=$(realpath -e -- "$2") || fail 'worktree does not exist'
top=$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null) || fail 'not a worktree'
[[ "$top" == "$wt" ]] || fail 'expected worktree root'
base=${3:-origin/main}
head=$(git -C "$wt" rev-parse --verify 'HEAD^{commit}' 2>/dev/null) || fail 'HEAD is not a commit'
base_commit=$(git -C "$wt" rev-parse --verify --end-of-options "$base^{commit}" 2>/dev/null) || fail 'base is not a commit'
git -C "$wt" merge-base --is-ancestor "$base_commit" "$head" || fail 'candidate does not descend from base'
commits=$(git -C "$wt" rev-list --count "$base_commit..$head") || fail 'commit comparison failed'

evidence="$wt/docs/evidence/agents/$cid/COMPLETION-EVIDENCE.md"
[[ -s "$evidence" && -f "$evidence" && ! -L "$evidence" ]] || fail 'nonempty regular evidence file required'
resolved=$(realpath -e -- "$evidence") || fail 'evidence cannot be resolved'
[[ "$resolved" == "$wt/"* ]] || fail 'evidence escapes worktree'

board=$(skcapstone coord show "$cid" --json 2>/dev/null) || fail 'board read failed'
jq -e --arg cid "$cid" 'type == "object" and .id == $cid and (.status | type == "string")' \
    <<< "$board" >/dev/null 2>&1 || fail 'board identity or state missing'
status=$(jq -r '.status' <<< "$board")
verdict=$(jq -r '.links.verdict // ""' <<< "$board")
if [[ "$verdict" =~ ^BLOCKED($|[[:space:]]) ]] && grep -qw BLOCKED "$evidence"; then
    printf 'VERIFIED BLOCKED: board=%s; not successful completion\n' "$status"
    exit 0
fi
[[ "$status" == review || "$status" == done ]] || fail 'board is not review or done'
[[ "$commits" -gt 0 ]] || fail 'no candidate commits beyond base'
printf 'PASS artifacts: head=%s commits=%s board=%s; independent review still required\n' "$head" "$commits" "$status"
