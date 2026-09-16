# Governed reviewer profile TDD v1

Card: 90151318. Parent: 91ad57c8. Author: codex-reviewer-tdd-90151318.
Status: proposed implementation contract; independent review required.
Implementation consumer: a2be68d9. Its independent source review is b4cf79ea.
Date: 2026-09-08 UTC. This document does not complete either consumer card.

## Scope and source identity

Authoritative repository: https://github.com/smilinTux/skcapstone.git.
Inspected checkout: /home/skuser01/work/skcapstone.
Baseline: 26676b63ea98f406cddce6f348bf74acb0c0484f.
The following machine-readable contract is inert documentation, never a live
profile registry or authorization. Hashes identify Git blob bytes at the full
baseline commit, not Git object IDs. All listed files matched those bytes in
the inspected worktree. Other pre-existing dirty files are excluded.

```json
{
  "schema": "skfleet.reviewer-profile-design/v1",
  "card": "90151318",
  "parent": "91ad57c8",
  "baseline": "26676b63ea98f406cddce6f348bf74acb0c0484f",
  "activation": "disabled",
  "pins": {
    "AGENTS.md": "74307d6f80188ee67c8f1dab983b02310b0dc24827ce14ac621ba26b028c02ee",
    "src/skcapstone/seat_runtime.py": "03eee30b195e79eafe66182038a7df2663d1700d7e5bf3c419e162a8a0d0d3c2",
    "src/skcapstone/seat_boundaries.py": "21f94539e8e1a214ff208ca1467c4e414bd9f2116bbaaad5379454b263f73d25",
    "src/skcapstone/seat_cycle_entrypoint.py": "0e3b183e62b176fda02ca4f2e4b77604037ad3393c03eabe05fdc4fcd9a0d1aa",
    "src/skcapstone/seat_cycle_guard.py": "7cd9cb17e68c0b109017dc678838056146ee7e68771289504fcb4b4d49f24323",
    "src/skcapstone/seat_mail.py": "796e8a52dcd2f0130409f6cf50f6b0772a58f1565395c9cb07f53ca00c1e8317",
    "src/skcapstone/link_cycle.py": "4ee3a1274585f70a51e103ba5f1c732687febe774ddcaa518ecaa067133ff65d",
    "src/skcapstone/link_observation_feed.py": "1322ead79cc000163f39135e82af78ef8f9bebf66a12dddb00498ee578bc33b4",
    "scripts/fleet/skfleet-worker-wrapper.py": "c8eebc105b797eccc67ec68b28b641aa12362d64d892f3c6f5e95d8993a707aa",
    "scripts/fleet/skfleet-rotate.py": "b72037c0680de647fb53ca56bca0e4b431ef36d83779738f42a517dc71db00cd",
    "scripts/fleet/seat-control-plane.json": "8c1629483f1796197812ef38700320815edd8fc7f383995240c814a02b1e302b",
    "systemd/skfleet-link.service": "8fe00da9e1dc68db9b230452c9bfacc8d895820b28fec30b466e1bd01788a9ea",
    "systemd/skfleet-link.timer": "3ea0204861ac542b70f2d04f1c9132c7e6d7a548b980ea5a291f24c8bdc61b6a",
    "systemd/skfleet-mero.service": "0f42d0dfa7106dfc615f9202ace4e38d18484c2a02e383f6fb4c42f83bf5691b",
    "systemd/skfleet-mero.timer": "d30e209ed1c780829e3bd21e1b812a042ea89e03865b65efecfba2395ee44b55",
    "docs/fleet/seat-charters.md": "7660aba4245ac2a975fd7fd6da34073fb2f97294f0f98b00b0365bad043b70c0",
    "docs/fleet/SKMAIL-WORK-PROTOCOL.md": "18e32f51434e8a55edc3d3cb0730c60fa01e0311d94edbb965c56b33dd162c6b"
  },
  "profiles": [
    {"id": "link", "seat": "link", "policy": "review.link/v1", "mailbox": "link", "route": "none", "provider": "none", "mode": "recurring", "operation": "link_operation", "authority": ["observe", "recommend", "triage", "assign_reviewer", "evaluate_merge"], "tools": ["feed.read", "handoff.propose", "mail.scoped", "evidence.append"], "interval_s": 300, "timeout_s": 120},
    {"id": "seraph", "seat": "seraph", "policy": "review.seraph/v1", "mailbox": "seraph", "route": "none", "provider": "none", "mode": "disposable", "operation": "verify_artifact", "authority": ["observe", "verify_artifact"], "tools": ["candidate.read", "checks.run_sandboxed", "verdict.propose", "mail.scoped", "evidence.append"], "interval_s": 0, "timeout_s": 1800},
    {"id": "mero", "seat": "mero", "policy": "review.mero/v1", "mailbox": "mero", "route": "none", "provider": "none", "mode": "recurring", "operation": "mero_operation", "authority": ["observe", "recommend"], "tools": ["observations.read", "census.run", "recommendation.propose", "mail.scoped", "evidence.append"], "interval_s": 600, "timeout_s": 180},
    {"id": "qwen-reviewer", "seat": "qwen", "policy": "review.qwen/v1", "mailbox": "qwen-reviewer", "route": "review.qwen.local/v1", "provider": "local-qwen", "mode": "disposable", "operation": "review_candidate", "authority": ["observe", "verify_artifact"], "tools": ["candidate.read", "checks.run_sandboxed", "model.propose", "verdict.propose", "mail.scoped", "evidence.append"], "interval_s": 0, "timeout_s": 1800},
    {"id": "codex-reviewer", "seat": "codex", "policy": "review.codex/v1", "mailbox": "codex-reviewer", "route": "review.codex.approved/v1", "provider": "openai-codex", "mode": "disposable", "operation": "review_candidate", "authority": ["observe", "verify_artifact"], "tools": ["candidate.read", "checks.run_sandboxed", "model.propose", "verdict.propose", "mail.scoped", "evidence.append"], "interval_s": 0, "timeout_s": 1800}
  ]
}
```

## Reviewed lineage and limits

Evidence paths below are relative to the SKCapstone evidence/work directory.
These are historical results, not independent approval of current baseline or
this proposed adapter. In particular the current feed hash differs from the
last historical feed review. Requalification must test the complete candidate.

| Source | Evidence path | SHA-256 |
| --- | --- | --- |
| a11ce003 runtime, reviewed by a11ce005 | a11ce003/PASS_FOR_REVIEW.md | 92668a201cfd6f246c3535dfccb323511f3b47d926c8d6b7872e481a036fbeb8 |
| a11ce004 feed, reviewed by a11ce008 | a11ce004/PASS_FOR_REVIEW.md | 8d0653a219a5fd8496ccd784622c94faf6fc0b4e3423c2bb888845bbfc45ca8e |
| a11ce00b final inner freshness review | a11ce00b/PASS_FOR_REVIEW.md | b9d4960fe29aebc200e95a3a0513b46bd937f7a3e5924f9e5f8eba8a1ba4aae6 |
| e8d8a97b lifecycle, reviewed by 06865e25 | e8d8a97b/candidate-evidence.json | f088a049cf3d8c26849f73e6812c19a04436abc413f7e929bfb32fa8e37ffda0 |
| a2be68d9 missing design finding | a2be68d9/FAIL-CLOSED.md | 6d3e05b22d71d60735f6dc4bd844d74ac3429aecfec7015710f2586c3de6e32a |

e8d8a97b reviewed commit is ff15faf4e2aca6b1449d37f1f3cac2b392a908a9,
tree 13587784574edb272e1f9a3d8ef01806596f307f, ref
refs/heads/review/e8d8a97b-lifecycle-handoff. Its typed durable candidate and
quiet-process semantics are retained. A ref name alone never proves reachability.

The wrapper already records claim-scoped mail, terminal evidence and exits.
The launcher already selects a workspace, checks admission, claims, reads back
the revision, and starts the wrapper. It currently takes a generic lane model,
generic Pi tools and arbitrary child command; owner naming does not enforce a
profile. Link/Mero currently use separate recurring entrypoints and host-local
cycle guards. There is no existing five-profile resolver to declare qualified.

## Versioned profile schema

The future closed object is `skfleet.reviewer-profile/v1`. Unknown fields,
duplicate JSON keys, unsupported schema versions, blank identifiers, duplicate
profile IDs and coercion of strings to booleans or integers are errors. All
fields are required. Profile, policy and binding hashes cover the exact UTF-8
file bytes, before parsing; no reserialization silently changes the approved
identity. The enclosing registry pins the profile hash without a self-hash.
Hashes are lowercase 64-hex SHA-256; Git commits and trees
are full lowercase 40-hex object IDs. IDs are lowercase ASCII matching
`[a-z][a-z0-9-]{0,63}`. Paths are resolved within policy-owned roots, rejecting
traversal, symlink escape and writable aliases to another run's resources.

| Field | Type and invariant |
| --- | --- |
| schema, profile_id, revision | Exact schema, one of the five IDs above, positive integer revision |
| identity | Object with seat, principal_ref, instance_namespace; registered principal must match the reviewed seat and profile |
| policy | Object with id, revision, sha256; resolves to the exact reviewed public policy bytes |
| mailbox | Object with seat_recipient, instance_template; template is `{profile_id}.{run_id}` and may not resolve to another instance |
| model | Object with route_id, provider_class, binding_ref, binding_sha256, requested_model, served_model_allowlist; no fallback route |
| tools | Unique closed list of logical operations from the profile row; no wildcard, arbitrary shell or environment-derived additions |
| authority | Unique closed list from the row; effective authority is its intersection with authenticated principal, policy and exact card |
| scheduling | Object with mode, interval_s, timeout_s, active_placement_ref, active_placement_sha256, max_inflight; positive bounds, max_inflight=1 per instance |
| review | Object with required, distinct_host, distinct_principal, distinct_session, distinct_workspace; all true for reviewer operations |
| lifecycle | Object with heartbeat_s=30, no_progress_s=300, recovery_max_attempts=1, retry_backoff_s=60 |
| approval | Object with review_card, candidate_commit, candidate_tree, evidence_sha256; exact independent PASS required before admission |

For route `none`, binding_ref, binding_sha256 and requested_model must be null
and served_model_allowlist empty; any model call is denied. Other routes require
nonempty public binding references, digest, exact requested model and a nonempty
served model allowlist, verified before any provider call. Runtime credentials
are never profile fields. They remain behind the governed service boundary.

The design rows specify intended policy and route IDs, not installed identities
or qualified endpoints. No principal fingerprint, provider endpoint, or model
revision is invented here. The implementation must accept synthetic bindings
in isolated tests and fail closed on absent real bindings. A later exact
deployment packet supplies registered principals, policy hashes, placement,
model mapping and approval. That dependency does not block authoring source.
Seraph v1 verifies artifacts deterministically without a model; adding a model
or recurring queue polling requires a separately reviewed profile revision.

Stable seat identity names responsibility; every process gets a distinct run
ID, registered principal binding, claim owner, mailbox recipient, session,
workspace and evidence namespace. A profile's display name is never identity
proof. An attested instance binding connects the run principal to the seat and
must not grant the seat principal's broader permissions. Qwen/Codex map to the
existing feed seat identifiers `qwen`/`codex`, without adding fleet mutation
permissions to the global Seat enum or aliasing either profile to Seraph.

## Worker and policy interfaces to implement later

One small pure resolver, proposed `src/skcapstone/reviewer_profile.py`, parses
and validates profiles, hashes and public bindings and returns an immutable
resolved context or a typed refusal. It must not read credentials, claim a
card, start a worker or execute tools. The implementation card adds its tests.
Reuse existing lifecycle functions rather than a parallel claim store, mailbox,
heartbeat daemon, scheduler or recovery loop.

Proposed wrapper inputs are `--profile-id`, `--profile-revision`,
`--profile-sha256`, `--binding-ref`, `--binding-sha256` and `--run-id`, in
addition to existing card, owner, claim-revision, host and output paths. Both
launcher and wrapper independently resolve and compare the same context hash.
Model, lane and tool flags conflicting with it are refused. Profile mode
selects a closed operation ID from the table; arbitrary child argv is forbidden
on the governed profile path. Existing generic workers remain a separate path
and cannot impersonate a governed profile through owner strings or labels.

`candidate.read` reads only admitted immutable candidate artifacts.
`checks.run_sandboxed` executes a hash-pinned test plan with no network, ambient
credentials or access to the producer checkout; outputs go to run-owned scratch.
It is not raw bash. `model.propose` has only the selected governed route.
`verdict.propose`, `handoff.propose` and `recommendation.propose` return typed
data; a trusted sink validates schema, actor, card, revision and artifact hash
before mediated append. Mail and evidence append are scoped coordination
effects, never approval or external action. Mero's census is observational.

Link has no verdict operation and never reviews the work it assigns. This v1
profile deliberately lacks merge execution although the broader Link charter
has separately gated merge authority. Seraph cannot repair reviewed bytes.
Mero cannot claim, assign, launch, release or adjudicate reviews. All five
profiles deny deploy, install, release, merge, service control, credential
access, unrestricted repository writes and application actuation. Activated
Niobe alone retains fleet control; models and seat workers do not acquire it.
Jarvis remains a user-directed assistant, not a lifecycle fallback actor.

## Admission and scheduling

1. Resolve immutable profile, policy, approval and deployment binding. Reject
   missing/mismatched hashes, disabled placement, invalid route or tool widening
   before mailbox reads, candidate reads, provider calls or process spawn.
2. For Link/Mero retain chiap08 active placement and chiap01 disabled standby
   as historical v1 defaults, subject to an exact reviewed placement binding.
   Fence host first and take the existing nonblocking SeatCycleGuard. One cycle
   per profile/control revision may run; overlap is a recorded no-op. Cadences
   and hard timeouts remain 300/120 seconds and 600/180 seconds respectively.
3. Disposable Seraph/Qwen/Codex consume exactly one eligible review card and
   immutable candidate. Re-read dependency completion and acceptable verdicts,
   human/reserved holds, lane capability and review/source state immediately
   before admission. Respect do-not-claim and not-claimable exclusions. Missing
   policy or stale observations consume no claim and no review slot.
4. Resolve one canonical isolated Git workspace and full candidate commit/tree,
   read-only source plus separate scratch. No ambiguous root, shared writable
   checkout, symbolic link escape or default-to-current-directory behavior.
   Reserve an exclusive workspace lease, then use existing atomic claim and
   readback. Pin `(card_id, owner, claim_revision)` and process generation.
5. Activated controller starts the common wrapper only after a fresh matching
   readback. On launch failure release only that exact generation through the
   mediated API; drift records refusal and leaves the newer owner untouched.
   The child cannot claim work or substitute a profile. Finish one card and exit.

Recurring seats retain standing responsibility without holding a review card
across ticks. Each cycle has its own cycle ID, process generation and bounded
evidence; no fabricated review claim is used for observational cycles. If a
recurring operation needs a card mutation, it emits a proposal for a separately
claimed exact task. Common heartbeat/evidence/cleanup interfaces carry either
the real claim tuple or cycle/control revision, and reject mixing the two.
Seraph's standing verification responsibility is supplied by repeated exact-card
dispatch, not an unbounded daemon. Horizontal replicas never share run state.

## Independence, feed and capacity

Link consumes only `skfleet.link-observation-feed/v1`. Keep the existing
envelope and inner-observation freshness limits of 15 minutes, future tolerance
of 30 seconds, source revision and canonical evidence digest checks. Preserve
typed PullRequestObservation, ProducerIdentity and ReviewerIdentity inputs.
Missing, stale, malformed, replayed or mismatched feeds produce a bounded no-op
or BLOCKED; no GitHub client or credential reader is added to Link.

Reviewers must differ from every candidate author/repair producer and from
Link. Identity, host, session and workspace must all differ from the producer,
as required by recommend_one_reviewer. Resolve canonical principal aliases,
host identity, session generation and real workspace paths before comparison.
Reject blank/unknown lineage, renamed source identities and unproven aliases.
All contributors since the reviewed base count as producers; a repair requires
a new immutable candidate and independent review. Host scarcity causes waiting,
never relaxation. The shared runtime account is not evidence of independence.

Use the existing full handoff recommendation hash and revision readback.
Additionally reserve one active review key over canonical repository, source
card/generation, candidate head and review purpose across ALL reviewer profiles.
Changing reviewer name must not bypass suppression. Per-run claim/cycle keys
identify attempts. Repeated observations of the same head do not create reviews;
new heads require new keys. A terminal exact PASS is reused only with unchanged
scope and no later FAIL/BLOCKED lineage. Persist consumed recommendation IDs and
claim-fenced receipts through the authoritative control path, not a process set.

Host-local flock is insufficient for cross-host exclusivity. Until one qualified
single-active dispatcher owns reservations, profile admission is disabled.
That dispatcher serializes global review keys and lease accounting before claim,
rechecking after claim; inability to prove single authority fails closed.
The TDD does not claim that eventually synced files provide distributed CAS.

Deployment binds total qualified capacity C and reserved review capacity R,
integers with C >= 1 and 1 <= R <= C. Count live processes/cgroups, pending
launch leases and unreconciled generations once each. Source work is admitted
only while its count is below C-R; review workers may use any remaining slot.
Reserved slots never spill to source jobs in v1. Unknown/stale accounting means
no new launches. Link and Mero singleton budgets do not impersonate review slots.
Choose eligible reviews by priority, creation time, then card ID; choose an
eligible approved profile by explicit card constraints and stable profile ID.
No model switch to fill idle capacity. No same-key parallel review without a
separately approved purpose and capacity policy revision.

## Mail, heartbeat, terminal evidence and recovery

Each instance reads only its addressed card/revision mailbox. Stable seat mail
may carry advisory queue notices, not private task context or approval. Keep
skmail.work.v1 recipient, expiry, body hash, message ID deduplication and claim
fences. Broadcast is advisory only. No execution of mail text and no automatic
global acknowledgement. Mail outage records degraded status and cannot grant
authority or manufacture healthy presence. No shared instance read cursor.

Every 30 seconds the wrapper records presence from the actual child process
and mailbox result, with separate last-progress time and progress sequence.
Success requires observed work, not log bytes or a timer tick. Include profile
revision/hash, policy hash, binding hash, run ID, seat/principal, claim or cycle
tuple, host/boot ID/PID/start ticks/cgroup, requested and served model/revision,
route, source commit/tree, tool/schema/prompt hashes and evidence pointers.
For route none, model fields are null. Never infer death from quiet logs.

At 300 seconds without substantive progress on a still-live disposable worker,
emit one escalation keyed by card, owner, claim revision and observation
generation. Preserve the worker and its slot. At hard timeout the controller
requests bounded stop of the exact owned process generation and records it.
Recovery requires verified process/cgroup death, fresh claim readback and
current policy. Unknown liveness keeps capacity reserved and emits BLOCKED.
Allow at most one replacement attempt with a new generation after 60 seconds;
second failure blocks for operator review. Recurring dead-cycle recovery reuses
SeatCycleGuard boot ID/start-tick evidence, never PID age alone.

On exit, stop heartbeat, collect bounded sanitized diagnostics and publish
immutable bytes plus hashes before linking a typed result. Exit 0 alone is not
PASS. Publish candidate commit, tree, durable ref or reconstructable archive,
source/producers and exact evidence SHA-256. Verify actual reachability/content.
PASS_FOR_REVIEW is provisional; it cannot complete independent review, authorize
integration, or enable services. e8d8a97b readiness remains exact and fail closed
on partial metadata. Stale-generation terminal output is evidence about that
attempt only, not a verdict against a newer claim.

Cleanup releases only exact owned workspace/slot leases after process death;
retain candidate, mailbox history, review and recovery evidence. No broad rm,
history rewrite or claim release inferred from stdout. Corrections supersede
records. Publication/cleanup retries are idempotent by run and claim generation.

## Deterministic verification contract

This card's executable tests validate the inert profile matrix and baseline
blob pins and exercise existing independence and authority APIs. They do NOT
test an implemented profile resolver, new tool sandbox or reserved scheduler.
The future implementation must add the following cases without skipped or
xfail placeholders. Use tmp_path, fake clocks, synthetic principals/bindings,
fake provider and subprocess adapters, and effect-counting spies only.

| ID | Required future test and observable result |
| --- | --- |
| P01 | All five valid reviewed profiles resolve exactly; each unknown field/version, duplicate key, blank identity or changed hash refuses with zero effects |
| P02 | Same seat replicas have different registered run identities/mailboxes/workspaces; forged alias or shared resource refuses before claim |
| P03 | Requested/served model or provider mismatch, generic lane override, missing binding, fallback or unapproved egress refuses; route none invokes no provider |
| P04 | Tools equal the reviewed subset; raw shell, extra MCP tool, hostile argv/environment and mail instructions cannot expand it |
| P05 | Eligible one-card admission runs once; stale claim, dependency FAIL, held card, symlink/ambiguous workspace and duplicate key have zero launches |
| P06 | Valid fresh Link feed emits one advisory handoff; missing/stale inner or outer feed, future time, tamper, replay and changed head refuse |
| P07 | Each shared producer principal/host/session/workspace and source-author alias refuses; fully distinct reviewer passes |
| P08 | Concurrent source arrivals at C-R stop; review fills remaining R; stale capacity and two competing dispatcher generations launch none |
| P09 | Recurring overlap is nonblocking no-op; inactive host has zero operational reads; disposable child exits after one card |
| P10 | Presence with no progress stays live; silence never releases; one bounded escalation; timeout stops only matching generation |
| P11 | Proven death allows one fresh-generation retry; PID reuse, live cgroup, unknown liveness or changed claim prevents release |
| P12 | Wrong recipient/revision, expired/duplicate mail and mailbox outage preserve isolation; no global ack or instruction execution |
| P13 | Exit 0 without typed proof has no PASS; missing candidate tree/ref/hash blocks; stale terminalization leaves newer owner untouched |
| P14 | Partial publish or cleanup retry retains evidence, emits one verdict/receipt and removes only owned dead-run resources |
| P15 | Disabled migration and rollback deny admission while preserving old histories, live workers, claims and exact artifact provenance |

Existing boundary suite to reproduce with the new test file:

```sh
PYTHONPATH=src python -m pytest -q tests/test_reviewer_profile_tdd.py tests/test_seat_cycle_entrypoint.py tests/test_seat_cycle_guard.py tests/test_link_cycle.py tests/test_link_observation_feed.py tests/fleet/test_seat_boundaries.py tests/fleet/test_seat_runtime.py tests/test_skfleet_seat_runtime_wiring.py tests/test_skfleet_wrapper_mail_heartbeat.py tests/test_skfleet_wrapper_mail_poll.py tests/test_skfleet_worker_exit_evidence.py tests/test_skfleet_terminal_review.py tests/test_skfleet_worker_stop_semantics.py tests/test_skfleet_workspace_resolution.py tests/test_skfleet_provisional_opener.py tests/test_reaper_stall.py
```

## Metrics, staged rollout and rollback

Append run events and derive metrics without model adjudication. Count admission
refusals by reason/profile/version, duplicate suppression, reserved/used/waiting
capacity, queue age, review latency (admission to typed verdict), heartbeat age,
progress age, missing mail, recovery attempts, stale claim refusals and incomplete
evidence. Keep high-cardinality card/run/principal values in event records.
Unknown values remain unknown, never healthy zeros.

Quality uses human/independent-review-confirmed labels: reproducibility is
successful independent reruns divided by attempted reruns; false PASS counts
PASS candidates later overturned for an in-scope defect; escaped findings count
in-scope defects found after acceptance; repair recurrence counts repeated root
causes across candidate generations. Report denominators, observation window and
unadjudicated cases. Latency and throughput never substitute for correctness.

1. This card publishes TDD/tests and immutable evidence, no runtime changes.
2. a2be68d9 implements against these pins with synthetic routes, disabled
   registration and the complete P01-P15 matrix. Rebase drift requires fresh
   hashes and independent review. b4cf79ea reviews the complete candidate.
3. A separate exact installation card may install disabled profiles only after
   registered principal, tool sandbox, provider bindings, capacity and single
   dispatcher qualification. Capture prior artifact/config hashes and rollback.
4. A separately approved canary uses one disposable synthetic public-data review
   and shadow Link/Mero cycles. Require zero unauthorized effects, duplicate
   launches, false PASS and leaked data, and exact reproducibility of all runs.
   At least ten deterministic cycles and one dead-worker recovery rehearsal
   must pass; any failure stops promotion. No live canary is authorized here.
5. Increase only approved review capacity within C/R with observation windows
   fixed by the deployment card. Preserve distinct hosts and provider purity.

Migration is additive: versioned disabled registry plus public alias mappings;
do not rename historical seats, rewrite claims/mail/evidence or convert in-flight
generic workers. Existing runs finish with their original pinned context.
Rollback disables new profile admission, drains or exactly stops owned processes
under the deployment card, restores the prior pinned disabled registry/runtime,
verifies old hashes and no duplicate workers, and retains all receipts. No
automatic generic fallback or old timer re-enable. Source-only TDD rollback is
a superseding version; there is no data migration in this card.

No protected Matter or HammerTime data, credentials, provider traffic, model
calls, external actions, service installation or deployment are authorized.
SKMail and CardStore publication here are explicitly authorized coordination.
No commit or push is required or authorized by card 90151318. Deliver the actual
TDD and tests as a hashed shared archive so review survives loss of this checkout.
Do not hash the TDD into itself: the immutable evidence manifest pins its bytes
and test/archive hashes, and the CardStore link pins that manifest.
