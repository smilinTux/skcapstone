- `~/.local/bin` is now the canonical, consistently-asserted location for
  the per-host fleet dispatcher (`skfleet-rotate.py`) and its worker
  wrapper, reversing the remaining half of PR #591 (2026-09-09). #591
  pointed `skfleet-niobe-live.service`'s `--dispatcher` flag at the
  wheel-owned `~/.skenv/bin/skfleet-rotate.py`; every chi host has run the
  `~/.local/bin` copy since (measured 2026-09-19), so that unit was
  launching a file no rollout step kept current. Fixed in both the
  `systemd/` and `src/skcapstone/data/systemd/` copies of the unit. Two
  stale fleet docs (`docs/fleet/model-lane-routing.md`,
  `docs/fleet/seraph-review-dispatch.md`) that still described the dispatcher
  as wheel-owned are corrected to match `docs/fleet/activation-runbook.md`,
  which already had it right. `tests/test_skfleet_seat_runtime_wiring.py`'s
  locked #591 assertion is replaced with the opposite invariant, with a
  comment explaining the reversal so it is not "fixed" back by mistake.
  A new SOP.md `docs-evidence` tier-3 check fails the build if any tracked
  doc or systemd unit ever asserts `.skenv/bin` for either artifact again.
  This is a minimal, targeted fix scoped to the sites that assert the path
  in text; PR #811 (open, `feat/fleet-version-coherence`) is the deeper,
  already-in-flight fix that declares every per-host artifact once in
  `deployment_manifest.PER_HOST_ARTIFACTS` and drives deploy, rollback, and
  drift detection from it, including the same unit and
  `seat_cycle_entrypoint.py`'s two `sys.executable`-relative guesses. That
  work is deliberately left to #811 rather than duplicated here.
