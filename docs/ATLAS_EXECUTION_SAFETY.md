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
5. Check the stable intent fingerprint's cooldown and circuit breaker.
6. Actuate and require explicit `performed: true` proof.
7. Re-observe the owning app and require the bound condition to clear.
8. Persist success or failure atomically.

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
failure—not an applied action. Reports use the brief's already normalized
`firing` and `stale` fields so health-condition polarity cannot be inverted by
the formatter.
