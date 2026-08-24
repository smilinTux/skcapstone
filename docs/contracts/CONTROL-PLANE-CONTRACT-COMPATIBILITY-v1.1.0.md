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
| `Policy filtered` | `visibility.state: policy_filtered` with `visibility.authorization: denied` or `unknown`; preserve the source `truth_state` and never map an access decision to `not_applicable`. |
| UI service selector | Query and scope field `service_id`. |
| `stale-target` preview | Action preview `status: stale`; disable authorization and require a new preview. |
| `denied-policy` preview | Action preview `status: denied` with a nonempty `denial_reasons` array. |
| Changed parameters | Invalidate the exact preview hash and require re-preview before authorization. |
| Proposal | Typed insight outcome `status: proposal`. |
| Abstention | Typed insight outcome `status: abstained` with a structured `abstention_reason` and no `preview_command` next step. |

## Compatibility rules

This is a breaking contract revision for producers and validators. New clients
use the `1.1.0` OpenAPI document and versioned schema references. Legacy
clients continue to use the original `1.0.0` files only while the migration
owner explicitly permits them. A compatibility adapter may translate a legacy
read, but it may not turn missing, failed, unavailable, unknown, or not
applicable evidence into a numeric or textual value. Visibility and
authorization are separate typed dimensions. `not_applicable` is reserved for
an explicitly out-of-scope metric, not a policy, membership, privilege,
tenant, Matter, or protected-data denial. `unreachable` is a distinct source
reachability failure and remains separate from unknown, unavailable, and
visibility authorization. A policy-filtered record remains visible as a typed
denied or unknown visibility state with its source truth preserved.

Observed numeric zero is valid only when the metric has an evidence-bearing
truth state and at least one explicit, nonempty source evidence reference.
Every non-null measured or derived current, stale, or partial value carries a
nonempty evidence reference and source watermark. Unavailable, unreachable,
unknown, and not applicable metric results carry a null value. A current
metric or projection with one or more source errors cannot claim current
freshness.

Proposal insights require at least one metric reference, a nonempty summary,
evidence references, calculation references, uncertainty, policy decision
reference, and every nonempty model provenance field. Nested recommendation
references and grounding fields are nonempty at every level. Action-oriented
proposed recommendations also require
nonempty best-practice, impact, risk, counter-indicator, alternative, and
precondition grounding. A proposed `preview_action` next step additionally
requires nonempty target, action contract, and parameter proposal references.
An insufficient-evidence insight uses `status:
abstained`, a typed nonempty `abstention_reason`, and cannot expose a ready
preview next step.

High-risk, external, destructive, and protected-Matter action previews require
at least one approval before `status: ready`. Every approval entry on a ready
preview must be `approved`, marked `current`, and set
`exact_version_required: true`; a rejected, expired, unresolved, or stale
entry invalidates readiness. A preview with unresolved approvals uses
`status: needs_approval`; denied previews carry a denial reason; expired
previews use `status: expired`; every preview carries a nonempty policy
decision reference. Any ready mutating preview also carries a nonempty target
identity and a non-null exact expected version, each containing a non-whitespace
character. Read-only previews remain preview-only and do not imply
authorization.

These rules preserve the architecture decision that source failure remains
visible and that reporting cannot bypass policy or exact-version approval.
