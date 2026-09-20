Fix `open_provisional_reviews` creating review cards that are correctly bound
but structurally unroutable, for two independent reasons.

First, `coord create` leaves the new card in the `backlog` column. Seat
admission in `_pool_v2_admission` computes `review_status` from
`claimability.get("status") == "review"`, and both `seraph_review_admitted`
and `elastic_review_admitted` require it, so a review card left in `backlog`
can never be admitted no matter how correctly it is bound. Second, the
generated title, `[REVIEW] Review provisional outcome for <parent>`, carries
no `[S]/[M]/[L]/[XL]` size marker and no size label was tagged, so
`_size_class_for` fails closed, `_logical_route_for` returns None, and the
card is dropped from the candidate scan with no log line at all.

Measured on live infrastructure: 8 correctly bound review cards sat
unroutable while Seraph reported `seraph_no_eligible_work` every cycle.
Moving them to the `review` column and adding an `sk-s` label by hand made
them dispatch immediately in a dry run.

The fix tags the created card `sk-s` (Seraph reviews only `[S]` work, per the
seat entrypoint docstring) and moves it into the `review` column right after
the existing lineage readback succeeds. The move is verified by reading the
column back through `authoritative_claimability(review_id, fresh=True)`, a
different path than the one that wrote it, never by trusting the `coord move`
exit code alone: a CLI that reports success and persists nothing is a
documented failure mode in this stack. If the move fails, or the readback
does not show `review`, the review id is added to `_REVIEW_READBACK_BLOCKED`
and a new `OPEN_REVIEW_COLUMN_FAILED` line is logged, matching how the
neighbouring readback failure paths already behave.
