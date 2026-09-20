- Seraph dispatcher timeout: raised the default from 180s to 190s and made it
  overridable via `SKFLEET_SERAPH_DISPATCH_TIMEOUT_SECONDS` (invalid values -
  non-integer, zero, negative, or one that would exceed the load-bearing 300s
  service budget - fall back to the default instead of crashing, disabling
  the timeout, or silently breaking that budget). Measured on the chi estate
  2026-09-20 against a roughly 7,000-card store: a normal run already takes
  close to 180s (00:36:33 to 00:39:10, 2m37s), and the next run hit 180s
  exactly and was killed (01:15:00 to 01:18:00), even though the cycle had
  already launched two reviewer units. The receipt then falsely records
  `seraph_dispatch_timeout`/`dispatch_failed=1` for a cycle that worked, and
  the SIGTERM to the whole process group can land between a card being
  claimed and its worker being launched, leaving an orphaned claim behind
  until a reaper clears it. 190s, not something larger, because
  `SERAPH_LOCK_WAIT_SECONDS`(75) + this timeout + a 30s cleanup margin must
  stay under the 300s `TimeoutStartSec` and 300s timer cadence on
  `skfleet-seraph.service`/`.timer` (test_dispatcher_routes_niobe_and_seraph_
  through_safe_bounded_waits enforces this); that ceiling caps the timeout at
  194, so this is only modest headroom over the measured ~180s run, and a run
  landing near that mark is still at real risk of being killed. The actual
  fix for that is a faster dispatcher run or a deliberate whole-budget raise
  (TimeoutStartSec and the timer cadence together), not this constant alone.
