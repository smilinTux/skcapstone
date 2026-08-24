# SKCP-22 ITIL and SRE reliability workspace qualification

Date: 2026-08-24

Card: `da097cbb`

Base main: `cdaff529c6ecff842ddce6499ab2047c0ef3bd6b`

## Delivered slice

The read-only `/control-plane/reliability` workspace projects folded SKCapstone
ITIL records through the existing typed CapAuth and request-local currentness
boundary. The workspace includes:

- explicit user-facing SLI, approved SLO, and error-budget measurements that
  remain Unknown when approved owner evidence is absent;
- MTTA, MTTR, full-population open response-target breaches, Problem-linked
  recurrence, terminal change lead time, verified change success, PIR coverage,
  and KEDB linkage;
- numerator, denominator, sample, window, classification, exclusions, legacy
  coverage, source owner, watermark, and evidence references for every metric;
- separately capped incident, Problem, change, KEDB, and breach-risk display
  records without using a truncated display as a metric denominator;
- traceable validation, CAB, scheduling, deployment, verification, PIR, and
  rollback evidence; and
- preserved INC, PRB, and CHG provenance aliases without creating new canonical
  domain types.

The shared ITIL overview now classifies only verified outcomes as successful and
failed outcomes as failed. Rejected and pending changes are excluded from the
change-success denominator. A closed change is classified from its accepted
terminal lifecycle edge.

## Qualification

- Focused reliability tests: 7 passed.
- Rebased changed-boundary tests: 39 passed.
- Full repository suite: 446 passed with 6 existing deprecation warnings.
- Ruff over `src/` and `tests/`: passed.
- JavaScript syntax checks over every static script and qualification script:
  passed.
- Chrome 151 CDP qualification: 11 metrics rendered; the breach display showed
  1 row while the metric retained numerator 10 and denominator 10; lifecycle,
  PIR, and KEDB evidence rendered; keyboard role change passed; 390 px and 320
  px layouts passed; zero writes, external requests, or browser exceptions.

## Acceptance evidence

1. SLI, SLO, and error-budget measurements are Unknown without approved owner
   records. Empty owner data also projects Unknown rather than observed zero.
2. Every metric carries numerator, denominator, sample, window,
   classification, exclusions, and legacy coverage.
3. The breach numerator is calculated before the eight-row display cap.
   Verified success versus failed is the only change-success denominator.
4. Incident-to-Problem links, KEDB links, CAB votes, validation, schedule,
   deployment, verification, PIR, rollback plan, and rollback event states are
   exposed as bounded evidence.
5. Legacy identifiers are emitted only as `legacy_alias` provenance values.

## Limits and rollback

This slice does not approve CAB decisions, validate, schedule, deploy, verify,
close, roll back, mutate owner records, dispatch external actions, rank people,
access protected Matter content, or access HammerTime. Service catalog and
approved SLO owner records do not yet exist in the bounded source, so those
measurements correctly remain Unknown.

Rollback is reverting the implementation commit. No migration, owner-record
mutation, runtime deployment, or external effect is created by this card.
