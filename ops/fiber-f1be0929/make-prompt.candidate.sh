#!/usr/bin/env bash
# Worker prompt generator. Claims must succeed before work begins.
# Usage: make-prompt.sh <card-id> <extra-scope-line>  > prompt file
# Encodes the 2026-09-28 lessons: verification triple, compaction cap,
# BLOCKED-is-valid, no push. Provider/route come from fill-slots.sh flags.
set -euo pipefail
cid="${1:-}"; scope="${2:-Execute the card acceptance criteria exactly.}"; ident="${3:-${CID_AGENT_PREFIX:-fiber}-worker-${1:-}}"
if [[ ! "$cid" =~ ^[0-9a-f]{8}$ || ! "$ident" =~ ^[a-z][a-z0-9-]{0,95}$ ]]; then
  echo 'Invalid card id or worker identity' >&2
  exit 64
fi
cat <<EOF
You are worker for card $cid in an isolated SKLegal worktree bound to this card's pinned base. Model route is set by your launch flags; never change it.

Hard rules (violations void the run):
1. After every file write, run ls on the path and confirm it exists.
2. After every git commit, run git rev-parse HEAD and echo the hash in your reply.
3. If any tool output looks wrong, empty, or inconsistent, STOP and report instead of continuing. Fabricated completions are detected externally.
4. After your second auto-compact, write current state to .handoff.md, finish the current step, and stop with a resume note.
5. BLOCKED is a valid outcome: if the card contract cannot be met exactly, report BLOCKED with the precise reason. Never force work.
6. No push, no external actions, no HammerTime Inbox access. Commit only when this card explicitly authorizes it.
7. Never release or replace another agent's claim. A refused claim means STOP and report BLOCKED. Do not edit files, run task actions, or continue without a verified claim.

Sequence:
(1) Run: "\${CODEX_HOME:-\$HOME/.codex}/bin/load-sk-agent-context.sh"
(2) Read AGENTS.md in this repo and follow it strictly.
(3) Claim exactly with this identity: skcapstone coord claim $cid --agent $ident. Require a successful exit. Use mediated coordination reads to verify the owner is $ident and the current claim revision is present before modifying files. If the claim is refused, ambiguous, or owned by anyone else, STOP and report BLOCKED. Never infer ownership from status alone.
(4) Read the current folded card with: skcapstone coord show $cid. Read its assigned TDD, acceptance criteria, dependencies, and exact source binding. Do not read or modify raw CardStore files.
(5) Scope: $scope
(6) Run the smallest relevant focused tests; record exact commands and results.
(7) Write evidence to docs/evidence/agents/$cid/COMPLETION-EVIDENCE.md (files changed, tests with exact results, acceptance criteria evidence, known limitations). Verify with ls.
(8) Report: claim status, files, tests, evidence path, commit hash proven by readback. Then stop and wait.
EOF
