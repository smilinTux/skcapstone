# ADR-0001 candidate capture amendment 1.1.2

Status: proposed for human review

## Decision

This V1.1.2 package is an additive, review-only superseding candidate. It
preserves the original candidate, V1.1.0 package, and V1.1.1 package as
historical audit inputs. It does not rewrite a prior capture, create
implementation authority, or change the owner-service boundary.

The original manifest SHA256 is
`88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`.
The V1.1.0 manifest SHA256 is
`6b35f9e77f8f51dde5243bd9ebc5f55adbf65141d344218b165845bd3475a194`.
The V1.1.1 manifest SHA256 is
`2876a22ea8fe29fb28c8c2c918c9e67b339e9f7836e59218b9bac1dba573dbe0`.

V1.1.0 has a future `generated_at` value and is consequently a preserved but
non-authoritative chronology input. V1.1.1 is a truthful point-in-time capture
of 2026-08-23T22:35:28Z, but it preceded F4A completion and the F5 and F6
publications. It is preserved as non-authoritative lineage evidence rather than
being retroactively edited. V1.1.2 is the current exact-hash review input.

## Pinned current capture

V1.1.2 captures the installed folded CardStore projection with:

- command: `skcapstone coord kanban --json`;
- capture time: `2026-08-24T02:52:32.186485Z`;
- exit status: `0`;
- raw command-stream SHA256:
  `cad72a5b2f1525577826d4764cd1eff3d1e0c955e9fe9aac44c722675c17d953`;
- full command stream publication: withheld because it contains unrelated
  board content; the exact relevant closure and provenance are published; and
- raw cardinality: 732 unique card IDs.

The raw projection is parsed in memory before use. The canonical relevant-board
capture records the selected roots, folded dependencies, statuses, attributed
event references, subset hash, and closure counts. The selection does not
reconcile, migrate, or rewrite the shared board.

At this capture, F8 `26c69f86` is done, the human gate `bea13a70` is backlog,
the independent review `d0edbff1` is review, and F7 `ef91a99f` is ready. The review retains a folded
dependency on the human gate. Neither status grants implementation eligibility.

## Contract and visual boundary

The current repaired contract set is the tracked F5 contract surface plus the
merged F8 invariant repair and is pinned by path and SHA256 in the V1.1.2
manifest. Its machine contract remains
Draft 2020-12 with local OpenAPI references. The prior V1.1.0 contract bytes
are preserved only under `docs/review/lineage/v1.1.0/`; the active F5 files are
not overwritten.

The V2 HTML estate-pulse wireframe is the active renderable visual. Two
historical PNG wireframes are recorded only as unavailable lineage inputs. The
mandated patch tool rejected their standard Git binary patch format before any
write, so no PNG bytes are fabricated, copied by another write method, or
listed as active artifacts.

## UI state map

| UI element | Typed contract location | Review rule |
| --- | --- | --- |
| Service query | `scope.service_id` | It scopes a read-only projection. Ownership, watermarks, and freshness are attribution, not service state. |
| Truth strip | Metric or projection `truth_state` | Preserve `current`, `stale`, `partial`, `unavailable`, `unreachable`, `unknown`, `unauthorized`, and `not_applicable`. Missing evidence cannot become healthy or zero. |
| Schedule and Forecast Gantt | Versioned scoped schedule projection, item semantics, and forecast contracts | Roadmap, Gantt, Flow, Forecast, and Scenario lenses share a scope and remain no-write projections. Cycles, blackout conflicts, missing required dates, and inaccessible required nodes fail closed. |
| AI insight | Insight or recommendation proposal or abstention | Evidence, counter-evidence, uncertainty, alternatives, and provenance are required. AI explains engine output and cannot authorize, mutate, or execute. |
| Action preview | Exact action-preview status, hash, expiry, policy, and approvals | Preview is non-executing. A changed or stale parameter needs a new preview and eligible human approval remains separate. |

## Gates, remediation, and parity

F5 `057f981b` merged dashboard PR 27 as
`e1b7c978b00974c7f580c2706c5cdc9d485255ed`; its card records `266 passed`,
focused `21 passed`, Draft 2020-12 and local-reference checks, architecture
hashes, Ruff, and dash checks. F6 `54cd56f2` published SKCoord `v0.1.35` from
`9cfe0db9c2f6d3a57cbef999168658134c830fe7` and SKCapstone `v0.15.55` from
`2244a5f50b8111499f1b1a944c78c9c410f33493`. The F6 evidence is SHA256
`8ba590df883503741539fd24291fa7a8455d73829aff7c834c3912169d76db17`.
F8 `26c69f86` merged dashboard PR 28 as
`dcdd6b25df3663656e7d476ac848ffdf6e183c66`; it separates unreachable,
unknown, unavailable, and unauthorized states and requires evidence-grounded
metrics, insights, and exact-version mutation previews.

Historical parity is preserved exactly as 985 checked, 590 matched, 125
mismatches, 270 missing, and open drift 10. Fresh read-only parity is a failing
observation: 1051 checked, 651 matched, 130 mismatches, 270 missing, legacy
open 225, store open 215, drift 10, threshold 5, and exit status 1. V1.1.2
records both values separately. It does not claim clean global parity or
authorize reconciliation.

## Non-authorizations and rollback

This package does not authorize implementation, deployment, production
activation, service restart, external action, account access, Matter access,
protected-data retrieval, HammerTime access, HammerTime Inbox access, board
reconciliation, completion of `bea13a70`, or completion of `d0edbff1`.

Rollback is declining V1.1.2 and retaining all predecessor bytes. No migration
or data rewrite occurs. The board capture is append-only evidence, not a board
mutation or a replacement for an owner projection.
