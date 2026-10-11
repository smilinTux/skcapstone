# SKCapstone fleet and RSI workspace lifecycle — R2 completion evidence

Card: `8c4a9e21`
Title: `[SKFLEET-RSI-WORKSPACE-LIFECYCLE-R1][M] Preserve handoff state and clean completed agent workspaces`
Producer: `pi-deepseek-builder-node-chiap03-8c4a9e21`
Claim revision: `fd871095e29641ca8dcccb74ef6b1471`
Feature branch: `skcapstone:card/8c4a9e21-workspace-lifecycle-r2`
Base revision: `8c8a2a2284ae8f9bdcd3e6562ac65de87ca0376d`
Prior independent review: `fb470f9f` verdict `FAIL` (findings below)

## Why this card reopened

Independent review card `fb470f9f` failed the first integration with exactly
four findings, all of which this change confronts directly rather than
arguing around:

| Finding | Resolution in R2 |
| --- | --- |
| `interruption_quarantine` | The wrapper's `SIGTERM`/`SIGINT` path records the signal and the `finally` block writes a hash-pinned `RECOVERABLE_QUARANTINE` manifest with the exact `quarantine_locator` when no workspace manifest exists. |
| `unverified_cleanup_proof` | `cleanup_decision` now derives, rather than trusts, the live claim generation, completion evidence, unique-commit custody, and the review dependency from the card and Git. |
| `unique_commit_custody` | New `unique_commit_custody` proves the workspace HEAD is an ancestor of the card's own custody `commit_sha`, or that no unique commit exists. |
| `cleanup_retry_partial_failure` | `cleanup_worktree` reports actual surviving bytes (`bytes_remaining`), a deterministic already-removed retry, and verifies the post-command state instead of trusting the command's word. |

## Acceptance criteria coverage

- **AC1 states and transitions.** `WorkspaceState` now includes
  `REVIEW_PENDING`; `TRANSITIONS` encodes deterministic edges, including
  `HANDOFF_REQUIRED -> REVIEW_PENDING -> CLEANUP_ELIGIBLE` and
  `RECOVERABLE_QUARANTINE -> HANDOFF_REQUIRED`.
- **AC2 pre-cleanup proofs.** Before `CLEANUP_ELIGIBLE`, `cleanup_decision`
  independently proves the exact card, live claim generation
  (`card_claim_generation`), branch and HEAD identity, byte-exact porcelain
  hash, dirty/untracked counts, commit/branch custody (`card_custody`),
  unique-commit custody (`unique_commit_custody`), completion evidence
  (`card_completion_evidence`), the review dependency
  (`card_review_dependency`), no live processes inside the workspace, and
  non-empty recovery instructions.
- **AC3 quarantine and recovery manifest.** `recovery_manifest` emits schema
  `skcapstone.workspace-recovery.v3` with `reasons`, `recovery_instructions`,
  and a hash-pinned `quarantine_locator` for quarantine, handoff, and review
  states; the locator is inside the hashed content.
- **AC4 exact-path cleanup and fail-closed.** `cleanup_worktree` remains
  plan-only by default, uses only `git worktree remove -- <exact-path>`,
  reports `bytes_reclaimed` and `bytes_remaining`, and refuses any state that
  is not `CLEANUP_ELIGIBLE`.
- **AC5 integration and tests.** Success, failure, timeout, reviewer handoff,
  stale/released claim, dirty/untracked bytes, unique commit without custody,
  active process, idempotent retry, and partial failure are covered in
  `tests/test_workspace_lifecycle.py` and
  `tests/test_skfleet_worker_exit_evidence.py`.
- **AC6 evidence and distinct review card.** This document, plus the producer
  handoff records `PASS_FOR_REVIEW` for a distinct reviewer; no review card is
  self-issued by this producer.

## Files and SHA256

- `src/skcapstone/fleet/workspace_lifecycle.py`: `7be57223d498223d33f485e3d6a2386693d39cba7db47a5659c2646d13199e6f`
- `scripts/fleet/skfleet-worker-wrapper.py`: `c35899e4a4b6760ff0176f3d5daa96b7ceb87af8b66f46964d1f70d34367be6a`
- `tests/test_workspace_lifecycle.py`: `4506be21ab212c3bd70912fc88fc47eb7842ffe1cba43ea2a22f78e6cff24f9f`
- `tests/test_skfleet_worker_exit_evidence.py`: `184673d85fbd347699ea95a4e16fa7282c30b2ff6c166306058e496119981073`
- `docs/fleet/workspace-lifecycle.md`: `62af158d891783dc968f76bccaeed557f0dde907dc3111d0b7ad9d5f52068063`

## Verification

The card's own `test_profile_recipe` selects one test file each from
`tests/test_workspace_lifecycle.py` and
`tests/test_skfleet_worker_exit_evidence.py`, with two baseline-excluded node
IDs. Those two baseline node IDs — `test_detached_beat_cannot_strand_wrapper_after_immediate_child_exit`
and `test_wrapper_exit_path_calls_workspace_lifecycle_decision_end_to_end` —
fail identically on the untouched base because this workspace has no `skmail`
mailbox (`poll_mail` returns `skmail_exit_1`); they are unrelated to this
change and are explicitly deselected by the card's approved baseline.

Command:

```text
python3 -m pytest -q tests/test_workspace_lifecycle.py tests/test_skfleet_worker_exit_evidence.py \
  --deselect "tests/test_skfleet_worker_exit_evidence.py::test_detached_beat_cannot_strand_wrapper_after_immediate_child_exit" \
  --deselect "tests/test_skfleet_worker_exit_evidence.py::test_wrapper_exit_path_calls_workspace_lifecycle_decision_end_to_end"
```

Result: `61 passed, 2 deselected`.

Command:

```text
python3 -m ruff check src/skcapstone/fleet/workspace_lifecycle.py \
  scripts/fleet/skfleet-worker-wrapper.py \
  tests/test_workspace_lifecycle.py tests/test_skfleet_worker_exit_evidence.py
```

Result: `All checks passed!`.

Command:

```text
python3 -m ruff format --check src/skcapstone/fleet/workspace_lifecycle.py \
  scripts/fleet/skfleet-worker-wrapper.py \
  tests/test_workspace_lifecycle.py tests/test_skfleet_worker_exit_evidence.py
```

Result: `4 files already formatted`.

Python compile checks passed for the changed executable paths
(`python -m py_compile src/skcapstone/fleet/workspace_lifecycle.py scripts/fleet/skfleet-worker-wrapper.py`).

## Live decision against this very card

Running the shipped `cleanup_decision` against this worker's own workspace and
this card proves the governed path end to end and shows the review gate is
not decorative:

```text
custody:  (True, 'commit_sha-and-branch-linked')
claim:    (True, 'claim-generation-current')
evidence: (True, 'evidence-linked')
review:   ('pending', 'review-verdict-not-terminal:fb470f9f')
unique:   (True, 'head-reachable-from-custody-commit')
decision: LifecycleDecision(state=WorkspaceState.REVIEW_PENDING,
          reasons=('review-pending:review-verdict-not-terminal:fb470f9f',))
```

Every derived proof except review passes, so the only reason this workspace is
not `CLEANUP_ELIGIBLE` is that the review dependency has not recorded a
canonical terminal verdict. That is the intended behaviour: the lifecycle
refuses to authorise deletion while a review is open, and it is exactly why a
distinct review card is required for R2 rather than self-completion.

## Limitations

- Source-only: this change does not install, activate, or deploy the
  lifecycle, and performs no live cleanup.
- Cleanup execution remains behind an explicit `execute=True` caller
  boundary with no production caller; building that authorized pass is out of
  scope and is recorded as intentionally unwired in the module and docs.
- The two baseline-excluded end-to-end tests fail on the untouched base for
  the missing-`skmail` environment reason above, not because of this change.

## Rollback

Before integration, rollback is reversal of the five tracked file hunks in
this branch. No runtime, service, data, credential, or deployment change was
made, so no operational rollback is required.
