# Claim TTL with heartbeat extension

Status: design, 2026-09-18. Amendment C to the nimble-factory spec
(`2026-09-16-nimble-factory-design.md`). Supersedes nothing; it is the
production-pain half of the exclusion work that Amendment B
(`2026-09-17-seat-exclusion-amendment.md`) does not address.

## The problem, measured

On the chi cluster, **371 card claims are open and will never close**.

    median age            189.5 hours  (7.9 days)
    maximum age           553.2 hours  (23 days)
    over 24h                     371   (all of them)
    over 7 days                  213
    distinct owners              260

Measured on chiap01, 2026-09-18, by replaying every `events/*.jsonl` under
`~/.skcapstone/cards` and counting cards where `claim` outnumbers
`release_claim + unassign` with no terminal event.

This is the failure Chef reports as "multiple running and ones dead and not
running keeping cards open then they go into a dead state and never picked
back up". The card is not lost, it is held. Nothing dispatches it again.

## Root cause

`reap_dead_claims()` in `scripts/fleet/skfleet-rotate.py` releases a claim
only after, in its own words, *"every authoritative host reports absence"*
of the owner.

That predicate cannot be satisfied for most owners in the store:

1. **The evidence is host-local.** Worker liveness lives in
   `~/.skcapstone/fleet/status/<node>/heartbeat.json`. Only 3 such files
   exist on chi, all at NODE granularity. A node heartbeat asserts "this
   host is up", never "this claim's owner is up".
2. **Most owners have no process to find.** Of 260 distinct stuck owners,
   the large majority are one-shot identities that existed for a single run
   (`pi-codex-chiap02-f17d9e32`, `codex-review-pr407-94998d5b`), each
   holding exactly one card. The rest are session identities (`jarvis` 65,
   `codex` 29, `seraph` 5) that never had a dedicated process at all.
3. **Absence is not provable in a partitioned system.** A host that is
   down, unreachable, or simply slow to sync cannot "report absence". The
   predicate is unsatisfiable exactly when it matters most.

Requiring proof of death is the defect. Nothing can supply that proof, so
the reaper correctly refuses forever, and claims accumulate without bound.

## The design: require proof of life instead

Invert the burden. A claim carries an expiry. A worker that is alive says so
by extending it. A worker that stops saying so loses the claim, and no host
has to prove anything about a process it cannot see.

This is the standard resolution for distributed leases, and it is the same
shape as the seat lease in Amendment B, applied one level down: to the card
claim rather than the seat.

### Three changes

**1. A claim event carries an expiry.**

Claim events today carry `event_id, ts, writer, node, seq, action, owner,
claim_revision, transition_id, prev_hash`. Add one field:

    expires_at    ISO-8601 UTC, absolute, set by the claiming host

Absolute rather than a duration so that a reader never has to know the
writer's TTL policy, and so a mixed-version fleet agrees on the deadline.

**2. A new `beat` event extends it.**

    {"action": "beat", "owner": "...", "expires_at": "<new deadline>"}

`beat` is a **card event**, not a host-local file. This is the load-bearing
choice in the whole design: host-local evidence is precisely what makes the
current reaper unable to act. A card event replicates through the same
Syncthing path the claim itself took, so any host can read it.

Cost is bounded by beating at `TTL/3`, not continuously. At the proposed
6h TTL that is one event every 2 hours: about 30 events for a 61-hour
worker, against the 3,726 claim events the store already holds.

**3. The reaper gains an expiry path.**

The existing absence-proof path stays exactly as it is, and keeps its
current authority. Alongside it: a claim whose `expires_at` has passed may
be released by any host, with no absence proof required. The release is an
ordinary `release_claim` event carrying `reason: "lease-expired"` so the
action is auditable and distinguishable from a worker's own release.

### Choosing the TTL

The TTL must exceed the longest legitimate gap between two beats, or live
work gets stolen. Measured on chi, workers legitimately run a long time:

    chiap04   8 live `pi` workers          61h 51m
    chiap01   codex                       251h 30m
    chiap01   skfleet-working watch loop  376h 44m

Note these are process lifetimes, not beat gaps. Once beating exists, the
relevant number is the gap, which is bounded by the beat interval plus
scheduling jitter. **6 hours** is proposed: 3x the 2-hour beat interval, so
two consecutive missed beats are tolerated before a claim is reclaimable.

The TTL is configuration, not a constant, so it can be raised without a
code change if measurement disagrees.

## Rollout: observe before enforcing

A TTL that reclaims from a worker which has not yet learned to beat is
strictly worse than the current bug. So enforcement ships off.

**Phase 1, emit only.** Claims carry `expires_at`; workers beat. Nothing
reclaims. Run until the fleet shows beats arriving for every live worker.

**Phase 2, report only.** The reaper logs what it *would* reclaim and emits
no release. Compare that list against known-live workers. A live worker
appearing in it is a phase-1 defect and blocks phase 3.

**Phase 3, enforce.** Reclaiming turns on, per host, behind a switch.

The gate between phases is evidence from the fleet, not elapsed time.

## What this does not do

- It does not touch seat exclusion. That is Amendment B, card `f8865032`,
  deliberately sequenced after this.
- It does not retroactively fix the 371 existing stuck claims. Those
  predate the field and will never gain one. They are cleared by a one-time
  sweep, scoped and gated separately (see below).
- It does not make `~/.skcapstone` synchronous. Replication lag is still
  10 to 20 seconds, which is why the TTL is hours and not seconds.

## The one-time sweep

The existing 371 are cleared once, by hand, under a conservative rule:
release only where the owner is **provably not live**.

    total stuck                                          371
    safe (older than 7d AND owner not live on any host)  155
    held back                                            216

The 155 are unique one-shot identities holding one card each, whose owning
process no longer exists anywhere on chi. The 216 held back are:

- 19 claims younger than 7 days
- 99 owned by ambiguous session identities (`jarvis` 65, `codex` 29,
  `seraph` 5) which cannot be distinguished by name from a live process:
  chiap01 is running a `codex` that has been up for 251 hours
- the remainder owned by names that substring-match a live process

Ambiguous owners are reported for a human decision, never swept
automatically. `jarvis` holding 65 cards is a standing question about a
seat being vacated, not a stuck-claim question.

## Success criteria

1. A claim written after phase 1 carries `expires_at`.
2. A live worker's claim survives indefinitely, proven by a worker that
   outlives its own TTL while beating.
3. A worker killed mid-claim has its card reclaimed within TTL + one reaper
   cycle, with no host proving absence.
4. The absence-proof path still releases what it released before.
5. Phase 2 produces an empty would-reclaim list for every live worker.
