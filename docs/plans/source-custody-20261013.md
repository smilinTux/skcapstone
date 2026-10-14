# Shared source custody implementation plan

Goal: use one locked event snapshot to distinguish HELD, RELEASED_CLEAN and CHANGED, preserving existing admission and acceptance policies and opening reviews for cleanly released producers without impersonation.

1. Add `fleet/source_custody.py`: typed result, cached native and legacy event snapshot under the card lock, latest custody proof, candidate and outcome binding validator, exact pre-release read-only view. Table tests cover held, release, new claim, assign, completion, void, content and outcome drift.
2. Migrate admission, review finish, guarded link/completion helpers, acceptance reseal and older released-source reconciliation. Keep caller-specific held status sets and artifact checks. Preserve existing CLI interfaces and journal formats. Run existing custody, finish, guarded acceptance, reseal, and released-source tests.
3. Extend guarded `coord review-work` using the same release proof. Preserve the pre-release generation and producer claim without writing source custody. Keep source revision fencing, candidate bytes validation, replacement lineage checks, native review creation and idempotence. Test clean release, repeated call, later custody/content/outcome refusal, and unchanged live-owner path.
4. Run Ruff and Black on changed Python, add changelog, open a separate non-draft PR. Do not merge or deploy the refactor.

Native writes remain through coord only. The snapshot/view never writes CardStore files. No second PASS or review completion is introduced. Scope excludes scheduler, runtime deployment, and the separate identical-projection hotfix.

Implemented policy boundaries:

| Caller | Held policy | Released policy |
|---|---|---|
| Trusted test admission | Exact doing claim | Exact newest release, before publication content may be unavailable |
| Acceptance finish | Preserve the original held-owner predicate | Exact newest release plus sealed candidate and outcome binding |
| Guarded link and completion | Existing strict owner fence | Explicit binding, exact release proof and current or pre-release revision |
| Operator reseal | Existing ready/doing/review claim | Same released proof, operator corrections and full history preimages |
| Sealed producer recovery | Caller artifact and lifecycle policy | Shared custody proof plus exact sealed outcome and bundle checks |
| Review-work | Existing ready/doing/review producer | Typed candidate bytes intact, no source revision change after release, original generation and producer claim |

`EventSnapshot` reads each source ledger once while the card lock is held. Writers retain their enclosing lock until the operation is finished. The shared view removes only the proven release; the review opener additionally compares the snapshot at release to the current projection. Native writes use the ordinary store, never the read-only source view. After review reconciliation the live source revision is re-read to detect cross-host changes despite advisory locks.

The rebase retains main PR #1197's sealed direct-source manifest, launch receipt, route and terminal-process checks. The manifest binds the original producer claim; review metadata retains the pre-release source revision and generation. Unsealed releases cannot open reviews. No source claim, verdict, or completion is written by review-work.

CI follow-up: admission passes its already-folded card into the shared evaluator under its enclosing lock. This preserves failed-launch finalization's authoritative snapshot rather than silently substituting a second fold. The six failing CI cases are covered by `tests/fleet/test_failed_admission_terminal.py`.
