### Fixed

- **Rollout deploys every per-host artifact, not just the dispatcher.**
  `staged_rollout`'s deploy and rollback steps copied a single hardcoded
  script name. `skfleet-rotate.py` resolves its worker wrapper as
  `os.path.dirname(__file__)/skfleet-worker-wrapper.py`, so the wrapper that
  actually runs is the copy beside the *deployed* dispatcher in
  `~/.local/bin` - which no step ever copied. It was current only by
  accident. The copy steps are now generated from
  `deployment_manifest.PER_HOST_ARTIFACTS`, declared once, so an artifact
  added there is deployed by both the forward and the rollback path and
  cannot be forgotten.

- **Drift detection grades every per-host artifact.** `detect_drift` graded
  only `~/.local/bin/skfleet-rotate.py`. It now walks the same
  `PER_HOST_ARTIFACTS` tuple the rollout copies from, so a stale deployed
  worker wrapper is a reported finding instead of an invisible one.

### Added

- **The checkout version surface, `checkout:git_sha`.** A chi host carries
  three surfaces that can each go stale alone: the checkout, the installed
  package, and the explicitly-copied artifacts. Every expected value
  `detect_drift` compared against was read from the node's own checkout, so
  a node that never pulled agreed with itself perfectly and reported clean
  on every surface - which is how five uniformly stale hosts reported
  healthy during the 2026-09-19 dispatch outage.

  `skcapstone fleet node drift --expect-git-sha SHA` lets a caller pin the
  commit a node is supposed to be on, and `staged_rollout`'s gate now passes
  the rollout manifest's `git_sha` through to each remote. Because every
  node in one rollout is gated against the *same* pin, agreement between
  hosts falls out of the existing gate rather than needing a second
  fleet-wide report.

  The installed-package finding is renamed `git_sha` -> `package:git_sha`,
  so a drift report names which of the three surfaces is stale rather than
  leaving a bare name to be interpreted. The old name is still treated as
  unambiguous by the role filter, so a controller on this code gating a
  node on older code does not silently downgrade a finding mid-rollout.
