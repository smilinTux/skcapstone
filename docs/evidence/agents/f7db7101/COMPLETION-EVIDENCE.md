# Candidate evidence for f7db7101

Status: source candidate only. Independent review, installation and the real
end-to-end work cycle are still required. No deployment or completion claimed.

## Behavior

The user's continuation supersedes the old estate12, node-slot, provider3/5/2
and75second launch ceilings. SKGateway now owns provider request concurrency.
Reservations and exact run identity still prevent duplicate card execution.
Automatic placement ranks all six installed worker hosts by occupancy without
a hard count cap. Gateway and authority remain their existing service roles.
Memory and CPU observations remain visible without stopping model admission.

The sole existing timer runs five seconds after each cycle. A cycle can admit
each distinct eligible queued request. Unknown or failed work retains its
evidence and cannot be accepted or replayed, while unrelated cards can proceed.
Automatic global HOLD creation was removed. Explicit operator maintenance stop
remains available and must be cleared by the operator after verified rollout.

Native review gate checks refresh actual sealed SKGateway observations before
eligibility and again immediately before claiming a review. Repeated tiny model
qualification calls can reuse a successful result inside the current controller
process for60seconds, only after fresh lane-health/catalog checks with the same
gateway revision, route and credential identity. No measured token saving is
claimed by this source change.

The user explicitly confirmed per-worker CPU/RAM/runtime quotas remain. This
candidate does not change them. Live host inventory on2026-10-01 found12to32
CPUs and5.8to51GiB total RAM across the six installed worker hosts, sufficient
for the existing per-worker2CPU/3GiB quota. Physical telemetry is retained.

## Files

- fiber_admission.py: remove duplicate capacity/spacing policy, broaden installed placement.
- fiber_dispatch.py: preserve per-card fences without global unrelated-work stops.
- fiber_control.py: gateway truth refresh, scoped faults, multiple launches and probe reuse.
- fiber_lifecycle.py: optional per-card failure isolation used by the live controller.
- fiber_probe.py: retain telemetry without worker slot or memory admission limits.
- skfleet-fiber-dispatch.timer: five-second reconciliation interval.
- Relevant admission, dispatch, control and probe tests plus test_fiber_gateway_capacity.py.

All source paths above are under ops/fiber-f1be0929.

## Verification

Before repair the new gateway-capacity fixture failed all5methods: old estate,
host and provider restrictions rejected the requested behavior. After repair
the initial77focused tests passed. Additional regressions prove duplicate
prevention, independent progress past failed lifecycle cards, multiple launches,
native route refresh, probe reuse invalidation and low-memory telemetry.

Exact final command:

```
/home/skuser01/.skenv/bin/python -m pytest -q ops/fiber-f1be0929/tests tests/test_fiber_lifecycle_finish.py tests/test_fiber_lifecycle_recovery.py tests/test_fiber_lifecycle_connection.py
```

Result:349passed,3existingconditional skips in28.28seconds. Two skips require
the qualified local sandbox; one requires a remote resource check.
git diff --check passed before the final run.

## Rollout and limitations

Install only exact reviewed candidate files with per-host preimages and atomic
replacement under the existing authority mechanism. Rollback restores those
preimages and the previous timer, preserving run/reservation history and claims.
No state migration or source push is included. Review/helper candidates from
f34f26c5,31120409,1298966f and02a236d6 must be composed and tested separately.
The old native snapshot remains historical evidence; publish actual fresh
gateway truth through the reviewed publisher, never edit its seal manually.

Native card f7db7101 remains claimed by actual operator jarvis until exact
independent review, installed checks and rollout evidence are recorded.
