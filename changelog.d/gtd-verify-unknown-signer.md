### Fixed

- `skcapstone gtd verify` counts a signature by a key missing from the local trust roster as `unverifiable` and lists it under `unknown_signers`, instead of reporting it as `invalid`. `invalid` now means the claimed key is trusted here and the signature still fails.
