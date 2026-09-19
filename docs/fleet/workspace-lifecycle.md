# Fleet workspace lifecycle

Ported and reworked from stranded commit a45aed76 on
`origin/fix/68a14a4f-seat-model-repoint` (the chiap08 salvage). That commit
predates Plan B1 (commit 621c358d, "PR policy: push a branch by default, PR
only for sensitive cards") by 235 commits: it assumed review was mandatory
by default and proved custody through a caller-supplied bundle or commit-ref
locator that every real caller left at `None`, so nothing was ever actually
verified. This document, and the module it describes
(`src/skcapstone/fleet/workspace_lifecycle.py`), speak Plan B1's vocabulary
instead.

## Why this exists

A fleet worker's output lives only in its own workspace until it reaches
somewhere durable. Plan B1's evidence gate
(`skcapstone.coord_completion._commit_evidence_problem`) only inspects a card
at `coord complete` or `coord move done`. A worker that crashes, is reaped,
or has its workspace cleaned up *before* reaching completion leaves nothing
behind, and no gate ever notices, because no gate ever runs. This module
covers exactly that window: before any workspace is deleted, prove the work
already reached somewhere that survives the deletion.

## What "custody" means now

Custody is not a locator a caller asserts. It is read straight off the
card's own links, the durable record Plan B1 introduced:

- `commit_sha`: a genuine 40-character hex commit SHA, or the literal
  sentinel `none` recording that the card needed no repository change.
- `branch`, repo-qualified as `<repo>:<name>`: required alongside a genuine
  SHA, because it is the only part of the record that says what to fetch. Not
  required alongside the `none` sentinel, because there is no code to fetch.

`skcapstone.fleet.workspace_lifecycle.card_custody(home, card_id)` reads
these two links through `CardStore(home).fold(card_id)` and reuses
`skcapstone.coord_completion.commit_sha_is_valid`, the one shared shape
check, rather than a second copy. Nothing about custody is trusted from a
caller-constructed `WorkspaceProof`; a proof carries no custody field at all.

Review is not part of this at all. Plan B1 made a PR opt-in (required only
for a sensitive card, decided by `scripts/fleet/skfleet-rotate.py`'s
`pr_required()`), not a default every proof had to carry. Whether a card
needed a PR is a publishing decision about the pushed branch, unrelated to
whether a workspace is safe to delete, so `review_required` does not exist
in this module.

## States

`ACTIVE` enters `HANDOFF_REQUIRED` after a terminal exit (success, failure,
timeout, blocked, or interrupted all land here; only a clean, fully
identified success proof avoids `RECOVERABLE_QUARANTINE` instead). It may
enter `CLEANUP_ELIGIBLE` only after `cleanup_decision` independently proves:
Git worktree identity, a clean and matching porcelain state, no live
processes inside the workspace, non-empty recovery instructions, and
`card_custody` for the card. Repository-native cleanup then records `CLEANED`
or `CLEANUP_FAILED`. Execution requires an immutable receipt path. A retry
reads the exact proof-bound `CLEANED` receipt; a partial failure reports
whether the path and Git registration remain.

Any failure, timeout, missing custody, dirty or untracked bytes, live
process, or workspace identity mismatch keeps a workspace in
`RECOVERABLE_QUARANTINE` or `HANDOFF_REQUIRED`. It never skips straight to
cleanup.

## Cleanup is fail closed

`cleanup_decision` requires a coordination `home` (to read `card_custody`)
and a `repository` (to independently verify Git identity and state); without
`repository` it always returns `HANDOFF_REQUIRED`. An eligible Git worktree
is removed only with `git worktree remove -- <exact-resolved-path>`. Broad
recursive deletion, path globs, unresolved environment variables, and
cleanup of a live or foreign workspace are prohibited. `cleanup_worktree`'s
default is plan-only; execution requires a separate authorized caller and
records bytes reclaimed and recovery status.

## What is actually wired up, and what is not

`cleanup_decision()` is called from
`scripts/fleet/skfleet-worker-wrapper.py`'s `record_workspace_lifecycle_decision`,
which every fleet worker's own exit path reaches (`main()`, right after
`record_terminal_exit`). It reads the worker's live Git state from its own
process `cwd` (the exact directory `skfleet-rotate.py`'s
`_worker_launch_command` binds as the worker's working directory) and the
card's current `commit_sha`/`branch` links, then writes a hash-pinned
manifest recording `CLEANUP_ELIGIBLE` or `HANDOFF_REQUIRED` and the reasons,
and mails a status line naming the decision. That is real production
traffic, not only a unit test: `tests/test_skfleet_worker_exit_evidence.py`
exercises it by running the actual wrapper subprocess end to end.

Execution (`cleanup_worktree(execute=True)`, the only call that removes a
workspace) has **no production caller**. This is deliberate, not an
oversight: agents must never delete their own workspace (see the worker
prompt in `scripts/fleet/skfleet-rotate.py`'s `_worker_done_instructions`,
item 8), and no separate authorized cleanup pass exists yet in this
codebase to make that call on a worker's behalf.
`src/skcapstone/fleet/workspace_runtime.py`'s `retire_workspace` (a
different, older concern: workspace materialization and admission, not
custody) has the same gap today, with zero production callers of its own.
Building that pass is out of this task's scope.
`# intentionally-unwired: workspace deletion execution. Reason: no
authorized cleanup pass exists yet; building one is future work, tracked
separately from this custody-verification port.`

## Agent and RSI protocol

1. Start in `ACTIVE` with exact card and claim identity.
2. Publish source under the exact commit SHA the workspace's own HEAD
   points to, push the branch, and record both as `commit_sha` and `branch`
   links on the card (`coord link <card> commit_sha <sha>` and
   `coord link <card> branch <repo>:<name>`), or link `commit_sha` to the
   literal `none` when the card needed no repository change.
3. Never delete your own workspace as part of finishing.
   `record_workspace_lifecycle_decision` records whether it is now cleanup
   eligible; it does not act on that record.
4. A distinct, future cleanup executor may act only on a fresh
   `CLEANUP_ELIGIBLE` decision and an exact resolved path.
5. Reapers may release a provably dead claim, but must not delete its
   workspace; that stays `RECOVERABLE_QUARANTINE` or `HANDOFF_REQUIRED`
   until custody and identity are proven the same way.

Operators use the lifecycle decision reasons (recorded in the mail line
`record_workspace_lifecycle_decision` sends, since `recovery_manifest`
itself does not carry them) to distinguish pending handoff, dirty bytes,
live processes, missing custody, and cleanup failure.
