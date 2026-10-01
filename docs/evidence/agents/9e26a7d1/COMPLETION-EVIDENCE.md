# 9e26a7d1 dependency-verdict admission evidence

Owner: pi-fiber-implementation-9e26a7d1.
Card: 9e26a7d1 — `[SKFLEET-DEPENDENCY-ADMISSION][S] Reject missing dependency verdicts
before worker admission`.
Repository: `https://github.com/smilinTux/skcapstone.git`.

## Binding

- Claim revision: `0a9750768dab44eabf945e93a663c016` (matches card `meta._claim_revision`).
- Exact workspace base revision: `cd8570bb72bf0df5103569a8d7399485af4fcb43`
  (matches card `meta.base_revision`).
- Base ref: `work/bd9a559a-final-runtime`.
- Dependency `41a6039b` is DONE with a native typed PASS (checked via the card
  snapshot and downstream contract; the sandbox has no `skcapstone` CLI, so the
  dependency projection is taken from the immutable controller snapshot).

## Observed defect

`fiber_control.eligible` admitted a source by checking only dependency
`status == 'done'`, unlike the lifecycle contract at
`fiber_lifecycle.py:99-101` and the selection contract at
`fiber_selection.py:102`, which additionally require
`links.verdict == 'PASS'`. Trial `83b5ae19` produced exactly this: four DONE
dependencies, one (`b74e8c10`) lacking `links.verdict=PASS`, and 47 Qwen
requests were spent before `lifecycle._producer` refused
`lifecycle-dependency-changed`.

## Changed files

- `ops/fiber-f1be0929/fiber_control.py` — `eligible` now refuses when any direct
  dependency is other than DONE with exact `links.verdict == 'PASS'`. This
  mirrors the existing lifecycle dependency contract; it does not change that
  consumer, weaken it, mutate a dependency, or add a global HOLD.
- `ops/fiber-f1be0929/tests/test_fiber_gateway_capacity.py` — six new regression
  methods in `DependencyAdmissionTest` plus a scoped fake native-gate module
  (`_FakeNativeGate`) used only at the unavailable installed-runtime boundary
  (`skcapstone.coord_gate_diagnostic`). `eligible` itself runs for real.

Exact base commit for rollback: `cd8570bb72bf0df5103569a8d7399485af4fcb43`.
Candidate commit recording this change: see `git rev-parse HEAD` at the end of
this task (local CANDIDATE commit, no push).

## RED before / GREEN after

Command (unchanged for both runs):

    /usr/bin/python3 -m unittest discover \
      -s ops/fiber-f1be0929/tests \
      -p test_fiber_gateway_capacity.py -v

RED on base `cd8570bb` with the new tests present and `eligible` unfixed:

    Ran 13 tests ... FAILED (failures=7)

Failures were the observed mismatch (DONE dependency with `{}` / non-PASS
verdict admitted) plus the missing-verdict, non-PASS, missing/non-DONE,
rejected-isolation, and both-decision cases — each returned `True` where the
contract requires refusal.

GREEN after the fix:

    Ran 13 tests in 0.003s
    OK

13 passing methods, zero skips; the original 7 `GatewayCapacityTest` methods are
preserved and still pass.

`git diff --check` exits 0 (clean).

## Acceptance mapping

1. Every direct dependency is DONE with exact PASS before native admission;
   malformed or missing verdicts refuse only the affected source —
   `test_missing_verdict_is_rejected_before_native_gate`,
   `test_non_pass_verdicts_are_rejected_before_native_gate`,
   `test_missing_or_non_done_dependency_rejected_even_with_pass`,
   `test_rejected_source_does_not_block_separate_valid_source`. The fake native
   gate records zero calls for every refusal, proving refusal precedes native
   admission.
2. Existing native gate and review-route checks remain authoritative —
   `test_all_dependencies_done_pass_reach_native_gate_both_decisions` proves both
   the positive and negative native decisions are honored when all dependencies
   are DONE/PASS; `test_no_dependency_source_still_reaches_native_gate` proves a
   dependency-free source still reaches the native gate. `eligible` does not
   change `refresh_review_routes` or `diagnose`.
3. At least 11 gateway-capacity methods pass without skips, including
   RED-before/GREEN-after evidence for the observed mismatch; exact local
   candidate is committed — Ran 13, OK, zero skips; RED/GREEN recorded above.

Regression coverage from the card:

1. DONE dependency with missing/non-PASS verdict rejected before the native gate,
   including the observed missing-link case — covered.
2. Missing dependency or non-DONE dependency rejected even with PASS — covered.
3. All dependencies DONE/PASS reach the existing native gate, preserving both
   positive and negative decisions, using a scoped fake native module only for
   the installed-runtime boundary — covered.
4. A rejected source does not prevent a separate valid source being eligible —
   covered.

## Limitations

- The sandbox has no installed `skcapstone` package or `coord` CLI; the
  installed-runtime native gate is exercised only through the scoped fake module
  at exactly that boundary. Independent review and the external test plane rerun
  the required command on the exact candidate.
- `test_fiber_control.py` has 3 pre-existing `ModuleNotFoundError: No module
  named 'skcapstone'` errors (unrelated to this change; they need the installed
  runtime). They are not introduced by this task.
- No full fleet suite, deployment, push, merge, credential or protected-content
  access was performed. Board operations are controller-owned.

## Rollback

Restore `ops/fiber-f1be0929/fiber_control.py` and
`ops/fiber-f1be0929/tests/test_fiber_gateway_capacity.py` from exact base
`cd8570bb72bf0df5103569a8d7399485af4fcb43`. Historical prerequisite evidence and
board state remain untouched.
