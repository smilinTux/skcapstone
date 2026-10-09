### Fixed

- `skfleet-auto-rollout` waits for the local dispatch cycle
  (`skfleet-seat-cycle`, `skfleet-niobe-live`) to finish before deploying, and
  defers to the next tick if it is still busy after 10 minutes. A deploy that
  landed mid-cycle crashed that cycle, and the drift gate then halted on the
  failed unit (2026-10-09 at 02:32Z and 04:37Z).
