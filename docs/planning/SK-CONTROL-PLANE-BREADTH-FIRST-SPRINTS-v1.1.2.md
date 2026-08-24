# SK control plane breadth-first candidate plan 1.1.2

Status: proposed for human review

## Purpose and chronology

V1.1.2 is a review package, not an implementation plan approval. It corrects
the predecessor chronology by retaining V1.1.0's future timestamp and V1.1.1's
earlier capture as non-authoritative audit lineage. The current board capture
is 2026-08-24T02:52:32.186485Z. Human gate `bea13a70` remains backlog and independent
review `d0edbff1` remains review, so no leaf card is eligible through this
document.

## Shared planning workspace

The product is a versioned, scoped, read-only projection. Every lens preserves
the selected scope, service, role, window, baseline, selected item, and saved
view in the URL. `service` maps exactly to `scope.service_id`; service owner,
watermark, and freshness are attribution fields.

| Lens | Reader need | Contract and safety boundary |
| --- | --- | --- |
| Now | Current scope, blockers, evidence, and truth quality | Typed truth state and source metadata, never synthetic health. |
| Roadmap | Outcome and sprint sequencing | Proposal dates and dependencies, not delivery commitments. |
| Flow | Pull state, aging, WIP, and blockers | Folded board projection with blocked and review states preserved. |
| Schedule and Forecast Gantt | Timeline, baseline, dependencies, critical path, blackouts, and probabilistic range | One typed schedule projection. Cycles, missing required dates, inaccessible dependencies, and blackout conflicts fail closed. |
| Forecast | Method, P50, P85, P95, history, calibration, and backtests | Critical-path and throughput Monte Carlo forecasts stay distinct; low sample abstains. |
| Scenario | Stable no-write what-if identity, diff, and reset | Input version and base hash remain exact. No owner data changes. |
| AI action preview | Grounded explanation and a possible exact preview | AI remains advisory. Preview is non-executing and requires policy and human approval. |

The visual design takes direct inspiration only from [GitHub Projects layouts](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/changing-the-layout-of-a-view), [GitHub roadmap layout](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/customizing-the-roadmap-layout), [Azure Boards Delivery Plans](https://learn.microsoft.com/en-us/azure/devops/boards/plans/?view=azure-devops), [Jira timeline](https://support.atlassian.com/jira-software-cloud/docs/what-is-the-timeline-and-how-do-i-use-it/), [Jira Plans scheduling](https://support.atlassian.com/jira-software-cloud/docs/estimate-and-schedule-issues-in-advanced-roadmaps/), and [The Kanban Guide](https://kanbanguides.org/the-kanban-guide/2020.12/). No external integration or imported data is authorized.

## Schedule contract leaves

The four schedule cards remain backlog or blocked and are only contract and
implementation planning leaves:

| Card | Outcome | Direct dependencies |
| --- | --- | --- |
| SKCP-20A `c3a9c9e9` | Typed schedule projection, overlays, immutable scenarios, and exact reschedule preview | `bea13a70`, `d0edbff1`, `b7ada8b9` |
| SKCP-21A `eddaa1fb` | Synchronized Roadmap, Gantt, and Flow explorer | `bea13a70`, `d0edbff1`, `b7ada8b9`, `5ee56779`, `c3a9c9e9` |
| SKCP-30A `7888e091` | Forecast scenarios and AI schedule recommendations | `bea13a70`, `d0edbff1`, `169028ce`, `f080f150`, `efa9bee8`, `eddaa1fb`, `4e1130cc` |
| SKCP-30B `4e1130cc` | Discriminated forecast calibration and AI schedule insight contracts | `bea13a70`, `d0edbff1`, `169028ce`, `f080f150`, `efa9bee8`, `c3a9c9e9` |

The schedule requirements remain pinned in the V1.1.1 supplement. They define
timezone, null dates, variance, rollup, partial rollup, cycle, drawer,
accessibility, export, performance, calibration, scenario, and visibility
semantics. An accessible table and dependency-list alternative is mandatory for
every visual lens.

## Catalog and gate path

The 24-card catalog remains the V1.1.0 lineage catalog: SKCP-11 through
SKCP-52, including SKCP-21A and SKCP-30A. The complete catalog and every exact
direct dependency are preserved in the pinned V1.1.0 plan and the V1.1.2
relevant-board capture. The two additive schedule contract cards SKCP-20A and
SKCP-30B are captured separately. All catalog leaves plus legacy SKCP-01
`d12b8951`, SKCP-02 `94cbf19a`, and SKCP-07 `f0c63c2a` require a path through
review `d0edbff1` to human gate `bea13a70`.

## Remediation disposition

| Stream | Disposition in V1.1.2 |
| --- | --- |
| F1 through F4A | Pinned historical remediation evidence. The predecessor artifacts are byte-preserved, with V1.1.0 contract bytes archived under lineage to avoid replacing F5 repairs. |
| F5 | Dashboard PR 27 merged as `e1b7c978b00974c7f580c2706c5cdc9d485255ed`; the repaired active contract set is pinned. |
| F6 | SKCoord and SKCapstone releases publish the non-bypassable gate and descriptor safety corrections. The receipts and automatic publication details are pinned. |
| F8 | Dashboard PR 28 closes metric evidence, reachability, insight grounding, and exact-version mutation-preview invariants. |
| F7 | Produces only a truthful candidate, exact board capture, detached manifest receipt, and tests. It does not authorize implementation. |

## Non-authorizations

No implementation source, deployment, restart, external action, product
integration, protected data, Matter data, HammerTime access, claim probe,
human-gate completion, review completion, global-parity reconciliation, commit,
push, merge, or tag is authorized by this plan. A reviewer can decline the
candidate while retaining every predecessor artifact byte-exact.
