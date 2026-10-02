# Historical acceptance profile repair

Card `c1a30143`, owner `codex-historical-tests-c1a30143`, claim
`0a48695d5eb241d2a35bdd94b219967f`. Base
`019cda05e6600ac024a9733624e34fbe13fed73d`; dependency `c1a30135` read back DONE.
Status: candidate only. Independent review, root installation and current
runtime qualification are separate remaining gates.

## Change and cause

Completed source `a8300e02` and review `ac8b9cbd` retain a valid Node test
receipt. The historical helper incorrectly allowed only Python check IDs and
always read `pytest.xml`, despite the retained `vitest`, `typecheck`, `lint`
sequence and `vitest.xml`. All actual logs already matched their hashes.

`src/skcapstone/fleet/production_review_finish.py` now derives the exact
ordered checks using the existing `recipe_checks` or legacy `approved_checks`
and selects the JUnit variant using `is_node`. Plan checks and receipt length,
IDs and argv must match those templates; exit codes must be integer zero.
Source bindings, native completion/revisions, plan and receipt hashes, raw log
hashes, strict JUnit coverage, context, intent and acknowledgements remain
required. No current-runtime admission is added to completed history.

`tests/fleet/test_production_historical_acceptance.py` covers legacy Python,
qualified Python and Node history; omitted, duplicated, reordered, extra and
unsafe check IDs; changed argv, exit, binding, counts, logs, JUnit and native
proof; and failures, errors or skips even after rehashing synthetic evidence.
The successful replay test denies mutation/guard calls and compares all bytes.

The parent authorized the focused integration fixture update in
`tests/fleet/test_production_review_finish.py` to the current qualified c126
authority manifest. All six installed module hashes match exactly. It retains
fail-closed hash checks without fallback. Qualification provenance is the
c126 `PRODUCTION-TRIAL-PASS.json`, c135 `INSTALL-HANDOFF.json`, and committed
`docs/evidence/agents/c1a30126/AUTHORITY-DEPENDENCY.json`.

Task plan, canonical production runbook, changelog fragment and this evidence
complete the changed file set. No scheduler/cache, authority module,
predecessor-history, profile, timer or completed-card changes are included.

## Observed verification

```text
PYTHONPATH=src python -B -m pytest -q -p no:cacheprovider \
  tests/fleet/test_production_historical_acceptance.py \
  tests/fleet/test_production_test_node.py \
  tests/fleet/test_production_tests.py \
  tests/fleet/test_production_test_profile.py
243 passed in 86.31s; zero failures, errors or skips.
```

The pre-fix Node replay failed with `historical raw test output changed`.
Missing, duplicate, reordered and changed-argv receipts also exposed the old
helper's incomplete membership checks before the correction.

Private `REPLAY-v2.json` records actual retained acceptance with this exact
candidate module over installed qualified support. It accepts all 481 tests,
zero errors/failures/skips, with no commands, model calls, native test reruns
or mutations. The audit guard recorded zero prohibited attempts. All retained
acceptance, plan, run-output and report/decision hashes remained unchanged.
The separate d570 pair still refuses `replacement predecessor history changed`.
Private lock creation was omitted only in this diagnostic process; this does
not establish atomic board snapshots or qualify lock timing.

Ruff, changelog, whitespace, three AST parses and ASCII dash delta checks pass.
Black would reformat the two historical source/test files at the base as well
as the candidate; broad pre-existing formatting was left outside this repair.

The uncomposed integration run stopped on stale authority-manifest pins, then
on source-base versus installed native revision mismatch after the authorized
pin update. The explicit composition runner loads only candidate finish over
installed support. Its full run produced 11 passed and one failed in 52.10s.
That existing fixture's minimal `test_binding` contains only `source_head`;
its completed replay fails `invalid exact source binding`, an unchanged gate
present at the base. The preceding actual native completion succeeded. This
fixture was not given fabricated historical proof or an acceptance bypass.
The 243 focused checks and actual retained replay exercise complete history.

The feasible composed subset then passed 11 tests in 44.61s, with that one
explicitly deselected baseline-incompatible fixture. Its private runner and
`COMPOSITION-MODULES.json` retain every imported module path and SHA-256.
There were zero failures, errors or skips in the selected subset.

## Install handoff and rollback

Private evidence directory `~/.skcapstone/evidence/work/c1a30143/` is owned
0700, with owned regular non-symlink 0600 files. Root receives the exact local
commit/tree/ref, verified bundle, imported-module inventory, evidence hashes,
strict dependency composition and `INSTALL-HANDOFF.json`.

The sole runtime target is `fleet/production_review_finish.py`:

- Preimage: `45d23ff7de6c860cd7f1c264d34ba363c97c27955922df4ea8a93b97b3d6a09d`.
- Postimage: `a3a01a1b2a55a0f96b454e9f8d7a91eb0ad4a7b862caae0c1f45d3e6a6340724`.
- Runtime before: `6b90fc7ed518ea08941f4287ecbc0870b2eaa671ec2f2a06ff3d46ac78d4e8c7`.
- Projected runtime: `7bd350f243b8a3917271681bc442456d71474899b849b97774086de9fd49dcf8`.

Projection used unmodified fingerprint logic with its cache cleared and only
the exact target byte read replaced once. It is not installed qualification.
Root must recheck preimage/dependency/runtime hashes, independently review the
candidate, install only this backed-up delta and verify installed replay.
Rollback restores the exact preimage and recorded mode, verifies the original
runtime fingerprint, and preserves every retained artifact, claim and timer.
There is no data migration.

The prior a830 calibration script requires an active source claim. That card
is now DONE, so it cannot be blindly reused. Fresh runtime admission requires
its own eligible/current-claim qualification, without reopening completed
cards or rewriting prior profiles/tests. No install, calibration, push or
claim release was performed here. Native evidence linkage is supplied by the
private exact-source descriptor before parent review.
