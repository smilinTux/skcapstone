# CI enrollment receipt Implementation Plan

> **For agentic workers:** Execute this single-task repair inline as assigned. Independent review follows the sealed candidate.

**Goal:** Distinguish centrally authorized manifest-only enrollment evidence from established repository CI results.

**Architecture:** The Git binder adds literal `initial_enrollment: true` only after the registered manifest-only branch succeeds. Completion selects a dedicated exact-card receipt from that immutable flag, with strict five-field pins and immutable evidence; established capsules retain their existing full-policy gate.

**Tech Stack:** Python 3.11/3.12, pytest, Click, MCP, existing SKCoord CardStore.

**Spec:** `docs/superpowers/specs/2026-09-11-repository-ci-applicability-design.md`, repair card `a5bf5e69` and its approved enrollment amendment.

## Global Constraints

- Exact base: `7cbdada4028f4f4a179b0c2d0268f76188360bbd`.
- Source-only isolated worktree; no live install, migration, merge, push, or SKGateway edits.
- Preserve natural HOME and reuse retained isolated development environments.
- Established capsules omit the flag; a present flag must be literal true and match the central registry.
- Enrollment receipts contain exactly schema_version, repository, candidate_revision, profile_sha256, evidence. They make no CI check claims.
- All four completion entrypoints reject invalid evidence before claim or lifecycle mutation.

## Task 1: Separate enrollment completion evidence

Files: modify `src/skcapstone/ci_applicability.py`, `src/skcapstone/coord_links.py`, the design and changelog; add `tests/test_ci_profile_enrollment.py`; replace the obsolete enrollment expectation in `tests/test_coord_completion_parity.py`.

Interfaces: preserve `bind_ci_profile(meta, request)` and `validate_profile_completion(card_id, home, core)`. Extend `_latest_receipt(card_id, home, key="ci_applicability")` internally; allow both receipt keys in `append_coord_link`.

- [ ] Add regression tests asserting `bind_ci_profile(meta, request)["initial_enrollment"] is True` for registered initial enrollment, and absent flags on established profiles. Test dedicated receipts, substitution, invalid flags, stale pins, malformed/duplicate/escaped rows, conflicts, and CLI/MCP persistence.
- [ ] Run `python -m pytest tests/test_ci_profile_enrollment.py -q` before source edits. Expect missing-flag, dedicated-receipt, and writer regressions to fail.
- [ ] Add `capsule["initial_enrollment"] = True` only when the base manifest is absent. Validate the optional flag using `is True` and recheck the registry digest. Choose receipt key and exact fields from that immutable flag; use existing strict reader, pin checks, and `_evidence` validator. Add the enrollment key to the shared writer's key set.
- [ ] Run focused suites on both Python versions, broader boundary suites, static checks, package build, secret checks, and the isolated Python 3.12 full unit suite. Capture exact commands and statuses without inferring missing checks.
- [ ] Commit the exact source candidate, seal hashed evidence, link source card and move it to review. Do not complete it. Rollback is source revert before installation; no live data is changed.
