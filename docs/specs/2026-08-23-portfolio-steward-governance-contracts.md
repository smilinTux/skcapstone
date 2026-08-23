# Portfolio Steward governance contracts

**Date:** 2026-08-23
**Status:** REMEDIATION CANDIDATE
**Remediation card:** `24d5b867`
**Failed review:** `69a6bd7b`

This companion specification closes findings F1 through F7 in
`docs/reviews/2026-08-23-portfolio-steward-architecture-review.md`. It narrows
and supplements the main Portfolio Steward architecture. When the documents
conflict, this contract is authoritative only after independent PASS review
and exact-hash human approval.

## 1. Canonical plan content and presentation

Authority-bearing plan content and human-facing presentation are separate
objects.

```text
PortfolioPlanContentV1
  schema_version: "portfolio-plan-content.v1"
  producer_role: "portfolio-steward"
  authority: "advisory"
  mode: "shadow"
  objective_hash: sha256
  snapshot_id: string
  snapshot_hash: sha256
  snapshot_expires_at: timestamp
  policy_id: string
  policy_version: string
  policy_hash: sha256
  recommendations: list[CanonicalRecommendationV1]
  exclusions: list[CanonicalExclusionV1]
  warnings: list[CanonicalWarningV1]
  abstention: CanonicalAbstentionV1 | null
  source_refs: list[CanonicalSourceRefV1]
```

`PortfolioPlanContentV1` excludes persona, soul, session, rendered prose,
request correlation, proposal instance id, model prose, creation time, and
presentation expiry. Its only validity timestamp is the already-bound snapshot
expiry. The objective is represented by a normalized objective hash, while the
human-readable objective remains in the presentation envelope.

Canonical content uses JSON Canonicalization Scheme rules. All strings are NFC
Unicode, timestamps are UTC RFC 3339, maps have unique string keys, arrays have
contract-defined order, floats and non-finite numbers are forbidden, and hashes
are lowercase SHA-256 hex. The content hash is:

```text
plan_content_hash = sha256(UTF8(JCS(PortfolioPlanContentV1)))
```

Recommendations are stored in deterministic ranking order. Exclusions,
warnings, reason codes, source refs, tags, dependency ids, and allowed repo ids
are sorted by their contract key before canonicalization. Duplicate values are
invalid rather than silently deduplicated.

```text
PortfolioPlanPresentationV1
  schema_version: "portfolio-plan-presentation.v1"
  proposal_instance_id: string
  plan_content_hash: sha256
  requested_by_subject_id: string
  presenter_agent_id: string
  interaction_profile_id: string
  soul_revision: string | null
  session_id: string
  objective_text: string
  rendered_text: string
  created_at: timestamp
  expires_at: timestamp
  correlation_id: string
```

The presentation object has its own
`presentation_hash = sha256(UTF8(JCS(PortfolioPlanPresentationV1)))`. CapAuth,
allocation, reservation, execution, review, and completion bind
`plan_content_hash`, never `presentation_hash`. Audit stores both hashes and
their relationship.

Persona invariance means that identical normalized inputs produce the same
`PortfolioPlanContentV1` bytes and `plan_content_hash`. Presenter, soul,
session, instance id, timestamps, objective text, rendered text, and
`presentation_hash` may differ. A persona cannot add, delete, reorder, or
rewrite canonical recommendations, exclusions, warnings, or abstention.

## 2. Trusted role invocation

Every invocation uses `RoleInvocationV1`:

```text
RoleInvocationV1
  presentation: presenter id, interaction profile, soul revision, session id
  human: verified principal id and verified session id
  acting: principal id, role id, role revision, role-spec hash
  target: board snapshot hash and exact card ids
  authorization: sanitized decision and exact capability scope
  run: analyze | propose, correlation id, idempotency key, route and hashes
```

The trusted server derives `human`, `acting`, `target`, and `authorization`.
The client supplies presentation context and user intent only. Body-supplied
principal, role, capability, target expansion, execution mode, writer,
executor, or reviewer is rejected and audited.

The Portfolio Steward has only `portfolio.read` and `portfolio.propose`.
Neither capability implies board mutation, claim, reprioritization, approval,
review, dispatch, execution, deployment, merge, or external action.

## 3. Allocation decision preconditions

The allocator is deterministic and produces no mutation.

```text
AllocationDecisionV1
  schema_version: "portfolio-allocation.v1"
  decision_id: string
  plan_content_hash: sha256
  operation: "claim"
  card_id: string
  expected_card_revision: string
  dependency_revision_vector: map[card_id, revision]
  dependency_vector_hash: sha256
  approval_ref: string | null
  approval_hash: sha256 | null
  approved_card_revision: string | null
  policy_id: string
  policy_version: string
  policy_hash: sha256
  capacity_revision: string
  expected_active_wip: integer
  wip_limit: integer
  expected_lease_generation: integer
  target_executor_principal_id: string
  target_repo_id: string
  target_repo_revision: string
  eligible: boolean
  reason_codes: list[string]
  ranking_key: tuple
  requested_lease_seconds: integer
  idempotency_key: string
  authorization_state: "pending"
  decided_at: timestamp
  expires_at: timestamp
```

The decision id is content-addressed from the canonical decision fields except
itself. A changed plan, card, dependency, approval, policy, capacity, WIP,
lease generation, executor, repo, operation, expiry, or requested lease
requires a new decision and idempotency key.

The legacy `force` claim path is forbidden for enrolled Portfolio Steward work.
No adapter may translate an ineligible decision into a forced claim.

## 4. Fenced atomic reservation and claim

Host-local card locks do not provide cross-node compare-and-swap. Live
allocation is disabled until SKCoord provides one global managed-claim critical
section and SKCapstone exposes it only through a single authoritative mutation
service. CardStore remains the workflow source of truth. No second workflow
database or automatic failover is introduced.

```text
WriterFenceV1
  mutation_service_id: string
  deployment_instance_id: string
  pinned_node_id: string
  pinned_endpoint_id: string
  leader_epoch: monotonically increasing integer
  manifest_revision: string
  manifest_hash: sha256
  fence_issued_at: timestamp
  fence_expires_at: timestamp

ManagedClaimReceiptV1
  claim_event_id: string
  claim_receipt_hash: sha256
  decision_id: string
  idempotency_key: string
  plan_content_hash: sha256
  card_id: string
  executor_principal_id: string
  writer_fence: WriterFenceV1
  lease_generation: integer
  lease_expires_at: timestamp
  authorization_decision_id: string
  authorization_obligations_hash: sha256
  validated_preconditions_hash: sha256
  cardstore_revision: string
  state: executable | released | expired
  created_at: timestamp
```

One `managed_claim()` critical section must:

1. Validate an unexpired writer fence and reject an older or duplicate epoch.
2. Prove the decision is eligible, unexpired, unused, and requests `claim`.
3. Compare the current card, dependency, approval, policy, capacity, WIP, lease,
   executor, repo, and operation values with the decision.
4. Obtain and bind the final CapAuth decision and obligations.
5. Fold the global managed-claim stream and card state under the same lock,
   reserve the card and one WIP slot, increment lease generation, consume the
   decision and idempotency key, and append one durable CardStore event that
   contains the reservation, claim, lease, WIP, and receipt facts.
6. Return the exact prior receipt on an idempotent retry, or append nothing on
   every failure.

The executor receives no runnable handoff until the event append succeeds and
the exact folded receipt is read back. It never starts work optimistically.

Every executor validates `leader_epoch`, claim event id, receipt hash, lease
generation, lease expiry, executor principal, decision id, and exact task
revision before starting and at each renewal. An older epoch or generation is
fenced even if a stale node still believes it is leader.

Only the authoritative mutation service owns managed claim transitions. Its
signed manifest pins one principal, node, endpoint, and epoch; CapAuth checks
that exact current epoch on every request. There is no automatic failover. A
partition, stale manifest, policy outage, or wrong node returns unavailable or
denied and appends nothing. Models,
personas, Atlas, Portfolio Steward, allocator, dashboard, MCP handlers, and
SKHarness cannot call `managed_claim()` directly. Legacy `force` and unfenced
claim paths reject enrolled cards.

## 5. Independent review and completion

```text
ReviewAssignmentV1
  assignment_id: string
  artifact_type: string
  artifact_id: string
  artifact_revision: string
  artifact_hash: sha256
  author_principal_ids: list[string]
  disallowed_reviewer_principal_ids: list[string]
  required_reviewer_capability: string
  review_policy_id: string
  review_policy_version: string
  assigned_by_principal_id: string
  created_at: timestamp
  expires_at: timestamp

ReviewDecisionV1
  assignment_id: string
  artifact_hash: sha256
  reviewer_principal_id: string
  verdict: pass | fail
  findings_hash: sha256
  evidence_refs: list[string]
  decided_at: timestamp
  expires_at: timestamp
```

The reviewer principal must not equal any proposal author, allocator, writer,
executor, artifact author, approval subject, or principal in the assignment's
deny set. Presenter identity neither qualifies nor disqualifies a reviewer.
The artifact author, executor, and model cannot choose or replace the reviewer.

Completion requires a nonexpired PASS for the exact artifact revision and hash,
no unresolved blocking finding, satisfied dependency revisions, and any exact
human gate. The coordination writer reevaluates these facts atomically. A FAIL,
stale assignment, changed artifact, or principal collision blocks completion
and creates no completion event.

## 6. Nonselectable service profiles

Internal service roles use a common versioned profile record:

```text
ServiceProfileV1
  profile_id: string
  profile_kind: "service"
  selectable: false
  fallback_eligible: false
  memory_principal_id: string
  default_tools: []
  capability_policy_ref: string
  profile_revision: string
  profile_hash: sha256
```

Missing, unreadable, unknown-version, or conflicting service metadata defaults
to nonselectable, fallback-ineligible, zero tools, and no memory alias. No
component may convert a missing service profile into the broad interactive
agent default.

SKCapstone owns the canonical schema, active-agent guard, shell picker guard,
and fail-closed tool exposure. SKMemory owns memory discovery and fallback
filtering. Jarvis Mission Control owns UI and session bootstrap filtering.
Each repo receives its own implementation card and independent review. The
Portfolio Steward service card depends on every reviewed prerequisite.

Service roles run in separate processes. Code must not mutate `SKAGENT` to
impersonate a service. `SKMEMORY_AGENT`, SKHarness economic attribution, board
writer, and audit principal remain bound to the real acting principal.

## 7. Versioned SKHarness handoff

```text
ExecutionHandoffV1
  schema_version: "portfolio-execution-handoff.v1"
  plan_content_hash: sha256
  allocation_decision_id: string
  claim_event_id: string
  claim_receipt_hash: sha256
  leader_epoch: integer
  lease_generation: integer
  lease_expires_at: timestamp
  authorization_decision_id: string
  card_id: string
  card_revision: string
  repo_id: string
  repo_revision: string
  task_brief_hash: sha256
  acceptance_hash: sha256
  allowed_paths: list[string]
  allowed_tools: list[string]
  executor_principal_id: string
  reviewer_policy_ref: string
  idempotency_key: string
  expires_at: timestamp
```

The producer signs or authenticates the exact envelope. The SKHarness consumer
rejects unknown versions, altered hashes, stale fences or leases, wrong
executor, wrong repo revision, unapproved tools or paths, missing claim receipt,
or any card outside the exact ordered `only_ids` input. It does not rescan the
board for substitute work.

The handoff excludes persona prompt, personal memory, unrelated board context,
raw capabilities, credentials, and rendered recommendation prose. SKHarness
cannot merge, self-approve, select follow-on work, or bypass the independent
review assignment.

A canonical editable SKHarness source checkout and versioned consumer
qualification are prerequisites. Installed site-packages are never patched.
The producer, consumer, and cross-repo qualification have separate cards and
review evidence before SKPM-04 can become eligible.

## 8. Atlas capability separation

Atlas may receive a narrow `portfolio.observation.propose` capability for typed
operational evidence. CapAuth policy explicitly denies Atlas:

- `portfolio.plan.author`;
- `portfolio.allocate`;
- `portfolio.claim` and direct `managed_claim()` access;
- `portfolio.approve`;
- `portfolio.review` for Portfolio Steward artifacts;
- `portfolio.execute` and SKHarness handoff creation;
- policy, WIP, lease, class, priority, or reviewer mutation.

The deny policy is tested against Atlas while active, frozen, restarted, and
presented through different personas. Atlas status never changes the result.
An independent capability review is an exact prerequisite of SKPM-02 through
SKPM-04.

## 9. Fail-closed rollout

All components remain shadow-only until the remediated architecture passes
independent review and exact-hash human approval. Live claims also require a
qualified managed-claim boundary, clean scoped parity, exact service-profile gates,
Atlas deny proof, versioned SKHarness producer and consumer qualification,
multi-node race tests, and rollback evidence.

Any unavailable policy, authorization, profile registry, canonical board read,
parity proof, managed-claim boundary, fence, review, audit append, or receipt
readback produces abstention or no executable claim. No fallback broadens
access or starts work.
