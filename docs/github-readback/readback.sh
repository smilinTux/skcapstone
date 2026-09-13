#!/usr/bin/env bash
# Read-only GitHub control-plane readback. Requires gh and jq; never calls mutating endpoints.
set -uo pipefail
OWNER=${OWNER:-smilinTux}
OUT=${OUT:-github-readback-$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
repos=(skcapstone skdashboard skworld sk-standards)
api() { local name=$1 endpoint=$2; gh api "$endpoint" >"$OUT/$name.json" 2>"$OUT/$name.error" || { printf '{"read_failed":true,"endpoint":%s,"error":%s}\n' "$(jq -Rn --arg x "$endpoint" '$x')" "$(jq -Rs . <"$OUT/$name.error")" >"$OUT/$name.json"; }; }
for repo in "${repos[@]}"; do
  base="repos/$OWNER/$repo"
  api "$repo-repository" "$base"
  default=$(jq -r '.default_branch // "main"' "$OUT/$repo-repository.json")
  api "$repo-protection" "$base/branches/$default/protection"
  api "$repo-rulesets" "$base/rulesets?includes_parents=true"
  api "$repo-environments" "$base/environments"
  api "$repo-deployments" "$base/deployments?per_page=100"
  api "$repo-workflows" "$base/actions/workflows"
  jq -r '.workflows[]?.path' "$OUT/$repo-workflows.json" | while read -r path; do
    [ -z "$path" ] && continue
    safe=$(printf '%s' "$path" | tr '/ ' '__')
    api "$repo-workflow-$safe" "$base/contents/$path?ref=$default"
    jq -r '.content // empty' "$OUT/$repo-workflow-$safe.json" | tr -d '\n' | base64 -d >"$OUT/$repo-workflow-$safe.yml" 2>/dev/null || true
    jq -n --arg path "$path" --arg file "$OUT/$repo-workflow-$safe.yml" '{path:$path,concurrency_keys:([inputs|select(type=="string")|capture("(?m)^concurrency:\\s*(?<v>.*)$").v] // [])}' 2>/dev/null <"$OUT/$repo-workflow-$safe.yml" >"$OUT/$repo-workflow-$safe-concurrency.json" || true
  done
done
# GraphQL is read-only; this query documents whether the account schema exposes mergeQueue.
gh api graphql -f query='query($o:String!,$n:String!){repository(owner:$o,name:$n){name mergeQueue{groupingStrategy maxEntriesToMerge mergeMethod} }}' -f o="$OWNER" -f n=skcapstone >"$OUT/merge-queue-graphql.json" 2>"$OUT/merge-queue-graphql.error" || true
sha256sum "$OUT"/* >"$OUT/SHA256SUMS"
printf 'Readback written to %s (owner=%s)\n' "$OUT" "$OWNER"
