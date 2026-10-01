# Test concurrency source candidate

Card: `1298966f`. Producer: `codex-test-concurrency-1298966f`.
Base: `5f621b47bfe98efe4bdee59282cb06ba5c20bf7b`.
Branch: `work/1298966f-test-concurrency`.

## Change

Test reservations are keyed by source card and bind one immutable request.
Independent cards can start tests on the same host. A conflicting request for
the same card is refused. Historical host reservation files remain unchanged;
they no longer impose a host scheduling limit.

Execution locks are inside each request's token directory. A durable immutable
execution intent is published before materializing source or running tests, so
the same request cannot execute concurrently or replay after an interruption.
Historical host locks remain unchanged and do not block independent requests.

Host CPU, RAM and load admission thresholds were removed from check/start. All
six installed worker hosts are accepted: chiap02, chiap03, chiap04, chiwk12,
chiwk13 and ziowk01. Gateway and authority hosts are still outside this placement
contract. An old adapter reporting host capacity is an unverified preflight,
not a new scheduling authority; it cannot consume an execution intent.

Service resource isolation remains CPU200%, RAM3GiB, swap512MiB, Tasks256 and
1200 seconds, with NoNewPrivileges and the existing resource verification.
The root rollout owns qualification and adjustment against each node's actual
hardware. This source candidate does not claim the uniform profile fits every
currently loaded host.

## Verification

The initial reproduction had four failed test methods plus two failed placement
subtests. The focused tests now pass. A barrier requires two independent request
executors to overlap, while two real unittest harness subprocesses produce
actual reports. Their exact request/result hashes and passing counts are
checked. A held historical host lock does not serialize these requests.
Simultaneous duplicate locking and a later repeated execution both refuse
before source materialization or another framework run.

Full boundary command with `PYTHONPATH=src` and the existing `.skenv` Python:

```text
python -m pytest -q ops/fiber-f1be0929/tests tests/test_fiber_lifecycle_finish.py tests/test_fiber_lifecycle_recovery.py tests/test_fiber_lifecycle_connection.py
```

Result: **347 passed, three existing conditional skips, 28.23 seconds**.
The skips require a qualified local sandbox or remote resource check.
After removing the obsolete capacity-response compatibility branch, the changed
test cycle/worker/plane suites plus lifecycle connection tests were rerun:
**58 passed, zero skips, 4.98 seconds**. `git diff --check` passed.

Coverage retains source and claim changes, required PASS review, exact invocation,
lost acknowledgement, missing or changed receipts, failures, unauthorized skips,
log hashes, requirement expiry, sandbox boundaries and service resource flags.

## Rollout, migration and rollback

No deployment, model launch, board completion or external push occurred.
Independent exact-candidate review remains required. The root must install
matching test-plane and worker modules on all six workers and the matching cycle
module on the authority with exact preimages and native tests.

No card data migration is performed. Old host reservation and lock files remain
at their original paths. Existing immutable controller request intents are
reused; an in-flight request acquires its new card pin without reissuing start.
The only new worker artifact is an execution-intent record within the existing
request directory. Partial execution is preserved for diagnosis, never retried
under the same identity.

Rollback requires reconciling active invocations first, then restoring exact
module preimages without deleting new request evidence. The old controller's
host reservation semantics must not be reintroduced while new concurrent test
services remain active. Installed all-host runtime proof and per-node resource
qualification remain root rollout work.
