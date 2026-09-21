### Fixed

- A failed worker launch now records why. The launch output was captured and
  then discarded, so `LAUNCH_FAILED` carried no reason while the automatic
  `release-claim` returned the card to the pool to fail again next cycle. A
  card can loop that way indefinitely with every receipt, cycle summary and
  dispatcher log silent about the cause (the log file for such a card is zero
  bytes). A `LAUNCH_FAILED_DETAIL` line now carries the exit status and the
  first line of each stream.
