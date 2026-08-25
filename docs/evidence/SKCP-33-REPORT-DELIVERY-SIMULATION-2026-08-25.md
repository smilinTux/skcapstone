# SKCP-33 report delivery simulation evidence

Card: `631f90bf`

## Delivered boundary

- `src/skdashboard/report_delivery.py` adds a disabled-by-default domain
  service over the immutable `ReportSnapshotStore`. A draft has no outbox
  message and no delivery authority.
- Activation requires one exact sanitized `DeliveryApproval` that binds the
  report hash, destination, named audience, classification, source-rights
  reference, purpose, retention period, redaction profile, destination
  verification, approval state, policy decision, capability, schedule, and
  expiry. A caller-injected policy checker must return exact boolean `True`.
- Activation atomically changes the subscription and creates one idempotent
  outbox message. The idempotency record binds the exact approval payload.
- Only the exact built-in `SimulationDestination` is accepted. There is no
  network, HTTP, UI, MCP tool, connector, account, or arbitrary destination.
- Explicit destination errors retry with bounded exponential delay and stop
  after three attempts. An interrupted send has an unknown outcome and fails
  terminally with audit evidence rather than silently resending.
- Successful simulation writes a content-free exact receipt and moves the
  subscription and outbox to `receipt_verified`. Cancellation and unsubscribe
  stop queued or retrying work. In-flight and terminal deliveries cannot be
  cancelled.
- Confidential and restricted reports require metadata-only simulation.
  Protected Tenant or Matter reports fail closed and remain governed by the
  SKLegal external-action state machine and exact-version Approval gates.

## Acceptance evidence

1. `tests/test_report_delivery.py` proves disabled drafts, exact activation,
   policy denial, approval expiry, destination expiry, source-rights denial,
   schedule gating, duplicate idempotency, immutable report binding, and
   distinct subscription keys.
2. The same suite proves destination error, retry timing, three-attempt
   exhaustion, claim leasing, unknown-outcome terminal handling, policy
   recheck immediately before dispatch, cancellation, unsubscribe, and
   content-free receipts.
3. Redaction tests prove confidential report body text is absent from a
   metadata-only payload. Storage tests prove raw report section and metric
   content, bearer material, capability tokens, and secrets are absent from
   schedules, outbox state, receipts, audit projections, and database bytes.
4. Filesystem tests reject symlinked or loose-mode databases. Source scans find
   no network, subprocess, browser, shell, connector, HTTP route, UI route, MCP
   delivery resource, or production destination path.
5. Existing report, typed-client, MCP, CapAuth decision-context, read-only,
   queue, write-gate, and frozen-contract tests pass unchanged.

## Qualification

- Focused report-delivery suite: `23 passed`.
- Protected boundary suite: `139 passed, 4 warnings`.
- Full repository suite: `569 passed, 8 warnings`.
- Ruff over `src/` and `tests/`: passed.
- Ruff format over changed Python: passed.
- Wheel build and isolated install smoke: passed.
- Chrome 151 fail-closed workspace qualifier: passed with keyboard and focus
  return, 390 and 320 pixel layouts, delayed response purge, 401 and 403 purge,
  stale-response rejection, scratch cleanup, and zero writes, external
  requests, or browser exceptions.
- Git whitespace and prohibited-dash checks: passed.

The eight warnings are existing `jsonschema.RefResolver` deprecation warnings.

## Non-authorizations and limitations

- No report delivery route, UI control, MCP tool or resource, network client,
  connector, production destination, external account, deployment, service
  restart, or external effect was created.
- No production subscription or report-delivery state was created. Tests use
  temporary public-synthetic state and a deterministic in-process destination.
- No protected Tenant or Matter report is accepted. This module does not
  replace or bypass SKLegal's `draft -> validated -> approved -> queued ->
  dispatched -> receipt_verified` state machine.
- No HammerTime corpus or Inbox path was searched, read, moved, or processed.
- The policy checker is an injected fail-closed port. This card does not issue
  CapAuth credentials or broaden CapAuth policy.
- SQLite coordination is local-process and local-filesystem development state,
  not a multi-node production delivery store.

## Rollback

Revert the SKCP-33 merge. The change adds one package module, tests,
documentation, and changelog text. It changes no schema, runtime route,
deployment, owner record, immutable report snapshot, external account, or
production data. Temporary simulation databases can be deleted after tests;
no migration or external compensation is required.
