#!/usr/bin/env bash
# Grep the repo's PROSE for a pattern, excluding SOP.md's own docs-evidence block.
#
# Tier-3 assertions that forbid a stale number live inside that block, so a naive
# `grep -rn` over the tree matches the assertion's own text and the check fails on
# itself. Stripping the block is the whole job.
#
# Usage:   scripts/docs/prose_grep.sh <extended-regex> [path ...]
# Exit 0 = at least one prose match (i.e. the forbidden text IS present).
# Exit 1 = no match. A "no doc still claims X" assertion wants exit 1, so it
#          negates this with `!`.
set -uo pipefail
pat=$1; shift
paths=("$@"); [ ${#paths[@]} -eq 0 ] && paths=(docs/ README.md)
found=1
for p in "${paths[@]}"; do
  while IFS= read -r f; do
    [ "$f" = "SOP.md" ] \
      && body=$(sed '/<!-- docs-evidence/,/^-->/d' "$f") \
      || body=$(cat "$f")
    if printf '%s\n' "$body" | grep -nE "$pat" | sed "s|^|$f:|"; then found=0; fi
  done < <(find "$p" -type f \( -name '*.md' -o -name '*.json' \) 2>/dev/null)
done
# SOP.md is passed by name, not found by the directory walk above.
exit $found
