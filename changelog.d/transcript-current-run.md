### Fixed

- The runaway-transcript check now measures the current run instead of the
  historical maximum. pi opens a new `.jsonl` per run in the same
  per-workspace directory, so taking the largest made any card that ran away
  once permanently over the limit: `cf460fde` was killed, relaunched and
  killed again within four minutes while its current run was a healthy 2.4MB
  against a 329MB transcript from two days earlier.
