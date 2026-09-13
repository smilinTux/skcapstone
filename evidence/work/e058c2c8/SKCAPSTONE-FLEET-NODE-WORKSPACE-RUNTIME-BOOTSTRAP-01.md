# e058c2c8 workspace runtime bootstrap evidence

Card: `e058c2c8`
Producer: `cursor-e058c2c8`
Verdict candidate: `PASS_FOR_REVIEW`
Base at edit: `c591542354ac38922d92a93eee4448f655b16d95`
Checkout: `/home/skuser01/work/skcapstone-e058c2c8-herdr`

## Scope

Host-neutral source-only governed workspace runtime bootstrap:

- isolated per-card worktree create/retire with shared-checkout refusal
- Herdr reclaim limited to exact terminal (`done`) unchanged generations
- capacity admission from live mem/swap/active work/logical bucket limits
- exact claim/unit mapping, concurrency, rollback, successor safety
- no deployment, no literal host/model bindings, no SKLegal touch, no e91a20f5 activation

## Files and SHA-256

- `src/skcapstone/fleet/workspace_runtime.py`: `423edc81959ded1c775ce6fc7e593882ac025e42033a44a610986ed18c898e15`
- `tests/test_workspace_runtime.py`: `83dafea33a4fe46cfc87cfbc2f43671c0c85b2a676cd82b78e0209b4c86adce9`
- `tests/test_workspace_runtime_integration.py`: `41c5222193c1074e62855eb85e82778f6d0684faa102d431a8a5a6f1edd32ede`

Artifact digest (paths+bytes): `8fef2f0e668c6b904be1a8d89d0744c56b953ccc61ef47759499a31e50b13354`

## Verification

```text
python -m pytest -q tests/test_workspace_runtime.py tests/test_workspace_runtime_integration.py
```

Result: 9 passed.

```text
ruff check src/skcapstone/fleet/workspace_runtime.py tests/test_workspace_runtime.py tests/test_workspace_runtime_integration.py
```

Result: All checks passed.

## Rollback

Remove the three added paths. No runtime deployment or registry mutation was performed on live nodes.

## Non-goals preserved

- e91a20f5 not claimed, deployed, or activated
- SKLegal and protected live worktrees untouched
- no Lumina or direct CardStore JSONL edits
