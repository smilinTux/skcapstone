# Native test adapter source validation

Card: `5b672ea1`. Owner: `codex-native-test-5b672ea1`.
Parent: `9b230773`. Date: 2026-10-01. Host: `chiap08`.
Base: `4d2a00dc08039c37a3cb00e5bd18fba757499617`.

## Changed files

- `src/skcapstone/fleet/production_tests.py`: exclusive operator plans,
  exact-source checks, native service launch and raw receipt validation.
- `src/skcapstone/fleet/production_test_worker.py`: fixed sandbox checks,
  bounded output, real child exits, JUnit and invocation-bound receipt.
- `src/skcapstone/fleet/production_resources.py`: actual native builder/test
  and Pi units, including durable pending-test reservations.
- `tests/fleet/test_production_tests.py`: adversarial source/evidence/custody
  and executor tests.
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
