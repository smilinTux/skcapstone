# Seraph review dispatch

Link materializes canonical review cards from signed lineage observations. The
card identity binds the source card, exact source head, source generation, and
candidate evidence hash. Existing matching cards are reused and ambiguous
duplicates fail closed.

Every source-only review card also carries the exact workspace repository and
base reference from its signed Link recommendation or folded parent. The
repository must be credential-free HTTPS and the base reference must satisfy
the bounded Git-ref syntax. Link validates both before creating a card,
idempotently adds missing bindings to an existing canonical card, and rejects
recommendation-parent conflicts or parent drift before authorization. This
inheritance adds no approval authority and does not rewrite identity, source
head, generation, evidence, claim, or launch receipts.

Seraph runs as a bounded recurring seat on the active control-plane host. Each
cycle invokes the ordinary fleet selector with `SKFLEET_ONLY_SEAT=seraph` and
uses `SKFLEET_SERAPH_BATCH_SIZE` for both its dedicated target and launch cap.
The packaged default is two, values from one through eight are accepted, and
the shared `SKFLEET_CODEX_PHYSICAL_LIMIT` remains the final physical ceiling.
The default model is `sk-codex-mid`; governed routing may escalate an individual
card when its declared work requires it. The selector then performs the existing final
admission comparison, Link recommendation, exact claim readback, worker launch,
and launch-receipt checks for every item. Seraph reports complete success only
when every canonical `LAUNCHED` receipt matches the Link recommendation, exact
claim revision, producer-independent review card in `doing`, and active worker
unit. It reports a partial result when only part of the batch validates. A failed
launch must carry its exact claim revision, have a matching negative launch
receipt, and have that exact generation released after the receipt. CardStore
must then confirm a fresh unowned, nonterminal, dependency-complete, claimable
state. Generation mismatch, stale release order, ownership, terminal state, or
a claim-excluding label fails closed. Duplicate cards or two
cards for the same source-card and head-revision pair fail validation. Zero
eligible work and zero remaining physical or provider capacity emit
distinct `NOOP_RECEIPT` reasons and are honest successful no-ops. A missing,
duplicate, or malformed receipt, stale claim, dead process, or invalid item is a
suppressed failure without discarding valid sibling results. A producer cannot review its own candidate, and state
drift or recommendation replay prevents launch.

Canonical review cards remain excluded from every generic fleet cycle. In an
exact `SKFLEET_ONLY_SEAT=seraph` cycle, POOL_V2 may admit a backlog review card
only when its `review` and `seat-seraph` labels and complete producer and
candidate-evidence bindings pass the governed review parser. That Seraph-only
admission fact is part of both the selected and final preclaim fingerprints.
Any changed event, incomplete binding, wrong seat, or generic cycle therefore
remains non-dispatchable.

The regression test creates its canonical review card through production
`reconcile_review_work`, then runs the actual selector in one isolated child
process against a temporary CardStore. Stateful `systemctl` and `systemd-run`
shims prove the generic cycle does not claim or launch, the Seraph cycle makes
bounded real claims and active-unit launch receipts, and replay creates no
duplicate. Unit tests also prove partial launch failure, retryability, batch
configuration bounds, source-head dedupe, and producer independence. Test paths are discovered from the running interpreter and imported
packages so hosted CI does not depend on a workstation home directory. A
loopback gateway serves the production `/health` and `/queue` schemas, and the
portable revision probe returns the same sealed runtime revision, so the test
exercises real lane snapshot acquisition and fail-closed admission without
depending on a workstation gateway.

The packaged `skfleet-seraph.service` has a five-minute offset timer and a
five-minute service timeout. Link and Seraph retain separate cycle locks, while
CardStore creation and claim fencing provide cross-cycle convergence.
Seat-scoped selector cycles do not overwrite the generic fleet liveness and
capacity snapshot.

Installation must update the Python package, `~/.local/bin/skfleet-rotate.py`,
and the Seraph service unit from one reviewed commit, recording old and new
SHA-256 hashes before the timer is restarted. Rollback restores all three
pre-install bytes and runs `systemctl --user daemon-reload`; it does not delete
receipts. Emergency rollback is to disable `skfleet-seraph.timer`, remove
Seraph from the seat placement and control-plane records, and revert the source commit. Existing
append-only review evidence remains historical and is not deleted.
