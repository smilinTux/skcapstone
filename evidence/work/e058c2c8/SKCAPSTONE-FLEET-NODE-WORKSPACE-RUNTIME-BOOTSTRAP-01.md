# e058c2c8 workspace runtime bootstrap evidence

Card: `e058c2c8`
Producer: `cursor-e058c2c8`
Verdict candidate: `PASS_FOR_REVIEW`

## Scope

Atomic create/retire bootstrap under registry lock:

- live meminfo via injectable path/reader
- exact active binding count for the logical bucket
- `capacity.admit_headroom` + advertised reserves/capacity
- thin injected or default git materialize/retire actuators with exact path checks and rollback
- claim/unit mapping, successor safety, terminal Herdr reclaim
- no host/model bindings; no e91a20f5 activation

## Files and SHA-256

- `CHANGELOG.md`: `00077f20b96d83a00d00a3e1d9db077fc80a28eb87c8f2462e2e6a2f2bebcf33`
- `src/skcapstone/fleet/capacity.py`: `fea752425341a3ae13fa1fcd2cc6ba902894f52d8ae865abc9e684599e6c83b2`
- `src/skcapstone/fleet/workspace_runtime.py`: `bacc6984a669b9a4f115210a019a1d1cc790520c566a02b1fd0c5990f854c0cd`
- `tests/fleet/test_allocatable.py`: `2419dcfa7d26736d9eddb71c7317c665ceeb760ece2778ecf9fe2fd83018b254`
- `tests/test_workspace_runtime.py`: `3eee96a5dd55c83c07d572eca2bdd05f80fdee89ba52cb33d032b4061a701e7b`
- `tests/test_workspace_runtime_integration.py`: `77c6243d5b48c495d181fed42c2d22f5032e1e6cc19d592d7c9ba8d5eec8ec54`

Artifact digest: `5c8438e52a61b26d6987c4d79cf61e16505b9f1c3f694dee090e0e9018d26364`

## Verification

- `python -m pytest -q tests/test_workspace_runtime.py tests/test_workspace_runtime_integration.py tests/fleet/test_allocatable.py` → 15 passed
- docs_check tiers 1-3 with CHANGELOG in changed set → pass

## Rollback

Revert listed paths. No live node mutation.
