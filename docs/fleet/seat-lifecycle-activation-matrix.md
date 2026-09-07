# Lifecycle seat activation matrix

**Scope:** SKCapstone, SKDashboard, and SKWorld software lifecycle only.

This matrix is the operational bridge between the seat contracts in
`sk-standards` and the SKCapstone worker runtime. It does not grant authority
by itself. The seat manifest, current card, capability check, exact revision,
and evidence contract remain authoritative.

## Runtime posture

| Seat | Current state | Normal scheduling | Owns | Must never do in this state |
| --- | --- | --- | --- | --- |
| Link | `provisioned` | Bounded recurring cycle on chiap08, feed-gated and currently disabled | PR triage, independent reviewer assignment, merge eligibility recommendation, bounded merge queue | Fleet claims, worker launch or release, deployment, credentials, protected data, application actuation |
| Mero | `provisioned` | Bounded read-only census timer enabled on chiap08 | Convergence observation, drift measurement, blocker detection, typed recommendations | Claims, reassignment, fleet mutation, merge, deployment, or actuation |
| Niobe | `active_bounded` | Five-minute live dispatcher and read-only shadow beat enabled on chiap08 under Casey decision `casey-c4e7a9b2-20260906`, expiring 2026-10-06T22:00:00+00:00 | Claim, release, launch, stop, and reassign only for SKCapstone, SKDashboard, and SKWorld lifecycle cards | Merge, deploy, application actuation, and external dispatch always denied |
| Tank | `provisioned_not_activated` | Card-scoped worker only | Exact approved artifact install, release receipt, behavioral deployment verification, pinned rollback | Self-approval, arbitrary claims, artifact substitution, independent review of its own work, application actuation |
| Seraph | `provisioned_not_activated` | Card-scoped worker only | Exact candidate, release, deployment, health, and rollback verification; publication of its own exact-head PASS review | Merge, dispatch, deployment, release, claim, or actuation |
| ATLAS | `frozen` | No healthy lifecycle beat | Typed operations observation only until separately authorized | Coordination-board ownership, card claims, review, merge, release, or action while frozen |
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

All seats use `sk-codex-mid` and `gpt-5.6-luna` by default. A stronger model
is permitted only as a card-scoped, documented boost for a demonstrated
intelligence need. It does not change authority, data access, or human-gate
policy, and Casey is notified rather than interrupted.

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

Niobe remains shadow-only until parity, identity, mailbox, dry-cycle, handoff,
revision-fencing, rollback, and independent-review evidence all pass. Shadow
beats and recommendations proceed without a human gate. Activation is a
separate authority change and is the only point at which live fleet mutation
becomes available.

When activated, Niobe must re-read CardStore ownership and claim revision and
the current process state immediately before every claim release, launch, stop,
or reassignment. It rejects duplicate recommendation IDs and stale or
mismatched evidence.

### Tank

Tank may execute only an exact card with an approved source hash, complete
dependencies, capability confirmation, fresh readback, independent review, a
release receipt contract, and a pinned rollback path. Tank cannot approve its
own work or substitute an artifact. Production authorization remains a true
external-authority gate; non-production canaries and rollback rehearsals are
notify-only.

### Seraph

Seraph receives the exact candidate and verifies source hash, version, health,
deployment behavior, rollback behavior, and idempotent rerun evidence. Its
PASS, FAIL, or BLOCKED result is independent of Tank and cannot itself release,
deploy, merge, or authorize the candidate. For a named card, a hash-sealed
PASS may be published as a GitHub review after request-bound authoritative
CapAuth verification, exact-head, card-revision, immutable evidence-byte,
terminal-green-check, and distinct-identity checks. The review is only the
external record of Seraph's verdict. Link alone evaluates merge eligibility
and merges. Receipt replay repeats live CapAuth, CardStore, branch-protection,
required-check, connector-scope, PR-head, evidence-file hash, and GitHub-review
validation before returning. The live review card supplies the evidence path
and SHA256; the publisher accepts only a regular non-symlink file beneath the
approved evidence root. A missing CardStore receipt event is a failed
publication, even when the filesystem receipt or GitHub review already exists.

### ATLAS

ATLAS remains frozen until its separate operations contract, capability scope,
freeze-readiness evidence, and action authorization path are qualified. It may
retain identity and mailbox access for coordination, but it emits no healthy
lifecycle beat and receives no normal lifecycle card.

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

Link and Mero emit one beat per short cycle. Tank and Seraph emit beats only
while executing a card, including a one-shot review-publication card. Niobe emits shadow beats without mutation. Frozen ATLAS
does not emit a healthy lifecycle beat. Missing beats create an observation;
they do not automatically release a claim or wake Casey.

## Human interruption ceiling

Routine observation, documentation, mailbox handling, evidence collection,
shadow work, non-production canaries, rollback rehearsals, and independent
verification are notify-only. Interrupt Casey only for external authority,
production authorization, protected-data egress, legal or financial
commitment, irreversible material effect, or a role-contract exception.

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
systemd-analyze verify systemd/skfleet-link.service systemd/skfleet-link.timer systemd/skfleet-mero.service systemd/skfleet-mero.timer systemd/skfleet-niobe-shadow.service systemd/skfleet-niobe-shadow.timer
```

The Link timer remains disabled until the reviewed producer has a complete live
lineage manifest and a dry run has produced a fresh valid feed. Mero and the
Niobe shadow beat may run because they are bounded read-only observation paths.
