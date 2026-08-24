# SKCoord criteria fold uptake TDD

Date: 2026-08-24

Producer card: `db5ce143` (`SKCAP-CRIT-02`)

Independent review card: `a6c7fd2f` (`SKCAP-CRIT-02R`)

## Purpose

SKCapstone must require the first released SKCoord version that folds
`amend_criteria` events into the legacy coordination projection. The installed
SKCapstone CLI must therefore read the same current acceptance criteria through
the default CardStore lane and every supported legacy selector. Unknown,
unreadable, or malformed state must fail closed.

## Immutable upstream input

The only accepted upstream input is SKCoord `v0.1.39`:

- main and tag target commit:
  `e1551ce8463be80770777639fe91726961289167`
- source tree: `3d890c4c5347fb13d40c4b9ad6ce0215252a4c6e`
- wheel SHA-256:
  `28c01960a1bac630aecb4c4327c6f029031420844ba08ce91edf7b4775a3b288`
- source distribution SHA-256:
  `ead5494a1d28739f326a156912c87bcef20ad4ed4aca67d5377be0cc9542d275`
- release workflow: `https://github.com/smilinTux/skcoord/actions/runs/32749782963`

The release metadata, downloaded files, tag target, commit, tree, and hashes
must agree before the dependency file changes.

## Producer contract

Tests are added before the dependency edit. They must prove all of the
following:

1. `pyproject.toml` requires `skcoord>=0.1.39` and cannot select `0.1.38`.
2. A legacy task is seeded with nine birth criteria, then its CardStore receives
   a writer-attributed `amend_criteria` event containing nine different current
   criteria. The original task file and CardStore `core.json` remain unchanged.
3. The installed CLI reports the same ordered current criteria for an unset
   `SKCOORD_CARD_STORE` value and for `1`, `dual`, `0`, `off`, `false`, and
   `no`. This must exercise a non-editable SKCoord `v0.1.39` wheel and a
   non-editable SKCapstone candidate wheel in a new isolated environment.
4. Corrupt or malformed event state produces a nonzero result and never reports
   stale birth criteria as current. A deliberate fold-bypass sensitivity must
   make the parity qualification fail.
5. Package provenance proves neither package resolves through an editable
   install, sibling checkout, `PYTHONPATH`, `.egg-link`, or editable
   `direct_url.json`.

The producer updates only the exact SKCoord dependency floor, its dependency
assertion, the task-owned qualification coverage, and the existing
`CHANGELOG.md` Unreleased block. This repository has no committed dependency
lock file, so the built candidate wheel metadata and its recorded SHA-256 are
the reproducible dependency lock evidence. No new lock format is introduced.

The producer then runs focused tests, the full repository test gate, static
checks, documentation checks, build and Twine checks, scoped secret checks, and
a clean rollback proof. Evidence records the source commit and tree, upstream
release hashes, candidate commit and tree, candidate wheel hash, test results,
sensitivity results, rollback result, and fresh-index proof. Every commit has
the required co-author trailer.

## Independent review contract

The reviewer is distinct from the producer, claims `a6c7fd2f` only after
`db5ce143` is DONE, fetches current upstream state, and uses a new isolated
worktree. The reviewer does not repair the candidate.

The reviewer independently reproduces the exact upstream tag, commit, tree,
wheel, and source distribution hashes. It rebuilds the exact SKCapstone
candidate, confirms its dependency metadata and wheel hash, and repeats the
non-editable CLI matrix for unset, `1`, `dual`, `0`, `off`, `false`, and `no`.
It repeats malformed-state and deliberate fold-bypass sensitivity checks and
verifies that failed claims do not mutate task files, CardStore core, or event
history.

The review evidence explicitly checks that the candidate contains no editable
sibling dependency, task or core rewrite, baseline change, service change,
manual tag, check bypass, force push, or shared-checkout edit. It records an
immutable PASS or BLOCKED result and leaves source untouched.

## Rollback

Rollback restores the prior SKCoord dependency floor and removes only the
task-owned assertion, qualification coverage, changelog entry, and evidence.
The rollback proof is executed in disposable state and must leave the shared
checkout and all live CardStore, service, operator, ATLAS, and baseline state
untouched.
