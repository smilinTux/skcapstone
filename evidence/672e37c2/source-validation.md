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
