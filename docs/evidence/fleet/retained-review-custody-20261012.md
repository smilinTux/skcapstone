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
