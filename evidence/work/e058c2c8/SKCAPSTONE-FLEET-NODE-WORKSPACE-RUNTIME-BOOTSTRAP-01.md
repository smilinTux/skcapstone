# e058c2c8 workspace runtime bootstrap evidence

Card: `e058c2c8`
Producer: `cursor-e058c2c8`
Verdict candidate: `PASS_FOR_REVIEW`

## Scope

Atomic bootstrap under registry lock:

- live meminfo via injectable path/reader
- exact active binding count for the logical bucket
- `capacity.admit_headroom` + advertised reserves/capacity
- register only after admission; pure `admit_capacity` / `plan_isolated_workspace` helpers retained
- no subprocess/git actuator; no host/model bindings

## Files and SHA-256

- `CHANGELOG.md`: `9797a2e4e3c05c589062aa3e0f188e7cf9dd9859b134cad678c6b08546c42b38`
- `src/skcapstone/fleet/capacity.py`: `fea752425341a3ae13fa1fcd2cc6ba902894f52d8ae865abc9e684599e6c83b2`
- `src/skcapstone/fleet/workspace_runtime.py`: `2c26f7107fb57a913b9dac87c38ef664ba2ca65c49a24746be64bc930a87147a`
- `tests/fleet/test_allocatable.py`: `2419dcfa7d26736d9eddb71c7317c665ceeb760ece2778ecf9fe2fd83018b254`
- `tests/test_workspace_runtime.py`: `cc12770ec39fbdf843470033bbdf9bc3670eb8ecff375498c4e51cfbe263dfcf`

Artifact digest: `bff83c181ec5085a8490eafe1b77e9cdbcd2b33d329a4ae4731bdc5bf0f02118`

## Verification

- pytest focused: 14 passed
- docs_check tiers 1-3: pass (CHANGELOG included)

## Rollback

Revert listed paths. No live node mutation.
