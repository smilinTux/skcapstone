# Seraph GLM Capacity Evidence 12196d2b

## Candidate

- Base revision: `723e6a989b2c5deafcee26f2514739f9dfd7c2e0`
- Source commit: `ab41db7f1e59a6bb8ed2cce88b7bdfc0a31c8fac`
- Source tree: `09382831f439ff6deadd77ea6f41ed1e2db62216`
- Producer: `codex-12196d2b`

## Files changed

- `src/skcapstone/seat_cycle_entrypoint.py`
- `tests/test_seat_cycle_entrypoint.py`

## Acceptance evidence

- `seraph_operation` retains `SKFLEET_GLM_TARGET` from its copied parent environment.
- Seraph still forces `SKFLEET_QWEN_TARGET` and `SKFLEET_KIMI_TARGET` to zero.
- Seraph still resolves only the provider-neutral `S` size bucket.
- PR637 already supplies provider-neutral size bucket resolution and legacy compatibility aliases. This candidate does not alter either contract.

## Verification

- Red test before implementation: focused test failed because configured GLM target `2` became `0`.
- Focused regression after implementation: `1 passed in 0.27s`.
- Baseline and changed-boundary suite: `95 passed in 3.78s`.
- Strict asyncio mode was active for both pytest runs.
- Ruff passed for both changed Python files.
- Black check passed for both changed Python files.
- `git diff --check` passed.

## Known limitations

- This source-only candidate does not set an estate GLM capacity value. Operators retain responsibility for configuring the desired bounded target.
- No installed service, live worker, gateway route, deployment, merge, or external action was changed.

## Rollback

Revert source commit `ab41db7f1e59a6bb8ed2cce88b7bdfc0a31c8fac`. No data migration or cleanup is required.
