# Seraph review dispatch

Link materializes canonical review cards from signed lineage observations. The
card identity binds the source card, exact source head, source generation, and
candidate evidence hash. Existing matching cards are reused and ambiguous
duplicates fail closed.

Seraph runs as a bounded recurring seat on the active control-plane host. Each
cycle invokes the ordinary fleet selector with `SKFLEET_ONLY_SEAT=seraph` and a
maximum launch count of one. The selector then performs the existing final
admission comparison, Link recommendation, exact claim readback, worker launch,
and launch-receipt checks. A producer cannot review its own candidate, and
state drift or recommendation replay prevents launch.

The packaged `skfleet-seraph.service` has a five-minute offset timer and a
five-minute service timeout. Link and Seraph retain separate cycle locks, while
CardStore creation and claim fencing provide cross-cycle convergence.

Rollback is to disable `skfleet-seraph.timer`, remove Seraph from the seat
placement and control-plane records, and revert the source commit. Existing
append-only review evidence remains historical and is not deleted.
