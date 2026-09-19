# Lifecycle seat activation matrix

**Scope:** SKCapstone, SKDashboard, and SKWorld software lifecycle only.

This matrix is the operational bridge between the seat contracts in
`sk-standards` and the SKCapstone worker runtime. It does not grant authority
by itself. The seat manifest, current card, capability check, exact revision,
and evidence contract remain authoritative. See
[sk-standards ADR-0006](https://github.com/smilinTux/sk-standards/blob/HEAD/decisions/ADR-0006-dispatch-handoff-niobe-tank-seraph.md)
for the canonical contract and [seat-charters.md](./seat-charters.md) for the
SKCapstone enforcement detail this matrix summarizes as activation states.

**Roster:** `LIFECYCLE_SEATS` in `src/skcapstone/lifecycle_seats.py` is five
seats: `link`, `mero`, `seraph`, `niobe`, `atlas`. Tank is folded into ATLAS
(spec `2026-09-16-nimble-factory-design.md` section 3.6); it no longer runs,
has no timer, and `skfleet-tank.service` / `skfleet-tank.timer` no longer
ship. `seat_boundaries.Seat.TANK` stays in the authority-model enum only so
Tank's historical board actions keep resolving. See
[seat-charters.md](./seat-charters.md#atlas-release-and-install-duty-inoperable-pending-b3)
for what the fold did and did not carry forward.

## Runtime posture

| Seat | Current state | Normal scheduling | Owns | Must never do in this state |
| --- | --- | --- | --- | --- |
| Link | `provisioned` | Bounded recurring cycle, feed-gated on the elected control host | PR triage, independent reviewer assignment, merge eligibility recommendation, bounded merge queue | Fleet claims, worker launch or release, deployment, credentials, protected data, application actuation |
| Mero | `provisioned` | Bounded read-only census timer | Convergence observation, drift measurement, blocker detection, typed recommendations | Claims, reassignment, fleet mutation, merge, deployment, or actuation |
| Niobe | `provisioned` for the bounded presence cycle; live dispatch only under a current, validated activation record | Bounded read-only presence and mail cycle every five minutes; a separate, expiring live-dispatch timer runs only while `niobe_activation.parse_activation` accepts the current record | Claim, release, launch, stop, and reassign only for SKCapstone, SKDashboard, and SKWorld lifecycle cards, and only while live | Merge, deploy, application actuation, and external dispatch always denied; claim/release/launch/stop/reassign denied while no valid activation record is present |
| Seraph | `provisioned` | Bounded worker on the elected control host, at most one canonical review card per invocation | Exact candidate, release, deployment, health, and rollback verification | Merge, dispatch, deployment, release, claim, approval, or actuation |
| ATLAS | `provisioned`, release/install duty inoperable pending B3 | Bounded presence cycle; card-scoped operations batches admitted through Niobe's selector | Exact-target verification and postcondition evidence for governed releases; release and installation of exact approved artifacts, plus bounded rollback, are ported from the retired Tank seat as a duty on paper only | Coordination-board ownership, card claims, review, merge, or a governed deploy (no seat currently holds `DEPLOY`) |
| Jarvis | `not_a_lifecycle_seat` | Casey-directed assistant presence only | Casey-directed assistance and relay | Self-starting lifecycle work, lifecycle card ownership, impersonation, or replacing a named seat |

## Common readiness contract

Every lifecycle card intended for a seat must identify:

- product scope and lifecycle phase;
- responsible seat and permitted action;
- dependencies and exact current card revision;
- independent reviewer where the action changes an artifact or decision;
- evidence schema, rollback path, idempotency key, and terminal decision.

Before any mutation, the worker must read back the current card, owner, claim
revision, dependency state, capability, and relevant external or process state.
Missing, stale, replayed, or contradictory evidence fails closed. A mailbox
message is coordination evidence, never workflow authority.

All seats default to `sk-codex-mid`. A stronger model is permitted only as a
card-scoped, documented boost for a demonstrated intelligence need. It does
not change authority, data access, or human-gate policy, and Casey is
notified rather than interrupted.

## Seat-specific activation gates

### Link

Link may run recurring observation only when the active-host record is valid
and the mediated observation feed is present, fresh, hash-bound, and complete.
The feed producer may read GitHub through its separately bounded connector;
Link never receives GitHub credentials and never invokes a GitHub client.

Merge eligibility additionally requires the exact PR head, zero failed checks,
an independent full-SHA PASS, no unresolved FAIL or BLOCKED lineage, distinct
author and reviewer identities, and a non-sensitive category. The result is
an append-only recommendation or merge receipt with evidence hashes.

### Mero

Mero may run a read-only census and emit typed observations or recommendations.
It must not repair what it measures. Recommendations include the card,
observation time, observed owner and claim revision, process state, reason, and
evidence hash. Only an explicitly activated Niobe may consume a recommendation
for fleet mutation.

### Niobe

Niobe runs two distinct, separately gated modes:

1. **Bounded read-only presence.** `skfleet-niobe.service` / `.timer` runs
   `skcapstone.seat_cycle_entrypoint --seat niobe`, which sends the startup
   hello, polls mail, and records a bounded no-op beat. It performs no card
   scan, no claim, and no launch, and it needs no activation record. This is
   the always-on floor: Niobe is provably alive and reachable even with no
   live-dispatch authority.
2. **Bounded live dispatch.** `skfleet-niobe-live.service` / `.timer` runs
   `skcapstone.niobe_live_entrypoint`, which loads and validates a
   `skfleet.niobe-activation/v1` record (`skcapstone.niobe_activation.
   parse_activation`) before invoking the dispatcher at all. The record must
   name this exact host and live unit, an unexpired timestamp, the estate
   operator, the bounded five-action set (`claim`, `release`, `launch`,
   `stop`, `reassign`) with merge, deploy, application actuation, and
   external dispatch denied, an exact card fence, and a stated rollback. See
   `scripts/fleet/niobe-activation.example.json` for the machine-readable
   shape and [activation-runbook.md](./activation-runbook.md) for the
   procedure Casey (or the estate operator) follows to author one.

Niobe re-reads CardStore ownership and claim revision and the current process
state immediately before every claim, release, launch, stop, or reassignment.
It rejects duplicate recommendation IDs and stale or mismatched evidence.

**Superseded design, kept here as a record.** An earlier iteration of this
matrix specified a third unit, `skfleet-niobe-shadow.service` / `.timer`,
naming a standalone `skcapstone.seat_shadow_entrypoint` module. That module
never reached `main`; the unit shipped without it, failed the instant it was
enabled, and both were removed in favor of the presence cycle above (see the
`feat(estate): create and diagnose what a fresh estate actually needs` commit
history for `docs/fleet/activation-runbook.md`). The bounded presence cycle
described in mode 1 is the on-main equivalent: it gives Niobe a verifiable,
non-mutating heartbeat before ADR-0006's "verified running" bar is met,
without a second unit naming a module the readiness gate
(`scripts/fleet/skfleet_readiness.py`) and
`tests/fleet/test_no_dangling_systemd_unit_references.py` would have to keep
proving exists. Do not reintroduce `skfleet-niobe-shadow.{service,timer}` or a
`seat_shadow_entrypoint` module without also updating those two guards.

`niobe_activation.ROLLBACK_ACTION` still validates a literal naming the
retired `skfleet-niobe-shadow.timer` in its rollback text
(`disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer`); only
the `disable_skfleet-niobe-live.timer` half is actually enforced by
`ROLLBACK_MUST_DISABLE`. Repointing the full string at the presence timer
invalidates activation records already on disk and needs its own migration;
it is a documented gap, not an oversight.

### Seraph

Seraph receives the exact candidate and verifies source hash, version, health,
deployment behavior, rollback behavior, and idempotent rerun evidence. Its
PASS, FAIL, or BLOCKED result is independent of any producer and cannot itself
release, deploy, merge, or authorize the candidate.

### ATLAS

ATLAS absorbed Tank's on-charter activity (card-scoped release and
installation of exact approved artifacts, with pinned rollback evidence)
alongside its own existing postcondition-verification duty. That ported duty
is inoperable today: no seat holds `DEPLOY`, the dispatch-layer digest check
still gates only the retired `tank` branch, and ATLAS's own rail brief tells
workers not to deploy. See
[seat-charters.md, ATLAS release and install duty: inoperable pending
B3](./seat-charters.md#atlas-release-and-install-duty-inoperable-pending-b3)
for the full accounting before dispatching or approving any release work
through ATLAS. ATLAS's live verification duty runs through Niobe's existing
selector, claim, and launch primitives, admitted only by an exact matching
`seat-atlas` label plus `dispatch-approved`.

### Jarvis

Jarvis is Casey's assistant. Casey may direct Jarvis to create or inspect
cards, relay status, or use the emergency assistance tools, including card,
fleet, merge, deployment, release, verification, and actuation tooling. That
does not make Jarvis a lifecycle owner or permit it to replace the responsible
seat.

## Mail and beat contract

Every seat with an active identity:

1. sends a signed startup hello to `all`;
2. reads its own mailbox and the `all` view before work;
3. acknowledges applicable messages after reading and acting;
4. checks direct and `all` traffic at least every five minutes;
5. looks for help requests, dependency changes, reviewer conflicts, and
   handoffs; and
6. records a bounded beat with seat, activation state, host, card, mailbox
   poll time, and status.

Link, Mero, Seraph, Niobe, and ATLAS all run every five minutes. Niobe's
presence cycle emits a beat without mutation regardless of activation state; a
current, valid activation record additionally allows its live-dispatch timer
to claim, release, launch, stop, and reassign. Missing beats create an
observation; they do not automatically release a claim or wake Casey.

## Human interruption ceiling

Routine observation, documentation, mailbox handling, evidence collection,
read-only observation work, non-production canaries, rollback rehearsals, and
independent verification are notify-only. Interrupt Casey only for external
authority, production authorization, protected-data egress, legal or
financial commitment, irreversible material effect, or a role-contract
exception.

## External practice alignment

The matrix applies the lifecycle practices selected for this fleet:

- protected branches and required checks for integration;
- separate deployment environments with concurrency control;
- small batches and trunk-based integration;
- least-privilege workers and immutable artifact evidence; and
- independent supply-chain and deployment verification.

See [software-lifecycle-practices.md](./software-lifecycle-practices.md) for
the source links and the implementation mapping.

## Verification commands

The following checks are read-only and should be run after any seat or unit
change:

```bash
python3 scripts/fleet/seat-manifest-audit.py
python3 -m pytest -q tests/fleet/test_seat_boundaries.py tests/fleet/test_seat_runtime.py
python3 -m pytest -q tests/test_seat_cycle_entrypoint.py tests/test_link_observation_feed.py tests/test_link_observation_producer.py
python3 -m pytest -q tests/fleet/test_no_dangling_systemd_unit_references.py
python3 scripts/fleet/skfleet_readiness.py \
  --rotate-script scripts/fleet/skfleet-rotate.py \
  --units-dir "$HOME/.config/systemd/user" \
  --python-bin "$HOME/.skenv/bin/python3" \
  --env-from-systemd skfleet-rotate.service
systemd-analyze verify \
  systemd/skfleet-link.service systemd/skfleet-link.timer \
  systemd/skfleet-mero.service systemd/skfleet-mero.timer \
  systemd/skfleet-niobe.service systemd/skfleet-niobe.timer \
  systemd/skfleet-niobe-live.service systemd/skfleet-niobe-live.timer \
  systemd/skfleet-seraph.service systemd/skfleet-seraph.timer \
  systemd/skfleet-atlas.service systemd/skfleet-atlas.timer
```

The Link timer requires a reviewed producer with a complete live lineage
manifest and a dry run that has produced a fresh valid feed before it may be
enabled. Mero and Niobe's bounded presence cycle may run unconditionally
because they are bounded read-only observation paths.
