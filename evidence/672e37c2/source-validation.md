# Cleanup candidate filtering

Parent9b230773 authorized this isolated source fix at base
`f46b4c920120c9f52a3a5dd5d8224db2c984076c`. Card672e37c2 is owned by
`codex-cleanup-672e37c2`. Source89508f83 and installed runtime are untouched.

Observed live PID2139455 at elapsed4:42, CPU97.8%, CPU time4:36. Root's
actions.log last recorded `REVIEW_BATCH_PLAN` before the pool. No intrusive
process inspection was performed. The base parent cleanup constructed a new
CardStore for every historical parent before checking outcome and lifecycle.
Each instance starts an empty legacy mutation cache, so its first fold reloads
all legacy overlay/archive records. Claim cleanup reread every card's event
files before checking whether its owner was a local review worker.

The change filters cached provisional outcomes and lifecycle before the fresh
source-only native fold. Lifecycle already uses the production shared read
store. Claim cleanup first rejects nonlocal owners through existing cached
`_current_claim`. Cached rejection can defer new work one cycle. Actual
candidates still use the original fresh claim reads, source-only fold, process
check, second fresh claim comparison, fresh native fold, review-completion
validation and exact expected-claim CLI mutation. No mutation fence changed.

Validation command:

```text
python -m pytest -q -p no:cacheprovider tests/test_skfleet_cleanup_prefilter.py tests/test_skfleet_review_claim_release.py tests/test_skfleet_review_closer_generation.py tests/fleet/test_production_acceptance_hooks.py
21 passed in 1.62s
```

Four new regressions cover 2,000 historical outcomes with zero lifecycle/native
folds, 2,000 closed provisional parents with no new native stores, 2,000
unowned/nonlocal cards with zero fresh claim reads, and a stale cached local
owner whose fresh claim belongs to someone else. Existing tests cover unchanged
legacy completion/release behavior, process liveness and changed-generation
refusal, gated/rejected completion, and production source-only exclusion.
Ruff passed for all three changed test files; dispatcher compilation and
`git diff --check` passed. An initial command included the trial-only
test_skfleet_terminal_review_skip.py absent from this exact base; no tests ran
in that invocation. The corrected focused command above passed.

Production latency is not measured or claimed fixed by these source tests.
Root retains deployment, rollback and real trial qualification. Rollback is the
exact prior dispatcher bytes; no data or service changes are included.

## Measured follow-up delta

The first installed fix still exhausted the unchanged Seraph cycle deadline.
Root authorized this same-card follow-up: move the uncached parent source-only
check after exact completed PASS/current-generation proof, immediately before
join/completion. Release now uses its existing final fresh fold for both role
and exact claim verification, after durable outcome and stopped-process proof.
All freshness/custody checks remain; the role check is later and thus fresher.

An AST-extracted private probe ran the exact cleanup functions and their read
helpers against the native board without importing the dispatcher. A Python
audit hook refused filesystem mutations and subprocess launches except exact
read-only systemctl/tmux queries; mutation commands returned refusal. Actual
delta runtime SHA256 was
`1473574b4b58a499e9ef0481df0f818c9c7a4c2ff781497439286b8766a8072e`.
Outcomes: 1.302s, 5514 entries. Review index: 0.140s, 1795 parents.
Parent cleanup: 1.836s, one shared native store, 732 shared folds, zero fresh
candidate stores. Release: 0.051s over 8004 cards, six fresh claim reads and
six durable-outcome checks, zero native folds. No mutation attempts or read
errors occurred. Full private receipt: card evidence READONLY-CLEANUP-TIMING.json.
The a4dd baseline probe was stopped by root direction while parent cleanup was
still running; no complete baseline timing or speedup ratio is claimed.

The same focused command now passes 26 tests in 1.71s. Five added cases cover
active provisional parents without a current completed review and unfinished
or still-running claimed reviews, all without fresh native store creation.
Production role-exclusion tests now reach the actual final mutation boundary.
Initial fixture failures were missing capture groups in the test regex; the
fixtures now preserve the production matcher contract. Ruff/black, dispatcher
compilation and diff checks pass. Live end-to-end launch remains root's gate.
