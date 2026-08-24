# SKCP-23 DORA architecture CMDB capacity and drift qualification

Date: 2026-08-24

Card: `866ffaac`

Base main: `aa2529c252bdaecb6d08ec4d50475bb690f19573`

## Delivered slice

The read-only `/control-plane/architecture` workspace projects folded CMDB
records and approved aggregate adapters through the existing typed CapAuth and
request-local currentness boundary. It includes:

- the five current DORA metric families at service scope with exact definitions,
  version, balancing context, exclusions, and explicit Unknown values when an
  approved deployment-event source is absent;
- release quality, ADR freshness, technical-debt exposure, lifecycle risk,
  owner coverage, relationship integrity, verified reconciliation drift,
  unsupported components, capacity pressure, and SKPerf regression signals;
- CMDB nodes and relationship-table equivalents with owner, environment,
  evidence age, source authority, scan ID, reconciliation state, transitive
  dependent count, cycle state, and impacted services;
- architecture exceptions that reach bounded CI evidence in two interactions
  and preserve missing decision links as Unknown; and
- approved SKPerf aggregate fields only, excluding raw corpus targets,
  protected paths, samples, benchmark details, and unapproved runs.

The projection derives lifecycle and unsupported state only from explicit folded
CMDB status or tags. It does not infer technical debt, lifecycle dates, service
ownership, DORA outcomes, release quality, targets, or regression baselines from
repository activity, names, versions, card movement, or model output.

## Qualification

- Focused architecture tests: 7 passed.
- Rebased changed protected-boundary tests: 55 passed.
- Full repository suite: 469 passed with 6 existing deprecation warnings.
- Ruff over `src/` and `tests/`: passed.
- JavaScript syntax checks over all static and qualification scripts: passed.
- Chrome 151 CDP qualification: 15 metrics, 3 CIs, 2 relationships, explicit
  DORA Unknown, approved SKPerf regression and capacity values, exception-to-CI
  keyboard detail and focus return, keyboard role change, 390 px and 320 px,
  zero writes, external requests, or browser exceptions.

## Acceptance evidence

1. DORA metrics use `dora-2024-five-metrics`, remain service-scoped, expose
   balancing context, and prohibit individual ranking.
2. Topology and blast radius expose relationship authority, owner, environment,
   evidence age, reconciliation state, dependent count, and impacted services.
3. ADR, lifecycle, technical debt, capacity, and SKPerf metrics expose approved
   definitions, targets, baselines, and uncertainty. Missing sources or targets
   remain Unknown rather than zero.
4. SKPerf input is the existing `approved_benchmarks` aggregate contract only.
   No raw target, protected path, sample, or benchmark-detail field is emitted.
5. An exception opens its bounded CI evidence in one click or keyboard action,
   with affected services and decision state visible in the exception row.

## Limits and rollback

This slice does not discover, reconcile, mutate, deploy, dispatch, rank people
or teams, access protected Matter content, or access HammerTime. Approved
service deployment events, release-quality gates, ADR review targets,
technical-debt records, lifecycle dates, capacity targets, and SKPerf baseline
identifiers are not present in current bounded owner contracts, so those values
remain Unknown.

Rollback is reverting the implementation commit. No migration, owner-record
mutation, runtime deployment, or external effect is created by this card.
