# Claim TTL with heartbeat extension

Status: design, 2026-09-18. Amendment C to the nimble-factory spec
(`2026-09-16-nimble-factory-design.md`). Supersedes nothing; it is the
production-pain half of the exclusion work that Amendment B
(`2026-09-17-seat-exclusion-amendment.md`) does not address.

## The problem, measured

On the chi cluster, **349 card claims are held and will never be released**.

    held claims (fold says owner set, non-terminal)   349
    owner-idle median                       163.8 hours
    owner-idle minimum                       30.9 hours
    owner-idle maximum                      517.5 hours
    idle under 24h                                    0

Measured on chiap01, 2026-09-18, via `CardStore.fold()` for every card.

The fold is the authority here, and using anything else gets the number
wrong. An earlier pass counted cards where the `claim` action outnumbered
`release_claim + unassign` and reported 371. That arithmetic overcounts: a
worker that re-claims a card it already holds writes a second `claim`, and
one `release_claim` then settles both. Card `f17d9e32` is the worked
example, with `claim: 2, release_claim: 1` and no live owner. Event
counting called it stuck; the fold correctly says nobody holds it.

**"Owner-idle" is the load-bearing measurement**: hours since the current
owner last wrote ANY event to the card it holds. Not one of the 349 has
been touched by its owner in the last 30 hours.

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

**4. A second gate rejects most owners before liveness is even considered.**
`_parse_worker_owner()` (`scripts/fleet/skfleet-rotate.py:3732-3751`)
recognizes only owners shaped `pi-<lane>-<host>-<cid>`,
`<lane>-<host>-<cid>`, or `pi-<seat>-<host>-<cid>`. An owner named
`jarvis`, `codex`, `seraph` or `codex-w72-backend-r3` matches none of them
and is skipped unconditionally, before any liveness or age logic runs.
This gate alone accounts for the 146 largest-held claims (`jarvis` 104,
`codex` 28, `seraph` 5). **A TTL check appended to the bottom of
`reap_dead_claims()` would still never fire for them.** Any fix must bypass
this shape gate, not sit behind it.

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

**2. The deadline is extended by the owner's own card events, not by a new
heartbeat.**

The obvious design is a new `beat` card event. Measurement rejected it:
**the existing beat mechanism is already dead fleet-wide**, and adding a
second one would inherit the same failure.

    ~/.skcapstone/fleet/beats/   1018 files, identical on chiap01/04/08
    newest beat                  27 hours old
    beats fresher than 2h        0, on every host
    beat files for the 8 live workers   0

Worse, every one of those 1018 stale files still reads
`"disposition": "RUNNING"`. A worker's beat loop dies with the worker and
leaves a final RUNNING beat behind forever, so **the last beat is a lie**
and nothing prunes it. The 8 genuinely live workers on chiap04 (`pi
--name skl-w156-*`, up 61h) emit no beat at all, because they were not
launched through the dispatcher heredoc that starts the beat loop
(`scripts/fleet/skfleet-rotate.py:6686-6698`).

So the liveness signal is **the owner's own activity on the card it
holds**, which already exists, already replicates, and requires no new
mechanism and no worker cooperation:

    deadline = (last event written by the owner on that card) + TTL

A worker doing real work writes `move`, `describe`, `evidence`, `verdict`
and `link` events as a matter of course; the store holds thousands of them.
A worker that has written nothing for the whole TTL is either dead or
making no progress, and in both cases the card should return to the pool.

Measurement says this discriminates cleanly. Owner-idle time for the 349
held claims has a **minimum of 30.9 hours**, so a 24-hour TTL separates
every stuck claim from every live one with a 6.9-hour margin, and a
48-hour TTL keeps 327 of them with a 4.9-hour margin at the low end.

**3. The reaper gains an expiry path.**

The existing absence-proof path stays exactly as it is, and keeps its
current authority. Alongside it: a claim whose `expires_at` has passed may
be released by any host, with no absence proof required. The release is an
ordinary `release_claim` event carrying `reason: "lease-expired"` so the
action is auditable and distinguishable from a worker's own release.

### Choosing the TTL

The TTL must exceed the longest legitimate gap between two card events by
a working owner, or live work gets stolen. Workers on chi legitimately run
for a long time:

    chiap04   8 live `pi` workers          61h 51m
    chiap01   codex                       251h 30m
    chiap01   skfleet-working watch loop  376h 44m

Process lifetime is not the relevant number, though. The relevant number
is the event gap, and the measured distribution has a clean floor: the
least-idle held claim on chi is 30.9 hours idle.

**48 hours** is proposed, not the tighter 24. The margin to the observed
floor is smaller in relative terms but the failure is asymmetric: a TTL set
too long leaves a card stuck a while longer, which is the status quo, while
a TTL set too short steals a card from a worker that is mid-run, which is
strictly worse than the bug. 48h reclaims 327 of the 349 immediately and
still bounds the worst case at two days instead of three weeks.

The TTL is configuration (`SKFLEET_CLAIM_TTL_H`), not a constant, so it can
be tightened once phase 2 has produced evidence at 48h.

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

The existing 349 are cleared once, under a conservative rule: release only
where the owner is **provably not live**.

    total held                                        349
    safe (idle >48h, owner provably dead, has rev)    191
    held back: ambiguous session identity             146
    held back: idle under 48h                          20
    held back: no claim_revision to fence against       1

The 191 are one-shot identities whose owning process exists nowhere on chi,
idle between 52 and 518 hours. The sweep is safe by construction rather
than by my measurement being right: `coord release-claim` requires
`--owner` and `--expected-claim-revision` and refuses when a newer claim
generation exists, so a worker that re-claimed since the census causes a
refusal, never a theft.

The 146 ambiguous are session identities (`jarvis` 104, `codex` 28,
`seraph` 5, `tank` 3, `pi` 3, `mero` 2, `link` 1) which cannot be
distinguished from a live process by name: chiap01 is running a `codex`
that has been up 251 hours. These are reported for a human decision and
never swept automatically. `jarvis` holding 104 cards is a question about a
seat being vacated, not a stuck-claim question.

Ambiguous owners are reported for a human decision, never swept
automatically. `jarvis` holding 65 cards is a standing question about a
seat being vacated, not a stuck-claim question.

## Success criteria

1. A claim written after phase 1 carries `claim_expires_at`.
2. A claim held by an owner that keeps writing card events is never
   reclaimed, proven by a worker that outlives its own TTL while working.
3. A worker killed mid-claim has its card reclaimed within TTL + one reaper
   cycle, with no host proving absence of anything.
4. **The expiry path fires for owners that `_parse_worker_owner` rejects.**
   A claim owned by a bare `jarvis` must be reclaimable on expiry; this is
   the case the current reaper cannot reach at all, and a fix that does not
   cover it has not fixed the reported problem.
5. The existing absence-proof path still releases exactly what it released
   before, with the same gates.
6. Phase 2's would-reclaim list contains no owner that is live on any host.
