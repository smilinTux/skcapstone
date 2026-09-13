# e058c2c8 workspace runtime bootstrap evidence

Card: `e058c2c8`
Producer: `cursor-e058c2c8`
Verdict candidate: `PASS_FOR_REVIEW`
Prior HEAD: `1a17361b6482e4e2f49e0948235cabe3a7494199`

## Scope

Thin host-neutral orchestration seam:

- `capacity.admit_headroom` for fail-closed mem/swap
- pure plan/retire/successor occupancy checks (no git actuator)
- exact claim/unit mapping and terminal Herdr reclaim selection
- CHANGELOG Unreleased entry for docs-check tier 2
- no host/model literals, no deployment, no e91a20f5 activation

## Size reassessment

`workspace_runtime.py` is 254 lines (down from the earlier ~522-line actuator
module). Further deletion would remove typed seam surface still covered by
passing tests; left intact to avoid destabilizing behavior.

## Files and SHA-256

- `CHANGELOG.md`: `53e67dfb7c37cbe6b76426a3440573409eebf92e08dda6f09dc51b6a180813f6`
- `src/skcapstone/fleet/capacity.py`: `fea752425341a3ae13fa1fcd2cc6ba902894f52d8ae865abc9e684599e6c83b2`
- `src/skcapstone/fleet/workspace_runtime.py`: `1a65945b6f26d235911725f30984ce3323a74d3e6e5addb6c3a498b1f25c6ac4`
- `tests/fleet/test_allocatable.py`: `2419dcfa7d26736d9eddb71c7317c665ceeb760ece2778ecf9fe2fd83018b254`
- `tests/test_workspace_runtime.py`: `8a184b4173b48869d4517cf9c4b9e77fe97c5d1c10f567dc08e68a919b1bc8b9`
- `evidence/work/e058c2c8/SKCAPSTONE-FLEET-NODE-WORKSPACE-RUNTIME-BOOTSTRAP-01.md`: `50fde2bd650bcddbfe0eeef813aa232cbd0d2b9b72667a8382ff7bebd79da5a5`

Artifact digest (code+changelog paths): `2efa7bca631c2705cd206fe85b21c24f259e6b7deee2421d1777c175448d617f`

## Verification

- `python -m pytest -q tests/test_workspace_runtime.py tests/fleet/test_allocatable.py` → 12 passed
- `docs_check.py` tiers 1,2,3 with PR changed-file list including CHANGELOG.md → pass
- `ruff check` on touched Python paths → pass

## Rollback

Revert listed paths. No live node mutation.
