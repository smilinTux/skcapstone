# Completion Evidence — Card 6ec30d39

- **Owner:** `pi-glm-builder-node-chiap02-6ec30d39`
- **Claim revision:** `08a112a7ea3f4ced9a00fe45a862b448`
- **Authorized base:** `70e3d6ca8a211c8eba8b7f5428c239488174e0b9`
- **Branch:** `fix/6ec30d39-historical-selection`
- **Repository:** `https://github.com/smilinTux/skcapstone.git`

## What was wrong

`_historical_acceptance` in `src/skcapstone/fleet/production_review_finish.py`
computed JUnit counts with `junit_counts(raw_junit, profile)` and passed no
selection, while the live admission path (`production_tests.py`) loads the
retained `selection.json`, checks it against the receipt's `selection_sha256`,
and passes it into `junit_counts`. Any accepted receipt whose profile recipe
requires exact pytest collection evidence (non-`.py` targets or a `deselect`
baseline) therefore failed historical revalidation after a runtime upgrade.

## What changed

The fix preserves and validates the exact stored selection from the accepted
receipt's own run directory — it never recalculates or authors selection
metadata:

- When the historical profile requires selection, the retained
  `directory/selection.json` is read via `read_private`.
- Its bytes are verified against the retained receipt's `selection_sha256`
  (`historical pytest selection changed` on mismatch).
- The stored selection is passed to `junit_counts(raw_junit, profile,
  selection=selection)` so `selected_junit_counts` validates baseline and
  selected identities exactly.
- No mutation bypass, policy relaxation, or test exclusion was added.

Tests extend the `accepted` fixture with a `deselect`-based selected-test
profile and stored `selection.json`, prove a valid stored receipt passes
historical acceptance with its exact selection, and prove missing or
changed selection evidence is rejected.

## Provenance note

This card's fix was already authored and merged upstream as PR #965
(commit `4ad4772`, changelog `fix-6ec30d39-historical-selection.md`) before
this re-dispatch at the same authorized base `70e3d6ca`. The base blobs of
both touched source files are byte-identical to `4ad4772~1`, so this
candidate reapplies that exact reviewed fix at the authorized base via
`git cherry-pick 4ad4772` (original author preserved; committer is this
worker) and adds this evidence file. The card's linked commit
`3b7470c83e68ae10c7d0f7c54a3ba7c18ef4bc6d` was the pre-rebase branch commit
of that merged PR and does not exist in this clone.

## Commands run and results

- `git cherry-pick 4ad4772` — applied cleanly at base `70e3d6ca`
  (3 files: `src/skcapstone/fleet/production_review_finish.py`,
  `tests/fleet/test_production_historical_acceptance.py`,
  `changelog.d/fix-6ec30d39-historical-selection.md`).
- `python -m pytest tests/fleet/test_production_historical_acceptance.py -q`
  — **93 passed** in 6.78s (includes the new
  `test_historical_selection_is_required_and_bound` cases:
  missing / changed-selection / changed-hash are all rejected).
- `python -m pytest tests/fleet/test_production_tests.py -q`
  — **49 passed** in 1.79s.
- `python -m ruff check src/skcapstone/fleet/production_review_finish.py tests/fleet/test_production_historical_acceptance.py`
  — All checks passed.
- `python -m black --check` (same two files) — 2 files would be left unchanged.
- `git diff --check` — clean.

## Limitations

- `python -m pytest tests/fleet/test_production_review_finish.py -q` fails
  **2 failed, 12 errors** identically at the pristine authorized base
  `70e3d6ca` without this change (fixture-level
  `SourceBundleError: source candidate export refused` /
  `test_native_state_reuses_only_current_locked_store` environment failures),
  so they are pre-existing and unrelated; they are not from this candidate.
- Upstream `main` has since refactored this code region (post-`4ad4772`),
  so the required PR from this authorized base may not auto-merge cleanly
  against current `main`. The PR is created with auto-merge enabled exactly
  as the card directs; the merge outcome belongs to the native gates.
- No full repository suite was run (not required by this card's focused
  TDD). No host installation, service action, deployment, or CardStore
  mutation was performed; all board writes use `skcapstone coord`.

## PR

One PR created from `fix/6ec30d39-historical-selection` targeting `main`
with auto-merge enabled (URL recorded via `skcapstone coord link`).
