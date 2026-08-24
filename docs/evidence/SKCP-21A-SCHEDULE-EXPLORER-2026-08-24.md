# SKCP-21A schedule explorer qualification

Date: 2026-08-24

Card: `eddaa1fb`

Base main: `e63b9a892ec95507ef9dd615a5db57cecf8ce890`

## Delivered slice

The read-only `/control-plane/schedule` workspace renders one typed,
policy-filtered, versioned schedule projection through synchronized Roadmap,
Gantt, and Flow lenses. Lens, role, timezone, selected item, scope, service,
window, and baseline remain URL-addressable. The workspace includes:

- planned dates, original baselines, actual dates, milestone markers, typed
  dependencies, blockers, critical-path state, ITIL or blackout conflicts, and
  architecture overlay support from the frozen SKCP-20A contract;
- explicit unknown, stale, partial, unavailable, unreachable, policy-filtered,
  and not-applicable presentation without inventing dates or healthy results;
- accessible schedule-table and dependency-list equivalents;
- keyboard lens controls, zoom, collapse, expand, item detail, and focus return;
- responsive layouts and a versioned JSON export snapshot; and
- a visible no-write boundary with no individual productivity ranking.

The protected API route accepts an injected owner projection provider only
inside the existing typed CapAuth request boundary with request-local
currentness checks. It fails closed when the provider or policy decision is
unavailable. SKDashboard does not derive schedule dates from card order or
write owner records.

## Qualification

- Focused decision-context, API, and surface tests: 22 passed.
- Full repository suite: 432 passed with 6 deprecation warnings.
- Ruff over changed Python and tests: passed.
- Ruff format checks for changed tests: passed.
- JavaScript syntax checks: passed.
- Chrome 151 CDP qualification: Roadmap, Gantt, and Flow switched over one
  projection; 2 items and 1 dependency rendered; blackout exception remained
  visible; keyboard item detail and focus return passed; 390 px and 320 px
  layouts passed; zero writes, external requests, or browser exceptions.

## Limits and rollback

This slice does not create schedule data, scenarios, reschedule previews,
authorizations, mutations, deployments, external actions, or HammerTime
access. Export is a local browser download of the currently authorized
projection. Forecast quantiles and AI schedule recommendations remain separate
future cards.

Rollback is reverting the implementation commit. No migration, owner record,
or runtime deployment state is created by this card.
