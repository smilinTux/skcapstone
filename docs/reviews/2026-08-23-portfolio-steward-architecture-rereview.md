# Independent Portfolio Steward architecture rereview

**Review card:** `abab982e`
**Reviewer:** `codex-portfolio-rereviewer`
**Reviewed commit:** `c777ba6f6208e8d5529ab0c695b7cb451f265336`
**Main architecture SHA-256:** `df1686af38d54dd1e8be079c17423aebce5ab5148ce81e8dfbe75a3c5d626be9`
**Governance companion SHA-256:** `ee201f02f0fe4c6a04a1e41c0459b09a22bd451fe141f88cb25a0aa12bbdb909`
**Preserved failed artifact SHA-256:** `f3bb3b0ec0dfaa9a85cc6d8d65df49c640d0e56dfc685275a74422db4ac208ba`
**First failed review SHA-256:** `b3c036edcf5e044d3dd7b5c0e76daae8b6d5055b90989f58c874e3911e6f969e`
**Board observation:** `2026-08-23T22:52:27Z`
**Verdict:** **PASS**

The exact remediated architecture and companion close F1 through F7 from the
first review. This is an architecture PASS, not an implementation or activation
PASS. Current source still contains the unsafe seams identified in the failed
review. The live graph now makes their replacement and independent qualification
prerequisites, so no Steward service, allocator integration, execution handoff,
qualification, or canary is authorized by this verdict alone.

The reviewer did not author or repair either reviewed specification.

## F1 through F7 closure

| Finding | Result | Exact closure evidence |
|---|---|---|
| F1, persona-invariant hashing | PASS | Main architecture lines 200 through 235 separate `PortfolioPlanContentV1` from `PortfolioPlanPresentationV1`. Governance lines 14 through 86 define the authority-bearing projection, exclusions, JCS normalization, SHA-256 algorithm, deterministic ordering, duplicate rejection, separately hashed presentation, and the rule that authorization and execution bind only `plan_content_hash`. Card `7efc76c0` requires persona-invariance, canonicalization, adversarial identity, and repeatability tests. |
| F2, split-brain, WIP, and lease atomicity | PASS | Governance lines 156 through 224 define one authoritative managed-claim critical section, a signed writer fence with monotonic epoch, atomic fold and append of reservation, claim, lease, WIP, and receipt, idempotent replay, exact readback before execution, stale-node fencing, no automatic failover, and append-nothing failure. Cards `226430d5`, `048c5de2`, and `7e72aca6` form the implementation, service, and independent two-writer qualification chain. |
| F3, complete write preconditions | PASS | Governance lines 111 through 154 bind the exact operation, plan, card and dependency revisions, approval, policy, capacity, WIP, lease generation, executor, repository revision, expiry, and idempotency. Lines 195 through 207 require current atomic comparison and one-use consumption. `force` is forbidden at lines 153 through 154 and again at lines 217 through 224. Card `226430d5` explicitly rejects legacy force and direct unfenced claims. |
| F4, independent review and completion | PASS | Governance lines 226 through 264 define typed `ReviewAssignmentV1` and `ReviewDecisionV1`, exact artifact hash and revision binding, expiry, reviewer capability, deny set, principal inequality against author, allocator, writer, executor, and approval subject, plus an atomic completion gate. Cards `7efc76c0` and `226430d5` require the typed inequality and fail-closed completion behavior. |
| F5, service-profile safety | PASS | Governance lines 266 through 296 define a versioned, hash-bound service profile that is nonselectable, fallback-ineligible, zero-tool by default, explicitly memory-bound, and process-isolated. Missing, unreadable, conflicting, or unknown-version state fails closed. The exact single-repo chain is `a0a89b24` in SKCapstone, `b1ce03c4` in SKMemory, `248aa691` in jarvis-cli, then independent cross-repo review `90ebc6a7`. |
| F6, SKHarness handoff | PASS | Governance lines 298 through 339 define authenticated `ExecutionHandoffV1`, exact plan, decision, receipt, epoch, lease, card, repository, scope, executor, reviewer policy, idempotency, and expiry binding, with closed-version and tamper rejection and exact ordered `only_ids`. The exact chain is `47b32de2`, `de712b36`, `113aeadd`, and `fe3877e5`; installed site-packages are expressly excluded from modification. |
| F7, Atlas separation | PASS | Governance lines 341 through 357 permit only typed operational observation proposals and explicitly deny Atlas planning, allocation, claim, approval, review, execution, handoff, and policy mutation. Cards `8fb27a78` and `62406541` implement and independently test that policy while Atlas is active, frozen, restarted, and presented through different personas, without changing freeze or service files. |

## Threat and cross-cluster checks

| Required property | Result | Evidence |
|---|---|---|
| Persona confusion and body-supplied identity | PASS | Governance lines 88 through 109 require trusted server derivation of human, acting principal, target, and authorization, reject body-supplied authority, and limit the Steward to read and propose. Main lines 71 through 116 keep presenter identity outside capability derivation. |
| Unknown, unavailable, and unauthorized states | PASS | Main lines 175 through 198, 258 through 265, and 380 through 394 require ineligibility or abstention. Governance lines 283 through 286 and 359 through 370 require nonselectability, zero tools, and no executable claim on unavailable policy, authorization, registry, board read, parity, fence, review, audit, or receipt. Relevant card criteria preserve `Unknown` separately from healthy state. |
| Immutable service identity and profile | PASS | `ServiceProfileV1` is versioned and hash-bound, service roles run in separate processes, and in-process `SKAGENT` impersonation is forbidden at governance lines 266 through 296. Profile conformance card `90ebc6a7` requires immutable source revisions and exact-hash fixtures across all three repositories. |
| One workflow source of truth | PASS | Governance lines 158 through 162 retain CardStore and prohibit a second workflow database or automatic failover. Main line 479 repeats the non-goal. |
| Exact claim fencing and no force | PASS | Governance lines 164 through 224 define the fence, atomic operation, receipt, readback, direct-caller deny set, and force rejection. Mutation review `7e72aca6` requires same-card and last-WIP-slot races, stale epoch, policy outage, changed dependencies and approvals, replay, crash points, and force attempts. |
| Dependency and human-gate bypass | PASS | `215edee5` and `7efc76c0` both depend directly on `abab982e`; both remain backlog. Every later path is transitively behind those gates. Completion also requires an exact, nonexpired PASS and exact human gate under governance lines 260 through 264. |
| Self-review | PASS | Typed principal inequality is mandatory, presenter identity cannot qualify a reviewer, and author, executor, and model cannot choose the reviewer. Handoff and end-to-end review cards repeat the constraint. |
| Unsafe parity and fallback | PASS with active abstention | Live parity reported `checked=1029`, `matched=630`, `mismatches=129`, `missing=270`, open-count drift `10`, and `PARITY ALERT`. Main lines 462 through 465 and governance lines 359 through 370 therefore require shadow abstention and no live claim. |
| Atlas freeze and service boundary | PASS | `skoperator status` reported `active (freeze off)`. The denial design does not rely on freeze. `8fb27a78` and `62406541` prohibit changes to freeze state, `skoperator.service`, `skoperator.timer`, and service overrides. No Atlas freeze or service file was changed in this review. |
| Credential-free evidence | PASS | Review inspection used source text, version identifiers, sanitized board fields, and hashes only. No credential value, capability token, private key, protected corpus, or sensitive endpoint was read or recorded. The evidence file passed the repository review-artifact sensitive-content screen. |
| Non-vacuous tests | PASS as a gated contract | `7efc76c0` requires happy, edge, adversarial, collision, and repeatability cases. `a0a89b24` requires breaking each fixture condition. `7e72aca6` requires positive winner assertions plus losing race, stale, replay, crash, and force controls. `fe3877e5` requires golden vectors plus a one-field tamper matrix. `62406541` pairs the permitted observation action with exhaustive denied Portfolio actions. The docs gate's own negative control also proved that each tier can fail. |

## Live dependency graph

The canonical CardStore fold matched the graph in main architecture lines 426
through 454. Every Portfolio Steward leaf has exactly one `repo:*` label.

| Chain | Exact live dependencies |
|---|---|
| Architecture | `24d5b867 <- b151ac5a`; `abab982e <- 24d5b867`; `215edee5 <- abab982e`; `7efc76c0 <- abab982e` |
| Managed mutation | `226430d5 <- 215edee5,79a189a9`; `048c5de2 <- 226430d5`; `7e72aca6 <- 048c5de2` |
| Service profiles | `a0a89b24 <- 215edee5`; `b1ce03c4 <- a0a89b24`; `248aa691 <- a0a89b24`; `90ebc6a7 <- a0a89b24,b1ce03c4,248aa691` |
| SKHarness | `47b32de2 <- 215edee5`; `de712b36 <- 2850e05b,7e72aca6,90ebc6a7`; `113aeadd <- 47b32de2,de712b36`; `fe3877e5 <- de712b36,113aeadd` |
| Atlas | `8fb27a78 <- 215edee5,048c5de2`; `62406541 <- 8fb27a78,7e72aca6` |
| Integration | `d5c6f539` depends on the human, policy-slice, profile, Atlas, and existing SKCP gates; `2850e05b` depends on the human, policy-slice, mutation, Atlas, and existing SKCP gates; `a981f7bd <- d5c6f539,2850e05b,7e72aca6,90ebc6a7,fe3877e5,62406541` |

Repository ownership is exact: SKCoord owns `226430d5`; SKCapstone owns
`048c5de2`; SKMemory owns `b1ce03c4`; jarvis-cli owns `248aa691`; SKHarness
owns `47b32de2`, `113aeadd`, and `fe3877e5`; CapAuth owns `8fb27a78` and
`62406541`. Their cross-repo review cards depend on all corresponding author
cards. All implementation and human-gate cards were backlog at observation;
only this rereview was doing.

## Current source seam verification

The source confirms why the new gates remain mandatory:

- `src/skcapstone/cli/agent_profile_cmd.py:30` and `:156` still expose a broad
  default tool list when profile curation is absent.
- `src/skcapstone/data/sk-agent-picker.sh:62` and `:307` still enumerate agent
  directories and contain a noninteractive directory fallback.
- Installed SKMemory 0.11.17 `skmemory/agents.py:143` still falls back to the
  first non-template agent.
- jarvis-cli commit `44b5f7e`,
  `skills/mission-control/src/lib/chat-bootstrap.ts:158`, still combines config
  and session agent identifiers without a service-profile registry.
- `src/skcapstone/agent_run.py:202` still accepts free-form requester, agent,
  instruction, and mode fields.
- Installed SKHarness 0.3.15 `autocode/agentrun_bridge.py:154` still builds an
  ad hoc `WorkItem` from folded card and request context rather than consuming
  `ExecutionHandoffV1`.
- Fetched SKCoord `origin/main` at
  `082a5077920f7126957b28f624cb8bc75e1527ea` still uses host-local `flock` in
  `src/skcoord/card_store.py:228` through `:252`; legacy `claim_task()` at
  `src/skcoord/coordination.py:1015` accepts `force`, with the dependency bypass
  at `:1071` through `:1087`.
- CapAuth commit `d8942ac` has strict delegated fail-closed decisions but no
  Portfolio-specific Atlas capability matrix yet.

These are not hidden exceptions to the PASS. The reviewed architecture names
them as unimplemented boundaries and the live graph prevents downstream use
until their exact cards and independent reviews pass. Dirty unrelated source
work in the canonical SKCoord and jarvis-cli checkouts was preserved.

## Checks performed

- All four input hashes matched the values above, including the preserved
  failed artifact and first failed review.
- `python /home/skuser01/work/sk-standards/scripts/docs_check.py --repo . --tier 1 --tier 2 --tier 3` passed all configured tiers. Tier 2 correctly reported
  that no PR diff context was supplied.
- `python /home/skuser01/work/sk-standards/scripts/docs_check.py --self-test`
  passed its negative control by proving deliberately invalid fixtures fail all
  three tiers.
- `pytest -q tests/test_review_artifact_sink.py -k
  'secret_and_raw_credential_material_is_rejected'` passed 6 tests.
- Live board queries verified card status, ownership, exact dependencies,
  acceptance criteria, and one repository label per Portfolio Steward leaf.
- Targeted ASCII, sensitive-content, and `git diff --check` checks passed.
- HammerTime `Inbox/` was not searched, read, or processed.

## Residual risks and limits

- This review validates contracts and graph gates only. No managed-claim,
  service-profile, handoff, Atlas deny, or typed review implementation exists
  yet.
- Live parity is unsafe, so even shadow planning must abstain where a canonical
  read cannot be proven. Live allocation remains disabled.
- Fencing availability intentionally has no automatic failover. Loss of the
  pinned writer, policy service, audit sink, or receipt readback stops work.
- Canonical nested recommendation and source-reference schemas, profile
  registry storage, fence persistence, and handoff transport must be pinned by
  their implementation cards and golden vectors. Any incompatible expansion
  requires a new reviewed version, not permissive parsing.
- The human approval card must bind both reviewed hashes, this PASS evidence,
  policy version, and residual risks. This PASS does not authorize production
  activation, deployment, merge, external action, or a live canary.
