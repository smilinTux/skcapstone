# Atomic generation repair independent review

Card: `7deae2fd`

Repair card: `0acf2490`

Date: 2026-08-29

Verdict: `PASS`

## Scope and safety

This was an independent source-only review of PR 301. The review did not repair source, merge, install a package or launcher, invoke a live worker, mutate a live claim, deploy, restart, clean up runtime state, or access credentials or protected data. All mutation tests used pytest temporary directories. The only repository change is this review record.

The dependency card `0acf2490` was complete before review began. Card `7deae2fd` was owned by `pi-codex-chiap01-7deae2fd` with claim revision `a87e6592cfc548a9a6b16266710aa5ec`.

## Blocked evidence identity

The reviewed predecessor evidence was reproduced exactly:

- Path: `/home/skuser01/.skcapstone/evidence/work/8c516d98/INDEPENDENT-REVIEW.md`
- SHA256: `2d1dedc8fd80687555e51a39662072eec5de9bf9f13cf70d61a50ce416b4bb62`

That evidence blocked the earlier PR head because launcher arguments and the real CLI were not joined, owner-only release conflicted with the source CLI, and the generation fence could be lost between a fresh read and mutation.

## Exact repair identity

The repair evidence from card `0acf2490` was independently reproduced:

- PR: `https://github.com/smilinTux/skcapstone/pull/301`
- PR head: `0a7ca66d5c6c2182a5d5de42fa2ff81e85cb8e43`
- Remote branch head: `0a7ca66d5c6c2182a5d5de42fa2ff81e85cb8e43`
- Commit: `0a7ca66d5c6c2182a5d5de42fa2ff81e85cb8e43`
- Parent: `0d67ea9c983f9c0ac76e74cc5eff2c0640db50ef`
- Tree: `22fd34bb785720c3a19045e6a1b3c6c83b9a51e7`
- Binary full-index diff SHA256: `e09a054f42fdaa496cf46bdd71cf9a2568619e0f8c850cf3b829da6d3de10a67`
- Email-format commit patch SHA256: `8549f9c449e0ea37ce0536e7a67018acece334212e3f704832cb85de2577958e`
- Diff statistic: 5 files changed, 242 insertions, 16 deletions

Changed paths and exact file hashes at the repair commit:

- `scripts/fleet/skfleet-rotate.py`: `643bb1bafa699dc413c9d55da39314c57ceed55c934bb602b58181964a409d07`
- `src/skcapstone/cli/coord.py`: `96c7d1cf16a153a549bbbb20435835c9f262295931ee509e0f4b36cb5fd4fad5`
- `tests/test_cli_coord_deps.py`: `24a2a4c4e3d588fd31a997c8813d413b2ba6eaf0f6262d77ad83340a0662aaf7`
- `tests/test_skfleet_reaper_live_reconciliation.py`: `abdb2d09bab0079ede0539b21005ad1225b1eb28510961b29a02158f91a7b1cc`
- `tests/test_skfleet_reaper_provenance.py`: `b21747843e328d4886970376046ed4c05020cf6923468e82e1b4517f139c907a`

The commit, tree, parent, patch, changed paths, source hashes, test hashes, remote branch head, and current PR head all match card `0acf2490` evidence.

## Atomic generation boundary

Source inspection and real CLI execution established the following:

1. `coord release-claim` requires exactly one generation fence.
2. A revisioned claim requires its exact `--expected-claim-revision`.
3. A revisionless claim requires its exact timezone-aware `--expected-claim-timestamp`.
4. Owner-only release is rejected.
5. Supplying both fence forms is rejected.
6. A timestamp fence cannot release a current revisioned generation.
7. Revision and timestamp comparisons occur while both `_board_mutation_lock` and `card_mutation_lock` are held, immediately before `_release_claim_locked`.
8. A stale revision cannot release a newer claim with the same owner.
9. A stale timestamp cannot release a newer revisionless claim with the same owner.

The launcher derives a canonical aware timestamp only when the fresh claim is revisionless. Otherwise it emits the exact revision. The launcher argument vector is then executed against the registered source CLI command in the joined tests rather than accepted by a permissive subprocess fake.

## Joined launcher-to-real-CLI reproduction

The four joined cases passed:

- current revisioned claim released
- current revisionless claim released
- newer same-owner revisioned generation refused stale release
- newer same-owner revisionless generation refused stale release

Result: `4 passed in 0.38s`.

Log SHA256: `b4fcdb9f16bf8628805136321167c0606c374836392d783695ac08c9612167c7`

The separate CLI boundaries also prove that owner-only and dual-fence release remain forbidden, exact aware timestamp release works for a revisionless claim, and stale revision refusal preserves the newer same-owner generation.

## Original reconciliation assertions

The exact 18 assertions present at parent `0d67ea9c983f9c0ac76e74cc5eff2c0640db50ef` were selected by excluding only the four newly added joined cases.

Result: `18 passed, 4 deselected in 0.40s`.

Log SHA256: `2e2dbaaa105a822674c269021cdfa3fd82ba42d2dadc6ecc8be69a92fed111f0`

These assertions cover revisionless release, same-owner timestamp replacement, owner replacement, quorum, running-card, grace, ineffective-card, named-owner, invalid cached and fresh timestamps, missing revision provenance, revisioned CLI shape, legacy ineffective-state migration, lifecycle pruning, TTL retry, retained fresh entries, atomic sorted writes, and first-seen preservation.

## Relevant boundaries and preserved paths

The combined CLI, reconciliation, and provenance suite passed:

Result: `55 passed in 1.62s`.

Log SHA256: `3e678d202533207f940c2ef6b3f18625e26c50a20df61eb6cc8e58117dddf74d`

The provenance suite confirmed malformed or ambiguous launch evidence fails closed, successful launch provenance records the exact generation, quorum and fresh-owner guards remain, newer same-owner generations receive their own grace and are preserved, exact grace boundaries remain, malformed or ambiguous timestamps fail closed, and future clock skew fails closed.

Static source inspection found exactly three `--expected-claim-revision` occurrences and one `--expected-claim-timestamp` occurrence in the launcher. The reaper release supports revision or timestamp according to generation type. Worker-exit trap cleanup remains revision-fenced. Launch-failure cleanup remains revision-fenced. `git diff --check`, Python compilation of all five changed paths, Ruff on CLI and changed tests, and Ruff fatal-error checks on the launcher passed. Black was not rerun because the active environment did not contain the Black module; formatting had already been covered by repair evidence and was not needed to establish the review criteria.

## Conclusion

`PASS`

The exact blocker recorded by evidence SHA256 `2d1dedc8fd80687555e51a39662072eec5de9bf9f13cf70d61a50ce416b4bb62` is repaired at PR 301 head `0a7ca66d5c6c2182a5d5de42fa2ff81e85cb8e43`. Launcher arguments are compatible with the real source CLI, revisioned and revisionless releases are atomically generation-fenced, newer same-owner generations are preserved, owner-only release remains forbidden, the original 18 assertions pass, ineffective-state behavior passes, and worker-exit and launch-failure paths retain revision fences.
