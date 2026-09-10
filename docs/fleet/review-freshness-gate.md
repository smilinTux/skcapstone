# Review freshness gate

Card `3c9aa03f` [FLEET-REVIEW-FRESHNESS-GATE-01]. Origin: PR427 closeout.
A PASS verdict is only as good as the head it ran against. PR427's green
checks ran on a stale base and were nearly accepted as current evidence.

## Rule

An independent review may return PASS only when both hold:

1. The reviewed head contains current `origin/main` of the base branch.
2. Every check is green on that exact head (completed, SUCCESS or NEUTRAL or
   SKIPPED). Pending or empty check lists are not green.

## Gate command

```bash
scripts/fleet/review-freshness-gate.py --repo smilinTux/skcapstone --pr 427
```

Prints one JSON verdict. Exit codes: 0 fresh and green, 1 behind or diverged
from current main, 2 checks not green on the head, 3 data error.

Review cards that gate a merge should require the reviewer to paste the
gate's JSON verdict, including head SHA and `contains_current_main`, into
the review evidence. No PASS on exit codes other than 0.

## Remediation when the gate fails

Exit 1: merge current `origin/main` into the PR branch, push, and let checks
run on the fresh head. Then rerun the gate before writing the verdict.
Exit 2: wait for pending checks or repair the failing ones. Never treat the
previous head's green checks as covering the new head.
