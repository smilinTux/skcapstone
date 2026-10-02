# c1a30165 admission selection snapshot

Source base: `e7aff31e9e663834290ad8fb940c5ffa7f6a9aaf`.
Owner: `codex-admission-perf-c1a30165`.
Claim: `84569ddbacf04fada8a06ee5efc8b7fc`.

The launcher repeatedly invokes its validated native card fold while deriving
lifecycle, claimability, terminality and dependency facts in one selection
pass. Reuse the resulting snapshot only during that read-only pass. Copy on
storage and return prevents caller mutation from changing cached authority.
An explicitly different core input recomputes the snapshot. `fresh=True`
bypasses the cache, and the whole cache is discarded before dispatch.

Selection deliberately sees each card's first snapshot during that pass.
An event arriving mid-selection is observed at fresh preclaim. No cache is
used for a claim, lifecycle mutation, launch, or subsequent generation.

## Measured evidence

A private frozen sample contains 93 actual native card histories, sampled
evenly from sorted card IDs plus the current c136-c165 task family. It contains
561 event lines, 312,197 event bytes, median four lines/card and maximum 24.
The replay calls the launcher's actual lifecycle and admission functions in
the repeated pattern found in its selector. Native CardStore validation runs.

- Baseline: 372 native folds, 0.110079 seconds.
- Candidate: 93 native folds, 0.074436 seconds.
- All returned lifecycle states, admission fields and refusal reasons match.
- Native folds decrease 75%; this bounded replay's elapsed time decreases 32%.

This is not a full coordinator benchmark. Legacy overlays, projections,
external health queries, claims and launches are outside the replay. Missing
sample dependencies fail closed. Production generation duration and memory
impact are unmeasured. No live board, installed code, service, quota, route or
scheduler configuration was changed.

Private evidence directory: `~/.skcapstone/evidence/work/c1a30165/`.

- `PROFILE.json`: SHA256 `9cd25994af091e05b42a67ceb31b936685edadbf3b454d42a8254b15bd72573d`.
- `snapshot-manifest.json`: SHA256 `6aad6ed04126553c8e1d030e72ad5a5174d32d875c9235438f455c169bed312f`.
- `tests.log`: SHA256 `5767b21959e7d69d1351e5934d695557b557c9b4798fce1845a4333a5a3c44fd`.
- `profile_snapshot.py`, `before.pstats`, `after.pstats`, and profile summaries
  preserve reproduction and profiling evidence. Raw card histories stay private.

## Validation

`PYTHONPATH=src pytest -q tests/fleet/test_admission_snapshot.py
tests/test_skfleet_claimability.py tests/test_scheduler_decision.py
tests/fleet/test_production_dispatch.py -k
'not test_launcher_ruff_does_not_expand_the_exact_inherited_baseline'`

Result: **149 passed, 1 deselected in 9.14 seconds**. New tests cover native
fold reduction, identical decisions, caller mutation isolation, explicit core
mismatch, fresh bypass, disappeared source, and actual native claim, hold,
source and dependency changes between selection and preclaim. A dependency
that was complete during selection and is reopened before preclaim is refused.
The real admission fingerprint comparison rejects changed selected work.

Ruff and Black checks pass for the new test file. The excluded launcher lint
baseline test fails on both exact base and candidate: the historical test
expects at most 134 findings, while both versions produce the identical 147
findings by rule. `INHERITED-LINT.json`, both raw failure logs, and
`RUFF-BASELINE.json` preserve that existing failure. No baseline was relaxed.

## Handoff and rollback

Source-only candidate. Independent review and installation belong to the root
controller. No push, merge or deployment occurred. Installation needs only the
reviewed launcher change; retain and restore the exact preceding launcher
bytes for rollback. There is no data migration or persistent cache.
