# Authority-side claim binding

Card f1be0929, jarvis, 2026-09-29 21:54 UTC.

## Installed behavior

Only the chiap08 controller claims a new fiber job. The ordinary gated coord
claim command and readback remain authoritative. The controller verifies the
task fingerprint and source binding after claiming, then sends the exact
owner/revision with the launch request. No force-claim path was added.

Before creating a request file, the remote adapter reads the claim live from
chiap08 over existing SSH using the mediated coord show CLI under the worker
identity. It also requires its own local coordination copy to show the same
owner and exact revision, and both task fingerprints/source bindings to match.
It repeats these checks in the actual service process before materializing
the workspace. Remote workers no longer issue coord claim themselves.

Unknown authority, missing claim binding, unavailable authority, stale local
claim, changed task, or wrong revision refuses execution. An unacknowledged
launch continues to hold its capacity reservation and is never blindly
retried. No claim or reservation was released as part of this deployment.

The single-card coord show --json read is supported on all three target hosts
and chiap08. Claim checks now use it instead of fetching the whole board.

## Verification

- Local suite: 46 tests passed in 0.939 seconds.
- chiap04 suite: 46 passed in 0.221 seconds. Native unit
  f1be0929-controller-tests-r7-X6sJff.service, invocation
  d47d8bd67ebb46d6a6f96f74533d24cf, exit 0, runtime 572 ms.
- Ruff F checks passed for all controller modules and fiber tests.
- New tests cover authority-side claim before remote launch, exact revision
  propagation, failed claim preventing remote launch, missing binding before
  any request write, authority/local mismatch, unavailable authority, mediated
  read-only SSH under the worker identity, and wrong-generation refusal.
- Actual installed read_claim ran on chiap02/03/04 against the existing
  f1be0929 owner jarvis and revision fa5f2819810f4cf08fe5a35ea58da010. Each
  accepted that generation and refused a deliberately wrong zero revision.
  No card was claimed or changed by these read-only checks.
- Each target independently read chiap08's same live claim through SSH.
- One intermediate remote suite failed because a legacy test spied on a
  private helper absent from chiap04's older library. The test now checks the
  public consumer's launch/materialization callbacks and early actuation gate.
  No production package or safety gate was weakened. The full rerun passed.
- Installed controller --once returns admission-held. Timer remains disabled
  and inactive. Fresh collector remains complete, no errors: 10 DeepSeek,
  5 GLM, 6 conservative Codex observations, 1 unknown. No new worker launched.

## Installed hashes and rollback

| File | SHA256 |
| --- | --- |
| fiber_admission.py | fa46f4d87147cf613875daf529505f008b9aed3b9c99dcb41f0dc9f2faa87dee |
| fiber_control.py | 924fe3a242a0bd2db597837e88a2c2592db2a83d24eb79e9bc8a07593c39a95e |
| fiber_worker.py | 625fe8d70d1bee430d7405d4d9a38e05ffeb62e48c063091692a3ffe3d64e408 |

Root prior copies: backup/before-central-claim alongside this report.
Remote prior copies: ~/.local/lib/skfleet-fiber/fiber_admission.py.before-central-claim
and fiber_worker.py.before-central-claim. Both installed remote hashes match
on chiap02/03/04. Keep admission held during any restore and restore the
controller/admission/worker set together; the old adapter claimed locally.

## Remaining gates and limitations

This is a startup claim fence, not continuous distributed consensus or proof
that replication is repaired. A newly claimed card whose local replica has
not arrived will refuse launch. Its claim and ambiguous reservation are
preserved for exact reconciliation, not automatically discarded or retried.
That recovery path needs review before an unattended rollout.

The live implementation/review/test canary is still unexecuted. Source-bound
stage requests, artifact handoff, independent review, fresh qualified route
completions, available capacity, external acceptance, and useful-work metrics
remain required. Do not enable the timer merely because these tests pass.
