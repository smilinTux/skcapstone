- Seraph dispatcher timeout: raised the default from 180s to 600s and made it
  overridable via `SKFLEET_SERAPH_DISPATCH_TIMEOUT_SECONDS` (invalid values -
  non-integer, zero, or negative - fall back to the default instead of
  crashing or disabling the timeout). Measured on the chi estate 2026-09-20
  against a roughly 7,000-card store: a normal run already takes close to
  180s (00:36:33 to 00:39:10, 2m37s), and the next run hit 180s exactly and
  was killed (01:15:00 to 01:18:00), even though the cycle had already
  launched two reviewer units. The receipt then falsely records
  `seraph_dispatch_timeout`/`dispatch_failed=1` for a cycle that worked, and
  the SIGTERM to the whole process group can land between a card being
  claimed and its worker being launched, leaving an orphaned claim behind
  until a reaper clears it.
