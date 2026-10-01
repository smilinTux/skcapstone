# Production source custody reaper repair

Card: `f660bd1e`. Parent: `9b230773`.
Base: `ab063578c6ef26bbef88d3deacb97eca81576248`.

The legacy absence reaper classified an ended claimed producer as WORKER_DIED
and released its claim before review. This happened both without a proposal and
after PASS_FOR_REVIEW. The optional TTL reaper likewise attempted release when
owner activity aged out. Three isolated regression assertions reproduced these
paths without executing the dispatcher or mutating the live board.

The production-only guards run before generic outcome/release writes. They
require the current native source-only owner and claim plus exact source binding
and either an authoritative native production launch receipt with its private
sealed gateway observation, or a matching native remote request/status/unit and
preflight generation. A successful proposal is deliberately not required:
publication can be pending after the worker stops. Missing, changed, malformed,
wrong-writer and wrong-generation evidence cannot gain custody by filename or
owner naming. Legacy reclamation remains unchanged. Review, explicit typed
BLOCKED disposition and operator recovery retain their existing responsibilities.

Files changed: native production_custody helper, narrow absence/TTL guards in
skfleet-rotate.py, additive source_binding metadata in the existing production
launch receipt, focused regression tests and this evidence/changelog.

Validation command:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/.skenv/bin/python -m pytest -p no:cacheprovider -q tests/fleet/test_production_custody.py tests/fleet/test_production_receipts.py tests/test_skfleet_reaper_provenance.py tests/fleet/test_claim_expiry_reaper.py tests/fleet/test_production_exit.py
```

Actual result: 131 passed, zero skipped, 5.58 seconds. Ruff and git diff --check
passed. External log: task evidence directory `TESTS.log`. The initial reproduced
failures remain in parent evidence `CUSTODY-REAPER-REPRO.log`.

No runtime installation, activation, model-worker launch, source push or queue
mutation occurred. Exact independent review and parent composition/install are
pending. Existing local receipts without the new source binding do not acquire
this guard retroactively. No production trial has yet launched under this rollout.
Source rollback is the prior commit; append-only receipts and retained candidate
artifacts remain preserved. Worktrees and evidence must remain for the trial.
