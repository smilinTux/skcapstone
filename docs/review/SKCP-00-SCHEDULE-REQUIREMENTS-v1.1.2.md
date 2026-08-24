# SKCP-00 V1.1.2 schedule requirements supplement

Status: active repair supplement
Capture: 2026-08-23T22:35:28Z
Repair recorded: 2026-08-24
Supersedes presentation semantics in: `SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.1.md`
Repair card: `f30e9e0a`
Applies to: SKCP-20A `c3a9c9e9`, SKCP-21A `eddaa1fb`, SKCP-30A `7888e091`,
and SKCP-30B `4e1130cc`

## Contract decomposition

SKCP-20A `c3a9c9e9` is the Sprint 2 contract leaf for the canonical typed
schedule projection. It depends on human gate `bea13a70`, independent review
`d0edbff1`, and contract prerequisite `b7ada8b9`. Its acceptance freezes the
canonical envelope and item types, timezone, null-date, rollup, partial-rollup,
and dependency-cycle semantics, ITIL and architecture overlays, immutable
no-write scenarios, exact version and base-hash reschedule previews, and
OpenAPI plus policy, truth-state, and no-ranking tests. It is an append-only
dependency of SKCP-21A `eddaa1fb`.

SKCP-30B `4e1130cc` is the Sprint 3 contract leaf for forecast calibration and
AI schedule insight. It depends on `bea13a70`, `d0edbff1`, `169028ce`,
`f080f150`, `efa9bee8`, and SKCP-20A `c3a9c9e9`. Its acceptance discriminates
critical-path dates from throughput Monte Carlo quantiles, freezes rolling
backtests without leakage, calibration, low-sample abstention, reproducibility,
support and counter evidence, affected outcomes, alternatives, expected impact,
and the rule that AI explains engine results but cannot authorize, mutate, or
execute. It is an append-only dependency of SKCP-30A `7888e091`.

## Shared projection and URL state

Roadmap, Gantt, and Flow render one versioned scoped projection. Lens switching
preserves the following URL-addressable state exactly:

- `lens`
- `scope`
- `service`, normalized to `scope.service_id`
- `role`
- `window`
- `baseline`
- `selected_item`
- `saved_view`

A saved view records the projection version and scope. An unsupported lens,
scope, service, role, or saved view fails closed with a typed unavailable or
policy-filtered state. It does not silently substitute another query.

## Schedule semantics

| Condition | Required rendering and computation rule |
| --- | --- |
| Timezone | Store instants in UTC, display the selected named timezone, and label the timezone on every date-bearing lens and export. |
| Null date | Show an explicit unknown or unavailable date state. Do not turn null into zero duration, today, or an on-time bar. |
| Baseline variance | Preserve original baseline start and target independently from planned and actual dates. Show variance only when both comparable values exist. |
| Rollup | Parent start is the earliest valid child start; parent end is the latest valid child end; progress is derived only from visible, eligible children. |
| Partial rollup | Label partial rollup and identify exclusions, missing dates, or policy-filtered children. It is not a complete rollup. |
| Dependency cycle | Detect cycles before critical-path or schedule propagation. Mark every involved item as unavailable with cycle evidence. |
| Dependency path | Preserve edge type, source, target, lag, evidence, and visibility state. Do not infer a dependency from ordering alone. |

## Gantt and detail drawer

Every visible Gantt bar carries or opens a detail drawer with:

- canonical item ID, title, type, owner service, and scoped service ID;
- original baseline start and target, planned start and target, actual start and
  finish, displayed timezone, duration, and baseline variance;
- status, truth state, progress basis, source watermark, observed time,
  projected time, and data-quality exclusions;
- milestone, release, architecture migration, ITIL change window, blackout, and
  dependency markers where applicable;
- incoming and outgoing dependencies with edge type, lag, blocker state,
  cycle state, and evidence links;
- forecast method, P50, P85, P95, history window, sample, exclusions,
  assumptions, calibration, and backtest reference when available; and
- policy, tenant, service, and protected visibility state.

Critical-path calculation fails closed when dependency cycles, missing required
dates, unknown edge direction, inaccessible required nodes, or conflicting
blackouts make a result unsound. Blackout and ITIL change-window conflicts
remain visible and prevent a suggested schedule action from appearing ready.

## Accessible and exportable alternatives

The schedule has an accessible table and dependency-list alternative to every
visual lens. Both preserve the same scoped projection, selected item, truth
state, policy state, evidence links, and dependency semantics.

Keyboard operation supports lens switching, zoom, collapse, expand, row and
bar focus, dependency traversal, detail-drawer opening, and returning focus to
the invoking item. Screen-reader text names the item, dates, timezone, status,
truth state, dependencies, and blockers. Status and criticality never depend
on color alone.

Print and export create a versioned snapshot with projection version, scope,
timezone, filter state, selected baseline, capture time, source watermarks, and
a SHA-256 hash. Exports preserve unavailable, partial, unknown, and
policy-filtered distinctions.

## Performance and interaction budgets

Virtualization applies to long table and dependency-list rows. Zoom and
collapse operations preserve URL state and do not discard loaded truth-state or
policy metadata. Acceptance evidence must measure:

- initial render and lens switch at the approved fixture size;
- scroll and focus continuity through virtualized rows;
- zoom and collapse with no selection or URL-state loss;
- keyboard traversal and screen-reader announcements; and
- print or export snapshot generation with a recorded hash.

No individual productivity ranking is rendered or derived.

## Forecast, calibration, and scenarios

A forecast labels low sample size, missing history, exclusions, and
uncalibrated state. It shows P50, P85, and P95 only with method, history
window, sample, assumptions, dependency treatment, calibration, and backtest
evidence. Critical-path and flow forecasts stay separately labeled.

A scenario has a stable identity, source projection version, deterministic
input set, diff, reset action, and no-mutation guarantee. It compares scope,
capacity, dependency slip, milestone move, ITIL blackout or change window, and
architecture migration sequencing without changing owner data. Reset restores
the captured input state. An AI schedule explanation identifies supporting and
counter evidence, uncertainty, alternatives, expected impact, and abstains
when evidence is insufficient.

## Visibility and authorization

Policy, tenant, service, and protected visibility remain typed states. A
policy-filtered record preserves its source `truth_state` and separately shows
`visibility.state: policy_filtered` with denied or unknown authorization. An
access decision is never mapped to `not_applicable`, silently removed, or
treated as complete. A tenant or protected-data denial fails closed before
schedule aggregation, critical-path, forecast, scenario, export, or AI
explanation. Service scope uses `scope.service_id`; service ownership and
freshness are attribution fields, not authorization grants.

This repair supplement authorizes no data access, export, scenario mutation,
rescheduling, deployment, or external action. It is implementation evidence
only for `f30e9e0a` and does not complete independent rereview `39085b32` or
any downstream implementation card.
