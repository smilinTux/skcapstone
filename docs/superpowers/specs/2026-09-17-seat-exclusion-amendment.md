# Amendment B: seats cannot be depinned without a cross-host exclusion primitive

**Status:** decision required from Chef. No code in this amendment.
**Amends:** `2026-09-16-nimble-factory-design.md` sections 3.6 and A.5.
**Cause:** Plan B2 Task 1, measured 2026-09-17.

## What A.5 assumed, and why it is wrong

A.5 proposes deleting `active_host` and replacing it with the CardStore claim
fence, on the grounds that the fence is "already exact-revision fenced, and that
fence is what stops two workers taking one card across five hosts".

It does not stop that. Measured:

```
~/.skcapstone on chiap01     /dev/mapper/ubuntu--vg-ubuntu--lv  ext4, LOCAL
~/.skcapstone on chiap08     /dev/mapper/ubuntu--vg-ubuntu--lv  ext4, LOCAL
same card 000068c8           inode 46946198 vs 54037090, identical content
```

The fence is `fcntl.flock` on the card's own directory
(`skcoord/card_store.py:2318`), taken for the fold-check-append sequence. Each
host holds a separate local ext4 copy of that directory, replicated
asynchronously by Syncthing. A kernel-local advisory lock cannot exclude another
machine. `acquire_card_admission` says so in its own docstring: "Atomically admit
one worker for an exact card ON THIS HOST."

A test at `tests/test_seat_cycle_exclusion.py` proves both halves: against one
physical store exactly one of two racing claims wins, and against two
independently replicated stores both hosts win their own local claim on the same
card.

**So `active_host` is not redundant. It is the only cross-host exclusion the
estate has.**

## This is the estate's established pattern, not an oversight

`_resolve_authority_host` (`skfleet-rotate.py:144`) already applies the same rule
to shared reports, with the same reasoning:

> "letting all of them write is N-way contention over identical bytes, and on a
> Syncthing folder that produces conflict copies rather than content. Exactly one
> host publishes and the rest read."

A.5 was written as though single-writer election were an accident to be cleaned
up. It is a deliberate answer to Syncthing's semantics, already accepted for a
neighbouring problem.

## What the depinning was actually for

Worth separating, because it changes the cheapest answer. Seats are bounded
five-minute one-shots under timers, not daemons, so the goal is not throughput.
The value of "any host can run any seat" is **failover**: if the elected host is
down, the seats stop, and today nothing recovers that automatically.

That is a smaller requirement than per-card distributed locking.

## Options

**Option 1: keep the static election. Do nothing.**
Cost: a single host's outage stops all five seats until a human edits the control
plane. Benefit: zero new failure modes, and consistent with `AUTHORITY_HOST`.

**Option 2 (recommended): keep one host at a time, make WHICH host dynamic.**
Replace the static string with a lease that one host holds and refreshes, and
that another host may take over when it expires. Exclusion stays coarse, one
lease per estate rather than one per card, so the surface is a single value with
a TTL rather than a distributed lock protocol. `active_host` becomes the lease
holder rather than a hand-edited constant, and the hard refusal in
`seat_cycle_entrypoint.py:172-185` keeps its shape: it still asks "am I the
host", the answer just stops being static.
Cost: needs one store both hosts can read and write synchronously. Syncthing
cannot serve this; its asynchronous replication is the reason the flock fails.
Candidates already in the estate: FalkorDB at 192.168.0.59:16379, or an endpoint
behind skgateway (reachable, HTTP 200 on :18780). Either introduces a dependency
on the seat path, so the lease must fail CLOSED: no lease means do not run, never
"assume I hold it".

**Option 3: route all claims through one authority host over HTTP.**
Cost: the gateway becomes a hard dependency of every claim, not just seat
cycles, and it is already a single point of failure for routing. This buys
per-card exclusion the estate does not currently need, at the price of putting a
network hop in the hot path.

## Recommendation

Option 2, and only after Plan B3. B3 gives ATLAS the rollout controller role and
a deployment manifest, which is what makes "is what we merged actually running"
answerable per host. A lease whose holder might be running different code than
its challenger is worse than a static pin, because the failure is intermittent
rather than visible.

Until a decision, seats stay pinned. Plan B2 Tasks 4 and 5 are dropped, not
deferred: they should not be picked up as written, because their premise is
false.
