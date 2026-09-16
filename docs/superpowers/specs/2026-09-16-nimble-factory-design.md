# Nimble Factory Design

**Status:** Proposed
**Date:** 2026-09-16
**Authors:** Mero (measurement), Chef (direction)
**Supersedes nothing. Amends:** ADR-0005, ADR-0006 (seat count and dispatch host)

## 1. Problem

The factory produces work faster than it can close it, and the visible symptom
was read as a throughput stall. It is not one.

Measured 2026-09-16:

| Signal | Value |
|---|---|
| SKLegal weekly completions | 43, then 122, then 599, then 888 (accelerating) |
| SKLegal open residue | 444 cards, growing about 23/day |
| Open PRs, skcapstone | 284, median age 13.1 days, 130 older than 14 days |
| Reviews on last 60 merged PRs | 0 |
| CI wall clock | about 1 minute (max observed 0.9) |
| PR cycle time | median 41 minutes, p90 14.7 hours |

Two hypotheses held before measurement were wrong, and both were wrong in the
project's favour:

- **CI is not slow.** It is one minute. The latency is the PR wait, not the checks.
- **Human gating is not the bottleneck.** Human gates are 14 of 444 open SKLegal
  cards, which is 3 percent.

### 1.1 Ranked causes, by measured size

| Rank | Cause | Open SKLegal cards | Share |
|---|---|---|---|
| 1 | Acceptance criteria naming an external actor | 160 | 36% |
| 2 | Unsatisfied or cyclic dependencies | 95 | 21% |
| 3 | Abandoned with no recorded reason | 235 | 53% |
| 4 | Human gates | 14 | 3% |

Shares exceed 100 percent because a card can carry more than one cause.

Cause 1 is structural. A worker cannot satisfy a criterion such as *"Independent
review PASS on the successor commit before merge"*, so it claims, works, fails
the criterion, releases, and repeats. Card `06a95c23` did this **402 times**.

Cause 3 is the most important finding in this document: **the ledger does not
record why a claim was abandoned.** Over half the open residue has no
attributable cause. A factory cannot improve what it does not write down.

### 1.2 Review is real, but it does not live in the PR

Zero of 60 merged PRs received a review, and all were self-merged by their
author. Meanwhile seraph produced 874 card events with exact-head PASS and FAIL
verdicts in seven days.

Review happens in cards and the evidence store. **The PR carries latency and a
self-merge record, not review.** This is what makes the PR-per-change policy
safe to remove.

### 1.3 Deployment drift is a live, causal defect

`skfleet-niobe-shadow.service` has been failing since 2026-09-10 with
`No module named skcapstone.seat_shadow_entrypoint`.

The module is git-tracked, committed, and has tests
(`tests/test_seat_shadow_entrypoint.py`), as do `niobe_activation` and
`niobe_live_entrypoint`. Systemd units for both niobe modes are tracked. The
installed package on chiap08 is 11 commits behind repository HEAD and predates
the module.

**Niobe is built. It does not run because the deployment is stale.** The seat
chartered to answer "is what we merged actually running?" is atlas/tank, which
produced 8 and 26 card events respectively in seven days. The seat that would
have caught this is the one that is barely alive.

## 2. Goals

1. Make a card closable by the worker that claims it.
2. Record why work stops, always.
3. Remove ceremony that buys nothing, without removing review that works.
4. Run seats on fleet workers rather than a pinned host.
5. Keep full provenance. Provenance is the thing being protected, not traded.

### 2.1 Non-goals

- Adopting spec-kit's `/specify`, `/plan`, `/tasks` artifact chain. CardStore
  already does that job, and layering a second artifact tree over 7,147 cards is
  ceremony this design exists to remove.
- Adopting spec-kit's `extensions.yml` hook system.
- Deleting seraph or link. Both demonstrably work and produce the only real
  quality signal in the estate.
- Rewriting closed cards. History is append-only and stays untouched.

## 3. Design

### 3.1 Card schema: separate what the worker owns from what it does not

`acceptance_criteria` today conflates two different things. Split them.

```jsonc
{
  "acceptance_criteria": [...],   // EXISTING. Narrowed: worker-owned only.
                                  // Must be self-satisfiable and falsifiable.
  "exit_gates": [                 // NEW. Owned by another seat.
    {"gate": "independent-review", "owner": "seraph", "ref": "parent-5a7e5f41"}
  ],
  "non_goals": ["no deployment", "no credential access"],   // NEW
  "abandon_reason": null,         // NEW. Written on release. See 3.3.
  "spec_version": 2               // NEW. Absent means v1 legacy behaviour.
}
```

`exit_gates` entries are objects, not prose. The dispatcher needs `owner` to
route the gate and `ref` to know what satisfies it. A prose string cannot be
checked mechanically.

`spec_version` makes adoption per-card and reversible. Absent means legacy, and
no new gate applies. This avoids a flag day across 7,147 cards.

**Evidence for this split.** Card `5a7d31ce` carried seven criteria, every one
falsifiable by a test, plus an explicit non-goals line. It completed on its first
claim. Cards `06a95c23`, `114e513a` and `cce79eff` each carried criteria naming a
reviewer or a merge. They recorded 402, 85 and 34 claims and zero completions.

### 3.2 Dispatch state: a worker-terminal state that is a ledger fact

```
ready -> doing -> awaiting-gates -> done
                        ^
              worker terminal: acceptance_criteria satisfied,
              exit_gates outstanding
```

`awaiting_review()` exists today but infers the state by regex-matching a PASS
value in the outcomes store. The new state is written as a card event
(`action: "await_gates"`), so it is a ledger fact rather than an inference from
an artifact the failing path may never write.

A card in `awaiting-gates` is not claimable. The worker is finished. The gate
owner is now responsible.

### 3.3 Record why work stops

Every `release_claim` event MUST carry an `abandon_reason` from a closed
vocabulary:

```
criteria-unsatisfiable   the worker cannot satisfy a stated criterion
dependency-unsatisfied   a dependency is not met
capability-missing       the worker lacks a tool, credential or host asset
error                    the worker failed; message recorded
superseded               another worker or a human took the work
```

This closes the 53 percent unexplained residue. It is the cheapest change in
this document and the one with the longest-lived value, because every future
question about factory health becomes answerable from the ledger.

### 3.4 Claim ceiling independent of launch evidence

`blocked_backoff()` is a real gate in the selection loop. It did not stop
`06a95c23` because `launch_attempts()` reads worker logs and the outcomes store,
and that card produced **zero worker logs across 402 claims**. The backoff is
keyed on evidence the failing path never writes, while the CardStore ledger
recorded all 402 claim events reliably.

Add a ceiling keyed on **claim events**, which are always written:

- More than `SKFLEET_MAX_CLAIMS` (default 5) claim events within
  `SKFLEET_LAUNCH_TTL_H` and no `complete` and no `await_gates` means the card
  is not selectable, with reason `claim-ceiling`.
- The existing "unless the world changed" escape applies unchanged, so this is
  not a one-way door.

This one change would have prevented 394 of those 402 claims.

### 3.5 PR policy: branch push by default, PR per batch

Measured trigger analysis of the six skcapstone workflows:

```
fires on any branch push    docs-check, secret-scan        about 0.4 min total
fires on PR or main only    ci, pytest, providers, publish
```

New default:

1. Worker commits locally in its worktree.
2. Worker pushes a **branch**. Cost is two fast checks, not the full suite.
3. Worker records the exact commit SHA in the evidence store, which is already
   syncthing-replicated (3,355 candidates on chiap01, 3,356 on chiap08).
4. A PR is opened **once per batch**, or immediately when the change touches a
   gated category (credentials, custody, keys, deploy, release, migration).

Cross-host visibility, the original justification for PR-per-change, is provided
by the branch push plus the replicated evidence record. Worktrees stay
host-local, which is fine, because the branch is the handoff.

Provenance is preserved and arguably improved: today's provenance is a
self-merge record, and the proposed provenance is an exact SHA bound to a card
and an evidence digest.

### 3.6 Seats: seven to five, and onto fleet workers

| Seat | Decision | Rationale |
|---|---|---|
| seraph | Keep unchanged | 874 events, real verdicts, the only working quality signal |
| link | Keep unchanged | 262 events, real merges and reviewer assignment |
| niobe | **Deploy, then take dispatch** | Built and tested; blocked only by stale install |
| atlas | **Absorb tank** | ADR-0005 names ATLAS as Operations; port tank's on-charter behaviour into it |
| mero | **Cap and constrain** | 4,414 recommendations per week is unreadable; also holds DOING card `abe011e9` against its own charter |
| tank | Folded into atlas | 26 events; one part-time seat with atlas |
| jarvis | Stand down from dispatch | Per ADR-0006, once niobe is verified running |

**Seats move onto fleet workers.** Seats are already bounded five-minute
one-shots under timers (ADR-0006 section 6), not daemons, so they are a natural
fit for fleet dispatch. `seat-control-plane.json` currently pins every seat to
`chiap08`; that pin is replaced by fleet dispatch with a host-agnostic claim.

**Cold-start constraint.** Dispatch cannot bootstrap itself through the thing it
dispatches. Niobe therefore keeps a minimal timer-based presence on two hosts as
a supervisor of last resort, and everything else moves to fleet work. This is
stated as a constraint rather than deferred, because a dispatcher that can
deadlock on its own absence is the failure mode this section must avoid.

### 3.7 Break the dependency cycles

23 cycles exist, essentially one pathology: `SKLEGAL-SECRET-COHORT-SLICE-R1`
leaves and their parent "Resolve or quarantine" cards (`b72105c1`, `3fe63a97`)
depend on each other. 35 open cards are in or transitively blocked by a cycle,
and 6 of the 11 worst claim-thrashers are cycle-blocked.

Fix in two parts:

1. **Data.** Remove the parent-to-leaf dependency edge, keeping leaf-to-parent.
   A parent must not depend on its own children; that is what makes the cycle.
2. **Prevention.** `add_dependency` rejects an edge that would create a cycle,
   returning reason `dependency-cycle`. Cycle detection on a DAG of this size is
   trivial and belongs at write time, not discovery time.

## 4. Migration

```
new cards           created at spec_version 2; advisory criteria check at creation
existing open       backfill only cards still open AND claimed 3 or more times
                    (the measured tail, not all 160)
existing closed     never touched
abandon_reason      required for new release_claim events only; historical
                    events stay as they are
```

Backfill is a split, not a rewrite: criteria matching gate language move from
`acceptance_criteria` into `exit_gates`, and the rest stay. It is reversible,
because the event ledger is append-only and retains the pre-split state.

## 5. Rollout order

Ordered so that nothing depends on something not yet proven.

1. **Deploy skcapstone to seat hosts.** Unblocks niobe with no code change.
   Verify `skcapstone.seat_shadow_entrypoint` imports before proceeding.
2. **Claim ceiling (3.4).** Small, self-contained, immediately measurable.
3. **abandon_reason (3.3).** Starts collecting the data that explains the 53
   percent, so later decisions rest on evidence rather than this document.
4. **Schema split (3.1) and awaiting-gates (3.2).**
5. **Cycle fix (3.7).**
6. **Niobe takes dispatch; jarvis stands down.**
7. **PR policy change (3.5).**
8. **Seat fold and mero cap (3.6).**

Steps 1 through 3 are independently valuable and can each be reverted alone.

## 6. Verification

| Change | Measure | Success |
|---|---|---|
| Claim ceiling | max claims on any open card | no card exceeds ceiling plus 1 |
| abandon_reason | share of release_claim with a reason | above 95 percent within 7 days |
| Schema split | open cards with external-actor criteria | 160 falling, no new ones created |
| Cycle fix | dependency cycles detected | 0, and `add_dependency` rejects new ones |
| PR policy | median PR cycle time, open PR count | count falling from 284 |
| Niobe | niobe card events per week | above zero, jarvis dispatch events falling |
| Seat fold | atlas events per week | at least tank plus atlas combined today (34) |

## 7. Risks

| Risk | Mitigation |
|---|---|
| Niobe takes dispatch and fails | Shadow mode first; jarvis path stays warm until niobe shows a full week above zero |
| Claim ceiling parks work silently | Reason is reported in `SELECTION_EMPTY`, and mero's digest ranks parked cards |
| Branch pushes without PRs are never merged | Batch PR is scheduled work with an owner, not best-effort; open-branch age is measured |
| Backfill mis-splits a criterion | Only cards claimed 3 or more times, and the ledger retains the original |
| Cold-start deadlock on fleet seats | Niobe keeps a minimal timer presence on two hosts (3.6) |

## 8. Open questions

1. Who owns the batch PR, and on what cadence? Link is the natural owner as
   Integrator, but the cadence is unset.
2. Should `exit_gates` support an `expires_at`, so a gate nobody satisfies does
   not park a card forever? Deliberately deferred until `abandon_reason` data
   shows whether this happens in practice.
