# cfa0416c candidate evidence

Status: source candidate for independent review. No deployment, card completion
or full fleet acceptance is asserted by this document.

The native leaf was explicitly claimed by the actual operator jarvis with claim
e4fd2c22de61443fb603737d748aab6a on base
f63c233001a34c184517611d2eb60cf680f65be5. The broad user rollout goal authorizes
this separate test-plane implementation. Original09cf4fec ownership and its
worktree are preserved.

## Change

- `fiber_test_plane.py` compiles private expiring requirements into the existing
  candidate-bound test plan. Only qualified unittest and pytest commands run.
- `fiber_test_worker.py` uses existing bundle import and private clone helpers,
  systemd budgets and the curated sandbox. Source and dependencies are read-only;
  output is private. No model, network credential or completion authority enters
  the sandbox. Current source identity is checked on the authority before and
  after execution. Resource admission reserves 3GiB plus 1GiB host headroom.
- `fiber_test_harness.py` collects framework results rather than trusting printed
  summaries. Missing reports, wrong process exits, duplicate reports and
  unsupported invocation forms fail closed. Subtest failures count test methods.
- `fiber_test_cycle.py` persists launch and acceptance evidence, keeps one test
  reservation per host, observes ambiguous launches without replaying them, and
  publishes retained logs through unchanged `verified_tests`. A busy host waits
  before consuming a launch intent. Receipts survive a controller restart.
- The existing lifecycle invokes testing after independent PASS and before
  `finish_pair`. The existing sole controller applies HOLD on failed or
  ambiguous execution. Missing requirements remain disabled. No second timer,
  provider call, new completion writer or model-admission exception is added.
- Five focused test modules, three native lifecycle callback cases, the public
  fixture qualification script, plan and changelog document the boundary.

## Validation

Command from the isolated worktree:

```text
/home/skuser01/.skenv/bin/python -m pytest -q ops/fiber-f1be0929/tests tests/test_fiber_lifecycle_finish.py tests/test_fiber_lifecycle_recovery.py tests/test_fiber_lifecycle_connection.py
339 passed, 3 skipped in 26.92s
```

The three preexisting conditional skips are two qualified local candidate
sandbox checks and one remote resource check. New native sandbox tests actually
ran and passed. The unchanged native lifecycle completion and recovery suites
use isolated native boards, not the live board.

`ruff check --select F` on the new core modules and principal new tests passed.
`git diff --check` passed. This is not a claim that all legacy scripts pass the
repository's full style rule set.

Initial harness testing exposed inherited `/tmp/pyproject.toml` configuration
causing pytest to scan outside its disposable fixture. An explicit fixture-local
pytest.ini fixed the test isolation; production pytest semantics were not
weakened. Two initial private-file fixture tests correctly failed for writable
group modes, then passed with private test fixture modes. Original test results
remain in the session history.

Actual bounded public fixture qualification ran on chiap04 in unit
`skfleet-test-cfa0416c-ab416dcd7d604c1fac22d6ebff32ca69.service`, invocation
`8a0dc6b8cc714521be2715ad07cca21a`. systemd reported MainPID0,
ExecMainCode1, ExecMainStatus0 and active/exited. Kernel limits were verified:
2CPUs, 3GiB memory, 512MiB swap, 256tasks. Three sandbox assertions passed;
the intentionally failing fixture reported one failure and exit1; the early
exit0 fixture produced no framework report. Original inputs, logs and reports
remain in the remote private evidence directory named by the token.

Controller copies:

```text
~/.skcapstone/evidence/work/cfa0416c/native-ab416dcd-RESULT.json
sha256 f7669e759161420c7af6581d9dd430d804e53e6874377e23f39f1d86d5fb6ca1
~/.skcapstone/evidence/work/cfa0416c/native-ab416dcd-MANIFEST.json
sha256 ac3387cd07a4ee5fc3e7512e43619f74043b284a01c504607f3904ffaa483fa0
```

The manifest pins that qualification snapshot. Subsequent controller recovery
and preflight changes passed their focused regressions. Native fixture proof
does not assert source-bundle materialization, a native source claim, independent
model review, or a live automatic completion cycle.

## Remaining gates and limits

Independent exact-candidate review, reversible runtime installation, installed
verification and the full live lifecycle canary remain required. Retain HOLD
and all estate/provider caps. Three older terminal shutdowns still require the
pending human answer; no activity discount was invented from old rollout tails.

Tests are arbitrary source code. Framework result instrumentation is evidence
from reviewed test source, not a cryptographic proof of semantic coverage or
protection against malicious code modifying its own interpreter. Independent
source review and qualified commands remain mandatory. Required test source
must support the read-only repository and isolated runtime; unsupported tools,
network-dependent checks and missing environments remain blocked.

No data migration, push, merge, gateway change or application deployment occurs.
Installation must back up exact preimages and preserve the independently deployed
usage bridge hash25115162e88ecd306ed010b195b5eb898b79edb0b57b54ce9c417277e370e228.
Rollback restores only changed runtime files under the existing authority lock
and HOLD, preserving test evidence and all unrelated sessions.
