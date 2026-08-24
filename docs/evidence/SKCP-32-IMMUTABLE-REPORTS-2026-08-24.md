# SKCP-32 immutable report snapshots and reproducibility qualification

Date: 2026-08-24

Card: `38731952`

Base main: `754aabc8d15ffa160701caa0e5c8533039b17457`

## Delivered slice

The protected read-only `/control-plane/reports` workspace and GET-only report
APIs expose immutable report summaries, exact snapshots, explicit baseline
comparison, frozen metric results, calculation traces, source watermarks,
quality statements, AI narrative provenance, human review state, content hash,
and supersession.

Snapshot creation remains an offline library operation. The deterministic
builder:

- accepts the seven frozen report types;
- rejects Tenant, Matter, person, user, and agent scope;
- verifies metric truth, measurement kind, definition hash, calculation inputs,
  source watermarks, data-quality fields, and no-value invariants;
- derives definition hashes, source watermarks, and the report quality statement
  from exact frozen metric results;
- keeps typed AI insights and model provenance separate from metric values;
- creates a deterministic content-addressed snapshot ID and report hash; and
- validates against the frozen report snapshot schema in tests.

The local report store uses `O_EXCL`, `O_NOFOLLOW`, regular single-link checks,
bounded reads, canonical JSON, idempotent same-content writes, and a required
existing prior snapshot for supersession. A correction creates a new snapshot
and never rewrites or deletes the prior report.

## Qualification

- Focused report tests: 9 passed.
- Changed protected-boundary, contract, and navigation tests: 85 passed with 6
  existing deprecation warnings.
- Full repository suite: 500 passed with 8 existing deprecation warnings.
- Ruff over `src/` and `tests/`: passed.
- JavaScript syntax checks over all static and qualification scripts: passed.
- Chrome 151 CDP qualification: two immutable reports, supersession, typed AI
  provenance, no-value non-comparable baseline result, keyboard role state,
  390 px and 320 px layouts, zero writes, external requests, or browser
  exceptions.

## Acceptance evidence

1. Builder output validates against
   `control-plane-report-snapshot.v1.1.0.schema.json`, uses a deterministic
   content-addressed ID, and carries a verified report hash.
2. Definitions, source watermarks, scope, window, baseline, calculation
   references, quality, and frozen metrics reproduce from exact snapshot
   content. Tampering fails validation even if a caller recomputes only the
   report hash.
3. AI narrative is represented only as typed insights and separately collected
   model provenance. It cannot change or replace frozen metric results.
4. Supersession requires the prior snapshot to exist. Both prior and correcting
   snapshots remain readable and immutable.
5. Current, stale, partial, unavailable, unreachable, unknown,
   not-applicable, estimated, and forecast states remain visible through frozen
   metrics and the quality summary. No-value or definition-changed comparisons
   return `delta: null` and `comparable: false`.

## Limits and rollback

No HTTP route creates, corrects, supersedes, exports, subscribes, approves, or
dispatches a report. Snapshot creation is an explicit offline operation for an
approved caller. No production data migration or deployment occurred. The
workspace does not rank individuals, access protected Matter content, or access
HammerTime.

Rollback is reverting the implementation commit. Report files created later by
an approved offline caller are immutable owner evidence and must not be deleted
by application rollback.
