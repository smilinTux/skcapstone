# SKCP-21 Portfolio workspace qualification

Date: 2026-08-24

Card: `5ee56779`

Base main: `fbfab4d5326055064807cb4b1b9206455c1c16de`

## Delivered boundary

The read-only `/control-plane/portfolio` workspace projects current portfolio,
project, Agile flow, dependency, and milestone evidence through the protected
`GET /api/v1/overview` endpoint. The endpoint can consume the exact sanitized
CapAuth context and its request-local currentness verifier through the released
SKCoord policy provider. The provider remains the owner of visible record
selection and policy filtering. SKDashboard does not enumerate CardStore IDs,
fold records directly, or infer protected records.

The workspace presents 37 bounded signals with literal definitions, sample
boundaries, windows, exclusions, and one-click evidence. Current numeric values
come only from the released aggregate or authorized owner-record projection.
Inputs that are not projected, including historical flow, value, cost of delay,
decision latency, cycle time, blocked time, flow efficiency, churn, rollover,
risk history, milestone dates, and forecasts, remain explicitly Unknown.
Velocity is labeled local planning context and no person or team ranking is
derived.

Owner records, visible dependency edges, explicit milestone classifications,
and their safe evidence references use the released SKCoord projection shape.
Counts are derived only within their matching populations. Authorization
delivery fields that change per request are omitted from ETag calculation, while
visible-set, owner-policy, record, edge, and watermark changes remain relevant.

## Qualification

- Full repository test suite: 420 passed with 4 warnings.
- Ruff over `src` and `tests`: passed.
- Targeted Ruff format check for the new project workspace test: passed.
- Node syntax for the Portfolio browser module and CDP qualifier: passed.
- Real Chrome 151 CDP qualification: passed.
- Rendered projection: 37 signals, 2 owner records, 1 dependency edge, and 1
  explicit milestone from a bounded public-synthetic provider fixture.
- Native keyboard Enter opened evidence; Escape closed it and returned focus.
- 390 px and 320 px layouts contained wide tables without document overflow.
- Non-GET requests: zero.
- External requests: zero.
- Browser runtime exceptions: zero.
- Exact-context Python integration used a real signed CapAuth bearer and the
  released SKCoord policy provider. A second independently authorized request
  produced the same protected ETag and a 304 response.

## Deliberate limits

SKDashboard does not ship a production policy-entry catalog or credential
issuer configuration. Live server composition requires a trusted operator to
provide the CapAuth authorizer, the same SKCoord owner-policy provider, exact
resource binding, and per-role authorization. The static policy fixture used by
qualification is test-only and is not deployment authority.

This slice adds no mutation route, dispatch, deployment, runtime activation,
external action, HammerTime access, protected SKLegal detail, or person-level
productivity metric. The browser does not acquire or persist a bearer. Real
browser qualification injects a short-lived test bearer through CDP headers.
