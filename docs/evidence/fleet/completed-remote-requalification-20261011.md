# Completed remote requalification rejected after runtime refresh

Base: 5810e619e77512c3caeed21f5971179b7e1c4341 (origin/main).
Branch: fix/passing-test-receipts.
Tests-first commit: 195d1d1b9e8693a45c13e28e9678432be01563cc.

## Problem

The first investigation correctly identified stale per-file recipes and
runtime fingerprints but did not inspect the later remote job failure records.
That missed a distinct coordinator bug: a completed valid receipt can coexist
with a job failed by the remote consumer's stricter runtime check.

Exact binding matches from native artifacts:

| Card | Successful receipt run | Job marked stale |
| --- | --- | --- |
| 41aad472 | 7b2a42f1a5a058a0c9f64c48101115b6babc689700955ce09b420cd1dee12c59 | 9341ae1537d690aedb02f78bf563cc885253de846f077fcf4aa4574eab2b5e8c |
| c33a1004 | fd46b416d0823ca8b18d81ca4781618afaf133ac41f49967e259a817c1dd2ce8 | b2d816dcb1c75a55d29290cb39836d38a57c2768d355f639579a0d07a60df781 |

The receipts have pytest exit 0, counts 2144 and 1946 respectively, no receipt
failure, and pass full validate_test_receipt against their exact bindings and
workspaces. The corresponding .job.failed.json files say
"operator test plan is invalid or stale". Source card, owner, claim, revision,
head, tree and criteria bindings match between each receipt and job.

## Root cause and change

profile_requalification.consume_remote called load_plan with its default
require_current=True before reaching run_or_read_tests. A later harness update
therefore made a completed plan stale at this early check, and the exception
handler wrote an immutable failed job marker.

The authority's _advance and production_tests already call load_plan with
allow_completed=True. Match that behavior at the remote consumer's early check.
This is one changed call, with an explanatory comment.

The native loader waives current runtime equality only when launch.json,
receipt.json and terminal.json all exist for the exact plan generation. It
still validates the plan. consume_remote still checks host, authority, profile,
source and claim, then run_or_read_tests performs full native receipt validation.
Missing completion artifacts still refuse runtime drift. Malformed or forged
completion artifacts still fail receipt validation. No new launch is authorized
by the mere existence of completed artifacts.

A receipt whose own failure records runtime drift during execution remains
invalid. This fix covers later consumption of a successfully completed receipt;
it does not bless runs executed across an unqualified runtime change.

## Verification

Before the production change, eight new consumer cases produced six failures
and two passes. Completed initial and refresh runs were wrongly marked stale;
invalid receipt and host cases never reached their specific proof checks.

After the change:

```
PYTHONPATH=src pytest -q tests/fleet/test_requalification_completed_remote.py \
  tests/fleet/test_profile_requalification.py tests/fleet/test_production_tests.py
```

103 passed in 4.23 seconds. The new real-plan cases cover each missing completion
artifact, strict default behavior, completed-plan loading after runtime drift,
and rejection of empty fake receipt evidence by the full validator. A missing
parent directory in that added fixture was corrected before the final run.

Ruff, Black and git diff --check pass for the changed source and tests.

## Scope and operations

Earlier audited recipe fixes remain on 8cbad4d9, c5b8d940 and e75915f6. Their
source-matched sandbox checks passed 1831 and 2146 tests with JUnit validation.
At continuation readback, their latest remote jobs still held the previous
108/145-target recipes, not the corrected 116/153-target recipes. A corrected
card recipe does not rewrite an already sealed job. Native subsequent
qualification must consume the corrected recipe.

No deploy, service mutation, claim release, receipt rewrite or job-marker
mutation was performed. Existing failed generations are preserved. Merge and
operator rollout remain separate; no live completion is claimed by these tests.

Files changed: profile_requalification.py (one call), a new eight-case consumer
regression file, four real-loader regression cases in test_production_tests.py,
a changelog fragment and this evidence document.
