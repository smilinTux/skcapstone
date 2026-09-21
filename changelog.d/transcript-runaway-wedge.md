### Added

- The fleet watchdog now uses the agent transcript for worker health.
  `WORKER_PROGRESS` carries `claim_age_s` and `transcript_bytes`, neither of
  which was previously recorded even though both were already being computed
  or stat-ed.

### Fixed

- A worker looping on exploration is now killed. A runaway keeps its transcript
  mtime perfectly fresh, so every existing deadline exempted it: two workers
  each held a codex slot for 9.5 hours at `state=progress-fresh`, one with a
  165MB transcript holding 11,423 exploration calls against 4 edits totalling
  520 bytes. `wedge-transcript-runaway` joins `WEDGE_ACTUATING_STATES` at a
  100MB ceiling, overridable with `SKFLEET_TRANSCRIPT_LIMIT_BYTES`. It sits
  behind the identity fence, an unmeasured transcript never actuates, and the
  replay of 158 real `WORKER_PROGRESS` records confirms no genuinely working
  worker would have been killed.
