Narrow `_generation_invalidated` so a later link event invalidates a producer's
review binding only when some reader would actually compute a different outcome
because of it.

The function previously refused on ANY later `action == "link"`. That refuses on
events no reader interprets, which is not caution but a category error: it
converts annotation into destruction. Measured on the chi estate, 199 of 230
candidate parents were refused here and 178 of those by that bare catch-all. The
clearest case is the fleet invalidating itself: eight cards carry complete,
disk-verified, fully typed candidate evidence written by the new `coord verdict`
command, and every one of them is blocked by a `worker_liveness` heartbeat
written 13 to 34 seconds later by the producer or by niobe.

The replacement is derived from the interpreters rather than from what a key name
means to a reader, so the two cannot drift apart. A link now invalidates when it
is outcome-shaped AND its value parses as an outcome (an outcome-shaped key alone
is not enough, since `verdict_artifact` carries paths), when its folded key is
`blocked_on`, when it is a well-formed 64-hex `evidence_sha256` or
`blocked_evidence_sha256`, when it is a non-empty `independent_review`, or when it
is a `review_join` that fails the existing self-referential exemption. Everything
else is inert to outcome selection and no longer invalidates.

`evidence_sha256` stays refused deliberately even though it reads like pure
annotation: it is the exact field `_load_outcomes` records a chained BLOCKED
outcome on, including a chain whose earlier parts predate the verdict. That is
why an allowlist of "safe looking" keys would have been wrong.

Measured against live data before merge: 111 of 199 cards are freed from
invalidation and 8 gain full eligibility to open a review. The other 103 fail
immediately at `_provisional_candidate` because they never recorded a typed
binding at all, which is a producer-side gap this change does not touch. It still
correctly refuses a second fuller outcome link 1.4 seconds later, a genuine
64-hex `evidence_sha256`, and a `blocked_on` regardless of value.

What it does not catch, stated plainly: a backdated event bypasses the boundary
compare entirely, and writers and link keys are unauthenticated free text, so
this defends honest concurrency rather than a malicious writer.
