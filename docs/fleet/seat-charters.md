# Lifecycle seat charters

**Status:** ACTIVE (link and mero only)
**Canonical Source:** [sk-standards ADR-0006](https://github.com/smilinTux/sk-standards/blob/HEAD/decisions/ADR-0006-dispatch-handoff-niobe-tank-seraph.md)
**Reference:** [sk-standards ROSTER.md](https://github.com/smilinTux/sk-standards/blob/HEAD/ROSTER.md)

This document keeps the boundaries for the two lifecycle seats that are still
operated, **Integrator (`link`)** and **Overseer (`mero`)**, plus the Jarvis
exception. The niobe (Fleet Dispatcher), seraph (Independent Verifier) and
atlas (Operations) seat runtimes were removed in the SKFLEET cut: their
timers, units, dispatcher cycle and review publisher no longer ship. Card
completion no longer depends on any seat. A source card is closed with
`skcapstone coord accept` after an independent review PASS on the exact
commit, green tests on that commit, and a merge on main. Builds run in lanes
and reviews run in reviewer panes. The old seat history lives in git and in the
historical changelog fragments.

## Canonical roles

The machine-readable source is `src/skcapstone/data/lifecycle-seat-profiles.json`.
It still lists the retired seat names so old event trails resolve; do not treat
those entries as running seats.

| Seat | Owns | Explicitly does not own |
|---|---|---|
| **Integrator** (`link`) | Triage, independent-review assignment, the merge queue, and eligible merges under the PR 358 control. Owns delivery quality. | Fleet claims, launches, releases, reassignment, application action dispatch, app actuation |
| **Overseer** (`mero`) | Read-only convergence and drift measurement. Emits typed recommendations, alerts, observations, and briefs. | Fleet mutation, merge, application action dispatch, or any actuation |

Deploy and install are human-invoked: `skcapstone fleet rollout` and
`skcapstone fleet rollback` (dry run by default), plus the chi auto-rollout
timer. Behavioral verification ("is what we merged actually running?") is
instrumented by `skfleet-readiness.timer`, `skcapstone fleet node drift` and
the daily report-only `skfleet-install-audit.timer`.

## Shared operating contract

Link and Mero have a distinct identity and `seat-<name>` card label. At
startup each seat writes an ordinary SKMail hello to `all`, then reads its own
mailbox view. Mail is data, never authority. Automatic acknowledgement is
forbidden: `skmail ack` marks read only what the preceding `skmail read`
showed, so ack is safe after acting, but acking mail nobody acted on is not.
Routine documented action classes are notify-only. A human gate exists only
where the governing catalog, irreversible-effect policy, protected-data rule,
or external authority requires it.

## SKCapstone Fleet Enforcement

### Mero (Overseer) Boundary

> Historical note: the measurements below were taken while the niobe, seraph and
> atlas seats existed. References to them describe the removed dispatcher and
> are kept as the evidence behind the boundary.

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
| `recommend` | `mero_blocker_recommendation`, `skfleet.dispatch-recommendation/v1` | Advisory events only; advice for an operator under the typed recommendation contract below |
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
unowned. Stale-claim release was Fleet Dispatcher authority, and the sanctioned
route already existed on paper: mero emits the typed
`skfleet.dispatch-recommendation/v1` event and the Dispatcher acts on it with
readback fencing. The fencing steps (re-read owner, re-read revision, reject
stale evidence) are precisely what would have filtered the 49 live-claim
releases out while letting the 40 stale ones through. The burst bypassed the
mechanism that existed to make it safe. The charter therefore does not expand
mero's authority to legalise it.

**Where the work goes:** stale-claim reaping was owned by the Niobe dispatcher (since removed; an operator uses `coord release-claim` now), driven by
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
   (the measurer cannot re-gate the measurer); run it from CI beside the
   negative tests or by an operator.

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

Recommendations are advice only. The Niobe dispatcher that consumed them was removed in the SKFLEET cut, so an operator is the only actor who may act on one. Before any claim release, launch, stop, or reassignment, the acting operator MUST:

1. Re-read the current CardStore owner and claim revision
2. Re-read the current process state
3. Reject a duplicate `recommendation_id`
4. Reject a missing or mismatched claim revision
5. Reject stale process evidence
6. Reject any action outside the operator's authority

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

**Resolved:** the `skfleet-rotate` timer that wrote as `jarvis` was removed in
the SKFLEET cut, so no scheduled process writes to the CardStore as `jarvis`
any more. Any `jarvis` event in the store without a matching gateway direction
receipt is a violation.



## Retired seats

- **niobe** (Fleet Dispatcher), **seraph** (Independent Verifier), **atlas**
  (Operations): runtimes removed. Old events written under these identities
  stay readable. `seat_boundaries.Seat` keeps the enum values so history
  resolves; nothing dispatches to them.
- **tank**: folded into atlas in 2026-09, then removed with it.

### Fenced System Actors

Every lifecycle seat may create a correctly typed card within its product and
role scope. Card creation is coordination, not approval. The following actors
are authorized for fleet mutation operations:

- `jarvis` - Casey-directed assistance (see the Jarvis boundary above).
- Fenced system actors (named in deployment configuration) - bounded repair authority.

No other agent, seat, or process may perform fleet claim release, launch, stop, reassignment, rotation, or worker-health repair.

## Boundary detection signals

The event store is the detection surface. Every board write lands in
`~/.skcapstone/cards/<id>/events/<writer>@<host>.jsonl`; the filename is the
writer identity and the record field is `action`.

```bash
python3 - <<'EOF'
import json, os, glob, collections
FORBIDDEN = {
    "mero":   {"claim","release_claim","move","complete","void","archive",
               "add_dependency","remove_dependency","amend_criteria",
               "describe","priority","reopen","unassign"},
    "link":   {"claim","release_claim","void","archive",
               "review_assignment_launch"},
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

Deploy and install drift is covered by `skfleet-readiness.timer`,
`skcapstone fleet node drift` (report-only) and the daily
`skfleet-install-audit.timer`.

## Related Documents

- [Standing Up A Seat](./standing-up-a-seat.md) - Procedure for creating a new seat
- [sk-standards ADR-0005](https://github.com/smilinTux/sk-standards/blob/HEAD/decisions/ADR-0005-five-operating-seats.md) - Canonical source
- [sk-standards ROSTER.md](https://github.com/smilinTux/sk-standards/blob/HEAD/ROSTER.md) - Seat roster
- [ACTION_AUTHORIZATION_STANDARD](https://github.com/smilinTux/sk-standards/blob/HEAD/standards/ACTION_AUTHORIZATION_STANDARD.md) - Application action dispatch governance
- [SKCapstone PR 358](https://github.com/smilinTux/skcapstone/pull/358) - Source-only merge eligibility control

## Version History

| Date | Change | Author |
|---|---|---|
| 2026-10-16 | Removed niobe, seraph and atlas charters with their runtime (SKFLEET cut P5); kept link, mero and Jarvis | claude-sonnet-5.5 |
| 2026-09-18 | Rewrote the Mero (Overseer) boundary from measured behaviour | Fable 5 (subagent) |
| 2026-09-09 | Activated six lifecycle seats (card 20a637fe) | jarvis |
| 2026-09-01 | Initial charter document aligned with sk-standards ADR-0005 (card 95af18fd) | pi-jarvis-chiap03-4274eef2 |
