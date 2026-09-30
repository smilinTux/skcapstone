# Candidate evidence: 005ec3ec

Producer: jarvis (Codex). Parent: 3ee76566.
Exact preceding source: 85edcb6f62e63b2f1d6f0a3ddbcb9eddc0354bba.
Outcome: PASS_FOR_REVIEW only; no live installation or full unattended resumption claimed.

## Repair

The legacy rotation invoked `run_production_cycle` even when `DRY=True`.
That adapter can assist, reconcile and retire workers. Its builder offer block
also lacked the dry guard and could publish requests consumed by remote nodes.
The two existing blocks now require `not DRY`; live behavior is unchanged.
No source selection, claim handoff, timer configuration or capacity was altered.

## Reproduction and tests

- Before repair, actual entrypoint blocks isolated through AST: dry watchdog and
  dry builder tests failed by recording one prohibited call each; both live
  controls passed. First harness attempt lacked a diagnostic fake, corrected
  before establishing this two-failure reproduction.
- Focused pytest command covering rotation safety, worker liveness, wedge reaper
  and dispatch writer identity: 69 passed in 1.98 seconds.
- Builder dispatch suite run separately: 57 passed in 3.54 seconds.
- Combined invocation initially reported 77 passed and 49 fixture setup errors
  (`paths` missing). This is not a combined-suite pass. Independent suite runs
  load the existing fleet fixture and pass; no fixture or product workaround added.
- `git diff --check`: passed.

The new entrypoint tests also support stdlib unittest for the isolated reviewer
without installing packages. They execute only actual selected AST blocks with
recording fakes, never the live dispatcher. This proves these two actuation
guards, not absence of all diagnostic file writes throughout the entire script.

## Installation and rollback contract

After independent review, apply only the two exact substitutions to each matching
deployed dispatcher. Preserve per-file old bytes, mode and SHA256 before editing.
Do not overwrite whole deployed files with this snapshot: versions differ.
Verify deployed syntax and the same four isolated entrypoint tests afterward.
Keep legacy launch timers disabled. Rollback restores only a hash-matching
changed dispatcher from its preserved backup; no claims/workspaces are removed.

The remaining integration needs qualified queue supply and lifecycle bridging,
then an end-to-end review/WIP/CI canary. This repair alone does not finish it.
