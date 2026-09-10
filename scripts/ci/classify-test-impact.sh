#!/usr/bin/env bash
set -euo pipefail

base="${1:?usage: classify-test-impact.sh BASE HEAD}"
head="${2:?usage: classify-test-impact.sh BASE HEAD}"
git cat-file -e "$base^{commit}"
git cat-file -e "$head^{commit}"

mapfile -t changed < <(git diff --name-only --diff-filter=ACDMRTUXB "$base" "$head")
docs_only=true
if ((${#changed[@]} == 0)); then
  docs_only=false
fi
for path in "${changed[@]}"; do
  case "$path" in
    docs/*) ;;
    *.md)
      if [[ "$path" == */* ]]; then
        docs_only=false
      fi
      ;;
    *) docs_only=false ;;
  esac
done
if [[ "${DOCS_ONLY_ALLOWED:-true}" != true ]]; then
  docs_only=false
fi

printf 'comparison_base=%s\ncomparison_head=%s\nchanged_count=%d\n' \
  "$base" "$head" "${#changed[@]}"
printf 'changed_path=%s\n' "${changed[@]}"
printf 'docs_only=%s\n' "$docs_only"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'docs_only=%s\n' "$docs_only" >> "$GITHUB_OUTPUT"
fi
