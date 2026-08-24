# SK control plane candidate package 1.1.1

Status: proposed for human review

## Purpose

V1.1.1 is an additive review package, not a new implementation plan. It pins
the original and V1.1.0 candidate hashes, records the folded board at one UTC
capture time, and preserves the schedule delivery scope in a separately hashed
snapshot.

## Truthful board capture

The package captures `2026-08-23T22:35:28Z` from
`skcapstone coord kanban --json`. Its status values are historical
observations:

| Card | Captured status | Meaning |
| --- | --- | --- |
| F4 `90a02b0e` | `done` | The V1.1.0 assembly was completed before this capture. |
| F3F `f701b0d3` | `done` | Completion dependency enforcement and the human-gate event were completed before this capture. |
| F4A `559c8c48` | `doing` | This V1.1.1 package was being assembled. |
| Human gate `bea13a70` | `backlog` | Approval remains pending. |
| Independent review `d0edbff1` | `review` | The independent review remains incomplete and is blocked by the human gate. |
| SKCP-21A `eddaa1fb` | `backlog` | Schedule explorer implementation remains ineligible. |
| SKCP-30A `7888e091` | `backlog` | Forecast and scenario implementation remains ineligible. |
| SKCP-20A `c3a9c9e9` | `backlog` | Shared schedule projection and reschedule contract remains ineligible. |
| SKCP-30B `4e1130cc` | `backlog` | Forecast calibration and AI insight contract remains ineligible. |

The package does not reinterpret a prior capture after the fact. A later board
change requires a new attributed capture.

## Schedule scope

The pinned schedule snapshot preserves the complete description, acceptance
criteria, labels, folded dependencies, and full board metadata for:

- SKCP-20A `c3a9c9e9`, typed schedule projection, overlays, immutable
  scenarios, and exact reschedule preview contract.
- SKCP-21A `eddaa1fb`, synchronized Roadmap, Gantt, and Flow explorer.
- SKCP-30A `7888e091`, probabilistic forecasts, scenarios, and AI schedule
  recommendations.
- SKCP-30B `4e1130cc`, discriminated forecast calibration and AI schedule
  insight contract.

The snapshot preserves the layered Now, Roadmap, Gantt, Flow, Forecast,
Scenario, and AI action-preview design from V1.1.0. It does not grant
implementation authority to either card.

## UI contract map

| UI query or state | Contract mapping | Constraint |
| --- | --- | --- |
| `service` query key | `scope.service_id` | Scope only, never a health or execution assertion. |
| Truth state | Metric or projection `truth_state` | Preserve current, stale, partial, unavailable, unknown, and not-applicable distinctions. |
| Preview | Action-preview status, hash, expiry, policy, and approvals | Preview is not execution. |
| Proposal | Insight proposal or abstained; recommendation proposed or abstained | Evidence-linked advisory output only. |

## Gate path

The V1.1.1 manifest records the direct
`d0edbff1 -> bea13a70` edge and 29 checked paths. Those paths cover 24
catalog leaves, two discovered schedule contract cards, and legacy SKCP-01,
SKCP-02, and SKCP-07. Every path retains both the independent review and
superseding human gate.

## Non-authorizations

No human gate, review, implementation card, deployment, service restart,
external action, Matter access, HammerTime Inbox access, commit, or push is
authorized by this package. Rejection retains the V1.1.0 and original manifests
unchanged.
