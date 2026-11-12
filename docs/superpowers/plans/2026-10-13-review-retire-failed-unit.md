# Failed unused review offers and backoff diagnostics

Goal: retire an unused expired offer despite an older failed transient unit, and
name review backoff accurately in the histogram and coord gates.
Spec: ~/reviews/review-backlog-triage-20261013.md sections 2.1B, 4.5c and 4.5d.

1. Add regression tests for the unused-offer recovery sweep and receipt readback.
   Require strict own-host observations and refuse active/activating/unknown
   states, live main/control PIDs, nontransient units, sessions, status and launch
   evidence. Keep invocation matching for acknowledged runs.
2. Add an explicit unused-offer mode to production_review_custody.unit_terminal.
   Reuse its stopped-failed predicate in review_retire's prestart path and
   retirement receipt validation. No service or board mutation outside existing
   governed retirement machinery; no resets or deployment from this task.
3. Run targeted retirement tests and commit immediately when green.
4. Expose descriptive backoff reasons in review_admission, the rotator histogram
   and coord_gate_diagnostic. Keep admission decisions unchanged. Add PASS,
   BLOCKED, dependency and cleared-outcome diagnostic regressions.
5. Run targeted tests, related recovery/diagnostic suites, Ruff and Black; add a
   changelog fragment, push and open the requested non-draft PR. Do not merge.
