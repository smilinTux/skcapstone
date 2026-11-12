Add `scripts/fleet/skfleet-backlog-promoter.py`, which keeps the `ready` column
stocked so the fleet has work to dispatch.

The fleet can run dozens of workers concurrently and was running two. Measured
on the chi estate with 15 seats free and every lane target already raised:

    SLOTS|chiap01|codex=0/8 glm=1/2 qwen=0/1 kimi=0/3|total_free=15
    POOL_V2|population=1533 ready=15 ineligible=1518
    CYCLE_RECEIPT|chiap01|launched=2|attempted=3

Lane capacity was never the constraint and neither was the gateway: codex holds
32 slots and has never exceeded a peak of 2. The dispatcher draws candidates
from `ready`, and the estate held 20 cards in `ready` against 1134 live cards in
`backlog`. Nothing moved a card between those columns, so the pool drained and
stayed drained, which is why several rounds of lane tuning changed nothing.

The promoter applies every gate the selector applies downstream, before moving
anything. Promoting a card the selector will silently withhold is worse than
leaving it alone: it inflates `ready`, hides the real shortfall, and churns the
claim ceiling. First live run over 1133 backlog cards found 581 eligible and
named a reason for each of the rest: blocking-label 259, review-card-needs-
source-binding 119, dependency-open 75, sensitive-category 70,
size-marker-not-exactly-one 25, seat-unprovisioned 4.

Writes go through `skcapstone coord move`, never raw JSONL, and the result is
verified by re-folding each card in a fresh CardStore rather than trusting the
CLI exit code, because a CLI that reports success and persists nothing is a
documented failure mode in this stack. First apply run: promoted=25,
confirmed_ready_on_reread=25.
