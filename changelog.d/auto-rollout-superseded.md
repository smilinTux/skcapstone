### Fixed

- `skfleet-auto-rollout` no longer reports a false `HALTED` when a newer main
  merges during a rollout. The host pulls the newer main, so its gate shows
  git_sha drift against the manifest this run built. The run now re-fetches
  before retrying, and if main moved it stands down quietly so the next tick
  rolls the newer commit. Seen 2026-10-08: 55482d61 "halted" on chiap02 while
  e2d7dd9d merged.
