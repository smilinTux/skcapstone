# Atlas execution safety contract

Atlas remains report-only unless execution is explicitly enabled. Physical
honor additionally requires every proposal to carry an exact `app`, `condition`,
`object`, and `action` binding. The app must be a registered `Operatorapp`, the
condition must belong to that app, and a human must have ratified the action on
that same object. A model response cannot grant itself eligibility.

The execution path now follows this fail-closed sequence:

1. Acquire the nonblocking single-flight lock.
2. Observe and build the polarity-aware brief.
3. Validate the proposal against an exact firing-condition identity.
4. Validate target existence and app-scoped human ratification.
5. Re-read the freeze at this proposal's decision point, then require fresh
   structured evidence (see below). Either refusal happens before any ITIL
   change, ledger intent or actuator call.
6. Check the stable intent fingerprint's cooldown and circuit breaker.
7. Actuate and require explicit `performed: true` proof.
8. Re-observe the owning app and require the bound condition to clear.
9. Persist success or failure atomically.

The fingerprint is SHA-256 over app, condition, object, and action. The default
cooldown is 15 minutes. Three consecutive failures open a durable circuit and
require an operator to inspect or deliberately clear the state; ordinary loop
runs never reset it. Corrupt or unsupported state blocks execution.

Each pass has a five-minute cooperative runtime budget and all production probes
and actuators must retain their own bounded I/O timeouts. The budget is checked
between observation, planning, and each action. The lock prevents overlapping
timer/manual passes.

State is stored beneath the fleet root at `atlas/state/` with owner-only file
permissions. Physical action remains frozen independently by the fleet freeze
control; this implementation does not remove or relax that control.

An unsuccessful actuator response, a missing proof, a failed postcondition, an
unknown observer, a missing condition, a cooldown, or an open circuit is a
failure, not an applied action. Reports use the brief's already normalized
`firing` and `stale` fields so health-condition polarity cannot be inverted by
the formatter.

## Evidence freshness, freeze at the decision point, outcome words

Weak spots the gate 5 fault-injection drill documented, and what the independent
review of the first fix then found, are enforced and are PASS criteria of the drill
(`scripts/atlas_p32_fault_injection_drill.py`, scenarios 1, 9, 12, 22, 23, 24).

**Evidence is structured, comes from the source, and is aged on the real clock.** A
proposal that wants to actuate carries `evidence: {source, observed_at}`
(`operator_seat/evidence.py`), where `observed_at` is the time the SOURCE data was
true, as an ISO 8601 timestamp with a timezone. It is never the pass clock: adapters
report `source_observed_at` (a live probe reports the real clock when it probed, the
cmdb adapter the reconcile artifact's end time, the skos backlog the digest's own
window end, the fleet adapter the instant its derived conditions were evaluated),
the envelope keeps it only if it is valid and never invents one, and the brief and
the proposer carry it through. An observation with no source time carries no
evidence and cannot actuate. The proposer stamps evidence from the observation,
never from the model's reply.

Age is measured against the REAL wall clock read at each decision point, not against
the pass's own timestamp, so a wrong pass clock or a slow model call cannot make old
evidence look fresh. Missing, malformed, naive (no timezone), future-dated (beyond 60
seconds of skew) or older than `skoperator run --max-evidence-age` (default 600
seconds, at most 3600) is refused with `refused: evidence ...`. A maximum age that is
non-numeric, non-positive, non-finite or above the ceiling also refuses, so the check
cannot be switched off by passing a huge number; the ceiling is an hour because a pass
has a five minute runtime budget. The legacy free-text `evidence_ref` is still
recorded on the ledger after the structured stamp, but is never freshness evidence.

**Refuse before writing, settle what was written, and make the dispatcher check
again.** Any closed actuation gate (frozen or unprovisioned) refuses an auto action
before an ITIL change or a ledger intent is written. If an intent was written and is
not actuated in the same pass (a gate closed after the write, the deadline fired, the
dispatcher refused, a cooldown suppressed it) it is voided: the ledger gains a
`PROPOSED -> ESCALATED` edge for this, with `voided` on the event, because the
dispatcher enumerates PROPOSED intents and a leftover one used to be acted on later,
unattended.

The ITIL change type follows the proposal's DISPOSITION, not its class. A proposal the
planner escalated (an unresolvable target, a denied action binding, a major change)
gets a CAB-gated `normal` change without the `auto-normal` tag, so nothing auto-approves
it at the fold; only a human CAB vote does. The loop records the lane (`auto` or
`escalate`) and the evidence stamp on the intent's PROPOSED event.

The loop never trusts a change id on a proposal (a proposal is model output): it
always drafts its own change, tagged `op-bind:<app>|<action>|<target>`, the one intent
it is for. The dispatcher itself, for any intent however it was left, checks in this
order, and a refusal never writes AUTHORIZED because nothing was authorized:

* the change is bound to THIS intent (exactly one matching `op-bind:` tag; the three parts
  are percent-escaped and a raw `|` in any of them is rejected, so no part can forge the
  separator). A change approved for one action cannot run another, and a change with no
  binding authorizes nothing;
* who approved it is derived from the VOTES, never from a missing marker: the fold
  writes its `auto-approved` timeline entry only while a change is still PROPOSED, so a
  standard change that goes approved, scheduled, unscheduled folds `approved` with no
  marker. A change is human-approved only if a qualifying human vote exists (the drafter's
  own excluded, as the fold does) and none rejects; `standard`, and the fold's
  `auto-normal` derivation, are auto-approved whatever the timeline says;
* **a human approval is per episode, and it expires.** It is read from the newest
  qualifying vote's `decided_at` against the real clock. Older than
  `honor-pending --max-approval-age` (default 24 hours, at most 7 days) it is refused and
  the intent closed (ESCALATED, change retired to a terminal state). A vote dated after
  the clock, a bad configuration, or an unreadable time is refused and closes nothing. When
  re-observation sees the condition definitely cleared, the intent is closed with the reason
  "condition cleared before dispatch" and its change retired too, so a recurrence needs a
  new proposal and a new human vote. This is deliberately conservative (fail closed) and can
  be relaxed later by raising the limit or changing the closing rule;
* an escalate-lane intent with no qualifying human vote is left pending, never acted on
  and never voided (a human decision is waiting on it); an intent with no recorded lane
  and no human vote is refused and left alone;
* a stale catalog generation, or a classification hardened since the proposal, refuses
  and leaves the intent PROPOSED (a proposal under the new catalog is a new intent);
* the target (`target_known`) and the ratified action binding (`action_allowed`) are
  re-checked when the caller supplies them (`skoperator honor-pending` does);
* a change with no human behind it must carry fresh evidence stored on the intent,
  against the real clock, and is voided if that fails (stored evidence only ages);
* cooldown and an open circuit are suppression, not failure: a refusal with nothing
  recorded against the retry budget and nothing rolled back;
* the owning app is re-observed, and only a DEFINITE firing status counts. `Unknown` is
  not "still firing" (it is for alerting, not for permission to act), a missing
  condition or observer is not, and the re-observation must itself carry a fresh source
  time. A definitely cleared condition voids an auto-lane intent and leaves a
  human-approved one pending. A human-approved change skips the age limit, because
  approval legitimately arrives long after the observation, but never the re-observation.

An approval is spent by the run it authorizes, and taking it is a compare-and-swap. Under
an exclusive per-change-id `flock` (shared by every dispatcher process on the host; it does
not span nodes) the dispatcher re-reads the change, requires it still `approved`, moves it
to `implementing` with a note naming the intent (`dispatch of <intent_id>`), and requires
that the FIRST unconflicted `approved->implementing` entry on the timeline is its own. The
timeline check matters because the fold keeps a conflicted second `implementing` event in
the `implementing` status, so the status alone cannot tell the winner from the loser. Any
other outcome refuses before the actuator is called. The change then goes to `deployed`
after a verified run or `failed` after a failed one, so the ITIL record agrees with the
ledger.

An escalate-lane proposal that already has an open escalate-lane intent (same app, action,
target, condition and catalog generation, its change still open) reuses it: the loop does not
draft another change and intent on every pass. One bad intent never crashes a dispatch pass:
a ledger error while recording an authorization is returned as an `error:` outcome and the
pass carries on. An intent a crash stranded in AUTHORIZED or EXECUTING (older than ten
minutes on the real clock) is escalated at the start of the next loop or dispatch pass: FAILED
then ESCALATED, its change marked `failed`, a decision parked for a human and an alert
emitted. It is never re-run, because the actuator may or may not have run.

An injected clock must be timezone-aware; a naive one is refused, never read as local
time. A discovered (manifest) observer's own `source_observed_at` is dropped in both
spellings: evidence time comes only from an in-process adapter, so a discovered
observer can report but cannot license an actuation.

**The freeze is reported where, and as, it happened.** The gate is re-read for every
proposal and again after every attempt. A pass result carries `frozen` and
`freeze_state` (the state at the END of the pass, as it is), `frozen_mid_pass` (a freeze
landed after the pass began, even if it was lifted again), `freeze_summary` (actions
taken before the freeze, actions it refused, conditions still firing) and, on each
outcome, `freeze_state` at its decision point and `freeze_after`. The report and the
published brief say what ran before a mid-pass freeze, not that Atlas "stood down";
a pass that STARTS frozen still says so. The brief carries a stable
`state: frozen=... mid_pass=... firing=... stale=... acted_before_freeze=...` line for
downstream readers. A freeze REFUSAL is not a fault of the action and never counts
against the retry budget; a real failure that merely coincides with a freeze still does.

**"verified" means verified.** An auto action that ran reads `verified` only when the
post-action re-observation ran and passed (a lifecycle ledger or
`require_verified_actions` makes it mandatory). When nothing re-observed the
condition the outcome is `performed, unverified`. The word `applied` is no longer
produced. Nothing in the ledger, the ITIL correlation, the dispatcher or
`skcapstone doctor` branches on the outcome string; the brief artifact and the tick
report print it verbatim.
