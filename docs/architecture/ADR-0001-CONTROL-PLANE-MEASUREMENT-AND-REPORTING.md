# ADR-0001: Control-plane measurement and reporting contract

Date: 2026-08-23
Status: Proposed for human review
Card: SKCP-00, `9442b3b3`
Epic: SK control plane, `6f7fd828`

## Decision summary

SKDashboard will be the canonical human and approved-agent projection plane for
the SK estate. It will not become the authoritative store for project work,
ITIL records, configuration items, model usage, service state, policy, or legal
Matter content.

The canonical application will serve one UI origin and one versioned API
namespace:

- Local process: `skcapstone dashboard --port 7778`
- Local origin: `http://127.0.0.1:7778`
- Production ingress: one approved tailnet-only origin, selected and qualified
  by SKCP-05
- Read API: `/api/v1`
- Discovery: `/.well-known/skworld-module.json`
- Contract: `/api/v1/openapi.json`
- Health: `/api/v1/health`
- Event stream: `/api/v1/events`

The first product increment is breadth-first. It exposes an honest, high-level
estate pulse across every major silo before any silo receives a deep analytics
workbench. Later increments add drilldowns, forecasts, reports, AI explanation,
and narrowly scoped governed commands.

## Why this decision is needed

The current dashboard already has useful Board, ITIL, CMDB, Economy, Fleet,
Trust, model, assistant, and operator surfaces. The routes and payloads grew as
feature-specific interfaces. They do not yet share a versioned metric contract,
common scope model, truth-state semantics, report snapshot format, or governed
AI query boundary.

Current implementation evidence also shows why an explicit contract is needed:

- The overview labels a total that includes Done as active work.
- A failed CMDB read can become an empty health object that the browser presents
  as all healthy.
- ITIL breach and success calculations can hide denominator and truncation
  effects.
- Agent presence, fleet runtime, CMDB completeness, SKCounter collection, and
  Joule wallets describe different populations and must not be merged.
- Harness-reported and gateway-observed AI usage are different measurement
  lanes and must not be summed by default.
- The assistant can mix reporting and mutation syntax. The new insight boundary
  must be read-only and incapable of executing an action.

## Architectural boundary

```text
Authoritative stores and services
        |
        v
Bounded, policy-aware adapters
        |
        v
Append-only observations and source watermarks
        |
        v
Versioned metric registry and deterministic calculation engine
        |
        v
/api/v1 projections and immutable report snapshots
        |
        +--------------------+
        |                    |
        v                    v
Human UI and SKOS       AI insight service and MCP client
                             |
                             v
                    Typed proposal only, no command execution
```

Every user-visible number is calculated deterministically before it reaches a
model. AI may explain, compare, challenge, summarize, and propose a next step.
AI does not own the metric calculation, workflow state, authorization decision,
Approval, or external action.

## Resource ownership

| Resource | Authoritative owner | SKDashboard responsibility | Initial visibility |
|---|---|---|---|
| Portfolio, projects, epics, tasks, dependencies | SKCapstone and skcoord | Bounded projections, flow metrics, evidence links | Estate pulse and portfolio summary |
| Incidents, problems, changes, KEDB | SKCapstone ITIL and skcoord | Service-management projections and report snapshots | Open risk, service level, change health |
| Configuration items and relationships | CMDB | Coverage, drift, topology, impact projections | Estate coverage and top drift |
| Runtime fleet and collectors | SKCapstone Fleet | Freshness, expected versus reporting population | Coverage and stale or missing nodes |
| AI usage | SKCounter and SKGateway measurement lanes | Separate aggregate projections, cost-confidence labels | Usage, coverage, and estimated cost |
| Pipeline performance | SKPerf | Approved aggregate benchmark projections only | Regression and capacity signals |
| Agent and model operations | SKCapstone, SKGateway, model services | Queue, availability, evaluation, and outcome projections | Active work and degraded routes |
| Joule economy | SKJoule | Separate economic projection | Minted, spent, and balance state |
| Identity and authorization | CapAuth | Display policy decision provenance and capability names | Denial and policy-health summary |
| Operator conditions and actions | Atlas | Read condition and action-ledger projections | Decision queue and action readiness |
| Shell and navigation | SKOS and SKWorld standards | Publish a signed discoverable module contract | Consistent navigation and deep links |
| Legal Matters and protected records | SKLegal | Policy-filtered aggregate only at global scope | No Matter content in the global pulse |
| Corpus artifacts and provenance | HammerTime | Approved aggregate health only | No Inbox access and no raw corpus content |

SKDashboard never grants access based on a profile owner field. Protected
SKLegal data requires Tenant and Matter policy before retrieval. Global views
receive only approved aggregates and never become a path around CapAuth,
conflicts, privilege, ethical walls, source rights, or egress policy.

## Common scope contract

All analytics endpoints accept the same bounded scope vocabulary:

- `portfolio_id`
- `project_id`
- `product_id`
- `service_id`
- `team_id`
- `tenant_id`, only after CapAuth policy permits it
- `matter_id`, only after Tenant and Matter policy permits it
- `node_id`
- `environment`
- `measurement_lane`
- `from`, `to`, and `timezone`
- `baseline`

An omitted dimension means the caller's authorized estate scope. It never means
an unrestricted scan. Mixed-scope requests that cannot be calculated without
crossing an isolation boundary fail closed.

## Metric result envelope

Every metric result uses the versioned schema in
`docs/contracts/control-plane-metric-result.schema.json`. The minimum envelope
contains:

- Metric ID and definition version
- Value, unit, polarity, numerator, denominator, and sample size
- Scope, grain, window, timezone, and baseline
- Target or threshold when one has been approved
- Source owner, source references, observed time, projection time, and source
  watermarks
- Truth state and measurement kind
- Coverage, exclusions, errors, and quality notes
- Calculation expression or deterministic calculation reference
- Classification and policy-decision reference where required

Velocity is a local planning aid. It is not a cross-team productivity measure.
No default view ranks people by cards, commits, tokens, costs, Joules, model
requests, or activity time.

## Truth states

Truth state is separate from value. Zero is a valid observed value and must not
be used for missing evidence.

| Truth state | Meaning | UI treatment |
|---|---|---|
| `current` | Evidence is within its declared TTL and coverage floor | Normal polarity and timestamp |
| `stale` | Last valid evidence is older than the TTL | Amber, last observed time, refresh path |
| `partial` | Some expected sources reported and some did not | Amber, numerator and denominator shown |
| `unavailable` | The owner or adapter failed | Red or neutral unavailable state, error provenance |
| `unknown` | Evidence is insufficient to calculate the result | Gray, missing requirements shown |
| `not_applicable` | The metric does not apply to this scope | Neutral N/A, excluded from rollups |

Measurement kind is one of `measured`, `derived`, `estimated`, or `forecast`.
Estimates and forecasts include confidence and method metadata. Forecasts are
ranges, not single-date promises.

## Breadth-first estate pulse

The default `Now` workspace answers five questions in one screen:

1. What requires a human decision now?
2. What changed materially since the selected baseline?
3. Which outcomes, services, delivery flows, or dependencies are at risk?
4. Which evidence is stale, partial, contradictory, or unavailable?
5. Where can the user drill into the evidence in one interaction?

The first live slice includes one high-level signal from every silo:

- Portfolio outcome confidence and blocked investment
- Agile WIP, aging, throughput, and forecast range
- ITIL open risk, service-level pressure, change health, and error-budget state
- DORA delivery trend at service scope
- Architecture drift, dependency risk, and capacity pressure
- AI outcome, usage, evaluation, denial, queue, and cost-confidence signals
- Fleet and CMDB coverage, reconciled as separate evidence populations
- Governance, policy, data-quality, and report freshness

Unknown or failed sources remain visible. A source failure can never produce a
green tile.

## Report snapshots

Scheduled and ad hoc reports are immutable snapshots, not screenshots of a
moving query. The schema in
`docs/contracts/control-plane-report-snapshot.schema.json` records:

- Report type, audience, scope, selected baseline, and as-of time
- Metric definition versions and hashes
- Source watermarks and quality statement
- Metric results and evidence references
- Model provenance for AI-written narrative
- Human review state
- Report hash and superseding report reference

Initial report products are daily operations, weekly portfolio, sprint or flow,
monthly service, monthly AI and economy, quarterly strategy, and an ad hoc
evidence pack. Delivery subscriptions and external export are later cards and
remain policy gated.

## AI query boundary

`POST /api/v1/insights/query` is a read-only analytics request. It accepts a
bounded question, explicit scope, time window, output intent, and allowed metric
families. The server resolves authorization before data access, runs
deterministic metric queries, and gives the model only the permitted results and
evidence summaries.

Every AI response validates against
`docs/contracts/control-plane-insight.schema.json` and includes:

- Proposal or abstention status
- Scope, window, and metric-definition versions
- Evidence and calculation references
- Uncertainty, exclusions, and contradictions
- Model route, served model, prompt hash, schema hash, and policy reference
- Preview-only next steps when relevant

The insight endpoint does not accept an action contract, capability token,
arbitrary URL, shell command, filesystem path, or connector destination. It
cannot invoke the command API. A proposed action must be converted into a typed
command preview in a separate user interaction, then pass CapAuth, owner policy,
idempotency, verification, Approval, and rollback requirements.

## AI evidence-to-decision loop

AI assistance is available across every workspace, saved view, report, metric,
and evidence table. It operates in a visible ladder so the user always knows
whether the system is observing, inferring, recommending, or preparing an
authorized operation.

1. Observe: identify material changes, anomalies, threshold pressure, stale
   evidence, contradictions, and missing coverage.
2. Explain: show the contributing metrics, calculation references, time window,
   baseline, and likely causes.
3. Compare: evaluate the observed state against approved targets and versioned
   Agile, Kanban, ITIL, SRE, DORA, EBM, architecture, governance, and local
   operating practices.
4. Conclude: state a bounded conclusion with confidence, counter-indicators,
   uncertainty, and conditions that would change it.
5. Recommend: present ranked options with rationale, expected impact range,
   horizon, risks, alternatives, preconditions, and best-practice references.
6. Simulate: when supported, show a no-write forecast or what-if comparison.
7. Prepare: convert an accepted recommendation into an allowlisted typed action
   preview through a deterministic policy-aware service.
8. Authorize: let an eligible human approve the exact preview only after target,
   before state, proposed effect, blast radius, risk, verification, rollback,
   owner operation, required scope, policy result, and expiry are visible.
9. Verify and learn: link the owner receipt and outcome metrics back to the
   recommendation so acceptance, rework, override, effect, and calibration can
   improve future suggestions.

Recommendations validate against
`docs/contracts/control-plane-recommendation.schema.json`. They contain evidence
and metric references, best-practice references, impact and confidence, risks,
counter-indicators, alternatives, preconditions, and a typed next-step request.

The low-click interaction is deliberately two-stage:

- One click on `Review next step` opens the deterministic authorization preview.
- One explicit `Approve and queue` click authorizes the exact hash when CapAuth,
  owner policy, required Approval, expected version, and expiry all pass.

Opening the preview is not authorization. Changing any target or parameter
invalidates the preview hash and requires a new preview. High-risk, external,
destructive, protected-Matter, filing, service, email, mailing, calendar, and
client-communication actions retain their existing exact-version human gates
and may require more than one approval. The UI optimizes navigation, not the
removal of safety decisions.

The authorization preview schema is
`docs/contracts/control-plane-action-preview.schema.json`. It contains capability
names and policy references only, never bearer material. The action registry is
closed and versioned. There is no arbitrary operation, URL, shell, connector, or
filesystem escape hatch.

AI quality is measured by accepted outcome, recommendation acceptance, override,
rework, verified effect, calibration, abstention quality, citation coverage,
evaluation results, denial handling, latency, queue time, tool error, and cost
per accepted outcome. Raw activity is never treated as value by itself.

## API behavior

- All new resources live under `/api/v1`.
- Unsupported routes return an explicit `404` JSON error and never fall through
  to another handler.
- Methods not declared by the contract return `405`.
- Responses include schema version, request ID, source owner, truth state,
  observed time, projection time, and freshness metadata.
- List endpoints have a server-enforced limit and opaque cursor.
- Normal reads support ETag and `If-None-Match`.
- Event streams use opaque resume cursors, bounded buffers, heartbeats, and
  explicit reset-required events.
- Oversized query strings, bodies, windows, and result sets fail before any
  unbounded source scan.
- Errors use a shared envelope with a stable code, retryability, safe detail,
  and evidence reference. Errors never include secrets or raw protected data.

The baseline OpenAPI contract is
`docs/contracts/openapi.control-plane.v1.json`.

## Compatibility and deprecation

Current `/api/*` routes remain legacy during the migration. New clients must use
`/api/v1`. A compatibility adapter may translate a bounded legacy read to the
new response, but it may not invent missing truth-state or provenance values.

A breaking change requires a new API major version. Additive fields may ship in
v1 when clients are required to ignore unknown fields. Deprecated endpoints
publish a replacement, sunset date, and telemetry-confirmed zero-consumer gate
before removal.

## Security and failure analysis

| Threat or failure | Required control |
|---|---|
| Confused deputy | Bind actor, agent, node, purpose, audience, resource, and owner policy revision |
| Route fallback | Explicit Starlette routes, JSON 404, route-table contract test |
| Capability leakage | Capability references only in responses, never bearer material in URL, body, logs, prompt, event, or fixture |
| Replay | Short TTL, nonce or idempotency key, audience binding, owner verification |
| Cross-origin misuse | Tailnet-only ingress, explicit CORS allowlist, CSRF defense for browser mutations, origin checks |
| Oversized or unbounded query | Body, time-window, page, and adapter budgets enforced before source access |
| Stale projection | TTL, source watermark, stale truth state, no green fallback |
| Partial fleet | Expected and reporting populations shown with coverage |
| Unauthorized mutation | Reporting and command APIs separated, CapAuth and owner policy fail closed |
| Prompt injection | Data is untrusted content, closed tools only, typed outputs, no action execution |
| Protected Matter disclosure | Tenant and Matter policy before retrieval, classification and egress controls, global aggregate boundary |
| Sensitive telemetry | No raw prompts, responses, retrieval query content, session IDs, capability tokens, credentials, or source paths |
| Metric gaming | Team and service outcomes, balancing metrics, visible definitions, no individual ranking |
| Metric drift | Versioned registry, golden fixtures, definition hash in every report |

## Experience and accessibility budgets

- Default page meaningful content: under 2 seconds on the qualified control-plane
  node with cached bounded projections
- Common exception-to-evidence path: at most 2 interactions
- KPI-to-contributors drill: 1 interaction
- Global scope and command search: `Ctrl+K` or `Cmd+K`
- Complete keyboard operation and visible focus
- WCAG 2.2 AA contrast, labels, landmarks, reduced-motion support, and non-color
  status cues
- Saved views and deep links preserve scope, time, and selected baseline
- Every chart has a text or table equivalent

## Consequences

Positive consequences:

- Humans and agents see the same numbers and provenance.
- Breadth-first delivery produces value before every adapter is deep.
- Source failures remain visible and diagnosable.
- Reports can be reproduced and challenged.
- AI explanation remains useful without becoming a policy or action bypass.

Costs and tradeoffs:

- Metric definitions and source contracts require versioning discipline.
- Some current routes need compatibility adapters or retirement work.
- Cross-estate scope needs a deliberate identity and data-classification model.
- Early screens will show Unknown and Partial frequently. That is accurate and
  is preferable to false confidence.

## Human review gate

The API is not frozen until the owner reviews and explicitly accepts:

1. SKDashboard as projection plane, with named authoritative owners.
2. The `/api/v1` namespace and canonical `:7778` process.
3. The metric envelope, truth states, and measurement kinds.
4. Immutable report snapshots and reproducibility metadata.
5. The read-only AI query boundary and separate command review path.
6. The evidence-to-decision loop and two-stage low-click authorization pattern.
7. The breadth-first sprint sequence in
   `docs/planning/SK-CONTROL-PLANE-BREADTH-FIRST-SPRINTS.md`.
8. The visual direction in
   `docs/wireframes/control-plane-estate-pulse.html`.

Approval records the exact document and contract hashes. Requested changes
supersede this proposal through an attributable revision.
