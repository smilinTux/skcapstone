# Fleet Coordination Write Boundary

Workers must use `skcapstone coord` for every verdict, evidence, status, claim,
label, dependency, and lifecycle write. A worker must never create, append,
rewrite, rename, or delete CardStore event JSONL, even when the event shape and
hash chain appear valid. Use CLI reads for normal verification. Raw file
inspection is reserved for emergency operator diagnostics.

## Required usage

```bash
skcapstone coord verdict <card_id> PASS_FOR_REVIEW \
  --candidate ~/.skcapstone/evidence/work/<card_id>/<file> \
  --commit $(git rev-parse HEAD) --tree $(git rev-parse HEAD^{tree}) \
  --ref refs/heads/<branch> --agent <worker>
# Record it LAST: anything written after a verdict supersedes it.
skcapstone coord link <card_id> verdict PASS --agent <worker>
skcapstone coord link <card_id> evidence <repo-relative-path> --agent <worker>
skcapstone coord move <card_id> review --agent <worker>
```

Do not open or append files below `cards/<card_id>/events/` or
`coordination/card_events/`. The Pi worker tool-call extension terminates a
worker when native file tools or recognizable shell mutations target those
paths. This immediate runtime guard is defense in depth, not a
filesystem-complete security boundary.

## Why a provisional PASS needs its own verb

`coord link` writes a `CardEvent`, whose fixed field set holds no
`candidate_path` and no `candidate_sha256`. A governed review will not open
without both, plus the typed commit, tree and ref that bind them to a real
revision. Measured on chi 2026-09-18: `OPENED_REVIEW` was 0 across 14 days and
1,660 rotations while 214 cards logged `OPEN_REVIEW_EVIDENCE_BLOCKED` every
cycle; of 354 cards ever blocked that way, 305 held their `PASS_FOR_REVIEW`
only as an overlay link row and 346 held no hash-bound candidate anywhere.
`coord verdict` writes one native CardStore event carrying all of it, and
computes the sha256 from the file rather than trusting a typed digest.

## Follow-up boundary

This guard does not change CardStore ownership and does not add a daemon. A
Service-broker card `8f4e2a71` is the only planned filesystem-complete
enforcement. It should make the CLI or broker the only process
with filesystem write permission. That boundary is required to distinguish a
CLI-mediated append from a worker that produces the same valid bytes directly.

## Prior guard lesson

Commit `91127c8` implemented a Pi extension on an unmerged feature branch and
was never present on `main`, so the fleet launcher never deployed it. It also
covered only `cards/<id>/events/*.jsonl`, not the legacy
`coordination/card_events/*.jsonl` overlay, and did not update startup guidance
or CI. This patch reuses its tool-call approach while closing those reach and
adoption gaps.
