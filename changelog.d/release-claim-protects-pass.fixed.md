- **`coord release-claim` now refuses to release a claim whose holder already recorded a PASS.**
  On 2026-10-12 a stale-claim sweeper released 87 claims with `--abandon-reason error`;
  77 were producers correctly holding a claim after PASS or PASS_FOR_REVIEW while
  independent review and acceptance ran. PR #1166 fences who may touch custody; this is
  the content rule. The guard (`skcapstone.release_guard`) reads the holder's latest
  outcome for the CURRENT claim generation and refuses any `PASS*` verdict, printing
  the reason and the verdict. An operator may pass `--release-finished-work` with a
  required `--reason`; the override and reason are written on the `release_claim`
  event, and a production worker (`SKFLEET_CARD_ID` set) can never override. Stale
  claims with no PASS release as before, and in-process dead-worker reapers that call
  `Board.release_claim` directly are unchanged.
