- **The claim ceiling charged cards for the dispatcher's own heartbeat, which is
  how a fleet-side defect became a permanent freeze.** `_work_epochs` in
  `scripts/fleet/skfleet-rotate.py` decides whether anything was written on a card
  inside one claim's hold; a hold with nothing under it is forgiven and does not
  count toward `_MAX_CLAIMS`. It already skipped lifecycle bookkeeping by ACTION
  (`claim`, `release_claim`, `mero_observation`, and anything the liveness reaper
  wrote), on the stated grounds that those rows describe the WORKER, not the card.
  `worker_liveness` is the same kind of row and was missed, because it arrives as
  a `link` action rather than as an action of its own. The dispatcher writes one
  every five minutes for every held card, so it lands inside any hold longer than
  a single dispatch tick, `_work_between` then reports work, and the claim is
  charged. Forgiveness was therefore unreachable for exactly the long-running
  cards it was written for, and because the ceiling is monotonic and never
  self-clears, the card was frozen for good.

  Measured on chi, 2026-09-19. Eight cards sat frozen on chiap01. Not one carried
  a verdict, a PASS, a candidate commit or any evidence; in nearly every hold the
  only row written was niobe's liveness tick. Recomputing the real ledgers with
  the heartbeat excluded and no amnesty granted: 724c2e52 7 -> 3, 63d0474d 6 -> 0,
  9d6e9f72 6 -> 2, 05d6dd56 7 -> 1 countable claims. Four of the seven genuinely
  frozen cards release themselves with `_MAX_CLAIMS` untouched. The fence holds:
  34115541, 1960b107 and 64384b83 carry real work or real verdicts under their
  holds and stay charged, and 06a95c23, the 402-claim runaway, is unaffected
  because 394 of its claims never close and so have no window to examine.

  Raising `_MAX_CLAIMS` was rejected. It would convert a permanent freeze into a
  slower permanent freeze, hide the defect, and simultaneously free the one
  genuine runaway the ceiling exists to stop.
