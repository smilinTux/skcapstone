- Card `c1a30169`: bind worker liveness to the current claim, wrapper arguments,
  PID and systemd invocation. Ignore stale or unbound beats and publish changed
  observations through an atomic native lifecycle command without impersonating
  the worker or invalidating a guarded handoff with repeated identical links.
