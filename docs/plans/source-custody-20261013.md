# Shared source custody implementation plan

Goal: use one locked event snapshot to distinguish HELD, RELEASED_CLEAN and CHANGED, preserving existing admission and acceptance policies and opening reviews for cleanly released producers without impersonation.

1. Add `fleet/source_custody.py`: typed result, cached native and legacy event snapshot under the card lock, latest custody proof, candidate and outcome binding validator, exact pre-release read-only view. Table tests cover held, release, new claim, assign, completion, void, content and outcome drift.
2. Migrate admission, review finish, guarded link/completion helpers, acceptance reseal and older released-source reconciliation. Keep caller-specific held status sets and artifact checks. Preserve existing CLI interfaces and journal formats. Run existing custody, finish, guarded acceptance, reseal, and released-source tests.
3. Extend guarded `coord review-work` using the same release proof. Preserve the pre-release generation and producer claim without writing source custody. Keep source revision fencing, candidate bytes validation, replacement lineage checks, native review creation and idempotence. Test clean release, repeated call, later custody/content/outcome refusal, and unchanged live-owner path.
4. Run Ruff and Black on changed Python, add changelog, open a separate non-draft PR. Do not merge or deploy the refactor.

Native writes remain through coord only. The snapshot/view never writes CardStore files. No second PASS or review completion is introduced. Scope excludes scheduler, runtime deployment, and the separate identical-projection hotfix.
