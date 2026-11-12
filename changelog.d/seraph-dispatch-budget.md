### Fixed

- Seraph cycles no longer run permanently at the edge of their own deadline.
  Measured over 3h on a 7304-card store, fourteen consecutive cycles took 150s
  to 188s against a 190s dispatcher timeout, and `seraph_dispatch_timeout` was
  the dominant cycle outcome. The whole budget is raised together, as the
  existing note on the constant required: dispatcher timeout 190s to 420s,
  `TimeoutStartSec` 300s to 540s, and the seraph timer cadence from 5 to 10
  minutes (which the chi estate was already running via a drop-in, so the
  template had drifted from production). The invariant holds with room:
  75 + 420 + 30 = 525 < 540 < 600.
