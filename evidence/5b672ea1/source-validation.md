# Native test adapter source validation

Card: `5b672ea1`. Owner: `codex-native-test-5b672ea1`.
Parent: `9b230773`. Date: 2026-10-01. Host: `chiap08`.
Base: `4d2a00dc08039c37a3cb00e5bd18fba757499617`.

## Changed files

- `src/skcapstone/fleet/production_test_plan.py`: exclusive operator plans,
  exact-source checks, bounded dependency fingerprint and JUnit coverage.
- `src/skcapstone/fleet/production_tests.py`: retained native service launch,
  exact terminal custody and raw receipt validation.
- `src/skcapstone/fleet/production_test_worker.py`: fixed sandbox checks,
  bounded output, real child exits, JUnit and invocation-bound receipt.
- `src/skcapstone/fleet/production_resources.py`: actual native builder/test
  and Pi units, including durable pending-test reservations.
- `tests/fleet/test_production_tests.py` and `test_production_test_lifecycle.py`:
  adversarial source/evidence/custody, executor, retained-unit and runtime tests.
- `docs/fleet/native-test-evidence.md`, changelog fragment and this report.

## Exact checks and results

Using `/home/skuser01/.skenv/bin/python`, Python 3.12.3:

```text
PYTHONPATH=src python -m pytest -q -p no:cacheprovider \
  tests/fleet/test_production_tests.py \
  tests/fleet/test_production_resources.py \
  tests/fleet/test_production_builder.py
61 passed in 3.13s; zero failed, errors or skipped

python -m ruff check src/skcapstone/fleet/production_tests.py \
  src/skcapstone/fleet/production_test_worker.py \
  src/skcapstone/fleet/production_resources.py tests/fleet/test_production_tests.py
All checks passed!

python -m black --check --line-length 99 <same four Python files>
4 files would be left unchanged; exit 0

git diff --check
exit 0

python scripts/changelog_fragments.py --check
85 pending fragments including this card; exit 0
```

The source suite includes actual clean Git fixture head/tree checks, dirty and
stale source rejection, binding changes, private-file and link checks, fixed
argv validation, raw-output hashes, JUnit coverage, failed/error/skipped cases,
Boolean-success rejection, service PID/invocation/exit mismatch, lost launch
acknowledgement, repeated launch calls, quota argv, pending reservations,
actual bounded subprocess exit and timeout/reaping. Executor/service fixtures
are explicitly simulated and are not production receipts.

Two real inert bwrap probes used temporary fixture source and the qualified
interpreter, without launching a systemd unit or running the production trial:

```json
{"readonly":true,"interfaces":[[1,"lo"]],"home":"/tmp","source":"unchanged","private_home_visible":false}
```

Isolation probe exit 0; source bytes unchanged; writes succeeded only in
private tmp/output. Ruff fixture probe exit 0, `All checks passed!`; no source
cache directory created. The initial probe exposed ruff attempting to write
`/work/.ruff_cache`; the final sandbox uses `RUFF_CACHE_DIR=/tmp/ruff-cache`.

## Acceptance evidence and limits

Plans bind source owner/card/claim/head/tree/native revision/criteria, interpreter,
policy and fixed check profile. Receipt validation rehashes raw artifacts,
checks actual clean source, recomputes JUnit coverage and checks separately
observed exact unit invocation/PID/exit. Launch intent is fsynced before the
unique unit starts. Unknown custody retains the reservation and cannot replay.

An operator-authorized readiness-only run of the exact approved trial argv on
the retained `07dd08dcb1ec239f57c5b2391097131541b0f132` source passed all four
checks. Pytest reported 250 passed, zero failures/errors/skips and one expected
disabled-plugin configuration warning. Per-file counts were 40, 17, 68, 11, 15,
6, 3, 11, 15, 46, 7 and 11 in approved plan order. Compile, lint and changelog
each exited 0. HEAD, status including ignored caches, and all refs were identical
before and after. No native acceptance receipt was created by this readiness run.

The initial inert native unit probe demonstrated immediate successful-unit
garbage collection. The corrected test-only retention probe used the same node
CPU 200%, RAM 3 GiB and TasksMax 256 with a bounded 30-second inert lifetime.
Actual observation: loaded, active/exited, MainPID 0, ExecMainPID 1927577,
ExecMainCode 1, ExecMainStatus 0, empty ControlGroup, TasksCurrent `[not set]`,
invocation `6ed2e0bacb154689ba3dffe3fdb98658`. The exact main PID was absent.
The controller persisted this raw terminal state before exact-unit stop, reaped
systemd-run exit 0, and repeated stop safely after collection. This inert unit
is not a production test result. Dependency drift tests were added after the
initial 61-test source run above. Final command added
`tests/fleet/test_production_test_lifecycle.py` to the same pytest invocation:
76 passed in 21.05 seconds, zero failed/errors/skips. Ruff passed for all four
runtime modules and both test files; black left those six files unchanged;
`git diff --check` exited 0.

Independent Zai review of `1dbc0f05a6b3d5c40ba828889ae80f538b0322d3`
passed the tests/docs scope and identified one runtime blocker: a repeat call
rejected a successfully stopped unit that was still loaded inactive/dead.
The original FAIL receipt is preserved in private card evidence. A narrow
regression reproduced the failure before correction. The correction accepts
only the same invocation, MainPID zero and exact empty-cgroup state as already
stopped; a different invocation still fails closed without a stop call.
An actual inert D-Bus RefUnit probe kept the stopped unit loaded to exercise
this case: inactive/dead, invocation `770d7ac75c14449aa3806bd842774c26`, MainPID
zero, empty ControlGroup and TasksCurrent `[not set]`. Replay succeeded and
reaped the wait child; after UnrefUnit, collected-unit replay also succeeded.
Final corrective source/test delta receives its own exact-commit review.
The final four-file pytest invocation passed 78 tests in 21.28 seconds with
zero failed/errors/skips. Six-file ruff/black and `git diff --check` passed.
The inert probe unit list was empty after cleanup.

No live plan was sealed, production test unit launched, candidate card accepted,
runtime installed, protected data accessed or source pushed. The initial fixed
profile only supports the approved twelve-file trial plus compile/lint/changelog;
it does not implement arbitrary model test commands. The parent integration
owns current native claim/revision and source/reviewer terminal guards and the
dispatcher resource-query callsite. Installed-module resolution and real systemd
terminal observation still require operator qualification.

No data migration occurred. Revert this source commit to roll back source;
preserve any later live plans, raw evidence and service custody. Independent
exact-commit source review is recorded separately in private card evidence.
