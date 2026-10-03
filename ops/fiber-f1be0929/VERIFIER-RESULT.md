# External verifier refusal repair

Card f1be0929, jarvis, 2026-09-29 22:12 UTC.

## Observed defect and change

The old verify-card.sh parsed a full rendered board with grep/awk. A failed
or unrecognized board read printed WARN and did not change the exit code.
It also accepted an empty evidence file or an evidence-only BLOCKED claim.
The isolated regression run reproduced five failing assertions across three
tests against that old script. No live card was mutated.

The replacement reuses native Git, the existing jq installation, and the
mediated single-card coord show --json CLI. It fails on an unavailable,
malformed, mismatched, or unfinished board state. It resolves and pins the
base/head commits, requires candidate ancestry, and requires nonempty regular
evidence whose resolved path remains inside the exact worktree root.

Successful artifact verification requires review or done state and candidate
commits beyond the base. A BLOCKED exception requires both the board verdict
and evidence to record BLOCKED, and explicitly prints "not successful
completion". This preserves a truthful blocked outcome without confusing it
with a completed implementation. Artifact verification does not replace the
independent review, CI, task-criteria, or overall rollout acceptance gates.

## Test evidence

- Final candidate: 4 tests passed locally in 0.653 seconds.
- chiap04: 4 passed in 0.433 seconds, bounded native unit
  f1be0929-verifier-tests-r2-X6sJff.service, invocation
  dc9b337e6d7745768574ebcdc9d8d564, exit 0, runtime 489 ms.
- Installed live script, tested using only scratch repositories and a stubbed
  board CLI: 4 passed in 0.996 seconds.
- Tests cover review/done success, doing/backlog refusal, wrong card, invalid
  board JSON shape, CLI failure, nonexistent base, unrelated history, empty
  evidence, escaping symlink, and unrecorded versus recorded BLOCKED outcomes.
- An initial harness inherited BASH_ENV that changed its stub PATH. Only that
  task-owned test process was stopped, then the test harness was isolated and
  rerun. It made read-only board requests, not card writes. No preexisting
  worker or service was stopped.

## Installed file and rollback

Live: ~/.skcapstone/runtime/llm-orch/verify-card.sh
New SHA256: 7b6e958aa3a6db0eb22319b9757db85e7cb759a7992689001d1a221bc246ffdd

Backup: backup/verify-card.sh beside this report.
Original SHA256: 1c6d1e4ff0928d4f5d7d684a5ebb4ade9ce3aa4d73a1cad804f36b935dac9cfd
The backup and original hashes matched before replacement.

No automatic cleanup or card transition was added. A rollback may restore
only this exact backup after checking that the live script still matches the
new hash. Restoring the old verifier also restores its false-positive behavior;
it must not be used as a successful acceptance gate without independent checks.

The held canary TDD now explicitly requires moving the producer card to review
after its candidate-bound provisional outcome, not marking it done. The request
fingerprint was regenerated outside the live queue. Card 4ce90fdd remains
unowned, backlog, do-not-claim. The dispatcher still returns admission-held
and its timer is disabled/inactive. This repair is not a completed live canary.
