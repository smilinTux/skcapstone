# SK control plane sprint plan amendment 1.1.0

Status: proposed for human review

## Amendment boundary

The original sprint plan remains valid for audit and is not rewritten. This
amendment catalogs the candidate that supersedes it for review only. Before any
leaf work, human gate `bea13a70` must approve the superseding exact hashes and
independent review `d0edbff1` must pass. No leaf card becomes eligible from
this amendment alone.

## Product-shaped planning views

The plan is intentionally layered so a reader can move from immediate work to
portfolio timing without mistaking a synthetic preview or a planning proposal
for service truth.

| Layer | Planning purpose | Typed boundary |
| --- | --- | --- |
| Now | Show the currently selected scope, blocked work, truth-state strip, and evidence links. | Metric and projection `truth_state`, source owner, and `service_id` are read-only values. |
| Roadmap | Group outcomes by sprint, capability, and milestone. | Date, iteration, and dependency views are planning projections, not delivery commitments. |
| Gantt | Place planned start and target windows with visible dependency paths. | Dates are proposal inputs; the authoritative schedule remains with the owner service. |
| Flow | Show pull-based work columns, aging, WIP signals, blockers, and dependency effects. | Board state is a projection from the coordination owner and preserves blocked and review states. |
| Forecast | Explain probabilistic completion ranges, assumptions, and calibration. | Forecast values are estimates with method and evidence references, never completed work. |
| Scenario | Compare bounded what-if inputs, outcomes, and reversibility before selection. | A scenario is a proposal and cannot mutate the authoritative plan. |
| AI action preview | Translate a grounded recommendation into one exact, reviewable preview. | Insight and recommendation proposals remain read-only; action-preview status, hash, expiry, policy, and approval controls govern any later authorization. |

The layered form is informed by, but does not copy or integrate with, the
following primary product references:

- [GitHub Projects layouts](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/changing-the-layout-of-a-view)
  distinguish table, board, and roadmap projections over shared items.
- [GitHub roadmap layout](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/customizing-the-roadmap-layout)
  uses date and iteration fields to place work on a configurable timeline.
- [Azure Boards Delivery Plans](https://learn.microsoft.com/en-us/azure/devops/boards/plans/?view=azure-devops)
  describe multiteam timeline and dependency coordination.
- [Jira timeline view](https://support.atlassian.com/jira-software-cloud/docs/what-is-the-timeline-and-how-do-i-use-it/)
  describes timing, duration, dependency, and rollup views.
- [Jira Plans scheduling](https://support.atlassian.com/jira-software-cloud/docs/estimate-and-schedule-issues-in-advanced-roadmaps/)
  distinguishes a plan sandbox from committed work items.
- [The Kanban Guide, December 2020](https://kanbanguides.org/the-kanban-guide/2020.12/)
  defines the minimum visual, pull-based flow-management foundation.

These references are design inspiration only. They do not authorize an
integration, change SKDashboard ownership, or import any external data.

## V1.1 contract map

| UI state | Contract state | Interpretation |
| --- | --- | --- |
| Truth state | Metric or projection `truth_state` | `current`, `stale`, `partial`, `unavailable`, `unknown`, and `not_applicable` retain their exact meaning. |
| Service state | `service_id`, `source.owner`, `source_owner`, watermarks, and freshness times | The UI scopes and attributes data. It does not infer a service result. |
| Preview state | Action-preview `status`, `preview_hash`, expiry, policy decision, and approval entries | A preview can be ready, denied, need information, need approval, be stale, or expire. It is not execution. |
| Proposal state | Insight `proposal` or `abstained`; recommendation `proposed` or `abstained` | AI output is evidence-linked and advisory. It can lead to a preview request but cannot authorize or execute. |

## Remediation disposition

| Code | Card | Disposition |
| --- | --- | --- |
| F1 | `ee1d0874` | V1.1 metric truth-state invariants and evidence-bearing zero fixtures are pinned. |
| F1A | `bd732651` | V1.1 self-declared versions and UI normalization are pinned. |
| F2 | `0242f9f2` | Grounded recommendation or typed abstention rules are pinned. |
| F2A | `f94fde82` | Corrected V2 evidence routing, accessible role paths, and authorization state transitions are pinned. |
| F2B | `b24213ea` | V2 service scope propagation and truthful summary are pinned. |
| F3 | `83a404bf` | The independent-review dependency is folded into the affected legacy implementation cards. |
| F3A | `079cd760` | The installed operator environment was activated and the accidental gate probe was released without completion. |
| F3B | `1f9ee2c9` | Dependency addition and exact claim release are cross-process atomic and recoverable. |
| F3C | `a081d5ed` | Shared-owner serialization and single-event release recovery are in place. |
| F3D | `8ab522ee` | Claim-complete races, mirrors, labels, lifecycle, and storage paths are hardened. |
| F3E | `50e36b06` | Partial claims recover, durable completion mints once, and CardStore reads are descriptor-pinned. |

F3 is the independent-review dependency correction. F3B is the subsequent
atomicity correction for dependency and claim-release mutations.

## 24-card catalog

The first 22 entries preserve the original candidate lineage. `SKCP-21A` and
`SKCP-30A` are additive. Dependencies are exact card IDs from the folded board
topology, including the original gate `9508b8fd` where it remains part of a
legacy leaf card's lineage.

| Key | Card | Planned outcome | Exact dependencies |
| --- | --- | --- | --- |
| SKCP-11 | `9e88de5c` | Versioned metric registry and deterministic fixtures | `9508b8fd`, `d0edbff1` |
| SKCP-12 | `804f14de` | Bounded observation adapters and watermarks | `9508b8fd`, `d0edbff1` |
| SKCP-13 | `c6828b8a` | Breadth-first Now workspace | `9508b8fd`, `d0edbff1`, `d12b8951`, `9e88de5c`, `804f14de`, `5026359d`, `08f4cdcb` |
| SKCP-14 | `5026359d` | Truth-state, quality, and reconciliation strip | `9508b8fd`, `d0edbff1`, `9e88de5c`, `804f14de` |
| SKCP-15 | `08f4cdcb` | Synthetic full-estate fixture and metric pack | `9508b8fd`, `d0edbff1`, `9e88de5c` |
| SKCP-20 | `b7ada8b9` | Unified scope, saved views, and deep links | `c6828b8a` |
| SKCP-21 | `5ee56779` | Portfolio project and dependency flow workspace | `804f14de`, `b7ada8b9` |
| SKCP-21A | `eddaa1fb` | Synchronized roadmap, Gantt, and flow schedule explorer | `bea13a70`, `d0edbff1`, `b7ada8b9`, `5ee56779` |
| SKCP-22 | `da097cbb` | ITIL, SRE, service-level, change, PIR, and KEDB workspace | `804f14de`, `b7ada8b9` |
| SKCP-23 | `866ffaac` | DORA, architecture, CMDB, capacity, and drift workspace | `804f14de`, `b7ada8b9` |
| SKCP-24 | `77d6bae0` | AI outcome, evaluation, cost, and Joule workspace | `804f14de`, `b7ada8b9` |
| SKCP-25 | `b548a77a` | Governance, lineage, policy, and data-quality center | `5026359d`, `b7ada8b9` |
| SKCP-30 | `169028ce` | Probabilistic forecast, dependency simulation, and calibration | `08f4cdcb`, `5ee56779` |
| SKCP-30A | `7888e091` | Forecast scenarios and AI schedule recommendations | `bea13a70`, `d0edbff1`, `169028ce`, `f080f150`, `efa9bee8`, `eddaa1fb` |
| SKCP-31 | `f080f150` | Governed read-only AI insight and evaluation suite | `b7ada8b9`, `5ee56779`, `da097cbb`, `866ffaac`, `77d6bae0`, `b548a77a` |
| SKCP-31A | `efa9bee8` | Best-practice recommendations and outcome learning | `f080f150` |
| SKCP-32 | `38731952` | Immutable report snapshots and reproducibility | `9e88de5c`, `b7ada8b9` |
| SKCP-33 | `631f90bf` | Policy-gated report subscriptions and exports | `38731952`, `94cbf19a` |
| SKCP-34 | `5858a34f` | Read-only typed client resources and evidence links | `d12b8951`, `38731952` |
| SKCP-40 | `008bd490` | Governed command preview and receipts | `e6326000`, `5858a34f`, `d79100a7` |
| SKCP-41 | `cae1eaef` | Deterministic action preview and outcome loop | `94cbf19a`, `e6326000`, `efa9bee8`, `008bd490` |
| SKCP-50 | `83a8c40b` | Browser, accessibility, task-time, and visual qualification | `c6828b8a`, `b7ada8b9`, `5ee56779`, `da097cbb`, `866ffaac`, `77d6bae0`, `b548a77a` |
| SKCP-51 | `2d02b6ed` | API latency, cache, pagination, streams, and backpressure qualification | `d12b8951`, `804f14de`, `38731952` |
| SKCP-52 | `ecf1148c` | Metric governance, calibration, and AI outcome review | `169028ce`, `83a8c40b`, `efa9bee8` |

## Non-authorizations and rollback

This plan amendment does not authorize implementation, source changes for leaf
cards, deployment, production activation, a service restart, Matter access,
HammerTime Inbox access, external actions, an external product integration, or
completion of `bea13a70` or `d0edbff1`. The candidate may be rejected by
leaving the original manifest intact and declining the new exact-hash package.
