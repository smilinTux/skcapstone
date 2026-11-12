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

Three weak spots the gate 5 fault-injection drill documented are now enforced and
are PASS criteria of the drill (`scripts/atlas_p32_fault_injection_drill.py`).

**Evidence is structured and must be fresh.** A proposal that wants to actuate
carries `evidence: {source, observed_at}`, where `observed_at` is an ISO 8601
timestamp with a timezone (`operator_seat/evidence.py`). The proposer stamps it
from the observation that put the condition in the brief, never from the model's
reply. Missing, malformed, naive (no timezone), future-dated (beyond 60 seconds
of skew) or older than `--max-evidence-age` (default 600 seconds) is refused with
`refused: evidence ...`. A non-positive or non-numeric maximum age also refuses;
there is no switch that turns the check off. The legacy free-text `evidence_ref`
is still recorded on the ledger for audit, but is never freshness evidence. The
human-approval lane (`skoperator honor-pending`) is not subject to this age
limit, because an approval legitimately arrives long after the observation; it
re-observes the condition after the action instead.

**The freeze is reported where the decision is made.** The freeze is re-read for
every proposal, not only at the top of the pass. A pass result carries `frozen`
(true when the fleet was frozen at any decision point or at the end),
`frozen_mid_pass` (the freeze landed after the pass began), `freeze_state` (the
worst state seen) and, on each outcome, the `freeze_state` at that decision. A
freeze refusal is not a fault of the action, so it never counts against the retry
budget or opens the circuit.

**"verified" means verified.** An auto action that ran reads `verified` only when
the post-action re-observation ran and passed (a lifecycle ledger or
`require_verified_actions` makes it mandatory). When nothing re-observed the
condition the outcome is `performed, unverified`. The word `applied` is no
longer produced. Nothing in the ledger, the ITIL correlation, the dispatcher or
`skcapstone doctor` branches on the outcome string; the brief artifact and the
tick report print it verbatim.
