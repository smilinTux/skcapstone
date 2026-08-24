# SKDashboard schedule contract compatibility 1.0.0

Status: frozen contract for SKCP-20A

Card: `c3a9c9e9`

## Boundary

The schedule contract is additive to the control-plane 1.1.0 contracts. The
four files under `schedule/v1.0.0/` define the canonical schedule projection,
immutable scenario, exact reschedule preview, and OpenAPI surface. They do not
implement a route, read protected data, create an owner-system mutation,
authorize a reschedule, deploy software, or execute an external action.

All instants are UTC date-time values. `display_timezone` is a required named
timezone for rendering and export. Every null date has a separate state and a
reason. A null date is never today, zero duration, or on time.

## Deterministic semantics

- Baseline, planned, and actual dates remain distinct.
- Baseline variance is known only when comparable values exist.
- Complete parent rollup uses the earliest eligible child start and latest
  eligible child end. Progress uses visible eligible children only.
- Partial rollup names exclusions and cannot claim completeness.
- Dependency edges preserve type, direction, lag, visibility, and evidence.
- A cycle makes critical path unavailable and carries cycle evidence.
- Missing required dates, unknown direction, inaccessible required nodes, and
  conflicting blackouts also make critical path unavailable.
- ITIL windows, blackouts, architecture migrations, and deprecations are typed
  overlays with independent truth and policy visibility.
- A policy-filtered record preserves source truth. Authorization denial is not
  `not_applicable` and cannot be aggregated as absent or complete.

## Scenarios and previews

A scenario is immutable, no-write, hash-bound to one projection version, and
has a stable reset reference. A reschedule preview must carry the exact source
projection version and base SHA-256. A base mismatch yields `stale` with no
typed proposal. A ready preview is non-executing and cannot write owner data.
Any future authorization or execution requires a separate eligible card,
CapAuth policy, exact-version human approval where required, and owner-service
receipt handling.

## No ranking

The projection requires `individual_ranking_prohibited: true`. The contract
contains no person, user, assignee, activity score, productivity score, token,
commit, cost, or Joule ranking field.

Rollback is deletion or reversion of this additive contract set before any
producer adopts it. No migration or runtime state is created by this card.
