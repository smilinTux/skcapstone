# Seat Charters: SKCapstone Fleet Boundaries

**Status:** ACTIVE
**Date:** 2026-09-01
**Canonical Source:** [sk-standards ADR-0005](https://github.com/smilinTux/sk-standards/blob/HEAD/decisions/ADR-0005-five-operating-seats.md) (card 95af18fd)
**Reference:** [sk-standards ROSTER.md](https://github.com/smilinTux/sk-standards/blob/HEAD/ROSTER.md)

This document aligns SKCapstone runtime enforcement with the canonical seat responsibility contract defined in sk-standards. It is NOT the source of truth, the ADR and ROSTER are, but it translates those definitions into the specific SKCapstone fleet context and documents the enforcement boundaries.

## Canonical Reference (do not edit)

The following table is a direct reflection of the canonical definitions in sk-standards ADR-0005. Any discrepancy between this document and the canonical ADR is an error in this document, and the ADR prevails.

| Seat | Owns | Explicitly does not own |
|---|---|---|
| **Fleet Dispatcher** (`jarvis`) | Fleet claims, launches, releases, reassignment, rotation, lane routing, and worker health. | Review verdicts, the merge queue, application action dispatch, app actuation |
| **Integrator** (`link`) | Triage, independent-review assignment, the merge queue, and eligible merges under the PR 358 control. Owns delivery quality. | Fleet claims, launches, releases, reassignment, application action dispatch, app actuation |
| **Overseer** (`mero`) | Read-only convergence and drift measurement. Emits typed recommendations, alerts, observations, and briefs. | Fleet mutation, merge, application action dispatch, or any actuation |
| **Operations** (`atlas`) | Apps and infra. Observes, reasons, repairs, under the Atlas Constitution. | The coordination board, which it provably does not read |
| **Recorder** | *nobody* | A rule, not a role: every seat records its own decisions as it makes them. | n/a |

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

Only Jarvis MAY act on the recommendation. Before any claim release, launch, stop, or reassignment, Jarvis MUST:

1. Re-read the current CardStore owner and claim revision
2. Re-read the current process state
3. Reject a duplicate `recommendation_id`
4. Reject a missing or mismatched claim revision
5. Reject stale process evidence
6. Reject any action outside Jarvis's fleet authority

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

### Jarvis (Fleet Dispatcher) Boundary

**Allowed operations:**
- Fleet claims, launches, releases, reassignment, rotation
- Lane routing and worker health monitoring
- Acting on typed recommendations from Mero and Link (with readback fencing)
- Fleet mutation within authorized scope

**Prohibited operations (runtime enforcement):**
- Review verdicts (these belong to the Integrator)
- The merge queue (this belongs to the Integrator)
- Application action dispatch (this is a separate governed component)
- App actuation (this belongs to the application action dispatcher under ACTION_AUTHORIZATION_STANDARD)

**Application action dispatch separation:**
Jarvis gains no application actuation authority from the Fleet Dispatcher seat. The application action dispatcher remains a separate governed component with the closed inputs, ITIL fold, readiness, freeze, and current-catalog checks defined by ACTION_AUTHORIZATION_STANDARD. Fleet dispatch coordinates CardStore work and worker processes only.

### Fenced System Actors

The following actors are explicitly authorized for fleet mutation operations:

- `jarvis` (Fleet Dispatcher) - full fleet mutation authority
- Fenced system actors (named in deployment configuration) - bounded repair authority

No other agent, seat, or process may perform fleet claim release, launch, stop, reassignment, rotation, or worker-health repair.

## Boundary Violation Handling

The source enforcement API is `skcapstone.seat_boundaries`. It rejects unknown
actors by default, requires explicit fenced-system-actor configuration, binds
Link reviewer selection to distinct identities, and fences every Jarvis action
on a recommendation against current owner, claim revision, process state, and
previously consumed recommendation ids. Live seat activation remains a separate
deployment step.

### Runtime Enforcement

The SKCapstone runtime enforces seat boundaries through:

1. **Capability scoping**: Each seat's agent identity holds only the capabilities it needs
2. **Authorization checks**: Fleet mutation operations verify the caller identity against allowed seats
3. **Audit logging**: All operations record the acting seat, timestamp, and action type
4. **Recommendation fencing**: Jarvis rejects recommendations that fail current-state validation

### Negative Tests

The following tests verify that boundary violations fail closed:

1. **Mero cannot mutate**: Attempted fleet mutation from Mero identity is rejected
2. **Link cannot dispatch**: Attempted fleet dispatch from Link identity is rejected
3. **Link cannot deploy**: Attempted deployment from Link identity is rejected
4. **Jarvis cannot actuate**: Attempted application action dispatch from Jarvis identity is rejected
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

## Version History

| Date | Change | Author |
|---|---|---|
| 2026-09-01 | Initial charter document aligned with sk-standards ADR-0005 (card 95af18fd) | pi-jarvis-chiap03-4274eef2 |
