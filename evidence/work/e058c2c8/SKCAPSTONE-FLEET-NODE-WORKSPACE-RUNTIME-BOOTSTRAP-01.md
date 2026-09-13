# e058c2c8 workspace runtime bootstrap evidence

Card: `e058c2c8`
Producer: `cursor-e058c2c8`
Verdict candidate: `PASS_FOR_REVIEW`
Base at edit: `a04952cddd3e9f7d6d2ef5def079419c0acad0a9`

## Scope

Thin host-neutral orchestration seam only:

- `capacity.admit_headroom` for fail-closed mem/swap
- pure plan/retire/successor occupancy checks (no git actuator)
- exact claim/unit mapping and terminal Herdr reclaim selection
- no host/model literals, no deployment, no e91a20f5 activation

## Files and SHA-256

- `src/skcapstone/fleet/capacity.py`: `fea752425341a3ae13fa1fcd2cc6ba902894f52d8ae865abc9e684599e6c83b2`
- `src/skcapstone/fleet/workspace_runtime.py`: `1a65945b6f26d235911725f30984ce3323a74d3e6e5addb6c3a498b1f25c6ac4`
- `tests/fleet/test_allocatable.py`: `2419dcfa7d26736d9eddb71c7317c665ceeb760ece2778ecf9fe2fd83018b254`
- `tests/test_workspace_runtime.py`: `8a184b4173b48869d4517cf9c4b9e77fe97c5d1c10f567dc08e68a919b1bc8b9`

Artifact digest: `80c953b9c42379ba579a8b01613df18146d5751473c2bacc04239a3ff1cb5aab`

## Verification

`python -m pytest -q tests/test_workspace_runtime.py tests/fleet/test_allocatable.py` → 12 passed.

## Rollback

Revert the listed paths. No live node mutation.
