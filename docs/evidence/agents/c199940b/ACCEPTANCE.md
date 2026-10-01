# Gateway-derived Pi transport catalog

The user requires model IDs to come from SKGateway and card requirements.
This helper translates the exact public model row retained inside an acquired,
sealed route snapshot into Pi transport metadata. Card eligibility, model
selection and claim authorization remain with the native dispatcher.

`materialize_gateway_catalog(home, policy, snapshot)` performs no network call.
It rejects expired, tampered, unhealthy, missing and mismatched metadata, then
updates only `providers.skgateway` under an exclusive local lock. The existing
node-private gateway credential is preserved. If the generic provider is
missing, existing same-endpoint credentials must agree. Unrelated providers
remain unchanged. Every changed catalog has a private immutable preimage and
an atomic mode0600 replacement; repeated identical models produce no write.

No model identifiers, context limits or output limits are invented. When the
gateway supplies a generation default it becomes Pi's maxTokens; otherwise an
advertised output bound is used, or the field remains absent. DeepSeek's known
transport shape is represented through Pi's existing thinkingFormat adapter.

Regression first: the new test module refused to collect because the helper did
not exist. Final focused result: 130 passed, zero failed, zero skipped across
catalog, route snapshot, production policy, shared resolver and review tests.
Tests cover concurrent writers, no-op updates, symlink/FIFO rejection, stale or
tampered observations, immutable backup, exact new gateway IDs and preservation
of private credentials without returning their values. Ruff passed for new
Python files. The snapshot test proves model metadata is covered by its seal.

Command:
`python -m pytest tests/fleet/test_pi_catalog.py tests/fleet/test_review_capacity.py tests/fleet/test_production_dispatch.py tests/test_production_policy.py tests/fleet/test_production_review.py -q`

The source-only change is not deployed. Parent owns invocation and final source
composition. Configuration staging and measured seven-node quotas are retained
under the card's private evidence directory. Rollback restores the exact
node-local catalog preimage, preserving its original credential boundary; no
backup containing credentials is transferred off that node.

## Canonical production layout follow-up

The production layout uses one full script under `.skenv/bin`, bound to the
native interpreter, plus exact compatibility delegation under `.local/bin`.
Shared byte helpers make installation and drift verification agree. Production
drift checks use the checked-in production service templates and still inspect
canonical scripts when the legacy rotation timer is disabled. Legacy behavior
remains unchanged when no production policy marker exists.

The new regression first failed in six cases. Final focused validation passed
48 tests with no skips using `python -m pytest tests/fleet/test_rollout_drift.py
tests/fleet/test_deployment_manifest.py -q`. Cases reject changed script bodies,
wrong interpreters, duplicate full compatibility copies and absent shims. The
eight production templates preserve activation authorization and delegate old
rotation entrypoints to the serialized native cycle; no timer was enabled.

This commit stages source only. The exact overlay plan must record transformed
script hashes, original file bytes and modes, removed drop-ins and rollback
before deployment. Node eligibility remains a separate native configuration
step, with all gateway and coordinator roles preserved.

The effective drop-in audit retained the advisory dependency preflight once at
the canonical generation entrypoint and retained the existing exact-claim
fenced wedge actuator on Niobe. The misleading worker-readiness drop-ins only
set old timeouts and the wrong authority host; shared production settings
replace those. Fixed lane targets/model aliases are intentionally superseded.

## Production readiness compatibility

The canonical readiness service inspects the full native dispatcher and the
effective seat-cycle environment, scoped by its canonical timer. After the
native interpreter validates the actual shared policy and host authority, only
legacy lane-count and gateway-environment requirements are replaced. Unknown
required variables, dispatcher imports and service module imports still fail
closed. Missing policy cannot fall back to legacy configuration for the
canonical scheduler. The observer never launches work or invents installed
generation metadata.

The combined readiness, legacy wiring, import, drift and manifest suite passed
92 tests with zero failures/skips. Eight new cases use the real native policy
parser, including invalid Kimi enablement, wrong authority, missing/symlink
policy, unavailable validation, genuine missing imports and unrelated required
variables. The production readiness systemd template validates successfully.
