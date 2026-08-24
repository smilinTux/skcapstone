# ADR-0001 amendment: superseding candidate 1.1.0

Status: proposed for human review

## Decision

This additive amendment preserves ADR-0001 and every artifact pinned by
`SKCP-00-CANDIDATE-MANIFEST.json` byte for byte. It selects the versioned
`1.1.0` contract set, the compatibility note, and V2 synthetic wireframes for
a new exact-hash human and independent review. The authority boundary remains
unchanged: SKDashboard is a projection and preview plane, while named owner
services remain authoritative for records, policy, command execution, and
receipts.

The candidate is `proposed_for_human_review`, not implementation authorized.
Human gate `bea13a70` must approve the exact superseding manifest before
independent review `d0edbff1` can determine whether leaf work is eligible.

## Remediation disposition

| Finding or correction | Card | Disposition in this candidate |
| --- | --- | --- |
| F1 truth-state invariant | `ee1d0874` | The metric contract distinguishes observed zero from absent, unavailable, unknown, and not-applicable values. Failed-state values are null and zero requires evidence. |
| F1A version alignment | `bd732651` | Every versioned schema and OpenAPI document declares `1.1.0`; the compatibility note maps V2 labels to typed values. |
| F2 grounded recommendations | `0242f9f2` | Action-oriented recommendations require best-practice, impact, risk, counter-indicator, alternative, and precondition grounding, otherwise they are typed abstentions. |
| F2A V2 evidence and authorization correction | `f94fde82` | V2 exposes direct deep links, evidence drawers, accessible role paths, keyboard dialogs, and synthetic truth-state rendering. |
| F2B V2 service scope correction | `b24213ea` | V2 carries selected service scope into rows and reconciles the truthful row summary rather than inventing service state. |
| F3 independent-review dependency gate | `83a404bf` | Legacy implementation cards now carry the independent-review dependency and the operator CLI folds it before eligibility. |
| F3A activation and accidental-claim recovery | `079cd760` | The editable operator environment received the reviewed dependency semantics and released the accidental probe claim without completing its card. |
| F3B atomic dependency and claim-release amendments | `1f9ee2c9` | Dependency add and exact claim release have bounded, cross-process local atomicity, idempotency, audit attribution, and fail-closed recovery. |
| F3C shared claim serialization and release recovery | `a081d5ed` | A shared board mutation lock, fixed lock ordering, single recoverable release event, byte-exact compensation, and recovery records prevent split projections. |
| F3D claim-complete race and mutation durability | `8ab522ee` | Claim, complete, release, labels, lifecycle, stale-release, and Joule boundaries have deterministic transition IDs, failure propagation, and local-safe storage behavior. |
| F3E partial-claim recovery and read safety | `50e36b06` | Partial paired claims compensate or recover, durable completion mints once, and CardStore reads reject symlinked or multiply linked external content. |

F3 is the independent-review dependency correction. F3B is the separate
atomicity correction for dependency and claim-release board mutations. These
labels are intentionally not interchangeable.

## Contract-facing UI state mapping

V2 presentation is descriptive only. It does not create a second state
machine or cause the dashboard to become an owner service.

| UI layer | V1.1 source of truth | Required behavior |
| --- | --- | --- |
| Truth badge and metric row | Metric result `truth_state` and projection freshness `truth_state`: `current`, `stale`, `partial`, `unavailable`, `unknown`, or `not_applicable` | Render the typed value and source errors. `Policy filtered` is presentation text for `not_applicable`, never a new truth state. |
| Service selector and service row | `service_id`, `source.owner`, projection `source_owner`, source watermarks, and freshness timestamps | Scope the query and show the named service owner. A selector cannot assert freshness, health, or an execution result on its own. |
| Authorization preview | Action-preview `status`: `ready`, `denied`, `needs_information`, `needs_approval`, `stale`, or `expired`; exact `preview_hash`; required approvals | Render the returned state. Stale parameters require a new preview. Only a ready, exact, unexpired preview can be offered to the authorization boundary, which revalidates it. |
| AI insight and recommendation | Insight `status`: `proposal` or `abstained`; recommendation `status`: `proposed` or `abstained` | Display evidence-linked proposal or explicit abstention. Any action control remains preview only until the separate preview and authorization contracts are satisfied. |

## Review and safety boundary

This amendment does not authorize implementation, deployment, service restart,
external action, protected-data retrieval, Matter access, HammerTime Inbox
access, a successful implementation claim, completion of `bea13a70`, or
completion of review `d0edbff1`. It introduces no implementation source and
does not alter a production service. Rollback is selecting the original
manifest for audit or declining this superseding candidate; neither action
rewrites the original candidate.
