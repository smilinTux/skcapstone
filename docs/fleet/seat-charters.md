# Lifecycle seat charters

**Status:** ACTIVE
**Date:** 2026-09-18
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
| **Operations** (`atlas`) | Exact-target verification and postcondition evidence for governed releases. Release and installation of exact approved artifacts, plus bounded rollback, are ported from the retired Tank seat **as a duty on paper only**; see [ATLAS release and install duty: inoperable pending B3](#atlas-release-and-install-duty-inoperable-pending-b3) below before assuming ATLAS can perform either. | Source authoring, self-approval, independent review of its own release, merge, dispatch, coordination-board ownership, card claiming, reviewer assignment, or policy change, and, until B3 lands, any actual deploy |

**Tank is folded into ATLAS (spec `2026-09-16-nimble-factory-design.md` section
3.6).** ADR-0005 names ATLAS as Operations, and ATLAS absorbed Tank's real
activity: release and installation of exact approved artifacts, behavioral
verification, and bounded rollback rehearsals. `LIFECYCLE_SEATS` in
`src/skcapstone/lifecycle_seats.py` is now five entries: `link`, `mero`,
`seraph`, `niobe`, `atlas`. Tank no longer runs, has no timer, and the
`skfleet-tank.service` / `skfleet-tank.timer` pair no longer ships.

### ATLAS release and install duty: inoperable pending B3

**The fold moved the duty, not the authority. ATLAS cannot currently perform
a governed release or install, and there is presently no dispatchable seat
that can.** This is a deliberate decision, not an oversight left for later:
granting deploy authority now, before spec A.4 / Plan B3 exists, would create
an actuating seat with no deployment manifest to deploy, no readiness gate to
run after deploying, and no prior manifest to roll back to. The Atlas
Constitution keeps irreversible actions human-gated precisely because a
rollout is only safe when it is reversible by construction, and none of that
machinery exists yet. Do not treat this section as a bug report; it is the
intended state of this branch, and it must stay true until B3 ships.

Three concrete things are missing, all measured against the current tree:

1. **No `DEPLOY` authority.** `data/lifecycle-seat-profiles.json` grants
   `atlas` `approved_artifact_release`, `approved_artifact_install`, and
   `rollback` as duties, but `Seat.ATLAS` in `src/skcapstone/seat_boundaries.py`
   is scoped to `{OBSERVE, ACTUATE_APPLICATION, CREATE_CARD}` only, no
   `Action.DEPLOY`. `Action.DEPLOY` is still held by `Seat.TANK`, and Tank is
   not dispatched (its timer and service no longer exist). No seat that
   actually runs holds `DEPLOY` today.
2. **The digest gate never moved.** `_role_seat_metadata` in
   `scripts/fleet/skfleet-rotate.py` still reads `approved_artifact_sha256`
   only on its `tank` branch. The `atlas` branch reads
   `verification_target` and `verification_evidence_sha256` for its existing
   postcondition-verification duty, and nothing else; the artifact-digest
   fence that gated Tank's release was never ported to ATLAS's branch of that
   function.
3. **The rail brief still forbids it.** The dispatch-layer ATLAS role brief
   in `skfleet-rotate.py` tells every ATLAS worker: "Do not deploy, dispatch,
   invoke an actuator, or change the target." A worker dispatched to ATLAS is
   told not to do the exact thing this section says it cannot yet do safely.

**Update, Plan B3 phase 1 (2026-09-17): observation now exists, authority
still does not.** A deployment manifest (`deployment_manifest.build_manifest`),
a readiness gate with a real caller (`skfleet-readiness.service`/`.timer`),
and a drift check (`skcapstone fleet node drift`) now exist; see
[rollout-drift.md](rollout-drift.md). None of that changes this section's
verdict: ATLAS still holds no `Action.DEPLOY`, the digest gate is still on
the retired tank branch, the rail brief still forbids deploying, and nothing
consumes the manifest to roll a node back. The manifest and drift check are
observation, which `OBSERVE` already permits; a caller still has to read
their output and act by hand.

**Update, Plan B3 phase 2 (2026-09-17): the mechanism now exists, authority
still does not.** `record_deployment`/`previous_manifest`
(`rollout_history.py`) give a node a recorded prior state, and
`plan_rollout`/`execute_rollout`/`execute_rollback` (`staged_rollout.py`,
exposed as `skcapstone fleet rollout` and `skcapstone fleet rollback`) stage
a deploy across several nodes one at a time, gate each with the same
readiness-plus-drift check `node drift` uses, halt at the first failure, and
can return a node to whatever it ran before. See
[rollout-drift.md](rollout-drift.md#4-staged-rollout-and-rollback-nimble-factory-plan-b3-phase-2).
This still does not change this section's verdict, and is not a gap in the
mechanism, it is the order Plan B3 chose on purpose: build the mechanism
first, grant the authority second, because an actuating seat with no
rollback target is worse than no seat at all (see
`docs/superpowers/plans/2026-09-17-staged-rollout.md`'s pre-flight ruling).
Both commands are human-invoked CLI, dry run by default, requiring an
explicit `--apply`; nothing dispatches them, and nothing about their
existence gives `Seat.ATLAS` (or any seat) a new bound action.

**What granting ATLAS `DEPLOY` would now concretely take**, so the decision
stays small and bounded rather than open-ended, since the three things
missing when B2 first deferred it (a manifest, a gate, and a rollback
target) now exist:

1. Add `Action.DEPLOY` to `Seat.ATLAS`'s bound action set in
   `src/skcapstone/seat_boundaries.py` (currently `{OBSERVE,
   ACTUATE_APPLICATION, CREATE_CARD}`), and decide whether `Action.DEPLOY`
   moves off `Seat.TANK` or is simply shared; `Seat.TANK` stays in the
   authority-model enum regardless (see below), so this is a scoping
   decision, not a deletion.
2. Port the artifact-digest fence: `_role_seat_metadata` in
   `scripts/fleet/skfleet-rotate.py` still checks `approved_artifact_sha256`
   only on its unreachable `tank` branch. The `atlas` branch needs the
   equivalent check before a dispatched ATLAS worker can be trusted to
   deploy an exact approved artifact rather than whatever HEAD happens to
   be.
3. Rewrite the dispatch-layer ATLAS role brief in `skfleet-rotate.py`,
   which currently instructs every ATLAS worker: "Do not deploy, dispatch,
   invoke an actuator, or change the target." That sentence has to change
   to name the exact bounded action being granted, not become silent on
   the subject.
4. Decide who calls `skcapstone fleet rollout`/`rollback` under the grant:
   a bounded ATLAS batch calling the CLI itself (which still defaults to
   dry run and still requires an explicit apply-equivalent flag internally),
   or a human continuing to run it and ATLAS only verifying the result.
   That is a real design choice this section deliberately leaves open
   rather than pre-deciding, because it changes what "ATLAS holds DEPLOY"
   actually means operationally.

None of the four is large, and none requires new machinery: the manifest,
the gate, and the rollback target this duty was blocked on are the exact
three things Tasks 1 through 3 of Plan B3 phase 2 built. That is what makes
this a small, boundable decision now rather than the open-ended one it was
when this section was first written.

**What to do instead of a governed release: wait for B3, or route the
release manually with full human sign-off outside any seat.** ATLAS may
still verify a target that was released by other means and record PASS,
FAIL, or BLOCKED evidence; that half of the fold is real and operable today.

**What NOT to do: do not fall back to the Casey-directed emergency gateway
for a routine release.** `JarvisEmergencyGateway` (`Seat.JARVIS` in
`seat_boundaries.py`) holds `Action.DEPLOY` and `Action.RELEASE_ARTIFACT`
unbounded, with no artifact-digest fence at all, gated only on a signed Casey
direction bound to action, target, change, and scope. That gate protects
against forged or substituted direction; it does not narrow *what* gets
deployed the way Tank's `approved_artifact_sha256` check did. Using the
emergency path as a routine substitute for the retired governed release
surface is strictly worse than waiting: it trades a missing but plannable
capability for an unbounded one exercised on a routine cadence, which is the
opposite of what the gateway is for. Reserve it for the emergencies it is
named for.

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
action. See [ATLAS release and install duty: inoperable pending
B3](#atlas-release-and-install-duty-inoperable-pending-b3) above for the full
picture: the digest gate is one of three missing pieces, not the only one.
Routine documented action classes are notify-only. A human gate exists only
where the governing catalog, irreversible-effect policy, protected-data rule,
or external authority requires it.

## SKCapstone Fleet Enforcement

### Mero (Overseer) Boundary

**Purpose:** Mero measures the estate (convergence, drift, delivery fraction,
backlog, claim health) and reports what it finds. It never changes what it
measures, because a measurer that repairs its own findings can no longer be
trusted to report them.

**Why this section is a verb list rather than the word "read-only".** The
previous version of this boundary said "read-only" in prose, and it was
violated for eleven days before anyone looked. Measured on chi 2026-09-18
(per-card shard store, `~/.skcapstone/cards/*/events/mero@*.jsonl`, where the
filename is the writer identity): between 2026-08-31 and 2026-09-16 the
identity `mero` wrote 531 lifecycle mutations (327 `move`, 149
`release_claim`, 29 `complete`, 9 `void`, 9 `archive`, 8 `claim`), plus 5
`add_dependency` and 12 `describe`, and in the legacy `card_events` store 27
`add_label` and 23 `remove_label`. 477 of the 531 (90 percent) landed in one
burst, 2026-09-07 22:00 to 2026-09-08 02:59 UTC, from an interactive
controller session on chiap08 issuing `skcapstone coord ... --agent mero`
(evidence in PR 766). Over the same window the read side produced 8,257
`mero_blocker_recommendation` and 2,380 `mero_observation` events: 95.1
percent of everything mero wrote was inside charter, and the 4.9 percent that
was not went unnoticed because "read-only" named no verbs a checker could
check. This section fixes that: a verb absent from the permitted table is
prohibited, not undecided.

#### Permitted verbs

| Runtime action (`seat_boundaries.Action`) | Event-store `action` values | Covers |
|---|---|---|
| `observe` | `mero_observation` | Census, drift, delivery-fraction, backlog and claim-health measurement over CardStore, fleet-rotation evidence, and worker logs |
| `recommend` | `mero_blocker_recommendation`, `skfleet.dispatch-recommendation/v1` | Advisory events only; consumed exclusively by Niobe under the typed recommendation contract below |
| `create_card` | card creation via `coord create --by mero` | Filing discovered work; coordination, not approval (see Fenced System Actors) |
| (not yet gated at runtime) | `link`, `verdict`, `evidence`, `evidence_link` | Recording mero's own measurement verdicts and evidence hashes on cards, per the coord briefing's evidence contract |

The runtime authority set is `Seat.MERO = {OBSERVE, RECOMMEND, CREATE_CARD}`
in `src/skcapstone/seat_boundaries.py`, enforced at the coord mutation
entrypoints since PR 766. The `link`/`verdict`/`evidence` family has no
`Action` member yet, so it is permitted by this charter and checked by the
audit query below, not by the runtime gate.

#### Prohibited verbs

Each row names the verb, what mero was measured doing with it, and why it is
prohibited rather than merely unassigned.

| Verb | Measured (2026-08-31 to 2026-09-16) | Why prohibited |
|---|---|---|
| `claim` | 8, including self-claims (`owner: mero`) | The measurer must not hold work it measures. ROSTER.md already forbids this by example: the rubric-scoring card `48136bad` "must not be claimed by mero". |
| `release_claim` | 149; 129 of them released `jarvis`-held claims; 49 hit an owner who had written to the card within the previous hour; 47 hit claims younger than 2 hours; 37 released owners kept writing to the card afterwards | Releasing another seat's claim is fleet mutation, the Fleet Dispatcher's authority. A third of these releases were live-work stealing, the exact damage the seat separation exists to prevent. |
| `move` | 327; 284 into `backlog` | Requeueing a card is re-dispatch by another name. It directly inflates the churn metric (claims per card) that the Dispatcher is scored on, from a seat the Dispatcher cannot see. |
| `complete` | 29 | Completion is precisely the assertion mero exists to audit (claimed-done with no evidence is the estate's core defect). A measurer that completes cards is grading its own homework. |
| `void`, `archive` | 9 each | Terminal lifecycle decisions belong to the card owner or the Dispatcher. Note: these verbs have no `Action` member and remain ungated at runtime (PR 766 follow-up); only the audit query below catches them. |
| `add_dependency`, `describe`, `add_label`, `remove_label`, `reprioritize`, `amend_criteria` | 5, 12, 27, 23, 0, 0 | Card metadata shapes other seats' dispatch and triage. If a measurement implies a metadata change, emit a recommendation; do not make the change. |
| `launch`, `stop`, `reassign`, `rotate`, `repair_worker` | 0 | Fleet actuation, Dispatcher authority (ADR-0005). |
| `merge`, `deploy`, `actuate_application`, repository writes | 0 | ADR-0005 section 4: the Overseer has no actuation surface, no freeze story, and no capability token, by design. |

#### The 2026-09-07 burst: necessary work in the wrong seat, or overstepping?

Both, in measurable proportions, and the distinction matters because it
determines what changes.

The burst was the Jarvis-to-Niobe dispatch handover window (recurring
dispatch moved to Niobe 2026-09-09, per this document's own history). 129 of
the 149 releases drained `jarvis`-held claims. Splitting them by the state of
the claim at release time:

- **40 of 149** hit claims whose owner had been silent on the card for more
  than 24 hours. Releasing those was genuine janitorial work: dead claims
  block re-dispatch, and after the sweep 104 of the 149 released cards were
  re-claimed (only 26 by the same owner), so the requeue did feed the board.
- **49 of 149** hit claims whose owner had written to the card within the
  previous hour, and **47** hit claims younger than 2 hours. **37** released
  owners kept writing to cards they no longer held. That is not janitorial
  work; that is stealing live work and corrupting the claim ledger.

**Verdict:** the janitorial fraction was necessary work, but it was never
unowned. Stale-claim release is Fleet Dispatcher authority, and the sanctioned
route already existed on paper: mero emits the typed
`skfleet.dispatch-recommendation/v1` event and the Dispatcher acts on it with
readback fencing. The fencing steps (re-read owner, re-read revision, reject
stale evidence) are precisely what would have filtered the 49 live-claim
releases out while letting the 40 stale ones through. The burst bypassed the
mechanism that existed to make it safe. The charter therefore does not expand
mero's authority to legalise it.

**Where the work goes:** stale-claim reaping is owned by **Niobe**, driven by
mero's recommendations (mero already emits them at volume: 8,257 blocker
recommendations in the same window). No new seat is created for this;
"a seat for coordination is what produced one agent holding four jobs"
(PROPOSAL-SEATS-WITHOUT-JARVIS, 2026-09-02). The residual human factor is a
runbook rule: **controller and operator sessions must not wear a seat
identity via `--agent` or `SKAGENT` for board janitorial work.** A human
doing an emergency drain does it as an operator identity, on the record as
themselves.

#### Violation detection

A charter nobody can check is a wish. Three layers, in order of speed:

1. **Runtime refusal (fail closed):** since PR 766, `require_authority` is
   threaded through the coord mutation entrypoints; `--agent mero` on
   `claim`, `move`, `complete`, or `release-claim` raises `BoundaryError`
   before any event is written. This alone would have stopped the burst.
2. **Negative tests (regression):** `tests/fleet/test_mero_readonly_entrypoint.py`
   (13 tests) and `tests/fleet/test_seat_boundaries.py` pin the gate; the
   refusal tests were verified red against the unguarded tree first.
3. **Standing audit query (catches what the gate cannot):** the `void`,
   `archive`, and label verbs are ungated, and a writer could bypass the CLI
   entirely by appending shard files directly. The event store itself is
   therefore the signal of record. The check is one query over the shard
   store, keyed on the writer identity in the filename:

```bash
python3 - <<'PY'
import json, glob, os, collections, sys
ALLOWED = {"mero_observation", "mero_blocker_recommendation",
           "link", "verdict", "evidence", "evidence_link", "create"}
bad = collections.Counter()
for f in glob.glob(os.path.expanduser("~/.skcapstone/cards/*/events/mero@*.jsonl")):
    for line in open(f):
        line = line.strip()
        if not line:
            continue
        a = json.loads(line).get("action", "?")
        if a not in ALLOWED:
            bad[a] += 1
print(dict(bad) or "CLEAN")
sys.exit(1 if bad else 0)
PY
```

   Run against the chi store on 2026-09-18 (chiap01) this printed exactly
   `{'release_claim': 149, 'move': 327, 'complete': 29, 'describe': 12,
   'void': 9, 'archive': 9, 'claim': 8, 'add_dependency': 5}` and exited 1,
   so it demonstrably catches the 2026-09-07 burst it was designed after.
   **The query must not be run by mero.** Mero auditing mero is circular
   (the measurer cannot re-gate the measurer); it belongs in Seraph's
   bounded cycle as an exact-target verification, with the counter emitted
   in Seraph's health record. Until Seraph carries it, it runs in CI beside
   the negative tests. Detection latency target: one Seraph cycle
   (5 minutes), against the eleven days the burst actually went unnoticed.

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

### Seraph (Independent Verifier) Boundary

Measured basis: 2,596 card events in the 30 days to 2026-09-18 were written by
`seraph` or a `*seraph*` worker identity (claim 849, move 808, complete 492,
release_claim 193, review_assignment_launch 176), which makes the verifier one
of the three most active writers on the board. A seat this active cannot run
on prose. The lists below are the checkable contract.

**Allowed operations (explicit verbs):**
- `claim`, `move`, `complete`, `release_claim` on a card that carries the
  `seat-seraph` label, or on a review card the verifier operates in its own
  lane
- `review_assignment_launch` for bounded review workers running under a
  `pi-seraph-<host>-<card>` identity
- `link` for attaching verdict evidence
- `pass_for_review` and `blocked` verdict records
- Card creation (typed, provenance recorded), per Fenced System Actors below

**Prohibited operations:**
- `void` and `archive`: terminal board mutation belongs to the dispatcher
  seat's hygiene subsystems, never to the verifier
- `add_dependency`, `remove_dependency`, `amend_criteria`, `describe`,
  `priority`, `reopen`, `unassign`, `add_label` on cards outside its own lane
- `review_assignment_recommendation` (Link's verb)
- `mero_observation` and `mero_blocker_recommendation` (Mero's verbs)
- Merge, deployment, release, or actuation of any kind
- Verifying a candidate it authored or claimed (self-review), per ADR-0006

**Measured drift at adoption:** in the same 30-day window the seraph identity
set wrote 20 `void`, 20 `archive`, 7 `describe`, 7 `add_dependency`,
5 `remove_dependency`, and 2 `amend_criteria`. Under this boundary those are
violations. The detection query in
[Boundary detection signals](#boundary-detection-signals) flags every one.

**Detection signal:** the per-writer event-store query below, with this
section's prohibited-verb list. It would have caught each measured drift
event above on the day it was written.

### Atlas (Operations) Boundary

Measured basis, 30 days to 2026-09-18: 1,166 `skfleet-atlas` cycle records on
chiap08 and every one records `dispatch_succeeded: 0`. The freeze store
`~/.skcapstone/agents/atlas/objects/_freeze.json` is absent on all five chi
hosts (re-verified 2026-09-18 by direct filesystem check), so under AUTONOMY
invariant 4 the seat holds no actuation and is correctly refusing every
effect. In the same window, identities matching `*atlas*` wrote 34 board
events: claim 11, move 9, complete 8, void 2, archive 2, link 2.

**Allowed operations (explicit verbs):**
- `claim`, `move`, `complete`, `release_claim` ONLY on a card carrying both
  the `seat-atlas` label and `dispatch-approved`, entered through Niobe's
  atomic claim-and-launch (ADR-0006 section 6)
- `link` for postcondition and behavioral-verification evidence
- Card creation (typed, provenance recorded)
- Release, install, and rollback of exact approved artifacts, but only after
  BOTH of the following exist, and neither exists today: a human-provisioned
  freeze store in the off position, and an `Action.DEPLOY` grant per the
  bounded four-item list in
  [ATLAS release and install duty](#atlas-release-and-install-duty-inoperable-pending-b3)

**Prohibited operations:**
- `claim` on any card lacking the `seat-atlas` plus `dispatch-approved`
  admission pair
- `void` and `archive`
- `review_assignment_launch`, `review_assignment_recommendation`, and
  reviewer assignment of any kind
- `add_dependency`, `remove_dependency`, `amend_criteria`, `priority`,
  `describe` outside its own claimed cards
- Merge, policy change, fleet dispatch
- Any deploy, install, release, or rollback effect while the freeze store is
  absent: an absent kill switch means no actuation, not free actuation

**Measured drift at adoption:** the atlas identity claimed and completed two
cards carrying no `seat-atlas` label (`9149e27a` on 2026-09-08 and `4dcb5258`
on 2026-09-16), and on 2026-09-16 wrote `void` and `archive` on `5391d896`
and `86a7086f`, which are `seat-seraph` review cards. Its one clean lane
event set is card `a71a7a71`, which carries `seat-atlas`. Under this boundary
the unlabeled claims and both void/archive pairs are violations, and the
detection query flags all of them.

**Detection signal:** the per-writer query below with this section's
prohibited list, plus a label check on every atlas `claim` (flag any claim
whose card lacks `seat-atlas`). For the actuation half: `skcapstone atlas
eyes` plus `ls ~/.skcapstone/agents/atlas/objects/_freeze.json` on every
host; a dispatch or deploy effect while that file is absent is a violation of
AUTONOMY invariant 4 regardless of what any card says.

### Jarvis (Casey-directed assistance) Boundary

Jarvis is not a lifecycle seat and holds no recurring schedule. Everything
below restates ADR-0006 section 5 as checkable verbs.

**Allowed operations:**
- Any board verb, but only under a verified, signed, unexpired Casey
  direction through `skcapstone.jarvis_emergency.JarvisEmergencyGateway`,
  bound to the exact action, target, change, and product scope
- Reversible coordination for Casey, immediate, with Casey ownership
  provenance recorded

**Prohibited operations:**
- Recurring lifecycle writes of ANY verb: no timer, unit, cron, or scheduled
  process may write to the CardStore as `jarvis`
- Exercising the dispatcher verbs (`claim`, `release_claim`, `move`,
  `complete`, `void`, `archive`, `review_assignment_launch`) on a cadence

**Measured nonconformance, open:** the deployed `skfleet-rotate.py` defaults
its writer identity to `jarvis` (the `requested = ("jarvis",)` fallback and
the `--agent jarvis` reclaim calls in the script), and `skfleet-rotate.timer`
fires every 5 minutes on chiap01 through chiap04. Result: 12,230 `jarvis`
card events in the 30 days to 2026-09-18, of which 4,487 landed AFTER
ADR-0006 declared Jarvis outside recurring lifecycle scheduling (claim 816,
move 2,140, complete 816, release_claim 323, void 116). The dispatch
machinery is doing the dispatcher seat's job under the wrong identity. Until
the rotate writer identity moves to `niobe`, no query can separate
gateway-authorized Jarvis action from automation, which means this boundary
is currently uncheckable. Repointing that writer identity is a small change
in `scripts/fleet/skfleet-rotate.py` and is the single highest-value
enforcement fix this document names.

**Detection signal (armed once the identity moves):** any `jarvis` event in
the store without a matching gateway direction receipt is a violation. Until
then the signal is the inverse: `skfleet-rotate` cycles must stop appearing
as `jarvis`, and the query below reports the daily `jarvis` event count so
the cutover is visible.

### Tank (dissolved seat)

Tank does not survive as a seat. PR 751 folded it into ATLAS on 2026-09-17
(spec `2026-09-16-nimble-factory-design.md` section 3.6), and this section
exists so that no duty Tank owned is left unowned:

- Release and install of exact approved artifacts: owned on paper by ATLAS;
  performed in practice by a human invoking `skcapstone fleet rollout` and
  `skcapstone fleet rollback` (dry run by default) until the freeze store and
  the `Action.DEPLOY` grant land
- Behavioral verification ("is what we merged actually running?"): owned by
  ATLAS's postcondition duty; instrumented today by `skfleet-readiness.timer`
  and `skcapstone fleet node drift` (Plan B3 phase 1) and the daily
  report-only `skfleet-install-audit.timer` on all five chi hosts
- Bounded rollback: `rollout_history.previous_manifest` plus
  `execute_rollback`, human-invoked

Measured close-out: Tank identities wrote 33 board events in the 30 days to
2026-09-18, all coordination on DEPLOY and INTEGRATION cards, zero releases;
no tank unit or timer exists on chiap08 today. `seat_boundaries.Seat.TANK`
remains in the authority-model enum so historical events still resolve.

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

### Boundary detection signals

The event store is the detection surface. Every board write lands in
`~/.skcapstone/cards/<id>/events/<writer>@<host>.jsonl`; the FILENAME is the
writer identity, the record field is `action`, and `seq` restarts per shard
so order is `(ts, seq)`. One read-only query answers every boundary in this
document:

```bash
python3 - <<'EOF'
import json, os, glob, collections
FORBIDDEN = {
    "mero":   {"claim","release_claim","move","complete","void","archive",
               "add_dependency","remove_dependency","amend_criteria",
               "describe","priority","reopen","unassign"},
    "link":   {"claim","release_claim","void","archive",
               "review_assignment_launch"},
    "seraph": {"void","archive","add_dependency","remove_dependency",
               "amend_criteria","describe","priority","reopen","unassign",
               "add_label","review_assignment_recommendation",
               "mero_observation","mero_blocker_recommendation"},
    "atlas":  {"void","archive","review_assignment_launch",
               "review_assignment_recommendation","add_dependency",
               "remove_dependency","amend_criteria","priority"},
    "niobe":  {"pass_for_review","blocked",
               "review_assignment_recommendation",
               "mero_observation","mero_blocker_recommendation"},
}
hits = collections.Counter()
for f in glob.glob(os.path.expanduser("~/.skcapstone/cards/*/events/*.jsonl")):
    w = os.path.basename(f).split("@")[0]
    if w not in FORBIDDEN: continue
    for line in open(f):
        try: r = json.loads(line)
        except Exception: continue
        if r.get("action") in FORBIDDEN[w]:
            hits[(w, r.get("action"), f.split("/")[-3], r.get("ts","")[:10])] += 1
for k, n in sorted(hits.items()):
    print(*k, n)
EOF
```

Run over the 30 days ending 2026-09-18, this query flags every violation this
document names: Mero's 548 mutation events (537 before its boundary activated
on 2026-09-09 and 11 after, the post-activation set being claims and moves on
cards `7f3a9c21`, `556491d9`, and `abe011e9`); Atlas's void/archive of the
seat-seraph review cards on 2026-09-16; Seraph's out-of-lane terminal and
metadata writes; and, once the rotate identity moves off `jarvis`, every
scheduled `jarvis` dispatch event. The two-week unnoticed window that
motivated this section existed because the prose said "read-only" and named
no verbs; this query is the verbs.

Two further signals sit outside the event store:

- Deploy and install drift: `skfleet-readiness.timer` and `skcapstone fleet
  node drift` (report-only), plus the daily `skfleet-install-audit.timer` on
  all five chi hosts. These are the instruments that would have caught the
  2026-09-18 window in which every host ran a pre-fix
  `~/.local/bin/skfleet-rotate.py` after the fix had merged.
- Cross-repo contract drift: `scripts/check_lifecycle_seat_alignment.py` in
  sk-standards, run against
  `src/skcapstone/data/lifecycle-seat-profiles.json`. On 2026-09-18 it failed
  ("SKCapstone profile does not contain the canonical six seats") because the
  tank fold landed here without the sk-standards side; that failure is the
  gate working, and the sk-standards reconciliation PR restores it to green.

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
<<<<<<< HEAD
| 2026-09-18 | Rewrote the Mero (Overseer) boundary from measured behaviour: purpose statement, explicit permitted and prohibited verb tables with counts, verdict on the 2026-09-07 mutation burst (necessary janitorial fraction assigned to Niobe via typed recommendations, live-claim releases ruled overstepping), and a three-layer violation-detection contract including a shard-store audit query verified to catch the burst | Fable 5 (subagent) |
=======
| 2026-09-18 | Added explicit verb boundaries and detection signals for Seraph, Atlas, and Jarvis, a dissolution record for Tank naming where each duty went, and the event-store boundary query; recorded the measured drift each boundary flags at adoption (card-event census on chi, 30 days to 2026-09-18) | Fable 5 (subagent) |
>>>>>>> 83e49a1b (docs(fleet): verb boundaries and detection signals for Seraph, Atlas, Jarvis; Tank dissolution record)
| 2026-09-17 | Noted that Plan B3 phase 2 delivered the rollout mechanism (recorded prior manifests, staged rollout, rollback, `skcapstone fleet rollout`/`rollback`) without changing ATLAS's authority bound; added the concrete, bounded list of what granting `Action.DEPLOY` would now take | claude-sonnet-5 |
| 2026-09-17 | Noted that Plan B3 phase 1 delivered observation (deployment manifest, readiness gate with a caller, `skcapstone fleet node drift`) without changing ATLAS's authority bound; linked [rollout-drift.md](rollout-drift.md) | lumina |
| 2026-09-17 | Documented that ATLAS's ported release/install/rollback duty is inoperable pending B3: no `DEPLOY` authority in `seat_boundaries`, the digest gate still on the retired tank branch, and the rail brief forbidding deploy; named the emergency gateway as unbounded and not a substitute; added the upgrade-before-converge and converge-only-on-elected-host ordering constraints and the partial-rollback note | lumina |
| 2026-09-17 | Folded tank into atlas, reducing `LIFECYCLE_SEATS` to five; `seat_boundaries.Seat.TANK` retained as a non-dispatched authority-model actor; documented that seats remain host-pinned (Amendment B) and the niobe cold-start constraint | lumina |
| 2026-09-09 | Activated six lifecycle seats and moved recurring dispatch from Jarvis to Niobe (card 20a637fe) | jarvis |
| 2026-09-01 | Initial charter document aligned with sk-standards ADR-0005 (card 95af18fd) | pi-jarvis-chiap03-4274eef2 |
