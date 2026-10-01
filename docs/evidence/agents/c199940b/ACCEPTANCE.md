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
