# Independent Portfolio Steward architecture review

**Review card:** `69a6bd7b`
**Reviewer:** `codex-portfolio-reviewer`
**Reviewed artifact:** `docs/specs/2026-08-23-portfolio-steward-architecture.md`
**Reviewed SHA-256:** `f3bb3b0ec0dfaa9a85cc6d8d65df49c640d0e56dfc685275a74422db4ac208ba`
**Board observation:** `2026-08-23T22:25:10Z`
**Verdict:** **FAIL**

The selected role split is sound, but the exact architecture is not yet safe
to approve as an implementation contract. It leaves the proposal hash
invariant internally contradictory, and it does not define the atomic and
fenced mutation boundary needed to prevent split-brain claims and WIP races.
The implementation graph also omits exact cross-repository prerequisites for
service-profile filtering, the Jarvis front door, and the SKHarness handoff.

The reviewer did not author or modify the reviewed bytes.

## Findings

### F1 - HIGH - Persona-invariant proposal hashing is undefined and contradictory

Evidence:

- The proposal schema includes `proposal_id`, `presentation_persona_id`,
  `created_at`, and `expires_at` at spec lines 208 through 224.
- The required persona test says the canonical proposal hash must be identical
  across personas while presentation identity may differ at lines 377 through
  381.

Hashing the specified proposal makes the hashes differ. Excluding fields might
solve that, but no canonical content projection, field exclusion list, or
separate presentation envelope is defined. This leaves room for persona data to
enter an authority-bearing hash or for implementations to produce incompatible
hashes.

Required remediation: define a versioned canonical authority-bearing proposal
content object and hash algorithm. Keep persona, session, generated identifiers,
timestamps, and rendered text in a separately hashed presentation envelope.
CapAuth, allocation, and audit must bind the authority-bearing content hash.

### F2 - CRITICAL - Single-writer, split-brain, WIP, and lease atomicity are not specified

Evidence:

- The spec asserts a single writer at lines 117 through 119 but defines no
  leader identity, singleton deployment, distributed lease, fencing epoch, or
  failover protocol.
- Writer rejection at lines 262 through 264 covers a stale decision and current
  card mismatch, but it does not define an atomic compare-and-append operation.
- WIP and lease rules at lines 321 through 333 have no atomic reservation or
  renewal contract.
- Current `skcoord` locks are host-local `flock` locks in
  `src/skcoord/coordination.py:39-59` and
  `src/skcoord/card_store.py:1133-1151`. Card events are separately locked per
  writer log at `src/skcoord/card_store.py:520-591`. Synced nodes can therefore
  pass their own local checks concurrently.

Two nodes can both observe an unowned card and append claims, or two different
cards can both consume the last WIP slot. Fold-time conflict reporting does not
make either mutation safe and does not prevent two executors from starting.

Required remediation: define one authoritative mutation service with durable
leader fencing, or a transactional reservation ledger with a monotonic fencing
epoch. The mutation must atomically validate card ownership, portfolio-wide WIP,
executor capacity, lease generation, dependency and approval preconditions,
one-use decision state, and append receipt before execution can begin.

### F3 - HIGH - AllocationDecision does not carry all final-write preconditions

Evidence:

- `WorkCandidate` carries card, dependency, approval, owner, and lease facts at
  lines 148 through 175.
- `AgentCapacity` carries capacity observations and expiry at lines 185 through
  198.
- `AllocationDecision` at lines 241 through 260 omits the expected card
  revision, dependency revision vector, approval revision or hash, capacity
  revision, lease id or generation, and exact mutation operation.
- The live SKPM-03 card nevertheless requires an expected version, showing a
  mismatch between board acceptance and the reviewed schema.
- The final CapAuth paragraph at lines 291 through 295 binds only the current
  card revision explicitly.

A dependency, human approval, policy, executor capacity, or lease can change
between allocation and append without changing the target card revision.

Required remediation: bind all eligibility inputs or one authoritative
snapshot revision that is atomically compared at write time. Explicitly forbid
the legacy `force` claim path. Any changed dependency, approval, policy,
capacity, lease, executor, operation, or card revision must require a new
decision and idempotency key.

### F4 - HIGH - Separation of duties and self-review are prose, not a state invariant

Evidence:

- The role matrix denies self-approval at lines 105 through 115.
- The flow calls for independent review at lines 340 through 354.
- No typed review assignment, review decision, reviewer eligibility, principal
  inequality, or completion transition precondition is defined.

Nothing in the architecture requires reviewer principal identity to differ
from proposal author, allocator, coordination writer, executor, or artifact
author. It also does not say which append-only event prevents completion when
the independent review is missing or stale.

Required remediation: define versioned `ReviewAssignment` and `ReviewDecision`
contracts, exact principal inequality rules, artifact and revision hashes,
review expiry, and a deterministic completion gate. Reviewer selection must not
be controlled by the reviewed executor or model.

### F5 - HIGH - Service-profile nonselectability and zero-tool fallback lack an owned cross-repo gate

Evidence:

- The spec requires `profile_kind: service`, `selectable: false`, and a
  zero-privilege default at lines 89 through 98.
- Current profile resolution instead returns a broad default list when no
  exposure exists at `src/skcapstone/cli/agent_profile_cmd.py:156-167`. That
  list includes coordination mutations and outbound tools starting at lines 30
  through 58.
- The shell picker enumerates every ordinary agent directory at
  `src/skcapstone/data/sk-agent-picker.sh:62-70`, and noninteractive fallback
  may choose the first directory at lines 305 through 319.
- Installed SKMemory falls back to the first non-template agent at
  `/home/skuser01/.skenv/lib/python3.12/site-packages/skmemory/agents.py:143-169`.
- Mission Control builds its selectable agent set from configured agents and
  sessions at
  `/mnt/cloud/onedrive/projects/DAVE-AI/jarvis-cli/skills/mission-control/src/lib/chat-bootstrap.ts:189-253`.
- SKPM-02 and SKPM-04 are labeled only `repo:skcapstone`; no exact SKMemory or
  `jarvis-cli` implementation and review cards appear in the graph.

Adding the Steward directory today can expose it to humans or select it as a
fallback, and missing profile configuration expands rather than removes tools.

Required remediation: create exact, reviewed cross-repository prerequisites
for a common selectable-profile registry, fail-closed service defaults,
explicit memory identity, and front-door filtering. SKPM-02 and SKPM-04 must
depend on those reviewed cards.

### F6 - HIGH - The SKHarness handoff is not backed by an exact qualified dependency

Evidence:

- The required execution brief is described at lines 362 through 375.
- Current `src/skcapstone/agent_run.py:202-230` records only a free-form
  requester, agent, mode, instruction, and kind. It has no verified human,
  presenter, acting principal, role hash, authorization decision, snapshot,
  lease generation, repo revision, or tool-scope contract.
- SKPM-04 depends only on SKPM-02 and SKPM-03 in both the spec and live graph.
- The spec defers creation of a canonical SKHarness qualification card until a
  future live canary at lines 482 through 485, despite SKPM-04 promising a
  qualified handoff.

Required remediation: create and qualify a versioned producer and consumer
handoff contract before SKPM-04. Bind it as an exact dependency. A later canary
also needs its separately reviewed SKHarness qualification and human gate, as
the spec already recognizes.

### F7 - MEDIUM - Atlas separation is directionally correct but lacks a capability-deny proof

Evidence:

- The boundary at lines 383 through 389 correctly denies portfolio ownership,
  claims, and self-approval.
- `docs/ATLAS_CONSTITUTION.md` gives Atlas broad governed operations and the
  ability to rewrite most of its own estate.
- `skoperator status` returned `active (freeze off)` during this review.
- No exact dependency inventories Atlas capabilities and proves it cannot call
  the future Steward writer, allocator, approval, or reviewer operations.

The design must not depend on Atlas being frozen. Required remediation: add an
exact CapAuth policy and adversarial review dependency proving Atlas can supply
typed observations but cannot obtain portfolio mutation or decision authority.

## Required threat-boundary coverage

| Required check | Result | Evidence |
|---|---|---|
| Persona confusion | FAIL | F1; identity separation text itself is otherwise clear at lines 69 through 98 |
| Body-supplied identity | PASS at design level | Lines 94 through 98 explicitly reject body-supplied principal, capability, target expansion, and mode |
| Privilege escalation | FAIL as implementation graph | F5 and F7; intended role matrix is least privilege |
| Stale snapshots | FAIL | Snapshot expiry exists, but F2 and F3 leave write-time atomicity incomplete |
| Split-brain claims | FAIL | F2 |
| Lease and WIP races | FAIL | F2 and F3 |
| Dependency and human-gate bypass | FAIL | F3; final writer does not bind all prerequisite revisions |
| Self-review | FAIL | F4 |
| Unsafe parity and fallbacks | PARTIAL | Unsafe parity correctly requires abstention; service and memory fallbacks remain unsafe in F5 |
| Service-profile selectability | FAIL | F5 |
| SKHarness handoff | FAIL | F6 |
| Atlas boundary | PARTIAL | Clear policy intent, but no exact capability-deny gate; F7 |

## Live graph verification

The listed SKPM edges match the live CardStore fold:

| Card | Observed state | Exact direct dependencies |
|---|---|---|
| `b151ac5a` SKPM-00 | done | none |
| `69a6bd7b` SKPM-00R | doing, owned by reviewer | `b151ac5a` |
| `215edee5` SKPM-00H | backlog | `69a6bd7b` |
| `7efc76c0` SKPM-01A | doing | `b151ac5a` |
| `ccbe1a37` SKPM-01B | backlog | `7efc76c0` |
| `79a189a9` SKPM-01R | backlog | `7efc76c0`, `ccbe1a37` |
| `d5c6f539` SKPM-02 | backlog | `215edee5`, `79a189a9`, `efa9bee8`, `169028ce`, `50e36b06`, `bea13a70`, `d0edbff1` |
| `2850e05b` SKPM-03 | backlog | `215edee5`, `79a189a9`, `008bd490`, `50e36b06`, `bea13a70`, `d0edbff1` |
| `a981f7bd` SKPM-04 | backlog | `d5c6f539`, `2850e05b` |
| `8248ddd1` SKPM-04R | backlog | `d5c6f539`, `2850e05b`, `a981f7bd` |
| `f0820248` SKPM-05Q | backlog | `8248ddd1` |
| `45d97493` SKPM-05H | backlog | `f0820248` |

The main existing gates are real and currently blocking: `bea13a70` is a
pending human gate, `d0edbff1` remains in review with `review-failed`, and
`008bd490`, `169028ce`, and `efa9bee8` are backlog. `50e36b06` is done.

The graph is correct for the cards it names, but it is not complete for the
cross-repository and concurrency prerequisites in F2, F5, F6, and F7. No live
or autonomous claim path is eligible.

## Checks performed

- `sha256sum docs/specs/2026-08-23-portfolio-steward-architecture.md` matched
  the reviewed SHA-256 exactly.
- `skcapstone coord kanban --json` verified all listed card states, owners,
  acceptance criteria, and direct dependency IDs from the canonical fold.
- `skcapstone coord parity --show 25` reported `checked=1005`, `matched=607`,
  `mismatches=128`, `missing=270`, and open-count drift `10`, with `PARITY
  ALERT`. The spec correctly requires shadow abstention while this remains
  unsafe.
- `skoperator status` reported `active (freeze off)`.
- Source inspection covered CardStore and coordination locking, profile and
  picker fallback behavior, AgentRun attribution, SKMemory fallback, Mission
  Control agent selection, CapAuth delegated capability contracts, Atlas
  constitution, and SKHarness identity and handoff seams.
- The reviewed spec contains no forbidden Unicode dash characters.
- HammerTime `Inbox/` was not searched, read, or processed.

No implementation tests were run because this card is a document and live
graph review. No architecture repair was made.

## Residual risk after required remediation

Even with the findings repaired, rollout must keep shadow mode read-only until
clean scoped parity, deterministic replay tests, multi-node race tests, Atlas
capability-negative tests, profile fallback tests, independent review, and the
exact human gates pass. Model nondeterminism must remain outside eligibility and
allocation, and every live mutation must retain a reversible stop and recovery
path.
