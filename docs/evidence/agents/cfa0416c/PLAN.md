# Qualified automatic test execution implementation plan

> Inline execution by the existing operator under the authorized bd9a559a goal.
> No additional agent or model worker is launched for implementation.

**Goal:** Automatically run the original card's qualified tests on its exact
candidate and provide the existing completion gate with actual test receipts.

**Architecture:** The existing dispatcher compiles an expiring operator-owned
requirements record into a candidate-bound plan. It starts and observes one
bounded systemd test service through existing SSH transport. The service uses
the existing private source clone and sandbox; the authority validates its
retained framework report, logs and exact invocation before publishing the
existing receipt format. There is no second timer or completion authority.

**Stack:** Python standard library, existing pytest prefix where qualified,
existing bubblewrap and systemd services, existing Fiber helpers.

**Spec:** Native card cfa0416c, read with `skcapstone coord show cfa0416c --json`.
Claim e4fd2c22de61443fb603737d748aab6a. Base f63c233001a34c184517611d2eb60cf680f65be5.

## Constraints

- No changes to original09cf4fec worktree, ownership or completion state.
- Existing installation HOLD remains. Capacity12,DS3/GLM5/Codex2,75seconds remain.
- No model credentials, host network, Docker, DBus, protected Matter content,
  arbitrary host shell commands or writable source/dependency mounts in tests.
- Runtime files live under existing private Fiber namespaces. Reject symlinks,
  unsafe ownership/modes, conflicting records, stale time or changed bytes.
- Lost launch acknowledgement is observed/reconciled, never blindly replayed.
- Failed tests retain evidence and HOLD. Missing evidence never becomes PASS.
- Source-only receipts do not assert hosted CI, merge or application deployment.
- Local candidate commits are authorized. No push or merge.

## Task 1: Qualified requirements and candidate-bound plan

Create `ops/fiber-f1be0929/fiber_test_plane.py` and
`ops/fiber-f1be0929/tests/test_fiber_test_plane.py`.

Interface: `test_plan(requirements, producer, now) -> dict`.
Requirements come only from the controller-private
`test-requirements/<source-card>.json`, never the candidate checkout.
Exact fields: schema,source_card,source_fingerprint,repository,checks,
qualified_at,expires_at,evidence_sha256,host,test_environment.
Schema is `skfleet.test-requirements/v1`. The head comes from the immutable
producer intent. The output uses unchanged `skfleet.test-plan/v1` fields.
Reuse finite TTL validation, `_read`, `_directory`, `digest` and `_once`.
Validate original fingerprint/repository/card, complete checks, pinned approved
host, framework/interpreter and qualified environment before any launch.

The test table must cover:

```python
# Given one private requirements fixture and one exact producer fixture:
plan = test_plan(requirements, producer, now)
assert plan['source_head'] == producer['candidate']['commit']
assert plan['checks'] == requirements['checks']
# Changing fingerprint, repository, expiry, fields, check IDs or interpreter
# raises Refused. A requirements file symlink never reaches this function.
```

- [x] Write binding, expiry, unsafe-file, unsupported-command and changed-plan tests.
- [x] Implement validation and no-clobber plan publication.
- [x] Run focused tests and retain failures before fixes.

## Task 2: Actual isolated test execution

Create `ops/fiber-f1be0929/fiber_test_worker.py` for bounded start/observe/collect
and a small `fiber_test_harness.py` for framework results. Keep each below500lines.
Add focused tests beside existing Fiber tests.

Remote request pins card, candidate head/tree/base/ref/bundle, plan hash,
requirements hash, host and random32hex token. The unit name is exactly
`skfleet-test-<source-card>-<token>.service`. Use existing resource limits:
2CPUs,3GiB memory,512MiB swap,256tasks, finite execution deadline.
Persist request before launching. Identical retry may observe the same unit;
conflicting bytes, missing known unit or invocation change must refuse.

Reuse `fiber_bundle.receive/import_source`, `verify_cache_origin`, `ensure_commit`
and `fiber_sandbox.private_clone` under the existing per-repository lock.
Do not execute Git against repository-controlled metadata on the host after
tests. The exact source and dependencies are read-only inside bubblewrap;
only private temporary/output storage is writable. No gateway socket exists.

Framework scope is Python unittest and pytest. The exact qualified command
must execute, with an immutable runner adapter collecting framework outcomes.
Retain bounded combined output and structured results. Do not parse printed
PASS text as authority. Missing, inconsistent or truncated reports refuse.
Unittest subtest failures must not be confused with failed test-method counts.
Framework reports prove observed test execution, not the semantic adequacy of
arbitrary test source; independent source review remains mandatory.

```python
# Execute a disposable public fixture in the real private root:
# one passing test -> exit0, passed1, failed0, errors0, skipped[]
# one failing test -> nonzero exit and no acceptable success receipt
# os._exit(0) before report -> missing report, refused
# test code printing a fake PASS summary -> no effect on framework result
# source write/network attempt -> denied by the sandbox
```

- [x] Add success/failure/missing-report/skip and resource identity tests.
- [x] Implement service request validation, private materialization and framework execution.
- [x] Exercise actual bounded native units on disposable public fixtures.

## Task 3: Sole-dispatcher reconciliation and receipt publication

`ensure_tests(directory, producer, *, requirements_path, transport, guard, clock)`
owns a durable `test-execution-intent.json` under the existing lifecycle token
directory. `transport` is the controller adapter to the remote worker.
It returns a waiting state until the exact unit is terminal and its evidence
is collected. Never block the authority lock for the duration of a test run.
Bound test admission by test-host headroom and one active test reservation per
selected host, retaining reservations across controller restarts.

Modify only the necessary `fiber_control.py`/`fiber_lifecycle.py` hooks so a
qualified independent PASS can trigger tests before the existing `finish_pair`.
Preserve the default disabled behavior when no requirements record exists.
The collector publishes actual logs and `skfleet.test-receipt/v1` to the
controller-private lifecycle directory, then calls existing `verified_tests`
without weakening or bypassing it. `finish_pair` remains the only acceptance
path and rechecks current review/source/claim/test identities.

```python
# First pass: persist intent, exactly one start, return waiting.
# Lost acknowledgement: next pass observes same unit/token; start count stays1.
# Completed exact unit: collect logs, publish receipt, verified_tests succeeds.
# Changed source/plan/claim/invocation/log or a failed required test: Refused.
# Receipt already present and identical: no rerun and no duplicate completion.
# HOLD arrives: no new admission or acceptance; running service is preserved.
```

- [x] Add restart, lost-ack, stale-binding, failed-test and receipt-order regressions.
- [x] Connect the bounded executor to existing dispatcher callbacks.
- [x] Run focused suites plus existing lifecycle/finish/native integration suites.

## Task 4: Exact review, installation and live cycle

- [ ] Record exact source/tests/results and limitations in this card evidence.
- [ ] Commit only scoped files locally, echo `git rev-parse HEAD` and verify clean tree.
- [ ] Obtain independent cross-provider review of the complete exact candidate.
- [ ] Install only reviewed files with original-mode backups, atomic replacement,
  hash checks and rollback under existing authority lock and HOLD.
- [ ] Verify actual installed-source tests and native test success/failure receipts.
- [ ] Coordinate scope with09cf4fec through native card links. A new model worker
  still requires capacity and all original admission gates; no exception exists.
- [ ] Prove assignment, candidate handoff, independent review, automatic required
  tests and native completion in the real pipeline before whole-rollout acceptance.

Rollback keeps HOLD, verifies current candidate hashes and restores exact
backups. New test artifacts, requests and service receipts are retained.
Never stop unrelated sessions or erase source workspaces during rollback.
