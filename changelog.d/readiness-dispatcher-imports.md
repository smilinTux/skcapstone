### Fixed

- The fleet readiness gate now verifies that the dispatcher script itself
  imports. `unit_modules()` only recognises `python -m <module>` ExecStart
  lines, so `skfleet-rotate.service`, whose ExecStart names a script path, had
  no import check at all. chiap02 crashed at import on every cycle for hours
  (capauth 0.3.1 against the fleet's 0.3.9+) with 11 cards owned by it, while
  the gate reported only that its environment variables were set.
