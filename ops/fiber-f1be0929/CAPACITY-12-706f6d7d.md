# Twelve-worker qualification boundary

Card: 706f6d7d. Operations parent: 3ee76566.

On 2026-09-30 the human explicitly approved testing a twelve-worker estate
ceiling with rollback to nine. This is an estate admission limit, not an
upstream provider entitlement. It includes existing native sessions, unknown
occupancy and pending reservations, not only new distributed workers.

Unchanged limits: DeepSeek 3, GLM 5, Codex 2; launch spacing 75 seconds.
No Codex quota exception, Kimi admission or host-budget increase is authorized.
Unknown provider occupancy continues consuming every provider budget.
chiap08 coordinates; chiap01 retains gateway headroom. ZIOWK01 remains an
explicitly pinned Tailscale worker. Placement must pass live host and runtime
qualification, per-host CPU/RAM budgets, exact source and claim checks.

Qualification order:

1. Run admission boundary, distributed placement, failure and rollback tests.
2. Independently review exact source and install with per-host backups under HOLD.
3. Complete infrastructure-only distributed qualification, including the
   chiwk12 account migration requested by the human. No new W7 production
   cards may run during this stage.
4. After infrastructure passes, run controlled W7 processing and independent
   review, then verify guarded unattended continuation. Do not activate the
   old blind-prompt babysitter, automatic merge or application deployment.

If a capacity, route, claim, security or health gate fails, preserve existing
work and stop new admission. Repeated faults and ambiguous launches create
HOLD. Only the sole managed dispatcher may start work.

Rollback: restore the backed-up nine-worker admission module under the same
authority lock and HOLD. Re-run tests and read back the installed constant.
Do not stop existing workers to reduce occupancy. If occupancy exceeds nine,
allow it to drain while admission remains held. Preserve all run history,
claims, task repositories and candidate evidence.

Component tests are not proof of a throughput gain. Final operations evidence
must report actual worker placement, useful accepted outputs, error counts,
gateway latency, health, and any excluded hosts before claiming completion.
