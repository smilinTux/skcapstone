# SKDashboard control-plane contract compatibility 1.1.0

Status: superseding remediation contract
Date: 2026-08-23
Remediation card: `bd732651` (supersedes `ee1d0874`)

## Version boundary

The original `1.0.0` candidate contract files remain byte exact and remain
available for audit against the original candidate manifest. The versioned
files in the `v1.1.0/` subdirectory are the `1.1.0` superseding contract set:

- `v1.1.0/control-plane-metric-result.v1.1.0.schema.json`
- `v1.1.0/control-plane-recommendation.v1.1.0.schema.json`
- `v1.1.0/control-plane-action-preview.v1.1.0.schema.json`
- `v1.1.0/control-plane-insight.v1.1.0.schema.json`
- `v1.1.0/control-plane-report-snapshot.v1.1.0.schema.json`
- `v1.1.0/openapi.control-plane.v1.1.0.json`

The next assembly task must pin these files by exact SHA-256 and issue a new
human approval. It must not rewrite the original candidate manifest in place.

## V2 UI normalization

V2 keeps presentation labels and preview controls mapped to typed contract
values. The UI may use readable labels, but it must not invent a second state
vocabulary:

| V2 UI value | Contract value or behavior |
| --- | --- |
| `Policy filtered` | `truth_state: not_applicable`; retain the policy-filtered label only as presentation text. |
| UI service selector | Query and scope field `service_id`. |
| `stale-target` preview | Action preview `status: stale`; disable authorization and require a new preview. |
| `denied-policy` preview | Action preview `status: denied` with a nonempty `denial_reasons` array. |
| Changed parameters | Invalidate the exact preview hash and require re-preview before authorization. |
| Proposal | Typed insight outcome `status: proposal`. |
| Abstention | Typed insight outcome `status: abstained` with the reason preserved by the producing proposal. |

## Compatibility rules

This is a breaking contract revision for producers and validators. New clients
use the `1.1.0` OpenAPI document and versioned schema references. Legacy
clients continue to use the original `1.0.0` files only while the migration
owner explicitly permits them. A compatibility adapter may translate a legacy
read, but it may not turn missing, failed, unavailable, unknown, or not
applicable evidence into a numeric or textual value.

Observed numeric zero is valid only when the metric has an evidence-bearing
truth state and at least one explicit source evidence reference. Unavailable,
unknown, and not-applicable metric results carry a null value. A projection
with one or more source errors cannot use the current freshness truth state.

Action-oriented proposed recommendations require nonempty best-practice,
impact, risk, counter-indicator, alternative, and precondition grounding. An
insufficient-evidence recommendation uses `status: abstained` and a required
`abstention_reason`.

High-risk, external, destructive, and protected-Matter action previews require
an exact-version approval entry before `status: ready`. A preview that still
needs approval uses `status: needs_approval`; denied previews carry a denial
reason; expired previews use `status: expired`.

These rules preserve the architecture decision that source failure remains
visible and that reporting cannot bypass policy or exact-version approval.
