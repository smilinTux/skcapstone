# Criteria-fold uptake landing TDD

Card: `da1e302d`

Date: 2026-08-24

## Objective

Land only the independently reviewed criteria-fold uptake and reproducible
package build patch on current SKCapstone `origin/main`. Preserve concurrent
work, use the normal protected pull request route, and verify the automatic
release without touching runtime state.

## Immutable inputs

- Landing base commit: `3a50b69bfe8f0190ba9241e8abbdca165d75f072`
- Landing base tree: `d6e99c6ad3569f6e9b37afb59d8c31b5b4cf99cf`
- Reviewed source commit: `fbc1ec3e11e832bc7942992d3d3394f0bfa9875b`
- Reviewed source tree: `40b4bd0a2b5948d5ad79fa48707fd8b596caeaef`
- Producer evidence commit: `e28b185c1b46b018eec554183093fb68ea79a394`
- Independent PASS evidence commit: `ba5578ded0389c52c82fee3e9c9e1474203cadac`
- Exact released SKCoord version: `0.1.39`

The reviewed semantic patch is the full six-commit range from the landing base
through the reviewed source commit. The landing base is also the exact merge
base, so no unrelated candidate ancestry is included. The reviewed commits are:

- `57c05ab69e359b40b50ef6c449129eebc565ac21`
- `4b29d0dc51afba6cb5154598dfc0830e7fb7319f`
- `627c8193c9b76f1a409642d52b4981d314622cbf`
- `a9b7f32e8106dfea896af082e39f8bc5946be753`
- `c87cc3b2d9f94b1b3d8364d7c7d069351eba3d66`
- `fbc1ec3e11e832bc7942992d3d3394f0bfa9875b`

The patch may touch only:

- `.github/workflows/pytest.yml`
- `CHANGELOG.md`
- `SOP.md`
- `docs/RELEASING.md`
- `docs/specs/2026-08-24-reproducible-package-build-tdd.md`
- `docs/specs/2026-08-24-skcoord-criteria-fold-uptake-tdd.md`
- `pyproject.toml`
- `scripts/build_reproducible.py`
- `src/skcapstone/coord_amendments.py`
- `tests/test_coord_amend.py`
- `tests/test_reproducible_build.py`

The landing TDD and final landing evidence are the only additional allowed
paths.

## Reconciliation assertions

1. Start from the exact freshly fetched landing base and commit this TDD before
   applying source changes.
2. Apply the reviewed commits without substituting a new implementation.
3. Refuse any semantic conflict. A mechanical `CHANGELOG.md` conflict may be
   resolved only by retaining every current Unreleased entry plus the reviewed
   reproducible-build entry.
4. Prove that no path outside the explicit allowlist changed.
5. Prove every landing commit has the required co-author trailer and a fresh
   worktree index.

## Artifact assertions

1. Build from two separate clean, noneditable clones at different wall-clock
   times through `scripts/build_reproducible.py`.
2. Require byte-identical wheels and byte-identical sdists, exact SHA-256,
   member counts, normalized payload hashes, exact version metadata, and the
   same source-derived `SOURCE_DATE_EPOCH`.
3. Omit the deterministic build procedure in two clean controls and require
   both archive formats to drift.
4. Install each candidate wheel noneditably with exact SKCoord 0.1.39 in a
   fresh environment and require `pip check` to pass.

## Criteria projection assertions

For each installed candidate, create a card with nine birth criteria and an
append-only nine-criterion amendment. Require the exact current list for:

`unset`, `1`, `dual`, `0`, `off`, `false`, `no`

Append a malformed empty amendment event. Every selector must deny kanban read
and claim, emit no stale birth criteria, create no claim, and leave task/core
birth bytes unchanged.

## Required gates

- Focused reproducible-build and criteria-fold tests
- Full test suite with declared extras and the published noneditable SKHarness
  test sibling required by the sovereignty gate
- Black, pinned Ruff, retired-shim scan, and docs-check tiers 1 through 3
- Build and Twine checks for both artifact pairs
- Exact full-history Gitleaks 8.28.0 gate using the committed baseline without
  changing that baseline
- Separate-clone rollback to the freshly fetched landing base tree
- Clean candidate and shared checkouts

No test may be skipped, weakened, or masked to make this landing pass.

## Pull request and release gate

1. Fetch immediately before push and inspect `origin/main..HEAD`.
2. Push one new branch without force and open one owned pull request.
3. Wait for every required check to pass. Never use admin bypass.
4. Fetch immediately before merge and stop if current main introduces a
   semantic conflict.
5. Merge only through normal protected-branch controls.
6. Verify the resulting main commit, post-merge checks, automatic patch tag,
   release artifacts, and their digests before completing the card.

## Rollback

Before push, revert all landing commits in a separate clone and require the
exact landing base tree. Remote rollback, if later required, is a new normal
pull request. Published history is never rewritten.
