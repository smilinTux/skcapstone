# Shared production resource admission

Date: 2026-10-03. Card: `5291a019`. Status: source candidate, not installed.
Baseline: `0f4841bad55dace4949a0ca49af1b316d83ad9bd`.
Parent procedure: [Production Pi work cycle](production-pi-workers.md).

## Task design and implementation plan

The generation lock covers lifecycle waits and protects recovery ownership.
Operator launches formerly reused that lock for resource admission, blocking
unrelated work for minutes. Native tests used a different admission lock, and
worker and builder paths could check capacity without the same transaction.

1. Prove the race with synthetic multiprocessing tests and a held generation
   fence. Add crash, unknown-capacity, and exact-command checks before changing
   callers.
2. Add one host-local transaction in `production_admission.py`, then use it at
   the real worker, builder, and native test spawn boundaries. Preserve all
   existing source, policy, claim, retry, and lifecycle checks.
3. Verify the changed boundaries, preserve a local commit and private bundle,
   and independently review before a separately authorized installation.

The generation lock and its recovery behavior are unchanged. The new lock at
`fleet/resource-admission/<host>/.lock` covers fresh occupancy, outstanding
reservations, and durable intent publication. It closes before spawn, service
startup, or lifecycle waits. Native tests retain a per-run lock for their own
receipt processing; that lock does not serialize other launches.

## Reservation contract

`reserve_launch(home, policy, host, unit, binding, argv)` is the common boundary
for native workers, builders, tests, and operator-controlled launches. Callers
must first verify their exact current authorization and claim. The binding
includes card, owner, claim revision, and request/attempt identity where relevant.
The argv must target the local user service manager, use a worker or builder
unit name, retain every qualified quota, and separate options with `--`.

The transaction writes an owned private `intent.json`, fsyncs its bytes and
directory, and returns an argv with a non-secret `SKFLEET_ADMISSION_ID` marker.
Intent identity binds host, unit, and source generation. Changing command or
quota bytes does not permit replay of that same generation. The immutable
intent also binds the original argv hash and all quota values. No shell or
provider credential is persisted.

Every admission includes existing measured occupancy and each unobserved
intent's full RAM allowance. A pending intent transfers to normal live-cgroup
accounting only when its unit appears in the current occupancy snapshot and a
fresh query proves the exact marker, invocation, finite memory quota, and
current usage. This observation is durably acknowledged before its pending
charge disappears. The same live list is passed to the existing memory check.
Environment values are discarded after extracting the marker and are never
included in errors or evidence.

Only the existing RAM admission calculation is made atomic. The existing
CPUQuota, MemoryMax, TasksMax, and RuntimeMaxSec values remain unchanged and
are verified in the spawn command. This task introduces no aggregate CPU/task
policy, worker count limit, provider limit, launch spacing, or scheduler.

## Crash and custody behavior

- A crash before spawn leaves an unobserved reservation. No timeout deletes it.
- A crash after spawn but before acknowledgment can be reconciled by the next
  exact live observation. An unobserved service that already disappeared keeps
  its reservation until a separately qualified exact custody disposition.
- A crash during intent or acknowledgment publication leaves incomplete
  evidence that fails closed. It does not authorize another launch.
- An acknowledged generation cannot replay. Once its service finishes, normal
  live-unit accounting can admit a new, separately authorized generation.
- A nonzero worker launch result or uncertain builder spawn preserves the
  exact claim and reservation. Builder running custody is persisted before
  first spawn; retry/continuation retain their existing consumed-grant protocol.
- A final measured RAM refusal before any new intent returns a distinct
  deferral. First-launch callers may use their existing exact prelaunch claim
  release and retry without consuming an attempt. Retried or continued work
  retains its original claim. Unknown evidence never takes that release path.

Acknowledgment relies on native transient services having no autonomous
restart after collection. Command validation refuses restart configuration.
Explicit reactivation is a new authorized launch and must use this transaction.
Unknown state, marker mismatch, quota mismatch, malformed evidence, and
unqualified host capacity deny admission. The API does not release claims or
decide that a Work Product or task is complete.

The ledger scan is linear in retained reservations. No automatic expiry or
cleanup is added. Future compaction must retain replay and crash custody proof.

## Adoption and rollback

Do not switch only an operator helper to the new lock. Before activation:

1. Independently review the exact candidate and inventory every installed
   worker, builder, native test, and operator launch path on the host. Private
   pilot/install helpers are deployment inputs, not edited by this source card.
2. Establish an authorized boundary with no old launcher inside admission or
   spawn. Preserve existing workers, claims, source custody, and the generation
   fence. Do not kill a waiting process or unlink a live lock to achieve this.
3. Install and qualify all paths against the same module and canonical local
   stack home. Operator wrappers call `reserve_launch`, pass the exact source
   binding and qualified command, and spawn only its returned argv. They must
   not keep using the generation lock as their admission lock.
   The inventoried pilot installer also uses direct `systemctl restart` for
   retained auxiliary services. That operation is not admitted by this API.
   Qualify an exact-custody restart adapter, or replace it with a newly bound
   transient launch after proven termination, before retiring the old boundary.
4. Account existing native-test launch intents through the existing compatibility
   reader. Inventory any older worker/builder/operator in-flight intents whose
   unit was not yet observable; establish exact custody before new admission.
5. Qualify simultaneous mixed launches, lost acknowledgment, failed spawn,
   pending reservations, exact service markers, and unchanged quotas on the
   installed revision. Requalify native test profiles because runtime bytes
   changed. A source test pass alone is not rollout evidence.

No matter data migration is involved. Existing native test receipts retain
their original command verification; new receipts additionally verify their
exact durable reservation. Never discard the new ledger during rollback.
Rollback requires a separately authorized coordinated stop of new admission,
exact disposition of every pending reservation, and consistent restoration of
all launch paths. Preserve the generation ownership fence throughout.

## Source validation

The focused boundary suite passed: **608 passed in 51.31 seconds**, Python
3.12.3 and pytest 9.1.1. No installed service was started or changed.

```bash
PYTHONPATH=src python -m pytest -q \
  tests/fleet/test_production_admission.py \
  tests/fleet/test_production_resources.py \
  tests/fleet/test_production_tests.py \
  tests/fleet/test_production_builder.py \
  tests/fleet/test_builder_dispatch.py \
  tests/fleet/test_production_acceptance_hooks.py \
  tests/fleet/test_builder_retry.py \
  tests/fleet/test_builder_continue.py \
  tests/fleet/test_production_dispatch.py \
  tests/fleet/test_production_test_lifecycle.py \
  tests/fleet/test_production_test_node.py \
  tests/fleet/test_production_test_profile.py \
  tests/test_skfleet_reaper_provenance.py \
  tests/test_seat_cycle_orchestrator.py \
  tests/test_seat_cycle_orchestrator_budget.py \
  tests/test_fleet_duplicate_admission.py \
  tests/test_skfleet_worker_stop_semantics.py \
  tests/test_skfleet_worker_startup_runtime.py \
  tests/test_skfleet_worker_exit_evidence.py \
  tests/test_niobe_fanout.py
```

The new tests cover four simultaneous independent launchers, crash before
spawn and before/after acknowledgment, changed-command replay, reused unit
names, unknown/mismatched evidence, private files, unchanged resource limits,
retryable capacity deferral, and lost-spawn claim retention. Existing generation
and worker custody tests remain passing. Targeted Ruff lint, Python compilation,
changelog-fragment validation, and `git diff --check` passed.

Keep fleet test files together in the command above. Interleaving root test
paths between fleet files exposed an existing pytest fixture-discovery issue:
the combined attempt had 457 passes and 10 missing-`paths` fixture errors.
The unchanged baseline reproduced it with 90 passes and 8 fixture errors using
`tests/fleet/test_production_builder.py`,
`tests/test_skfleet_reaper_provenance.py`, then
`tests/fleet/test_builder_retry.py`. This candidate does not change pytest
configuration or claim that the full repository test suite was run.
