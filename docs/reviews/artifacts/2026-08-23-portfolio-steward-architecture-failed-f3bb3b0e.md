# Persona-neutral front door and Portfolio Steward architecture

**Date:** 2026-08-23  
**Status:** HUMAN-SELECTED ARCHITECTURE, implementation gated by cards  
**Decision owner:** Human owner  
**Architecture card:** `b151ac5a`  
**Parent epic:** `b43537a8`

## 1. Decision

Adopt a dedicated Portfolio Steward as the internal planning principal. Keep
Jarvis, or any other human-selected agent personality, as a persona-neutral
human front door.

The invariant is:

> Personality is presentation and never authority.

The Portfolio Steward may analyze portfolio state and produce typed planning
proposals. A deterministic allocator decides whether a specific card is
eligible to be claimed and emits a typed allocation decision. Existing
SKCapstone coordination code performs the append-only mutation only after
CapAuth authorization. AutoCoder and SKHarness execute only the exact claimed
leaf card. An independent reviewer evaluates the result.

No language model owns workflow state, grants permission, approves its own
work, claims work directly, or performs an external action.

## 2. Why this option was selected

Three options were considered:

1. Dedicated Portfolio Steward behind a persona-neutral front door. This is
   selected. It separates human interaction, planning, allocation, execution,
   and review while reusing the existing stack.
2. Expand Atlas into the portfolio manager. This was not selected because
   Atlas already has a focused operations and observation constitution.
   Combining operational command with portfolio priority would increase the
   authority and failure radius of one principal.
3. Make Jarvis the permanent all-in-one manager. This was not selected because
   the human wants Jarvis to remain generic and replaceable. Binding authority
   to a personality would also make audit, least privilege, and persona
   switching ambiguous.

The selected design gives the human one conversational surface without
creating one privileged identity.

## 3. Existing components and ownership

This architecture reuses the estate as follows:

| Component | Existing ownership | Portfolio behavior |
|---|---|---|
| Jarvis or selected personality | Human interaction and presentation | Presents plans, asks for decisions, and reports results |
| Portfolio Steward | New logical planning principal | Reads authorized projections and emits typed proposals |
| Portfolio Allocator | New deterministic policy component | Evaluates eligibility and creates allocation decisions |
| SKCapstone and CardStore | Coordination state and append-only events | Remain the only source of truth for card state and claims |
| SKCP and SKDashboard | Operator projection and control-plane UI | Display proposals, reasons, exclusions, decisions, and WIP |
| CapAuth | Authentication, authorization, and audit obligations | Authorizes each read and mutation for a named principal |
| Atlas | Operations observation and governed operational action | Reports health and operational blockers without taking portfolio ownership |
| AutoCoder and SKHarness | Sandboxed implementation and grading | Execute the exact claimed implementation leaf |
| Independent reviewer | Separation-of-duties review | Reviews architecture, code, evidence, and gate satisfaction |

CardStore is consumed from `skcoord`, not the legacy `skcapstone.card_store`
compatibility shim.

## 4. Identity model

Every request and audit record distinguishes four identities:

1. `human_subject_id`: the authenticated human or service that initiated the
   interaction.
2. `presentation_persona_id`: the selected voice and interaction style, such
   as Jarvis. This field cannot add capabilities.
3. `acting_principal_id`: the service principal performing a bounded action,
   such as `portfolio-steward`, `portfolio-allocator`, or
   `skharness-executor`.
4. `decision_principal_id`: the principal whose deterministic or human
   decision authorized a state transition.

The effective capability set is derived from the authenticated subject,
acting principal, resource, purpose, and policy. It is never derived from
`presentation_persona_id`.

Persona switching changes presentation settings only. It does not change
claims, leases, policy, approvals, capability grants, Matter access, tenant
access, or execution credentials.

Cross-role calls use a versioned `RoleInvocationV1` envelope. It separates
presentation agent, interaction profile, soul revision, and session; verified
human principal and session; acting principal, role revision, and role-spec
hash; exact snapshot and card targets; sanitized CapAuth decision and
capability scope; and run mode, correlation id, idempotency key, model route,
prompt hash, and schema hash. The trusted server derives every field except
presentation context and user intent. Body-supplied principals, capabilities,
target expansion, or execution mode are rejected. A Steward service profile is
`profile_kind: service`, `selectable: false`, and defaults to zero privileged
tools plus explicit `portfolio.read` and `portfolio.propose` grants.

## 5. Role and authority matrix

`Yes` means the role may perform the operation only within an explicit
CapAuth grant. `Proposal` means the result has no workflow effect.

| Role | Read | Propose | Mutate | Approve | Execute | Review |
|---|---:|---:|---:|---:|---:|---:|
| Human through selected persona | Yes | Yes | Explicit commands | Human gates | Explicit commands | Yes |
| Presentation persona | Delegated view | Relay only | No | No | No | No |
| Portfolio Steward | Authorized projection | Yes | No | No | No | No |
| Portfolio Allocator | Authorized snapshot | Decision only | No | No | No | No |
| Coordination writer | Exact target | No | Append exact authorized event | No | Claim event only | No |
| Atlas | Operational views | Operational advice | Existing Atlas grants only | No self-approval | Existing governed operations only | Operational evidence |
| AutoCoder and SKHarness | Claimed task brief and repo | Implementation plan | Isolated worktree and draft artifacts | No | Exact claimed leaf | Self-check only |
| Independent reviewer | Proposal, diff, tests, evidence | Findings | Review records | Gate-specific only | No | Yes |
| CapAuth | Policy inputs | Obligations | Audit obligations | Authorization decision | No | Decision trace |

The coordination writer is a narrow single-writer boundary. It receives a
validated allocation decision and an idempotency key. It does not call a
model, infer intent, select a different card, or broaden the requested action.

## 6. Portfolio data contracts

### 6.1 PortfolioSnapshot

A proposal is evaluated against one immutable snapshot reference:

```text
PortfolioSnapshot
  snapshot_id: string
  snapshot_hash: sha256
  observed_at: timestamp
  expires_at: timestamp
  board_revision: string
  projection_revision: string
  parity_state: healthy | degraded | unsafe
  policy_version: string
  cards: list[WorkCandidate]
  agents: list[AgentCapacity]
```

`snapshot_hash` covers the canonical normalized input. Any decision against a
different hash must be reevaluated.

### 6.2 WorkCandidate

The candidate shape contains facts, not model conclusions:

```text
WorkCandidate
  card_id: string
  title: string
  state: string
  card_revision: string
  priority: critical | high | medium | low
  class_of_service: expedite | fixed-date | standard | intangible
  human_order: integer | null
  enrollment_state: unenrolled | enrolled | suspended
  enrollment_policy_version: string | null
  tags: list[string]
  dependency_ids: list[string]
  dependency_states: map[string, string]
  acceptance_criteria: list[string]
  repo_id: string | null
  size: xs | s | m | l | xl | null
  execution_ready_attestation: string | null
  owner_principal_id: string | null
  lease_expires_at: timestamp | null
  human_gate_state: not-required | pending | approved | rejected
  approval_ref: string | null
  approved_card_revision: string | null
  approved_card_hash: sha256 | null
  created_at: timestamp
  updated_at: timestamp
  ready_at: timestamp | null
  fixed_date_at: timestamp | null
```

Unknown or conflicting values remain unknown. The adapter may not infer a
repository, approval, completed dependency, or expired lease from prose.

### 6.3 AgentCapacity

Capacity is a versioned observation, not an inferred model opinion:

```text
AgentCapacity
  principal_id: string
  allowed_task_classes: list[string]
  allowed_repo_ids: list[string]
  wip_limit: integer
  active_wip: integer
  active_card_ids: list[string]
  lease_state_fresh: boolean
  capability_ref: string
  policy_ref: string
  observed_at: timestamp
  expires_at: timestamp
```

Missing, expired, or conflicting capacity data makes the target executor
ineligible.

### 6.4 PortfolioPlanProposal

The Portfolio Steward returns a proposal, never a command:

```text
PortfolioPlanProposal
  schema_version: string
  proposal_id: string
  snapshot_id: string
  snapshot_hash: sha256
  policy_version: string
  authored_by_principal_id: string
  requested_by_subject_id: string
  presentation_persona_id: string | null
  created_at: timestamp
  expires_at: timestamp
  objective: string
  recommendations: list[WorkRecommendation]
  exclusions: list[CandidateExclusion]
  warnings: list[string]
  abstention: Abstention | null
  provenance: list[SourceReference]
```

Each `WorkRecommendation` includes the card id, proposed order, class of
service, deterministic eligibility summary, ranking key, rationale, expected
outcome, dependencies, risks, and requested human decisions. Rationale is
advisory. It cannot override the eligibility result.

Each `CandidateExclusion` includes the card id, stable reason codes, and the
input fields that caused exclusion. Exclusions must be visible to the operator
so missing metadata does not silently bury work.

### 6.5 AllocationDecision

The deterministic allocator reevaluates the selected recommendation against a
fresh snapshot and returns:

```text
AllocationDecision
  schema_version: string
  decision_id: string
  proposal_id: string
  card_id: string
  snapshot_id: string
  snapshot_hash: sha256
  policy_version: string
  eligible: boolean
  reason_codes: list[string]
  ranking_key: tuple
  acting_principal_id: string
  target_executor_principal_id: string
  lease_seconds: integer
  idempotency_key: string
  authorization_state: pending
  decided_at: timestamp
  expires_at: timestamp
```

The writer must reject the decision when it is ineligible, expired, already
used, based on a stale snapshot, addressed to another executor, or no longer
matches current card state.

## 7. Eligibility policy

The first implementation applies only to cards explicitly enrolled in this
managed workflow. Legacy cards are reported as unenrolled, not repaired or
mutated automatically.

A card is eligible only when every required predicate is true:

1. It is a claimable leaf task with an explicit, current `execution-ready`
   attestation and is not tagged `do-not-claim` or equivalent.
2. It is not done, superseded, blocked, or already in review.
3. Every declared dependency is complete in the canonical board fold.
4. It has non-empty acceptance criteria.
5. It has exactly one explicit repository identifier for implementation work.
6. It has one recognized size value.
7. Any required human gate is approved for the exact version in scope.
8. It has no active owner or unexpired lease.
9. The target executor is allowed for the repository and task class.
10. The target executor is below its WIP limit.
11. The board snapshot is fresh and its required read path is healthy.

The allocator fails closed on missing, duplicate, malformed, or conflicting
inputs. It never guesses the intended repository, dependency state, approval,
owner, or capability.

The allocator does not claim that authorization has occurred. Its decision
sets `authorization_state` to `pending`. The coordination writer obtains the
final CapAuth decision immediately before mutation and binds the authorization
decision id, obligations, current card revision, and write receipt in the
append-only evidence. A denial or unavailable decision creates no claim event.

## 8. Deterministic ordering

Models may suggest goals and explain tradeoffs. They do not calculate the
authoritative ordering.

Eligible candidates are ordered by the following stable tuple:

1. Explicit human order, when present.
2. Class of service rank.
3. Number of eligible downstream cards directly unblocked by completion,
   descending.
4. Fixed-date urgency, when present and validated.
5. Oldest ready timestamp.
6. Human-set priority rank.
7. Card id as a stable final tie breaker.

Class order is `expedite`, `fixed-date`, `standard`, then `intangible`.
`expedite` requires an explicit human approval with an expiry and rationale.
Without that approval, the candidate is treated as `standard` and a warning is
emitted.

The ordering tuple and all normalized inputs are returned in the decision so
the result can be reproduced without a model.

## 9. WIP and lease rules

Initial conservative policy:

- One active implementation card per execution principal.
- One active architecture or planning card per planning principal.
- Review capacity is tracked separately from implementation capacity.
- A claim has an explicit lease duration and owner principal.
- Lease renewal requires a fresh heartbeat and authorization.
- An expired lease is not silently stolen. Existing partial-claim recovery and
  board read-safety rules must complete before live allocation is enabled.
- `expedite` work may exceed a lane WIP limit only with a recorded human
  override. It never bypasses eligibility, CapAuth, or dependencies.

The Portfolio Steward may recommend pausing or reordering work. It cannot
cancel, unclaim, or rewrite an active card.

## 10. Interaction flow

```text
Human
  -> selected persona, such as Jarvis
  -> Portfolio Steward reads authorized SKCP projection
  -> PortfolioPlanProposal
  -> persona presents recommendation and exclusions
  -> human selects or policy requests one candidate
  -> deterministic allocator reads fresh canonical snapshot
  -> AllocationDecision
  -> CapAuth decision
  -> SKCapstone coordination writer appends exact claim event
  -> AutoCoder and SKHarness execute exact claimed leaf in sandbox
  -> independent review and required human gate
  -> CardStore completion evidence and SKCP projection
  -> selected persona reports the result
```

The same flow works through Jarvis, another named personality, a CLI, or a UI.
Changing the front door does not change the underlying principals or policy.
The Portfolio Steward runs as a separate service principal and process. The
front door must not switch `SKAGENT` in-process to impersonate that principal.

## 11. AutoCoder and SKHarness handoff

The execution brief contains only the exact card id and task revision, repo and
base revision, description, acceptance criteria, allowed paths and tools, gate
evidence, tests, grounding references, executor principal, lease, and
idempotency key.

Personal memory, persona prompt content, unrelated portfolio context, raw
capability tokens, and operator credentials do not enter the build sandbox.
This preserves the existing one-engine, two-plane, two-front-door separation.

SKHarness may create code and draft artifacts under its existing sandbox and
grading rules. It does not merge, self-approve, expand scope, or select follow
on cards.

The integration must include a persona-invariance test. Given the same human,
snapshot, policy, and request through two different presentation personas, the
canonical proposal hash, acting principal, capability scope, authorization
decision, target snapshot, memory boundary, board writer, and tool allowlist
must be identical. Only presentation text and interaction profile may differ.

## 12. Atlas boundary

Atlas remains the operational specialist and may supply typed health, capacity,
incident, change, cost, and runtime evidence. It acts only through its existing
constitution, ledger, gates, and capabilities. It does not own portfolio
priority, claim software work, self-approve, or gain access through a persona.
ITIL-created work still passes the same eligibility and override rules.

## 13. SKCP and board presentation

SKCP should add a read-oriented portfolio workspace after its current
measurement and read-safety gates complete. The minimum view contains:

- current objective and policy version;
- active WIP by principal and lane;
- eligible queue in deterministic order;
- excluded cards grouped by reason code;
- dependency blockers and downstream impact;
- proposal age, snapshot age, and expiry;
- persona, requester, author, allocator, writer, and executor attribution;
- accepted, rejected, expired, and abstained recommendations;
- links to card events, tests, review, and completion evidence.

The UI must visually distinguish a proposal from an authorized decision and a
completed mutation. A conversational response alone is never proof of a
claim.

## 14. Failure, abstention, and recovery

The Steward or allocator abstains when the canonical read is unsafe, parity or
partial-read recovery is unsafe, input is expired or conflicting, identity is
ambiguous, a dependency is unresolved, a human gate does not approve the exact
version, WIP or lease state is uncertain, or the requested mutation differs
from the decision. The writer separately fails closed when CapAuth is denied or
unavailable.

An abstention returns a stable reason code, human-readable explanation, and
safe remediation suggestion. It creates no claim event.

Retries reuse the same idempotency key only for the exact decision and target.
A changed card, snapshot, policy, executor, or requested action requires a new
decision and key.

## 15. Audit and outcome learning

Append-only evidence reconstructs the requester and persona; author principal;
snapshot, policy, model route, hashes, and sources; candidates, exclusions, and
ordering; allocation and CapAuth decisions; claim and lease; executor, repo,
task revision, tools, tests, review, and result; human decisions; proposal
disposition; and later outcomes without rewriting history.

Outcome learning may tune advisory prompts or propose policy changes. It may
not silently change eligibility predicates, class order, WIP, lease behavior,
or authority. Deterministic policy changes require a versioned card, tests,
review, and approval.

## 16. Security and legal boundaries

- CapAuth mediates each protected read and every mutation.
- Tenant and Matter isolation is enforced before data reaches a model.
- Protected content follows classification, egress, source-rights, and purpose
  policy.
- Models receive typed, least-context proposals and never raw credentials.
- External actions remain outside this architecture and require their own
  validated, approved, queued, dispatched, and receipt-verified workflow.
- No production activation, deployment, account creation, legal action, or
  HammerTime `Inbox/` processing is authorized by this decision.

## 17. Implementation graph

The implementation is split into independently claimable leaves. Existing
SKCP work is reused as a dependency instead of duplicated.

| Card | Leaf | Purpose | Direct dependencies |
|---|---|---|---|
| `b151ac5a` | SKPM-00 | This architecture and contract | None |
| `69a6bd7b` | SKPM-00R | Independent architecture and threat review | SKPM-00 |
| `215edee5` | SKPM-00H | Exact-hash human architecture approval | SKPM-00R |
| `7efc76c0` | SKPM-01A | Pure typed contracts and deterministic readiness and ordering in `skcoord` | SKPM-00 |
| `ccbe1a37` | SKPM-01B | Thin read-only SKCapstone shadow CLI | SKPM-01A |
| `79a189a9` | SKPM-01R | Independent review of the engine and CLI policy slice | SKPM-01A, SKPM-01B |
| `d5c6f539` | SKPM-02 | Governed shadow Portfolio Steward proposal service | SKPM-00H, SKPM-01R, SKCP recommendation, forecast, read-safety, and measurement review gates |
| `2850e05b` | SKPM-03 | Deterministic allocation decision service in simulation | SKPM-00H, SKPM-01R, SKCP command client, read-safety, and measurement review gates |
| `a981f7bd` | SKPM-04 | Persona-neutral front door and exact-card SKHarness handoff | SKPM-02, SKPM-03 |
| `8248ddd1` | SKPM-04R | Independent security, concurrency, and separation-of-duties review | SKPM-02, SKPM-03, SKPM-04 |
| `f0820248` | SKPM-05Q | Public-synthetic and read-only shadow qualification | SKPM-04R |
| `45d97493` | SKPM-05H | Human decision on an exact bounded canary | SKPM-05Q |

SKPM-01 can be implemented in an isolated worktree because it is pure logic
over public synthetic fixtures and has no live board read or mutation. The
canonical `skcoord`, `skcapstone`, and `skdashboard` checkouts contain unrelated
active work and must not be overwritten. SKPM-02 and later must wait for every
named board gate. No card inherits permission from the parent epic.

The current live-allocation blockers are the superseding SKCP measurement
candidate and human approval chain, independent review re-evaluation, partial
claim recovery, and clean scoped board parity. A shadow planner must return an
abstention while parity is unsafe.

A live canary and its independent review are intentionally not created yet.
They require a canonical SKHarness qualification card and then depend on that
exact card plus SKPM-05H. Broader SKCP metric-governance and estate-wide
qualification cards remain later expansion gates rather than direct blockers
for the first bounded shadow path.

## 18. Non-goals

This architecture does not:

- make Jarvis, Atlas, or a model the owner of the board;
- replace SKCapstone, CardStore, SKCP, CapAuth, or SKHarness;
- create a second workflow database or execution engine;
- infer authority from personality, model quality, or model cost;
- enable autonomous production deployment, merging, or external actions;
- repair legacy cards or silently normalize their metadata;
- process HammerTime `Inbox/` or protected Matter content.
