#!/usr/bin/env bash
# Is this PR actually mergeable, by REQUIRED CONTEXT rather than by count?
#
# Why this exists. On 2026-09-19 `docs / docs-check` stopped publishing any
# check run at all: a reusable-workflow ref was pinned to a squash-merged
# PR's head commit, which became reachable from nothing, so the workflow
# failed BEFORE creating a job. A workflow that dies at startup produces no
# check run, so the gate did not go red -- it went ABSENT. `gh pr checks`
# simply stopped listing it. Every merge to main for 43 minutes ran with no
# docs enforcement, tier 3 included, and two separate readers (a human and
# an agent) both read "10 checks, 0 failing" as healthy.
#
# Counting checks cannot catch that. The only question that can is: for each
# context branch protection REQUIRES, is it present, and is it green?
# Absence is the dangerous state and is reported as its own failure.
#
# Usage:
#   scripts/ci/required-checks.sh <pr-number> [--repo owner/name] [--branch main]
#
# Exit 0 only when every required context is present AND successful.
#
# THIS IS A GATE, NOT A WAITER. Read it immediately before merging, when the
# workflows have had time to register. Do not use ABSENT as a loop-breaking
# condition straight after a push: GitHub takes tens of seconds to create
# check runs, so a freshly pushed head legitimately reports ABSENT for
# contexts whose workflows simply have not started yet. Measured on PR #817:
# ABSENT for both unit-test contexts seconds after a push, then IN_PROGRESS
# for the same two a minute later.
#
# The distinction the script cannot make for you is WHY a context is absent:
# "not created yet" and "never going to be created" look identical in the
# API. Only elapsed time separates them. So when polling, wait for every
# required context to reach a terminal state, and treat a context that is
# STILL absent after the other workflows have finished as the real thing.
set -euo pipefail

PR=""
REPO="${GH_REPO:-smilinTux/skcapstone}"
BRANCH="main"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *)        PR="$1"; shift ;;
  esac
done

if [ -z "$PR" ]; then
  echo "usage: $(basename "$0") <pr-number> [--repo owner/name] [--branch main]" >&2
  exit 2
fi

# Read the required list from branch protection, never from a copy kept here.
# A second hand-maintained list is the same drift this repo keeps finding, and
# it would go stale the moment someone adds a required check.
if ! required_json=$(gh api "repos/$REPO/branches/$BRANCH/protection" \
      --jq '.required_status_checks.contexts' 2>/dev/null); then
  echo "FAIL: could not read branch protection for $REPO@$BRANCH." >&2
  echo "      Refusing to guess the required list: an assumed list is exactly" >&2
  echo "      the failure this script exists to catch. Needs a token with" >&2
  echo "      repo admin read." >&2
  exit 2
fi

checks_json=$(gh pr checks "$PR" --repo "$REPO" --json name,state 2>/dev/null || echo '[]')

# One pass, in jq, so the reported numbers and the exit code cannot disagree.
report=$(jq -n \
  --argjson required "$required_json" \
  --argjson checks "$checks_json" '
  ($checks | map({(.name): .state}) | add // {}) as $by_name
  | $required
  | map({
      context: .,
      state: ($by_name[.] // "ABSENT")
    })
  | {
      rows: .,
      total: length,
      green: (map(select(.state == "SUCCESS")) | length),
      absent: (map(select(.state == "ABSENT")) | length),
      pending: (map(select(.state == "IN_PROGRESS" or .state == "QUEUED" or .state == "PENDING")) | length)
    }')

echo "$report" | jq -r '
  "required-context coverage: \(.green)/\(.total) green" +
  (if .absent  > 0 then ", \(.absent) ABSENT"  else "" end) +
  (if .pending > 0 then ", \(.pending) pending" else "" end),
  (.rows[] | "  " + (
     if   .state == "SUCCESS" then "ok      "
     elif .state == "ABSENT"  then "ABSENT  "
     else "not-ok  " end) + .context + "  [" + .state + "]")'

absent=$(echo "$report" | jq -r '.absent')
green=$(echo "$report"  | jq -r '.green')
total=$(echo "$report"  | jq -r '.total')

if [ "$absent" -gt 0 ]; then
  echo
  echo "FAIL: $absent required context(s) published nothing at all."
  echo "      This is NOT the same as pending. A context that never arrives"
  echo "      blocks the merge forever and looks identical to a check that was"
  echo "      never configured. Check whether its workflow failed at startup:"
  echo "      gh run list --branch <head> --json workflowName,conclusion"
  exit 1
fi

[ "$green" = "$total" ]
