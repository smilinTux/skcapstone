# Retained review custody recovery

Design: retire only an exact obsolete offer after proving destination inactivity.
Do not translate worker failure into a review PASS or FAIL. Preserve original
request, status, terminal evidence, current card revision and native release.
Retirement records a superseded disposition. New offers still use current source,
criteria, placement and independence validation.

Two paths remain distinct:

1. Unlaunched offers use existing prestart proofs, including a stale heartbeat
   for admissions, empty invocation, no launched event, exact admission inventory,
   no exit for that claim, and destination process/session absence. An explicit
   supersession mode records old and current criteria rather than requiring
   equality or current producer availability. Expired unused offers are swept too.
2. Running generations use native offer/launch/claim bindings, an exact failed
   worker or production-review exit (or exact systemd failure), and a fresh strict
   destination death query. All proofs must pass before exact claim release.
   Successful production exits remain acceptance custody.

Implementation plan: add regression tests for drift and failed production exits,
commit tests before implementation, extend existing retirement helpers, wire the
existing recovery sweep, test refusal and archive verification, and document the
read-only per-card investigation. No board operations, deployment or merge in
this task.

Observation: all nine reported criteria refusals are newer unlaunched admissions.
Their launched predecessor offers already have recognized retirement receipts.
Do not use a predecessor exit to retire a different claim. The three source-only
blockers have no remote offer, and require separate source eligibility resolution.

Validation on 2026-10-12:

- Tests first: three expected failures, eleven refusal cases already passing.
- Final review and remote review suite: 278 passed.
- Ruff, Black and git diff whitespace checks passed on changed files.
- All 31 requested cards were read through `coord show --json`.
- Strict SSH queried reserved units on chiap01 through chiap04. All 28 retained
  offers had failed or absent units, zero MainPID and ControlPID, and no matching
  tmux session. This is inactivity evidence, not proof that all are retirable.
- Check-only calls qualified 26: nine pending admissions, fifteen unused offers,
  and two acknowledged OOM failures (64eb3cd1, 87af9021). No recovery sweep was
  applied to production. No claim or board write occurred.
- 46243acb is still refused: its unused offer has an older failed unit loaded;
  the no-status path continues requiring unit absence. 5bd4261b is still refused:
  its exact production review exit succeeded and remains acceptance custody.
- 10191291, 816f7f38 and a950ce42 have no remote offer. Their source cards are
  unowned in backlog; no retirement can repair that source eligibility gate.
- All nine pending criteria differences are solely the worker_died link. Six
  match the original digest after removing that link; three match after restoring
  its prior recorded value. Their older launched offers are already retired.
- Only 44a54f90 and 587ec6eb currently match source custody among the 28 retained
  offers. Supersession clears obsolete custody; it does not waive source gates.

Evidence inventory and per-card observations are in the operator report
`~/reviews/retained-custody-reviews-20261012.md` and its local evidence directory.
No deployment, merge, successful review verdict, or live recovery is claimed.

Automatic supersession also refuses a collected invocation whose journal only
proves completion. A failed exit or exact systemd failure is required; delayed
successful receipt synchronization cannot be treated as failure.
