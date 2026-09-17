# Lifecycle seat charters

**Status:** ACTIVE
**Date:** 2026-09-09
**Canonical Source:** [sk-standards ADR-0006](https://github.com/smilinTux/sk-standards/blob/HEAD/decisions/ADR-0006-dispatch-handoff-niobe-tank-seraph.md)
**Reference:** [sk-standards ROSTER.md](https://github.com/smilinTux/sk-standards/blob/HEAD/ROSTER.md)

This document aligns SKCapstone runtime enforcement with the canonical seat responsibility contract defined in sk-standards. It is NOT the source of truth, the ADR and ROSTER are, but it translates those definitions into the specific SKCapstone fleet context and documents the enforcement boundaries.

## Canonical roles

The shipped machine-readable source is
`src/skcapstone/data/lifecycle-seat-profiles.json`. Every lifecycle seat is
scoped to SKCapstone, SKDashboard, and SKWorld and defaults to
`sk-codex-mid`. The governed repository set is `smilinTux/skcapstone`,
`smilinTux/skdashboard`, `smilinTux/skworld`, and the supporting standards
repository `smilinTux/sk-standards`. A stronger route is a card-scoped
exception, not a profile default.

| Seat | Owns | Explicitly does not own |
|---|---|---|
| **Fleet Dispatcher** (`niobe`) | Fleet claims, launches, releases, reassignment, rotation, lane routing, and worker health. | Review verdicts, the merge queue, deployment, release, application action dispatch, app actuation |
| **Integrator** (`link`) | Triage, independent-review assignment, the merge queue, and eligible merges under the PR 358 control. Owns delivery quality. | Fleet claims, launches, releases, reassignment, application action dispatch, app actuation |
| **Overseer** (`mero`) | Read-only convergence and drift measurement. Emits typed recommendations, alerts, observations, and briefs. | Fleet mutation, merge, application action dispatch, or any actuation |
| **Independent Verifier** (`seraph`) | Exact-candidate review and PASS, FAIL, or BLOCKED evidence. | Self-review, merge, dispatch, deployment, or actuation |
| **Operations** (`atlas`) | Exact-target verification and postcondition evidence for governed releases, plus release and installation of exact approved artifacts, behavioral verification, and bounded rollback (ported from the retired Tank seat). | Source authoring, self-approval, independent review of its own release, merge, dispatch, coordination-board ownership, card claiming, reviewer assignment, or policy change |

**Tank is folded into ATLAS (spec `2026-09-16-nimble-factory-design.md` section
3.6).** ADR-0005 names ATLAS as Operations, and ATLAS absorbed Tank's real
activity: release and installation of exact approved artifacts, behavioral
verification, and bounded rollback rehearsals. `LIFECYCLE_SEATS` in
`src/skcapstone/lifecycle_seats.py` is now five entries: `link`, `mero`,
`seraph`, `niobe`, `atlas`. Tank no longer runs, has no timer, and the
`skfleet-tank.service` / `skfleet-tank.timer` pair no longer ships.

Retiring the seat is not erasing the actor: `seat_boundaries.Seat.TANK` is
deliberately retained in the authority-model enum. Tank's historical board
actions must still resolve against that enum even though nothing dispatches
work to Tank today. A reader who finds `TANK` still listed there should read it
as intentional, not as unfinished cleanup.

Jarvis is Casey's personal assistant, not a recurring lifecycle seat. Jarvis
retains emergency card creation, claim, completion, fleet, merge, deployment,
release, verification, and actuation tools for explicit Casey-directed help.
That tool availability does not transfer another seat's ownership, permit
impersonation, or bypass a card, policy, exact-revision, or capability check.
Every Jarvis emergency tool is exposed through
`skcapstone.jarvis_emergency.JarvisEmergencyGateway`. Reversible coordination
runs immediately under Jarvis with Casey ownership provenance. Before merge,
deployment, artifact release, or application actuation, the gateway verifies a
signed, unexpired Casey direction bound to the exact action, target, change, and
product scope. Missing, forged, expired, substituted-action,
substituted-target, and out-of-scope directions fail closed before an external
effect. Jarvis receives no recurring lifecycle schedule or standing authority
from this exception.

## Shared operating contract

All five lifecycle seats have a distinct identity and `seat-<name>` card label.
Their source placement is chiap08, and that placement stays host-pinned by
`active_host` in `seat-control-plane.json`; a 2026-09-16 proposal to depin
seats onto any host in the estate did not land, because the CardStore claim
fence it would have relied on cannot exclude a concurrent second host (each
host holds its own local, Syncthing-replicated copy of `~/.skcapstone`, so a
kernel-local `fcntl.flock` cannot reach across machines). See
[Amendment B: seats cannot be depinned without a cross-host exclusion
primitive](../superpowers/specs/2026-09-17-seat-exclusion-amendment.md) for the
measured cause, the options considered, and the recommendation. At startup
each seat writes an ordinary SKMail hello to `all`, then reads its own mailbox
view, which includes direct and `all` traffic. Each bounded cycle polls again for help, handoffs,
dependency changes, and reviewer conflicts. Mail is data, never authority.
Automatic acknowledgement is forbidden because `skmail ack` marks every
visible message read.

Every cycle emits a health record with seat, host, cycle generation, mail
poll result, work counts, and result. Link, Mero, Seraph, Niobe, and ATLAS
all run every five minutes. Each is a bounded one-shot. A host-local
nonblocking lock turns overlap into a recorded no-op. A prior cycle is
abandoned only after exact boot ID, PID, and process start evidence proves its
process generation dead. Receipts survive retirement and no cycle leaves a
persistent child worker.

ATLAS runs bounded batches through Niobe's existing selector, claim, and
launch primitives. Admission requires an exact matching `seat-atlas` label
plus `dispatch-approved`; generic workers cannot consume the lane. The
dispatch-layer metadata check (`_role_seat_metadata` in
`scripts/fleet/skfleet-rotate.py`) requires an exact `verification_target` and
`verification_evidence_sha256` for ATLAS's postcondition-verification duty.
That function still carries a separate, now-unreachable branch keyed on the
retired `tank` seat name that checked `approved_artifact_sha256`; it was not
folded into the `atlas` branch, so a reader should not assume the
release/install duty ported from Tank is gated on an artifact digest at the
dispatch layer today the way it was under Tank. ATLAS observes and records
postconditions, and cannot deploy, dispatch, or actuate outside its scoped
action.
Routine documented action classes are notify-only. A human gate exists only
where the governing catalog, irreversible-effect policy, protected-data rule,
or external authority requires it.

## SKCapstone Fleet Enforcement

### Mero (Overseer) Boundary

**Allowed operations:**
- Read-only observation of CardStore, fleet-rotation evidence, worker logs
- Emission of `skfleet.dispatch-recommendation/v1` events (advisory only)
- Drift measurement, delivery fraction calculation, backlog analysis
- Alert and observation publication

**Prohibited operations (runtime enforcement):**
- Any `claim`, `release_claim`, `launch`, `stop`, or `reassign` actions
- Merge, deploy, or repository write operations
- Fleet mutation of any kind
- Application action dispatch or actuation

**Typed recommendation contract:**
Mero and Link MAY append a `skfleet.dispatch-recommendation/v1` event to a card. The event is advice, never an instruction, and MUST contain:

- `card_id`: The card the recommendation concerns
- `recommendation_id`: Duplicate-suppression key (unique per recommender, per card)
- `recommender`: Identity of the seat making the recommendation
- `observed_at`: Timestamp of the observation
- `observed_claim_owner`: Current claim owner at observation time
- `observed_claim_revision`: Current claim revision at observation time
- `observed_process`: Process state snapshot
- `reason`: Textual explanation
- `evidence_sha256`: SHA-256 hash of supporting evidence

Only Niobe MAY act on a recurring recommendation. Before any claim release, launch, stop, or reassignment, Niobe MUST:

1. Re-read the current CardStore owner and claim revision
2. Re-read the current process state
3. Reject a duplicate `recommendation_id`
4. Reject a missing or mismatched claim revision
5. Reject stale process evidence
6. Reject any action outside Niobe's fleet authority

Acting records the recommendation id, current readback, exact claim revision, result, and evidence hash as an append-only event.

### Link (Integrator) Boundary

**Allowed operations:**
- Triage of pull requests across the estate
- Assignment of independent reviewers (distinct from author and from Link)
- Evaluation of merge eligibility under SKCapstone PR 358 control
- Recording of merge decisions with immutable evidence

**Prohibited operations (runtime enforcement):**
- Fleet claims, launches, releases, reassignment, rotation
- Application action dispatch or actuation
- Deploy, restart, or any runtime service mutation
- Credential, provider, or protected-data access

**Merge eligibility control (SKCapstone PR 358):**
Link MAY merge only when ALL of the following conditions are met for the exact PR head:

1. The PR is mergeable
2. Zero failed checks
3. A full-SHA exact-head independent PASS by an author distinct from the source author and from Link
4. No unresolved FAIL or BLOCKED lineage
5. The PR is not authored by Link
6. The title and category exclude: CapAuth, credential, custody, issuer, secret, key, rollback, deploy, production, release, migration, and any other sensitive class

Link records the exact head, check state, review identity, review evidence SHA256, lineage result, category result, and merge receipt as immutable evidence. Any failed predicate denies the merge and escalates to Chef.

### Niobe (Fleet Dispatcher) Boundary

**Allowed operations:**
- Fleet claims, launches, releases, reassignment, rotation
- Lane routing and worker health monitoring
- Acting on typed recommendations from Mero and Link with readback fencing
- Fleet mutation within authorized scope

**Prohibited operations (runtime enforcement):**
- Review verdicts
- The merge queue (this belongs to the Integrator)
- Application action dispatch (this is a separate governed component)
- App actuation (this belongs to the application action dispatcher under ACTION_AUTHORIZATION_STANDARD)

**Application action dispatch separation:**
Niobe gains no application actuation authority from the Fleet Dispatcher seat. The application action dispatcher remains a separate governed component with the closed inputs, ITIL fold, readiness, freeze, and current-catalog checks defined by ACTION_AUTHORIZATION_STANDARD. Fleet dispatch coordinates CardStore work and worker processes only.

### Fenced System Actors

Every lifecycle seat may create a correctly typed card within its product and
role scope. Card creation is coordination, not approval. Normal schema,
dependency, sensitivity, deduplication, and dispatch admission checks still
apply, and the creating seat is recorded as provenance.

The following actors are explicitly authorized for fleet mutation operations:

- `niobe` (Fleet Dispatcher) - recurring fleet mutation authority
- `jarvis` - Casey-directed assistance. Reversible coordination is immediate and
  attributed to Jarvis under Casey's configured ownership. Merge, deployment,
  release, and application actuation still use the signed Casey-direction gateway.
- Fenced system actors (named in deployment configuration) - bounded repair authority

No other agent, seat, or process may perform fleet claim release, launch, stop, reassignment, rotation, or worker-health repair.

## Boundary Violation Handling

The source enforcement API is `skcapstone.seat_boundaries`. It rejects unknown
actors by default, requires explicit fenced-system-actor configuration, binds
Link reviewer selection to distinct identities, and fences every Niobe action
on a recommendation against current owner, claim revision, process state, and
previously consumed recommendation ids. Live seat activation remains a separate
deployment step.

### Runtime Enforcement

The SKCapstone runtime enforces seat boundaries through:

1. **Capability scoping**: Each seat's agent identity holds only the capabilities it needs
2. **Authorization checks**: Fleet mutation operations verify the caller identity against allowed seats
3. **Audit logging**: All operations record the acting seat, timestamp, and action type
4. **Recommendation fencing**: Niobe rejects recommendations that fail current-state validation

### Negative Tests

The following tests verify that boundary violations fail closed:

1. **Mero cannot mutate**: Attempted fleet mutation from Mero identity is rejected
2. **Link cannot dispatch**: Attempted fleet dispatch from Link identity is rejected
3. **Link cannot deploy**: Attempted deployment from Link identity is rejected
4. **Niobe cannot actuate**: Attempted application action dispatch from Niobe identity is rejected
5. **Recommendation replay fails**: Duplicate `recommendation_id` is rejected
6. **Stale revision fails**: Recommendation with mismatched `observed_claim_revision` is rejected
7. **Unrecognized actor fails**: Fleet mutation from non-authorized identity is rejected

Test implementation: `tests/fleet/test_seat_boundaries.py`

## Related Documents

- [Standing Up A Seat](./standing-up-a-seat.md) - Procedure for creating a new seat
- [sk-standards ADR-0005](https://github.com/smilinTux/sk-standards/blob/HEAD/decisions/ADR-0005-five-operating-seats.md) - Canonical source
- [sk-standards ROSTER.md](https://github.com/smilinTux/sk-standards/blob/HEAD/ROSTER.md) - Seat roster
- [ACTION_AUTHORIZATION_STANDARD](https://github.com/smilinTux/sk-standards/blob/HEAD/standards/ACTION_AUTHORIZATION_STANDARD.md) - Application action dispatch governance
- [SKCapstone PR 358](https://github.com/smilinTux/skcapstone/pull/358) - Source-only merge eligibility control
- [Nimble Factory Design, section 3.6](../superpowers/specs/2026-09-16-nimble-factory-design.md) - the tank-into-atlas decision and the cold-start constraint
- [Amendment B: seat exclusion](../superpowers/specs/2026-09-17-seat-exclusion-amendment.md) - why seats remain host-pinned

## Cold-start constraint

Dispatch cannot bootstrap through the thing it dispatches. Niobe therefore
keeps a minimal timer-based presence on at least two hosts as supervisor of
last resort, independent of whatever else is running as fleet-dispatched
work. This is a standing constraint from the spec, not a deferred item: a
dispatcher that can deadlock on its own absence is the exact failure this
clause exists to prevent.

## Version History

| Date | Change | Author |
|---|---|---|
| 2026-09-17 | Folded tank into atlas, reducing `LIFECYCLE_SEATS` to five; `seat_boundaries.Seat.TANK` retained as a non-dispatched authority-model actor; documented that seats remain host-pinned (Amendment B) and the niobe cold-start constraint | lumina |
| 2026-09-09 | Activated six lifecycle seats and moved recurring dispatch from Jarvis to Niobe (card 20a637fe) | jarvis |
| 2026-09-01 | Initial charter document aligned with sk-standards ADR-0005 (card 95af18fd) | pi-jarvis-chiap03-4274eef2 |
