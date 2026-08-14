# Unified Consent Plane

**Status:** proposed
**Date:** 2026-08-13
**Author:** Lumina (with Fable ground-truth + coherence audits, external practice research)
**Supersedes nothing. Extends:** `2026-08-12-fleet-suggestion-engine-arch.md`, `2026-08-13-change-management-cab-ai-arch.md`

---

## 1. What Chef asked for

> "When the alert comes up, give me the option of next steps so I can just say 'do it' and you do it."

Then, on scoping it: make it a **common workflow for the entire system**, not a one-off, and refactor if that is what it takes.

The concrete first consumer is a GMKtec warranty-RMA follow-up alert on 2026-08-19 whose options are ops and comms actions ("send the escalation email", "snooze a week", "mark resolved"), not code changes.

## 2. The finding that reframes the work

This capability already exists. The fleet suggestion engine is shaped for exactly this and its own spec says "the button IS the consent event." Nothing new should be built to hold proposals, options, or execution.

What does **not** exist is a trustworthy answer to *who consented*. That is the actual work.

### 2.1 Verified state of consent today

Every claim below was verified in code or at runtime on 2026-08-13, not inferred.

| # | Consent point | Authenticated subject | Durable record | Verified state |
|---|---|---|---|---|
| 1 | capauth PDP `decide()` :8420 | fqid asserted by the calling PEP | AUDIT obligation emitted, **persisted by no PEP** | Fails closed correctly. **No capability tokens have ever been minted**: `capauth:lumina@skworld.io` returns "no token grants capability 'agentrun.queue'"; `chef@skworld.io` returns "unknown subject: no enrolled device" |
| 2 | skdashboard buttons (queue, validate, schedule, arm) | `X-SK-Actor` header, self-asserted | append-only event JSONL | **Live state is loopback-open.** Neither `SKAI_AUTHZ` nor `SKAI_QUEUE_TOKEN` set on the :7778 process |
| 3 | Dashboard client | n/a | n/a | `api.js`, `assistant.js`, `ai_compose.js` send a hardcoded `X-SK-Actor: "operator"` and **never** send `x-sk-capability`, which the server reads |
| 4 | MCP `queue_item` | none | `agent_run_request` event | **FIXED 2026-08-13.** Was ungated, hardcoded `requester="operator"`, accepted `mode=execute` |
| 5 | ITIL CAB vote | `SKAGENT` env var; voter "human" is a magic string any shell can claim | per-agent vote file + fold | no-self-approval guard holds |
| 6 | `update_change()` | free-text `agent` argument | status event | **FIXED 2026-08-13.** Appended `proposed -> approved` with no vote, bypassing CAB entirely. Reproduced before the fix |
| 7 | Autopilot digest `answer(n)` | whoever has the shell | GTD `meta.decision` | `n` renumbers on every rebuild, so a stale answer resolves to the wrong item. Telegram door exists in the SOP only, **not in code** |
| 8 | Hermes approvals | 13 Telegram user IDs | none attributable | **FIXED 2026-08-13.** `command_allowlist` contained `"*"`, fnmatch-matching every non-compound command, so `rm -rf <path>` was pre-approved before `mode: manual` was consulted. List trimmed to 2 |
| 9 | skchat Telegram bridge | possession of the bot chat | none | `resolve_fqid=lambda ident: "chef@skworld.io"` maps **every** sender to Chef. No sender allowlist |
| 10 | skchat operator session | HS256 JWT bound to an approved device fingerprint, revocable | device registry | **The strongest primitive in the fleet, used by no consent surface** |
| 11 | Freeze card | self-declared flag | plane JSON | signature field present, verification unwired |
| 12 | Outbound draft-by-default | convention only | none | zero enforcing code |

### 2.2 Diagnosis

**A pile, not a system.** Three specific incoherences:

1. **Four different answers to "who is the human"**: a device-bound revocable session (skchat), a self-asserted header (dashboard), an env var (`SKAGENT`, where CAB voter "human" is claimable by any shell), and a Telegram ID list. The strongest is used by none of the consent surfaces.
2. **No system can see another's consent.** The PDP emits an audit obligation that every PEP discards. Hermes approvals leave nothing attributable. There is no place to ask "did a human consent to X, and when."
3. **Consent-for-X-authorizes-Y is a live pattern.** The R3 priv-esc (capability checked at propose tier, model-supplied `execute` passed through) had two unfixed siblings found in this audit, both now closed (rows 4 and 6).

## 3. The model

> **Policy in capauth. Record in skcoord. One operator identity.**

No new store. No new service. One adapter per front door on ports that already exist.

- **capauth remains the sole answer to *may***: enrollment tiers, capability tokens, PDP decisions. Already correct, simply unused because no tokens were ever issued.
- **The existing skcoord append-only event stores become the sole answer to *did***. Every PEP that receives a PDP allow for a human-consent action persists the audit obligation it *already receives* as a `consent.granted` event in the store that owns the object.
- **One operator identity**: lift skchat's `operator_auth.py` into capauth (which already owns device pairing). Dashboard, CLI, and any future messaging door present that session. `X-SK-Actor` is retired.

**Single source of truth for "a human consented to action A at time T":** the `consent.granted` event in the object's own event store, carrying a capauth-verified subject.

### 3.1 Why event-sourced, not a status field

`~/.skcapstone` is a Syncthing-synced folder. GTD flat lists are last-writer-wins; concurrent overwrites clobber, concurrent appends merge. The ITIL engine is already event-sourced and got this right. External practice agrees independently: approvals should be discrete immutable events, never a mutable `approved` boolean, precisely because that composes with eventual consistency.

This rules out an earlier proposal to hang decision state off a GTD `meta.decision` block.

### 3.2 Binding, from external practice

Adopted from Temporal signals, Step Functions task tokens, GitHub Environments, and Terraform Cloud:

- **Bind approval to a content hash of the exact options presented**, not to a proposal ID. If the underlying artifact changes, the approval is void and must be regenerated. This is Terraform's stale-plan behavior.
- **Single-use, burned atomically.** A second use is an explicit error surfaced to the human, never a silent no-op. Silent no-op is actively dangerous on a multi-node sync where two nodes could each believe they were first.
- **Mandatory expiry** with an explicit escalation state. Never a silent drop.
- **Proposer is not approver.** Already enforced for CAB and change deploy; extend to this path.

## 4. Conversational consent

### 4.1 Honest state of practice

No mature production system uses unconstrained free text as its primary approval mechanism. Slack's own approval blueprint, GitHub Environments, Terraform Cloud, CodePipeline, LangGraph, and the OpenAI Agents SDK all use structured actions. The reason is that a button is artifact-bound by construction: you cannot click the button for the wrong pending item, because it only exists on that message.

Chef wants natural language anyway, and that is achievable without weakening the boundary.

### 4.2 The rule

**The model parses intent. The model is never the authorization boundary.**

Minimum bar, all required:

1. **Sender identity** equals Chef's Telegram user ID exactly, checked at the bridge. Not the display name, not a list, not the model's judgment.
2. **Mechanical binding to one proposal**: a reply-to on the message carrying the proposal ID, or an explicit ID in the text. **A bare "do it" with more than one pending decision is refused with a disambiguation prompt, never guessed.**
3. **Single-use and idempotent**, valid only against the generation that offered it (content hash).
4. **A confirmation echo** before anything irreversible fires, giving a cheap abort window.
5. **Scope cap**: conversational consent covers ATTESTED-tier capabilities first. VERIFIED tier (execute, deploy) stays on dashboard and CLI until the operator session is proven.

The Telegram reply door currently exists only in the SOP, not in code. There is nothing to unwind, so it can be built correctly the first time.

### 4.3 Prompt injection into option generation

The options in the GMKtec alert are derived from an email written by a stranger. Anthropic's published guidance covers untrusted *tool output*; it does not address untrusted content shaping **which options a human is offered**, and no settled practice was found for it. Treat the following as engineering judgment:

- **Untrusted text never writes the option list.** The model extracts structured fields; non-model code renders the human-facing options from a **fixed enum of allowed actions**. Injected text can then at most bias which of the permitted actions is proposed, never introduce a new one.
- **Strip imperative and second-person language** from quoted untrusted context so injected instructions cannot masquerade as system framing.
- **Label provenance inline** so a reviewer sees which parts of an option came from attacker-controllable input.
- **Log the raw untrusted input alongside the generated options**, so post-hoc review can detect injection that slipped through.

## 5. Execution

### 5.1 What is already true

Verified: the claim, lease, and state machine in `agent_run.py` are pure CardStore events with **zero repo, worktree, or git assumptions**. All git lives inside the bridge. A queued execute run on a card with no `repo:<name>` label refuses cleanly (well-formed refusal, card to NEEDS_REVIEW), it does not crash.

`set_execute_dispatcher` is a single module-global seam selected by mode only. There is no per-surface dispatch.

### 5.2 What is needed

- **A mux dispatcher** on the existing seam that folds the card, reads `meta.origin.surface`, and routes: repo-labeled to the code bridge, ops and comms surfaces to a new executor.
- **A comms executor** satisfying `fn(context) -> {"summary","activity","links"}`, never raising, structurally draft-only.
- **A send authority** copying the Change Management two-executor split exactly: prepare cannot send (structural, not policy), and a separate armed authority is the only thing that can, with arm check and no-self-approval. This is the one genuinely new build.

### 5.3 Draft-by-default is not negotiable here

Chef's standing rule is "outbound equals draft by default, never auto-send without per-item go." Currently that rule has zero enforcing code. Under this design it becomes structural for the comms path, the same way `_merge` raising makes the code bridge structurally unable to merge.

## 6. Surfaces

An `alert-` surface is a small adapter: one `ensure_card` branch, one heuristics list, one gate row, plus registry entries. Roughly 60 to 80 lines.

Note that `ensure_card()` handles `gtd-` and `inc-/prb-/chg-` only. `surface_registry.py` routes chat to `thr-` and security to `sec-`, prefixes `ensure_card` cannot materialize, so those dead-end at "card not found". **Registered is not the same as working.** Any new surface must land in both places.

## 7. Phasing

Ordered by dependency. Each phase is independently shippable.

- **Phase 0 (done 2026-08-13):** close verified bypasses. Automerge reset to PR-only; MCP `queue_item` gated; Hermes blanket allowlist replaced; Telegram list trimmed; CAB approval bypass closed.
- **Phase 1, identity:** move `operator_auth` into capauth; mint capability tokens; wire clients to send `x-sk-capability`; then and only then flip `SKAI_AUTHZ`. **This is the gate on everything else.**
- **Phase 2, record:** PEPs persist PDP audit obligations as `consent.granted` events.
- **Phase 3, front doors converge:** digest binds by stable ID (the number becomes display-only), stale generations rejected.
- **Phase 4, surfaces and executor:** `alert-` surface, mux dispatcher, comms executor, send authority.
- **Phase 5, conversational door:** Telegram reply consent on the Phase 1 spine, ATTESTED tier only.
- **Phase 6, plane integrity:** sign `_freeze.json` and `_protected.json`, wire the existing verify path.

**The GMKtec alert on 2026-08-19 does not wait for any of this.** It ships on the existing plain alert path. If the platform lands later and is better, the alert migrates onto it. Six days is not enough to build and soak a consent plane, and a working reminder beats an elegant design and a silent Wednesday.

## 8. Risks

| Risk | Mitigation |
|---|---|
| **Lockout.** Flipping everything fail-closed at once bricks Chef's own workflows. Already demonstrated: flipping `SKAI_AUTHZ` today would kill every dashboard button, because no client sends the capability header and no tokens exist | Phase 1 wires clients and mints tokens **before** the flag moves. Keep a break-glass local-shell path and the freeze card outside the new machinery |
| **Sync forks.** Consent events live on Syncthing; conflict copies of `autopilot-digest.json` already exist on disk | Append-only events; verify fold determinism; stamp an owning node and let only that node act |
| **Allowlist regression.** A too-noisy Hermes tempts re-adding `"*"` | A curated allowlist shipped with the removal (done in Phase 0) |
| **Silent reopen.** One missing env var restores loopback-open | Post-migration, unconfigured must mean deny |
| **Injection through options** | Fixed action enum plus non-model rendering (section 4.3) |

## 9. Not doing

- **No new consent service, store, or database.** Parallel stores are forbidden and would recreate the gate table and draft-only invariants from scratch.
- **No LLM anywhere in the consent-binding path.** Intent parsing only.
- **No Telegram inline-keyboard callback server.** Natural-language replies through the existing gateway suffice once bound properly.
- **No quorum or multi-signature CAB** for a one-operator fleet.
- **No folding GTD waiting-for into the spine.** It is tracking, not consent.
- **No attempt to make the freeze card cryptographically AI-proof against an interactive shell.** Shell is root-equivalent here; the honest boundary is the autopilot path plus signed plane files plus audit.
- **No retrofit of every Hermes per-command approval.** Only fleet-level actions need consent events.

## 10. Open questions

1. Does Casey (chi cluster) ever need to consent to actions on nor? Currently he is on the Hermes Telegram allowlist. The two clusters are meant to be separate sovereign installs sharing code via git only.
2. Should `automerge` return at all once the consent plane exists, or is PR-only the permanent posture for a one-operator fleet?
3. Does the autopilot digest survive Phase 3, or does it fold into the suggestion engine as one more surface?
