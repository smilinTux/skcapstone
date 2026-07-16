# SKDashboard: ITIL Cockpit + Fully-Functional Kanban + AI Next-Steps Runner

**Date:** 2026-07-16
**Author:** Opus (for Chef), synthesizing 4 deep-research passes
**Status:** Design / spec (feeds an implementation plan)
**Scope:** `src/skcapstone/dashboard.py` (+ new `static/`, `card_store.py`, `agent_run.py`, a scheduler job)

---

## 0. What we are building

Turn the read-only status page at `:7778` into an operable control surface with four capabilities:

1. **A fully-functional kanban board**: drag-drop move between columns, assign/reassign, edit card notes/labels/metadata, WIP limits, swimlanes, filters, and a per-card activity log, with live updates so a background runner's and other agents' changes appear without reload.
2. **An ITIL cockpit**: incident / problem / change management views (KPIs, SLA breach-risk, CAB approval queue, change calendar, incident-problem-change lineage, KEDB), in a three-tier information architecture.
3. **AI next-steps execution**: attach an instruction to any card or ITIL ticket for an AI agent to pick up and execute (make changes, add notes, propose an ITIL change), with a safe propose/dry-run/execute gate, a claim-lease runner, and typed activity feedback onto the card.
4. **An Assistant console**: a natural-language prompt surface in the dashboard where the operator asks for ad-hoc reports ("top 5 incidents", "top 2 most-involved tasks", "summarize open changes") and issues cross-ticket instructions ("add a note to inc-a1f and queue lumina to draft the fix"). Read queries and reports stream back over SSE; any mutation flows through the same capauth gate + audit trail as the rest of the dashboard.

**Decisions locked (2026-07-16):** backend = **Starlette + uvicorn** (chosen; the streaming Assistant console reinforces the async choice). Auth = capauth-gated mutations, loopback + tailscale serve.

The backend substrate already exists: the event-sourced `CardStore` (unified coord + ITIL cards), `ITILManager` (incident/problem/change + CAB + KEDB), the `jobs.yaml` scheduler, the autopilot sandboxed executor, and skgateway (`http://localhost:18780/v1`, model `sk-default`) for inference. This project is the interactive layer, the runner, and the assistant.

---

## 1. Architecture decisions (from research)

### 1.1 Backend: Starlette + uvicorn (incremental migration)
The current dashboard is a single-threaded `HTTPServer` (loopback, GET-only, inlined HTML). A long-lived SSE connection would freeze it, so the server model must change regardless. Adopt **Starlette + uvicorn** (2 deps into `~/.skenv`), not FastAPI (Pydantic/DI weight unused for a single-operator tool), not hand-rolled `ThreadingHTTPServer` (reimplements a worse Starlette once SSE + POST + auth land). The existing `_get_board_state`/`_get_agent_status`/etc. functions port unchanged as `JSONResponse(fn(...))` routes.

Migration is 5 independently-shippable steps: (1) Starlette serving the *same* HTML + GET JSON (behavior-identical), (2) split HTML into `/static` modules, (3) POST mutation routes writing to the stores with an `actor`, (4) `/api/events` SSE + client subscription, (5) capauth gate on the AI-queue route.

### 1.2 Real-time: SSE over a hybrid event bus
Push is one-directional (server to client). Use **SSE** (`EventSource`, auto-reconnect, `Last-Event-ID` replay). Feed it from a hybrid bus: an in-process async pub/sub for same-process mutations, plus a ~1s poll of the event-store cursor (`CardEventLog` + ITIL event logs) so mutations by *any* writer (the runner, agents on other nodes via Syncthing, other tabs) are captured. This reuses our event-sourcing instead of adding Redis. Ship dumb full-board polling first as the fallback; layer SSE on top.

### 1.3 Frontend: vendored ES modules, no build
Split the monolith HTML into `static/index.html` (shell) + native ES modules `static/js/{board,itil,editor,sse}.js` + `static/vendor/Sortable.min.js`. Browsers load ES modules natively (no bundler). **SortableJS** (~11.5 KB gzip, single UMD, zero deps, touch built-in) drives drag-drop via a shared `group` across columns; `onEnd` posts a `move` event. Same-origin serving removes the CORS wildcard. JSDoc for editor-checked types without a compile step.

### 1.4 Auth: network boundary + capauth on the privileged action
Keep binding `127.0.0.1`; reach it via `tailscale serve` (HTTPS + trusted identity headers). GET endpoints stay open (low-risk status). Gate mutating POSTs; require a **capauth-signed capability specifically on the AI-runner-queue endpoint** (queuing an AI to make changes is the privileged action). Every mutation is written to the event log with an `actor`, giving a free audit trail. Do not build OAuth/sessions/accounts.

---

## 2. Kanban board (feature set)

The "fully functional" bar, from surveying Linear / Trello / Planka / WeKan / Kanboard:

**v1 (must-have):**
- Drag-drop move between columns + reorder within a column (SortableJS), persisted as a `move` overlay event.
- Card detail panel (right-side) with editable title, description/notes, assignee, labels, priority, and **the card's own event stream rendered as its activity log** (nearly free given event-sourcing).
- Quick-assign / reassign (one click; assignee = any agent name, which is also the AI-run trigger).
- WIP-limit visual per column (count + over-limit color) using the limits we already model.
- Swimlane rows (feature/bug/security/expedite/change/problem) with collapse/expand.
- Client-side filters (owner/label/kind/priority).
- Labels as colored chips on the card face; priority stripe.
- SSE live refresh so runner/agent/other-tab changes appear without reload.

**v2 (nice-to-have):** keyboard shortcuts (Linear-style `C`/`A`/`S`/move), saved views, checklists with a progress bar on the card face, card templates per kind, multi-select bulk actions, optimistic UI.

Every board mutation is a `POST /api/card/<action>` that appends an overlay event (`move`, `assign`, `label`, `link`, `priority`, `note`) and broadcasts over SSE. Reads come from `KanbanBoard.grid()` (already served from the CardStore post-cutover).

---

## 3. ITIL cockpit (three-tier IA)

**Tier 1 - Overview cockpit (single glance):** KPI stat row (open incidents with SEV1/SEV2 in red, MTTA, MTTR, change-success %, changes awaiting CAB) + open-incidents-by-severity bar + **SLA breach-risk countdown list** (incidents past a per-SEV age target, sorted by time-over) + live activity feed + service-health strip. Keep to ~5 KPIs.

**Tier 2 - Per-discipline drill-downs:**
- *Incidents*: filterable table (state/SEV/service) + MTTR/volume trend (inline SVG polyline) + escalation/reopen rates + incidents-by-service.
- *Problems*: by-state list + linked-incident count per problem + KEDB search box.
- *Changes*: **CAB approval queue** (changes in `reviewing` with risk, requested window, live vote tally + threshold, who still must vote) + change calendar / Forward Schedule of Changes with same-service overlap warning + change success/failure trend.

**Tier 3 - Record detail:** full event timeline for one record + a **change-state stepper** (proposed → reviewing → approved → implementing → deployed → verified, with rejected/failed as red branches) + an **incident→problem→change lineage strip** (linear linked boxes from parent/child ids, not a graph).

All computed by server-folding the ITIL event log into JSON + inline SVG/CSS. No charting library. Metrics: MTTA, MTTR, open-by-severity, SLA/breach-risk, change success + failure rate, auto-incident volume. Everything maps to events we already emit.

---

## 4. AI next-steps runner (the heart of the request)

### 4.1 Trigger and the AgentRun object
Trigger = attaching an instruction (a `queue-ai` action from the card detail panel), or assigning a card to an agent name plus an `ai:queued` label. Either writes an append-only **`agent_run_request`** overlay event. The unit of work is a first-class **`AgentRun`** object (a card can have many runs over its life), stored as overlay/agent-run events keyed by `card_id` and folded into `meta.agent_run` (extends the existing `meta.autopilot` shape). Assignment is the routing hint; the AgentRun state is the execution truth; the runner reflects run-state into the column via a `move` event so the board cannot drift.

**AgentRun fields:** `run_id` (ULID, idempotency base), `card_id`, `kind` (task/incident/problem/change, drives the gate), `instruction`, `agent` (lumina/jarvis/opus), `mode` (propose|dry-run|execute), `sandbox` (bool), `allowed_tools`, `max_actions`/`timeout_s` (default 900), `requester`, `approval` (`required, approved_by, approved_at, cab_vote_ref, change_type`), `priority`, `run_after`, `state`, `worker_id`, `claim_token`, `lease_expires_at`, `attempts`/`max_attempts`, `last_error`, `links` (pr/commit/branch/transcript), `activity[]`, timestamps.

**Lifecycle:** `queued -> claimed -> running -> (needs-review | failed) -> done`, with `stale` reclaim. Mapped onto columns: queued=ready, claimed/running=doing, needs-review=review, done=done, failed=doing(blocked), stale=doing.

### 4.2 Runner architecture (honors the conflict-free model)
- **Dispatch** = a new `jobs.yaml` interval job `ai-runner` **pinned to one node** (`nodes: [noroc2027]`, like `autopilot-daily`), polling ~30-60s. Single-node dispatch makes cross-node double-execution impossible by construction (we cannot use cross-node `SELECT FOR UPDATE` since skmem-pg is local-per-node and coord is Syncthing files).
- **Claim** = commit-immediately lease in the node-local skmem-pg working index (or a `flock`): claim the next `queued` (or reclaim `lease_expires_at < now()`), set `claim_token`, heartbeat every ~lease/3 (lease 10-15 min for AI runs), reaper for dead runners. Mirror the existing `Board.release_stale_claims`. The claim is also written as an append-only lease event on the overlay for audit.
- **Execute** = `claude -p "<instruction + folded card context + CLAUDE.md/AGENTS.md>" --agent <name>` inside the **existing autopilot sandbox** (bwrap/Docker, secrets-absent, egress-allowlist) for dry-run/execute.
- **Retry** = full-jitter exponential backoff via `run_after`; dead-letter to `failed` at `max_attempts`; classify permanent (bad-input) errors to fail fast. **Idempotency** on every real side-effect keyed by `run_id + step_id`.

### 4.3 Safety: rule-based gate by card kind (propose before side-effect)
| kind | agent may do autonomously | gate before real side-effect |
|---|---|---|
| **task** (feature/bug) | propose, dry-run, produce a diff/draft PR | `execute` opens a **draft PR** a human reviews; never auto-merge; lands in `review` |
| **incident** | investigate, propose remediation, add notes | executing a real fix still lands in `review` for human close |
| **problem** | analyze, draft workaround/KEDB entry | KEDB write ok; resolution lands in `review` |
| **change** | create/draft the change (`proposed`/`reviewing`), build the implementation in the sandbox | **may only enter `implementing` after a human/CAB vote flips it to `approved`** via `itil_cab_vote`/`itil_change_update`. **No self-approval.** Standard (pre-approved) changes are the one class an agent may execute directly; emergency changes get expedited execution + a mandatory post-implementation review event. |

Approval = a durable suspend/resume: the run enters `needs-review`, emits an `elicitation` event, and resumes only when a human moves the card to an approved/execute column (the board move *is* the approval webhook). Default mode is `propose`. All runs capture requester provenance, the typed activity log (thought/action/elicitation/response/error), the linked transcript/PR/commit, and reversible `{field, old, new}` edit records.

### 4.4 Feedback onto the card
The runner appends typed activity events (never editing the immutable task file): `thought`, `action` ("ran pytest: 157 passed", "opened PR #42"), `elicitation` ("approve deploy?"), `response` (final summary), `error`. Artifacts merge into the card's `links` (pr/commit/branch/transcript/run_id). On success the card moves `doing -> review` (never straight to done for state-changing work); on failure back to `doing` with `last_error` + retry count. The full `claude -p` session is persisted under the agent's sessions dir and linked.

---

## 4b. Assistant console

A prompt surface (a tab plus a slide-out) backed by skgateway. Flow: operator types → `POST /api/assistant` with the prompt → the server builds context (a compact snapshot of the folded board + ITIL state, plus the operator's intent) and calls skgateway (`sk-default`) with a **tool set** bound: read tools (`board_query`, `itil_query`, canned analytics like `top_incidents`, `most_involved_tasks`) and act tools (the same `move/assign/note/queue-ai/itil_*` operations the runner and MCP expose) → the answer **streams back token-by-token over SSE** into the console.

- **Ad-hoc reports** are canned analytics functions the model can call and narrate: `top_incidents(n, by=severity|age|reopen)`, `most_involved_tasks(n)` (rank by event count / claim churn / comment volume), `changes_awaiting_cab()`, `sla_breaches()`, `board_summary()`. Deterministic data, LLM-phrased.
- **Cross-ticket actions**: "add a note to inc-a1f and queue lumina to draft the fix" resolves to `note(inc-a1f, ...)` + `queue_ai(inc-a1f, agent=lumina, mode=propose)`. Any mutating tool call is subject to the same **capauth gate** (the console is a mutation surface); read/report tools are open on the tailnet. Every action the assistant takes is written to the event log with `actor=assistant:<operator>` for audit.
- Reuses the runner's tool layer, so the assistant and the background runner share one code path; the difference is trigger (interactive prompt vs queued instruction).

## 5. Phased implementation

- **Phase 1 - Starlette migration (behavior-identical).** Swap `HTTPServer` for a Starlette app serving the same HTML + existing GET JSON; systemd `ExecStart` to uvicorn. Ships with zero visible change; de-risks the server move.
- **Phase 2 - Interactive kanban.** `/static` ES modules + SortableJS board; POST mutation routes (`move/assign/label/priority/note`); card detail panel with event-stream activity log; SSE live refresh. This delivers "assign or move tasks" with live updates.
- **Phase 3 - ITIL cockpit.** Tier-1 KPIs + breach-risk, Tier-2 incident/problem/change views + CAB queue, Tier-3 record detail with stepper + lineage strip + KEDB.
- **Phase 4 - AI runner + tool layer.** `agent_run.py` (AgentRun model + overlay events + fold), a shared **tool layer** (read + act tools over card/ITIL stores), the `queue-ai` endpoint (capauth-gated) + card-panel UI, the `ai-runner` scheduler job (claim-lease + sandbox execute + kind-gate + activity feedback), starting `mode=propose`, then dry-run, then execute behind the gates.
- **Phase 5 - Assistant console.** `POST /api/assistant` streaming over SSE, skgateway-backed, binding the Phase-4 tool layer (canned analytics + mutations); a console tab/slide-out; capauth on mutating tool calls; `actor=assistant:<operator>` audit.

Each phase ships working, tested software and is independently valuable. Phases 4 and 5 share the tool layer (build it once in Phase 4, bind it interactively in Phase 5).

---

## 6. Sources
Consolidated from 4 research passes: PagerDuty/ServiceNow/Jira/Freshservice/GLPI/BMC (ITIL dashboards); Linear/Trello/Planka/WeKan/Kanboard + SortableJS/Alpine/Preact-htm (kanban + web tech); Linear Agents/GitHub Copilot agent/Sentry Autofix/Devin/n8n/Trigger.dev/Temporal/Windmill (AI runner + HITL); Starlette/SSE/htmx/Tailscale (dashboard architecture). Full URL list in the research transcripts.
