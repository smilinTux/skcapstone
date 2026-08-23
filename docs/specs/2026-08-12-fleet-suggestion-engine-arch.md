# Fleet Suggestion Engine: "what next?" + "push the button" for every SKWorld surface

**Date:** 2026-08-12
**Status:** DESIGN (no implementation in this doc)
**Author:** Lumina (for Chef)
**Prime directive:** reuse-not-rebuild. Everything below is grounded in code that already ships.

## 0. The ask

Every SKWorld surface (kanban cards, ITIL tickets, GTD next-actions, chat threads,
model routing, security findings) should be able to ask "what are the sensible
next options here?", render them as buttons, and dispatch the AI to execute the
chosen option on one push. Today this exists for exactly ONE surface: coord/kanban
cards, via the skdashboard endpoints
`GET /api/card/{id}/ai-suggestions` and `POST /api/card/{id}/queue-ai`
(`skdashboard/src/skdashboard/dashboard.py:719-720`). This doc generalizes that
into two ports with per-surface adapters.

## 1. What already exists (the reference implementation, cited)

### 1.1 The suggester

`skcapstone/src/skcapstone/agent_run.py:205` `suggest_next_steps(home, card_id, use_llm, timeout)`:

- Returns `{"suggestions": [{"text", "mode"}...], "source": "llm"|"heuristic"}`
  (agent_run.py:213).
- LLM path: builds a prompt from the folded card (kind/title/description/status/
  labels/last 3 comments) and calls `skgateway_client.chat()` asking for a JSON
  array of 3 `{text, mode}` objects (agent_run.py:224-244).
- Always-instant fallback: `_heuristic_suggestions()` (agent_run.py:198) picks a
  canned per-kind table `_HEURISTIC` (agent_run.py:160-195) covering
  task/bug/incident/problem/change.
- Safety clamp already in the suggester: an LLM suggestion of `execute` on a
  `change` is rewritten to `propose` (agent_run.py:247-251).
- Tolerant parse `_parse_suggestions()` (agent_run.py:258) clamps unknown modes
  to `propose`.

### 1.2 The queue (the "button push")

`agent_run.py:104` `request_run(home, card_id, instruction, agent, mode, requester)`:

- Validates mode against `MODES = ("propose", "dry-run", "execute")`
  (agent_run.py:34), lazily materializes ITIL records into the CardStore via
  `ensure_card()` (agent_run.py:60-101, handles `inc-`/`prb-`/`chg-` prefixes),
  then appends an `agent_run_request` event with a fresh `run-<hex>` id
  (agent_run.py:122-132). State starts `QUEUED`.
- The run lives entirely as append-only `agent_run_*` events on the CardStore,
  folded into `card.meta.agent_run` (module docstring, agent_run.py:1-17).
  CardStore itself now lives in skcoord (CR-4.1): `skcapstone.card_store` is a
  `sys.modules` alias of `skcoord.card_store`
  (`skcapstone/src/skcapstone/card_store.py:1-15`).

### 1.3 The executor (it IS live, in canary)

- `run_ai_runner_job()` (agent_run.py:542) is registered as a real scheduler job:
  `~/.skcapstone/config/jobs.yaml:41-50`: `ai-runner`, `every: 60s`, node
  `noroc2027`, callback `skcapstone.agent_run:run_ai_runner_job`, `enabled: true`.
- One tick = `run_once()` (agent_run.py:480) over `list_queued()`
  (agent_run.py:144, scans cards for `meta.agent_run.state == queued`), then
  `process_one()` (agent_run.py:373): claim under a 900s lease
  (`claim_run`, agent_run.py:287), rule gate (`gate(kind, mode)`,
  agent_run.py:341: propose/dry-run always allowed; execute on `change` denied
  until CAB approval; execute elsewhere allowed because it only produces a draft
  PR), then dispatch.
- Real dispatch is behind `SKAI_RUNNER_LIVE=1` (`live_execution_enabled()`,
  agent_run.py:363). It is currently UNSET, so every run today records a plan,
  transitions to `needs-review`, and moves the card to the review column
  (agent_run.py:450-470). The dispatcher, when live, is `claude_dispatcher()`
  (agent_run.py:493): `claude -p <prompt> --agent <agent>` with per-mode
  instructions baked into the prompt (agent_run.py:504-519).
- Important: this runner is NOT the skos autopilot. The autocode engine
  (sandbox, grade, twin gate, merge) lives in `skharness.autocode`
  (`skos/src/skos/autopilot/orchestrator.py:1-12` and `executor.py` are re-export
  shims) and is separately disabled (`live_execution` off). Two dormant execution
  engines exist; see risk R1.

### 1.4 The HTTP surface and the UI affordance

`skdashboard/src/skdashboard/dashboard.py`:

- `GET /api/card/{id}/ai-suggestions` (dashboard.py:719, handler around :540)
  proxies `ar.suggest_next_steps()` with a 1s timeout for heuristics and 35s
  when `?llm=1` (dashboard.py:540-547).
- `POST /api/card/{id}/queue-ai` (dashboard.py:580-608) gates on
  `_ai_capability_ok()` then calls `ar.request_run()` and publishes a
  `card_changed` event on the SSE bus (dashboard.py:607).
- `_ai_capability_ok()` (dashboard.py:562-578): `X-SK-Capability` header
  compared (hmac.compare_digest) against env `SKAI_QUEUE_TOKEN`; when no token
  is configured it is loopback-open for dev. The docstring itself names the
  upgrade path: "a full capauth-signed grant" (dashboard.py:566-567).
- The assistant console reuses the same primitive: a model-emitted
  `ACTION {"tool": "queue-ai", ...}` line is parsed and routed through
  `request_run()` behind the same capability check
  (`skdashboard/src/skdashboard/dashboard_assistant.py:23,107,175-186`).
- UI pattern worth keeping verbatim
  (`skdashboard/src/skdashboard/static/js/ai_compose.js:83-110`): fetch
  heuristics instantly (`llm=0`), render chips, then upgrade in the background
  to LLM-tailored chips (`llm=1`); a chip click fills the instruction and mode;
  the queue button POSTs. Never blank, never slow.
- Live status: SSE `/api/events` (dashboard.py:641-659) streams bus events;
  the run's activity/state fold into `card.meta.agent_run` and render on the
  card panel.

### 1.5 Inference and model choice

- `skcapstone/src/skcapstone/skgateway_client.py:23` `chat()`: stdlib-urllib
  OpenAI-compatible call to `http://localhost:18780/v1` with model
  `sk-default` (env `SKGATEWAY_MODEL`), returns None on any failure so callers
  fall back to heuristics.
- The just-shipped model-ranking epic: gateway roles may target `"@match"`
  (`skgateway/src/proxy/registry.mjs:247-258`), which ranks the discovered
  catalog against the role's `{require, prefer, tier}` block via the pure
  ranker `rankModels()` (`skgateway/src/ranking/rank.mjs:216+`; sovereignty
  tier ladder, basis weights eval 1.0 > ratings 0.8 > card 0.6 > prior 0.3,
  rank.mjs:63). The whole branch is behind the master gate
  `matchRoutingEnabled = config.routing.match_enabled`, DEFAULT OFF
  (`skgateway/src/index.mjs:58-63`).

### 1.6 The authz kernel that should replace the shared token

`capauth/src/capauth/authz.py:1-60`: `decide(subject, capability, resource,
context)` is the deterministic, fail-closed PDP: enrollment mode
(tofu/verified) + granted capability tokens + requested capability/resource;
every decision emits an AUDIT obligation for the PEP to write; FEB/trust never
gates allow. It already runs as a service (loopback :8420, used by skgateway's
enforce path). This is exactly the "upgrade point" the dashboard docstring
promised.

### 1.7 The other surfaces and how they represent an item

| Surface | Item | Where |
|---|---|---|
| Coord/kanban | event-sourced Card, `Kind` = task/epic/incident/problem/change | `skcoord/src/skcoord/card.py:33-37`, CardStore in skcoord |
| ITIL | Incident/Problem/Change records, ALREADY bridged to cards by `ensure_card()` | agent_run.py:60-101, `card_from_incident/problem/change` in skcoord/card.py:275+ |
| GTD | flat-JSON items keyed by `(source, source_ref)` with `capture()`/`upsert()` semantics | `skos/src/skos/gtd_ingest.py:75-90,232,285` |
| Chat | `Thread` model + `get_thread`/`list_threads` | `skchat/src/skchat/models.py:411`, `skchat/src/skchat/encrypted_store.py:461,472` |
| Model dex | gateway catalog + allowlist, already proxied by the dashboard model console | dashboard.py:661-668 |
| Security | findings in sksecurity (`ai_remediation_engine.py`, repo root layout, least standardized) | `sksecurity/` |

Key observation: the ITIL bridge proves the pattern. `ensure_card()` already
materializes a foreign record into the CardStore so AI next-steps "attach
uniformly to tasks and ITIL tickets" (agent_run.py:63-66). Generalizing THAT is
the whole execute-side design.

## 2. The generalized model: two ports

### 2.1 SuggestionPort (read-only, cheap, unauthenticated-ish)

```
suggest(item: ItemRef, opts) -> SuggestionSet

ItemRef      = { surface: "coord"|"itil"|"gtd"|"chat"|"models"|"security",
                 kind: str,          # surface-local: task|incident|next-action|thread|...
                 id: str,
                 context: dict }     # adapter-built: title, body, status, recent activity
Option       = { text: str,          # one concise imperative instruction
                 mode: "propose"|"dry-run"|"execute",
                 rationale?: str,
                 est_cost?: "low"|"medium"|"high",   # tokens/time, heuristic at first
                 risk?: "none"|"reversible"|"real" } # derived from mode + surface gate
SuggestionSet = { suggestions: [Option], source: "llm"|"heuristic" }
```

This is `suggest_next_steps()`'s existing return shape (agent_run.py:213) plus
two optional annotations. `mode` already encodes risk coarsely (propose = none,
dry-run = reversible, execute = real-but-drafted); `est_cost`/`risk` are
additive and heuristic in P2 (a static map per mode/kind), upgraded only if the
LLM proves able to estimate them honestly. Do not block on them.

Adapter contract (per surface):

1. `build_context(id) -> ItemRef` : fetch the native item, produce the prompt
   context (the card adapter's version is agent_run.py:227-238).
2. `heuristics(kind) -> [Option]` : the instant fallback table (the card
   adapter's is `_HEURISTIC`, agent_run.py:160).
3. `clamp(kind, [Option]) -> [Option]` : surface safety rewrite (the card
   adapter's change-never-executes clamp, agent_run.py:247-251).

The core (prompting, JSON parse, mode clamping, llm-with-heuristic-fallback
race) is `suggest_next_steps()` minus the CardStore fetch, extracted once.

### 2.2 ExecutePort (privileged, audited, human-triggered)

```
queue(item: ItemRef, option: Option, instruction: str,
      agent: str, requester: Subject) -> RunHandle
status(handle) -> { state: queued|running|needs-review|done|failed,
                    activity: [...], links: {pr?, draft?, ...} }

RunHandle = { run_id, card_id }   # card_id may be a shadow card
```

This is `request_run()` (agent_run.py:104) + `current_run()` (agent_run.py:136)
unchanged. The one generalization: **every surface's run ledger lives on the
CardStore**, exactly the way ITIL already does. `ensure_card()` grows adapters:

- `gtd-<hash>`: materialize a shadow card from a GTD item, keyed by its
  existing `(source, source_ref)` dedup identity (gtd_ingest.py:80,285), kind
  `task`, label `gtd`.
- `thr-<id>`: shadow card from a chat thread (kind `task`, label `chat`),
  title = thread subject, description = last-N summary.
- `sec-<id>`: shadow card from a security finding (kind `problem` by default,
  so the incident/problem heuristics and gates apply).

Why shadow cards instead of a new run store per surface:

- The whole claim/lease/activity/state machine (agent_run.py:287-334) plus the
  ai-runner job (jobs.yaml:41) plus the dashboard's run panel and SSE bus come
  for free, on day one, for every new surface.
- CardStore is event-sourced, append-only, Syncthing-synced, multi-writer-safe
  (per-writer JSONL event files, see the `ai-runner@noroc2027.jsonl` event files
  under `~/.skcapstone/cards/*/events/`). That is exactly the durability an
  audited AI-action ledger needs, and it is already fleet infrastructure.
- One board shows every AI run fleet-wide regardless of origin surface. The
  operator seat (AI-first ops) gets a single queue to watch.

The shadow card links back: `meta.origin = {surface, id}` so the surface can
render the run inline (a chat thread shows its own run status by resolving
`thr-<id>`), and completion can be reflected back through the surface's own
API (e.g. `gtd_ingest.upsert()` on `done`).

## 3. Where the core lives, and how surfaces call it

### 3.1 Home: skcoord (library), NOT a new service

Argued options:

- **New `sksuggest` service**: rejected. Another daemon, another port, another
  SPOF, another thing to deploy on every node, for logic that is a few hundred
  lines of pure-ish Python with one HTTP dependency (skgateway). Violates
  reuse-not-rebuild and the redundancy mantra for no gain.
- **Stay in skcapstone (`agent_run.py`)**: workable, but the run substrate
  (CardStore) already moved to skcoord in the CR-4.1 extraction, and skdashboard
  imports `skcapstone.agent_run` only through the package boundary
  (dashboard.py:581). Coupling suggestion+execute to the coordination substrate
  is architecturally honest: the ledger IS coordination.
- **skcoord** (recommended): move `agent_run.py` beside `card_store.py` in
  skcoord as `skcoord.agent_run` (split later into `suggest.py` + `runs.py` if
  it grows), and leave `skcapstone.agent_run` as the exact `sys.modules` alias
  shim already proven by `skcapstone/src/skcapstone/card_store.py:1-15`. Zero
  import breakage, zero rebuild; the ports and their first adapter land in the
  package every surface already depends on. Per-surface adapters live in their
  own repos (skos ships the GTD adapter, skchat ships the thread adapter) and
  register against skcoord's small adapter registry, so skcoord never imports
  skos/skchat (dependency direction stays clean).

### 3.2 Call paths (all three, deliberately)

1. **Library** (in-process): skdashboard and any Python surface call
   `skcoord.agent_run.suggest()/queue()` directly, as dashboard.py:544/597 does
   today. Cheapest, no new auth surface.
2. **HTTP** (cross-process / the app): generalize the dashboard routes to
   `GET /api/suggest/{surface}/{id}` and `POST /api/queue/{surface}/{id}`,
   keeping `/api/card/{id}/ai-suggestions` and `/api/card/{id}/queue-ai` as
   aliases for `surface=coord`. skdashboard is already the coordination API of
   record (:7778), already publishes a `/.well-known` module manifest the
   Flutter shell consumes (dashboard.py:629-639,
   `skworld-app/lib/features/shell/external_module_pane.dart`), and already has
   the SSE bus for status. No new port.
3. **MCP tools** (agents): two tools on the skcapstone MCP server,
   `suggest_options(surface, id)` and `queue_option(surface, id, instruction,
   mode, agent)`, thin wrappers over the library, mirroring how
   `dashboard_assistant.py:175` already lets the assistant queue runs. This
   gives Telegram-bridge agents and Claude Code sessions the same buttons.

## 4. Modes, safety, and authz

### 4.1 Mode taxonomy (keep it, extend the gate table)

`propose | dry-run | execute` (agent_run.py:34) generalizes cleanly:

- **propose**: analysis only, no side effects. Always allowed (agent_run.py:347).
- **dry-run**: reversible/scratch work (worktree, draft object, no
  commit/push/send). Always allowed.
- **execute**: produces a REAL but REVIEWABLE artifact. Never auto-merge,
  never auto-send, never self-approve. The card gate's two rules
  (agent_run.py:341-360) become the coord adapter's rows in a per-surface gate
  table:

| Surface/kind | execute means | gate |
|---|---|---|
| coord task/epic/bug | draft PR | allowed (draft only), as today |
| itil incident/problem | remediation PR / KEDB draft | allowed (draft only) |
| itil change | implementation | DENIED until human/CAB vote = approved (agent_run.py:350-357), unchanged |
| gtd next-action | do the action | allowed if action is local; anything OUTBOUND (email, message) is clamped to draft, per the standing draft-by-default rule |
| chat thread | reply/summary/action extraction | execute = DRAFT reply staged in the thread, human sends; never auto-send |
| models | flip allowlist/role config | DENIED for AI-initiated; operator-only via the existing console |
| security finding | remediation | clamp to propose/dry-run initially; execute requires an ITIL change (route through the change gate) |

The gate stays a pure function `gate(surface, kind, mode) -> {allow_execute,
reason}`; adapters contribute rows, the core owns the evaluation and the
default-deny for unknown rows.

### 4.2 Execute authz: capauth capability, not a shared token

Replace `SKAI_QUEUE_TOKEN` (dashboard.py:562-578) with the PDP that already
exists (capauth/src/capauth/authz.py):

- Capability names: `suggest.read` (cheap, may stay open on loopback),
  `agentrun.queue` (attach a QUEUED run), `agentrun.execute` (queue with
  mode=execute), optionally `agentrun.execute.<surface>` for scoping.
- Resource: the item ref, e.g. `coord:card/97d707f7`, `gtd:item/<source_ref>`.
- PEP: each HTTP surface authenticates the caller (skchat's dataplane_auth
  split already models this: authn yields a subject fqid, then
  `decide(subject, capability, resource, context)`), calls the decide service
  on loopback :8420 (the same one skgateway's enforce path uses), and writes
  the returned AUDIT obligation to the security audit log. Fail closed
  (authz.py's hard rule: every uncertainty denies).
- `mode=execute` additionally requires enrollment mode `verified` (authz.py
  fact 1), so a tofu-paired device can propose but not execute.
- Migration: keep the token check as a fallback branch behind
  `SKAI_AUTHZ=token|pdp|both` for one release; default `both` (token OR pdp
  allows) on loopback, `pdp` on any non-loopback bind. The loopback-open dev
  mode survives only when neither a token nor a pdp URL is configured, exactly
  as today.

### 4.3 Human-in-the-loop

The button IS the consent event, and the architecture already encodes it:

- `suggest()` never queues anything. Only `queue()`/`request_run()` creates
  work, and it records the `requester` on the `agent_run_request` event
  (agent_run.py:123-132). With PDP authz the requester becomes a verified
  subject fqid, not a spoofable header.
- One push = one run = one item. No batch "execute all suggestions" affordance
  in P1-P3 (an explicit later decision if wanted, with its own capability).
- Execute output is always a draft (PR, staged reply, plan). The human review
  step is structural (`NEEDS_REVIEW` + move-to-review, agent_run.py:441-442),
  not a prompt nicety.
- The assistant/agent path (`ACTION queue-ai`, dashboard_assistant.py:175) must
  carry the ORIGINAL human's capability, never an ambient service grant, so an
  LLM cannot self-authorize execution (see risk R3).

## 5. Model choice for the suggestion call

- Today: `skgateway_client.chat()` with `sk-default` (auto-router). Correct
  default; keep the "returns None, caller falls back to heuristics" contract
  (skgateway_client.py:56-58), it is what makes the UI never-blank.
- Declare a **`sk-suggest` role** in the gateway registry `roles:` map targeting
  `"@match"` (registry.mjs:248: `roles: { sk-suggest: "@match" }`) with
  requirements approximately:
  `require: {tool_use or json-capable}`, `prefer: [instruction_following,
  latency, cost]`, `tier: [local, free-remote]` (never paid: suggestions are
  high-frequency, low-stakes; the tier ladder guarantees no escalation,
  rank.mjs:27-31).
- Gating: `@match` only activates when `config.routing.match_enabled` flips
  (index.mjs:63). Until then the registry resolves `sk-suggest` like any named
  role/alias, so the suggester should switch NOW from hardcoded `sk-default`
  to `model=os.environ.get("SKGATEWAY_SUGGEST_MODEL", "sk-suggest")` with a
  registry alias `sk-suggest -> sk-default` as the interim mapping. When match
  routing is enabled fleet-wide, suggestion calls get capability-aware ranked
  local models with zero code change in the suggester.
- The EXECUTOR's model is a separate axis and stays as-is: `claude -p --agent`
  (agent_run.py:524) rides the unified model axis already proven
  (`claude --model ornith-big` via the Anthropic frontend), so a future
  per-run `model` field on the queue call is additive, not required.

## 6. Results and status surfacing

- **Run handle**: `{run_id, card_id}` from `request_run()` (agent_run.py:133).
- **Truth**: the fold, `card.meta.agent_run` (state, typed activity
  thought/action/elicitation/response/error, links) via `current_run()`
  (agent_run.py:136).
- **Push**: the dashboard SSE bus already publishes `card_changed` on queue
  (dashboard.py:607) and the runner's events surface on the next fold;
  `/api/events` (dashboard.py:641) streams to any subscriber. Surfaces that are
  not the dashboard subscribe to the same SSE or poll
  `GET /api/suggest-run/{run_id}` (thin wrapper over `current_run`).
- **Where the operator sees it**:
  1. skdashboard card panel (today, unchanged): activity feed + review column.
  2. Native app: the Board pane is the embedded dashboard module
     (external_module_pane.dart + the well-known manifest, dashboard.py:629),
     so P1 requires nothing new; a native run-status widget is a P4 nicety.
  3. Chat: the originating surface renders inline status by resolving its
     shadow card (`meta.origin` backlink, section 2.2); completion of an
     outbound-draft run drops the draft INTO the thread for the human send.
- **Artifacts**: `links` on `set_state` (agent_run.py:315-333) carries the PR
  URL / draft ref; the runner records it, the fold exposes it, every surface
  renders it as "view the draft".

## 7. Phasing

- **P1 (in flight): surface the existing card suggest/queue in the native
  kanban.** The app's Board pane embeds skdashboard, whose composer already
  ships chips + queue (ai_compose.js:83-110). Finish/verify that pane wiring;
  no new backend. This is the reference implementation everything else copies.
- **P2: extract the ports.** Move `agent_run.py` to skcoord behind the proven
  alias shim (card_store.py pattern); split the card-specific parts
  (context build, `_HEURISTIC`, change-clamp) into the first adapter; add the
  generalized `/api/suggest/{surface}/{id}` + `/api/queue/{surface}/{id}`
  routes (old routes alias to `surface=coord`); land the PDP authz behind
  `SKAI_AUTHZ` with token fallback. Ship the two MCP tools. No behavior change
  for cards.
- **P3: second adapter: GTD** (recommended over ITIL, because ITIL is already
  ~done via `ensure_card`, so it proves nothing new; GTD proves the shadow-card
  mechanism and the outbound-draft clamp). `gtd-` shadow cards keyed by
  `(source, source_ref)`; heuristics table for next-action/waiting-for;
  completion reflects back through `gtd_ingest.upsert()`. Formally register the
  ITIL adapter (near-zero work) in the same release.
- **P4: the generic affordance.** One reusable "Suggest" component: web
  (extract ai_compose.js into a parameterized module keyed by
  `{surface, id}`) and Flutter (a `SuggestSheet` widget hitting the same
  endpoints). Then chat threads and security findings are adapter-plus-widget
  work only. Decide `SKAI_RUNNER_LIVE` flip criteria here (see R1).

## 8. Risks and open questions

- **R1: two execution engines.** `claude_dispatcher` (agent_run.py:493, raw
  `claude -p`, no sandbox, no grading) vs the skharness autocode engine
  (sandboxed, graded, twin-gated). Flipping `SKAI_RUNNER_LIVE=1` as-is means
  ungraded agent runs with operator permissions. Decision needed in P2: make
  `process_one`'s `dispatcher` for mode=execute delegate to
  `skharness.autocode` (sandbox + grade + draft PR), keep raw dispatch only for
  propose/dry-run. Do not let the two engines drift into parallel rebuilds of
  each other.
- **R2: shared token until PDP lands.** `SKAI_QUEUE_TOKEN` is a single shared
  secret and loopback-open by default; the assistant path inherits it. The PDP
  migration (4.2) is P2, not optional polish, before any non-loopback exposure
  of the queue endpoints.
- **R3: prompt-injection via item content.** Card/thread/GTD text is untrusted
  input to the suggester; a poisoned item can propose a malicious execute
  instruction, and a poisoned thread can steer the assistant's `ACTION`
  emission. Mitigations already half-exist (mode clamps, human button, draft-
  only execute); additionally: suggestions are NEVER auto-queued, the gate
  table is enforced server-side at queue time (not just suggest time), and the
  assistant's queue capability must be the human's, never ambient.
- **R4: shadow-card lifecycle.** GTD/chat/security shadow cards must not
  flood the board (label + default-hidden swimlane, auto-archive with the
  existing `coord maintain` job, jobs.yaml:62) and must dedupe hard on the
  origin key, mirroring gtd_ingest's `(source, source_ref)` discipline.
- **R5: suggestion cost/latency at fleet scale.** Every panel-open fires an LLM
  call (35s ceiling, dashboard.py:540-542). Add a short-TTL cache keyed by
  (item id, fold version) in the core, mirror the gateway's own match-decision
  cache pattern (router.mjs `_matchDecisionCache`), and keep the heuristic-
  first UX so cache misses are invisible.
- **Open: est_cost/risk honesty.** Do not display model-guessed cost/risk as
  fact; start with the static mode/kind-derived map (basis-honesty discipline,
  capabilities.mjs:21-24, applies to us too).
- **Open: multi-node runners.** The lease (agent_run.py:287) permits more than
  one runner node; jobs.yaml pins to noroc2027 today. Fleet-wide execution
  placement (which node runs which surface's runs) is a P4+ question that
  should ride the fleet-dispatch work in skharness, not a new scheduler.

## 9. Recommendation (one paragraph)

Generalize, do not rebuild: promote `agent_run.py` (suggester + queue + runner)
into skcoord behind the proven alias shim, define the SuggestionPort and
ExecutePort as its extracted interfaces, and make every new surface an adapter
pair (context+heuristics+clamp for suggest; a shadow-card materializer for
execute) on the CardStore ledger that ITIL already uses via `ensure_card`.
Surfaces call it as a library, via generalized skdashboard routes
(`/api/suggest|queue/{surface}/{id}`), and via two MCP tools. Execute authz
moves from `SKAI_QUEUE_TOKEN` to capauth `decide()` capabilities with the
button-push as the recorded consent event, and execute-mode dispatch is routed
through the skharness autocode engine before `SKAI_RUNNER_LIVE` ever flips to 1.
Suggestion inference moves from hardcoded `sk-default` to a `sk-suggest`
gateway role (alias now, `@match` capability-ranked local-tier routing when
`routing.match_enabled` flips), keeping the heuristic fallback contract that
makes the buttons instant.
