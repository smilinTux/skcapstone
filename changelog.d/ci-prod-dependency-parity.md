### Fixed

- CI now tests against the dependency set production runs. New
  `deploy/fleet-constraints.txt` pins skcoord, skmemory and cloud9 to
  the versions on every chi host; the unit-test and provider installs use
  `pip -c` on it, and the unreleased skcoord git overlay is gone. Auto-rollout
  installs with the same constraints and a new `verify_dependency_parity` step
  halts the host with `DEPENDENCY_DRIFT` if installed versions differ. Tests
  fail if a pyproject floor exceeds its pin or code imports a skcoord symbol the
  pinned version lacks (2026-10-13, PR #1181 vs skcoord 0.1.86/0.1.87).
