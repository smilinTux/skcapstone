# ADR-0001 candidate capture amendment 1.1.1

Status: proposed for human review

## Decision

This additive V1.1.1 package supersedes the V1.1.0 review package for the next
exact-hash human review. It preserves the original candidate and every V1.1.0
artifact byte for byte. Contract files remain at V1.1.0. This amendment adds a
truthful board capture, a full schedule-card acceptance snapshot, and the
human-gate topology established after V1.1.0 assembly.

The captured board time is `2026-08-23T22:35:28Z`. Status values in this
package are folded board observations at that instant, not predictions or a
rewrite of historical evidence. At capture, F4 and F3F were done, F4A was
doing, the human gate `bea13a70` was backlog, independent review
`d0edbff1` was review, and all four schedule-contract and implementation cards
were backlog.

## UI contract normalization

The V1.1.1 package retains the V1.1.0 truth, preview, proposal, and abstention
mappings. It clarifies one previously ambiguous service selector mapping:

| UI value | Typed V1.1 contract location | Meaning |
| --- | --- | --- |
| Query key `service` | `scope.service_id` | The query scopes a projection to one service. It is not a health result, execution result, or a second service state. |
| Truth badge | Metric or projection `truth_state` | `current`, `stale`, `partial`, `unavailable`, `unknown`, and `not_applicable` retain their typed meanings. |
| Authorization preview | Action-preview `status`, `preview_hash`, expiry, policy decision, and approvals | Preview remains non-executing. Stale or changed parameters require a new exact preview. |
| Insight and recommendation | Insight `proposal` or `abstained`; recommendation `proposed` or `abstained` | AI output is evidence-linked and advisory. Insufficient evidence remains an explicit abstention. |

The service selector can also display `source.owner`, `source_owner`,
watermarks, and freshness times for attribution. Those fields do not replace
`scope.service_id` as the normalized query scope.

## Gate ordering

Independent review `d0edbff1` has the effective folded dependencies
`9442b3b3` and `bea13a70`. The latter is an attributed append-only
dependency event. Every original 22-card leaf, schedule additions SKCP-21A,
SKCP-20A, SKCP-30A, and SKCP-30B, and legacy SKCP-01, SKCP-02, and SKCP-07 has
a direct or transitive path through `d0edbff1` to `bea13a70`. SKCP-20A
`c3a9c9e9` freezes the typed shared schedule projection, overlays, scenarios,
and reschedule previews. SKCP-30B `4e1130cc` freezes discriminated forecast,
calibration, and AI schedule-insight contracts. Both remain backlog contract
cards and do not authorize implementation.

## Non-authorizations and rollback

This candidate does not authorize implementation, deployment, production
activation, service restart, successful implementation claims, external
actions, product integrations, Matter access, protected-data retrieval,
HammerTime corpus access, HammerTime Inbox access, completion of
`bea13a70`, or completion of `d0edbff1`.

Rollback is declining this V1.1.1 package and retaining V1.1.0 and the
original candidate for audit. No original or V1.1.0 artifact is modified.
