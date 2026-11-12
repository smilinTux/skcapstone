Fix `open_provisional_reviews` creating review cards that are correctly bound
but structurally unroutable, for three independent reasons.

First, `coord create` leaves the new card in the `backlog` column. Seat
admission in `_pool_v2_admission` computes `review_status` from
`claimability.get("status") == "review"`, and both `seraph_review_admitted`
and `elastic_review_admitted` require it, so a review card left in `backlog`
can never be admitted no matter how correctly it is bound.

Second and third, there are two separate size resolvers with different
rules, and a card needs to satisfy both. `_size_class_for` in
skfleet-rotate.py accepts a single `[S]/[M]/[L]/[XL]` title marker OR,
failing that, a single canonical size label such as `sk-s`. But
`reviewer_capacity` in `src/skcapstone/review_admission.py` only falls back
to the label when the title is EMPTY (`next(iter(label_sizes)) if not title
and len(label_sizes) == 1 else None`). The generated title,
`[REVIEW] Review provisional outcome for <parent>`, is never empty and
carried no marker, so `reviewer_capacity` folded size to None and returned
`(0, 0)` regardless of any label tagged on the card. That is the resolver
that actually gates a claim: a card with `(0, 0)` capacity is refused with
"governed review claim denied: capacity", after everything else about it
checks out.

Measured on live infrastructure: 8 correctly bound review cards sat
unroutable while Seraph reported `seraph_no_eligible_work` every cycle.
Moving them to the `review` column and adding an `sk-s` label by hand was
not enough on its own; a dry run then produced ROUTE_PREFLIGHT_OK for all 8
followed by `CLAIM_REFUSED_TOTAL|chiap08|8 claim command(s) refused` and
`CYCLE_RECEIPT|seat=seraph|launched=0|attempted=8`. Reproduced directly on
card `d7d1b736`: with the unmodified title, `reviewer_capacity` returned
busy=0 target=0; with `[S]` added to the title, busy=4 target=48.

The fix tags the created card `sk-s` (Seraph reviews only `[S]` work, per the
seat entrypoint docstring; this satisfies `_size_class_for` directly and is
belt-and-braces against a describe event blanking the title later, a
documented incident where an mcp writer overwrote live card titles with
argv fragments), puts an `[S]` marker directly in the title so
`reviewer_capacity` resolves a real size regardless of the label fallback
rule, and moves the card into the `review` column right after the existing
lineage readback succeeds. The move is verified by reading the column back
through `authoritative_claimability(review_id, fresh=True)`, a different
path than the one that wrote it, never by trusting the `coord move` exit
code alone: a CLI that reports success and persists nothing is a documented
failure mode in this stack. If the move fails, or the readback does not show
`review`, the review id is added to `_REVIEW_READBACK_BLOCKED` and a new
`OPEN_REVIEW_COLUMN_FAILED` line is logged, matching how the neighbouring
readback failure paths already behave.
