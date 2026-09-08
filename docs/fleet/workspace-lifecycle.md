# Fleet and RSI workspace lifecycle

Every agent workspace is owned by one exact card and claim generation. An
agent must preserve recoverable work before cleanup can become eligible.
Agents never delete their own workspace as part of completion.

## States

`ACTIVE` enters `HANDOFF_REQUIRED` after successful work. If independent
review is required, it enters `REVIEW_PENDING`. It may enter
`CLEANUP_ELIGIBLE` only after custody, completion evidence, review or handoff,
process absence, and recovery instructions are proved. Repository-native
cleanup then records `CLEANED` or `CLEANUP_FAILED`. Execution requires an
immutable receipt path. A retry reads the exact proof-bound `CLEANED` receipt,
while partial failures report whether the path and Git registration remain.

Failure, timeout, blockage, interruption, stale claim, unknown ownership,
dirty bytes, unique commits, or incomplete evidence enters
`RECOVERABLE_QUARANTINE`. The quarantine locator and recovery manifest remain
available to subsequent agents. Recovery may return the workspace to
`HANDOFF_REQUIRED`; it never skips preservation or review.

## Preservation gate

The manifest pins the card, claim revision, absolute workspace, branch, HEAD,
porcelain hash, dirty and untracked counts, commit, bundle, or artifact
custody, completion evidence, review dependency and status, and recovery
instructions. Cleanup does not trust reachability or process assertions. It
independently reads Git worktree identity and status, hashes custody and
review files, proves that preservation contains the exact HEAD and workspace
state, and inspects live process working directories.

Cleanup fails closed unless all gates pass. An eligible Git worktree is
removed only with `git worktree remove -- <exact-resolved-path>`. Broad
recursive deletion, path globs, unresolved environment variables, and cleanup
of a live or foreign workspace are prohibited. The default API is plan-only;
execution requires a separate authorized caller and records bytes reclaimed
and recovery status.

## Agent and RSI protocol

1. Start in `ACTIVE` with exact card and claim identity.
2. Preserve source and evidence in reachable commit, bundle, or artifact
   custody, then publish hash-pinned recovery instructions.
3. Successful work enters handoff and required independent review. Failed or
   interrupted work enters recoverable quarantine.
4. A distinct cleanup executor may act only on a fresh
   `CLEANUP_ELIGIBLE` decision and an exact resolved path.
5. RSI retrospectives treat safe cleanup and recoverability as quality gates.
   They do not infer success from process exit or remove failed work.

Operators use the lifecycle decision reasons to distinguish pending handoff,
review, dirty bytes, live processes, missing custody, and cleanup failure.
Reapers may release a provably dead claim, but must classify its workspace as
recoverable quarantine and must not delete it.
