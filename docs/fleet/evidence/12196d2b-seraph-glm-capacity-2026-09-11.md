# Seraph GLM Capacity Evidence 12196d2b

## Candidate

- Base revision: `723e6a989b2c5deafcee26f2514739f9dfd7c2e0`
- Source commit: `c61773650c440fb4f034f812b7a7ebfa2708b0ec`
- Source tree: `240971601d7df84506f4a7e81450f7a7188e352b`
- Producer: `codex-12196d2b`

## Files changed

- `src/skcapstone/seat_cycle_entrypoint.py`
- `tests/test_seat_cycle_entrypoint.py`
- `CHANGELOG.md`

## Acceptance evidence

- `seraph_operation` retains `SKFLEET_GLM_TARGET` from its copied parent environment.
- Seraph still forces `SKFLEET_QWEN_TARGET` and `SKFLEET_KIMI_TARGET` to zero.
- Seraph still resolves only the provider-neutral `S` size bucket.
- PR637 already supplies provider-neutral size bucket resolution and legacy compatibility aliases. This candidate does not alter either contract.

## Verification

- Red test before implementation: focused test failed because configured GLM target `2` became `0`.
- Pull-request docs-check failure reproduced locally: tiers 1 and 3 passed,
  while tier 2 required a CHANGELOG entry for the source change.
- Docs check after the minimum CHANGELOG entry: tiers 1, 2, and 3 passed.
- Focused regression after implementation: `1 passed in 0.33s`.
- Baseline and changed-boundary suite: `95 passed in 4.05s`.
- Strict asyncio mode was active for both pytest runs.
- Ruff passed for both changed Python files.
- Black check passed for both changed Python files.
- Gitleaks full-history scan with the committed baseline found no leaks.
- `git diff --check` passed.

## Known limitations

- This source-only candidate does not set an estate GLM capacity value. Operators retain responsibility for configuring the desired bounded target.
- No installed service, live worker, gateway route, deployment, merge, or external action was changed.

## Rollback

Revert documentation commit `c61773650c440fb4f034f812b7a7ebfa2708b0ec`, then source commit `ab41db7f1e59a6bb8ed2cce88b7bdfc0a31c8fac`. No data migration or cleanup is required.
