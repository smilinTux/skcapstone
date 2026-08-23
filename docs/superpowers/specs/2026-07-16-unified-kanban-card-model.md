# Unified Kanban Card Model: Coord + ITIL Deep-Dive Arch Eval and Refactor

**Date:** 2026-07-16
**Author:** Opus (for Chef)
**Status:** Design / spec (feeds an implementation plan)
**Scope:** `skcapstone/src/skcapstone/coordination.py` + `itil.py` (+ `service_health.py`, CLI, MCP)

---

## 0. TL;DR

We run two parallel work-item systems that do not share a model, a board, or a
lifecycle:

- **Coord** (`coordination.py`): the workhorse. 1700 tasks, but **1219 are done
  and never archived**, 459 are stale-open (oldest March 1), only 22 are active.
  Status is *derived* from 108 unbounded agent files. No kanban, no columns, no
  aging, no archival. The active board grows without bound.
- **ITIL** (`itil.py`): the better-engineered system (event-sourced immutable
  core + append-only per-writer events + fold-on-read + auto-close + KEDB/CAB),
  but **completely empty and effectively unused**.

The fix is not to build a third thing. It is to **promote ITIL's proven
event-sourced engine to the one storage substrate for all work items**, add a
`kind` discriminator (`task | epic | incident | problem | change`), and put a
**kanban projection** (columns = lifecycle, swimlanes = kind) on top. Every view
(BOARD.md, kanban JSON, kanban HTML) becomes a pure `render(fold(events))`
projection, which structurally kills the drift.

This design is validated by research into the **Archon** project (coleam00,
`archive/v1-task-management-rag` branch), kanban best practice, and agent-native
boards (Linear Agents, block/agent-task-queue). See §7 sources.

---

## 1. Current state (ground truth, measured 2026-07-16)

### 1.1 Coord (`coordination.py`, 879 lines)

```
tasks/     1701 files   (1219 done, 459 open, 13 claimed, 9 in_progress)
agents/     108 files   (each with an unbounded completed_tasks list)
BOARD.md    generated flat text dump
```

Model:
- `Task` (immutable, write-once): `id, title, description, priority, tags,
  created_by, created_at, acceptance_criteria, dependencies, notes, meta`.
- `TaskStatus` is an enum (`open, claimed, in_progress, review, done, blocked`)
  but is **not stored on the task**. It is *derived at read time* by scanning
  every agent file: a task is `done` if any agent lists it in `completed_tasks`,
  `in_progress`/`claimed` if in `current_task`/`claimed_tasks`, else `open`.
- `AgentFile.completed_tasks` grows forever (conflict-free-by-design, but
  unbounded).
- `get_task_views()` loads all 1700 task files + all 108 agent files on every
  call. `generate_board_md()` re-derives the whole thing.

What is genuinely good here and must be preserved:
- **Conflict-free multi-writer**: each agent writes only its own file, Syncthing
  merges cleanly. This is the same insight ITIL later formalized.

What is broken:
1. **No archival.** 1219 done tasks sit on the active board forever. Nothing ages
   or removes them. `close_task_obsolete` exists but is manual and rare.
2. **Unbounded read cost.** Every board render is O(tasks + agents) file reads.
3. **Status is a scan, not a fact.** Derived from 108 files; no single place to
   read "what column is this card in."
4. **No kanban.** Six flat statuses, no columns, no swimlanes, no card ordering,
   no WIP limits, no card-level links to PRs/commits/docs.
5. **No card type.** A planned feature, an epic, and an operational incident are
   all the same shapeless `Task`.

### 1.2 ITIL (`itil.py`, 1702 lines)

```
incidents/<id>/{core.json, events/<agent>@<host>.jsonl}
problems/<id>/{...}
changes/<id>/{...}
kedb.jsonl
```

Model (the good one):
- **Immutable `core.json`** written write-once (O_EXCL, create-if-absent, so
  concurrent writers converge on one file via a deterministic id).
- **Append-only per-writer event log** `events/<agent>@<host>.jsonl`. No two
  writers touch the same file, so Syncthing never conflicts.
- **Fold-on-read**: `_fold_incident/_fold_problem/_fold_change` replay
  `core + sorted(events)` into current state. State (status, severity, timeline)
  is never stored, always folded.
- Deterministic dedup ids (`_auto_incident_id(service, failure_class, day_bucket)`),
  `auto_close_resolved(stable_hours)`, SLA checks, KEDB, CAB voting.

Current state: **0 incidents, 0 problems, 0 changes, 0 KEDB.** The engine is
sound and unused. `service_health.py:492` already calls
`ITILManager.create_incident` correctly (the earlier "misrouted to coord" concern
is resolved — coord has 0 `inc-` tasks).

### 1.3 The core problem, stated once

> We have the **right storage engine** (ITIL: event-sourced, conflict-free,
> fold-on-read, auto-archiving) and the **wrong one** (coord: derived-status,
> unbounded, no archival) — and the wrong one holds all the real work while the
> right one sits empty. Unify by moving coord's work onto ITIL's engine, generalized
> to a `Card` with a `kind`.

---

## 2. What we steal from Archon (and what we do not)

Archon's v1 task system (`archive/v1-task-management-rag`) is the closest prior
art. From its `archon_tasks` schema:

| Archon design fact | Adopt? | Our form |
|---|---|---|
| Kanban columns = the `status` enum (`todo→doing→review→done`), board is a `GROUP BY status`, never a stored artifact | **Yes** | `status` folded onto the card; board is a projection |
| `task_order` INTEGER = single ordering field within a column | **Yes** | `order: int` on the card |
| `assignee` is free **TEXT**, not an enum, so any agent name can own a card | **Yes** | `owner: str` (any agent name or `User`) |
| `feature` TEXT = lightweight swimlane/epic grouping | **Yes** | `swimlane: str` |
| `parent_task_id` self-FK = epic→subtask hierarchy | **Yes** | `parent_id` |
| `archived` bool + `archived_at` + `archived_by` soft-delete, board filters `archived=false` | **Yes** | folded `archive` event |
| `sources`/`code_examples` JSONB link cards to RAG knowledge | **Partial** | `links` jsonb (PR/commit/doc/KEDB) |
| **Supabase (Postgres + pgvector)** as the store | **No** | Sovereignty: keep the local event-sourced file log; SQLite is only a rebuildable read cache |
| Consolidated MCP tools `find_tasks` + `manage_task`, strict `todo→doing→review→done` | **Yes** | mirror in our MCP: `card_find` + `card_manage` |

The one hard "no" is Archon's cloud Postgres dependency. Our fleet is
Syncthing-synced sovereign file stores; the event log stays the source of truth.

---

## 3. Target architecture

### 3.1 One card, one store, folded from events

```
~/.skcapstone/cards/
  <card_id>/
    core.json                        # immutable creation facts (write-once)
    events/<agent>@<host>.jsonl      # append-only, per-writer (conflict-free)
```

**Card** (folded, never stored whole):

```
id            str         # ULID-ish / uuid hex
kind          enum        task | epic | incident | problem | change
parent_id     str | None  # epic→subtask, incident→problem→change
title         str
description   str
acceptance_criteria list[str]
status        enum        backlog | ready | doing | review | done   (the COLUMNS)
swimlane      str         # kind-derived default: feature | expedite | incident | change | problem | bug
priority      enum        critical | high | medium | low
originator    str         # who created it (accountability, never overwritten)
owner         str | None  # current claimant (any agent name or "User")
lease_expires str | None  # claim lease for dead-PID / timeout reclaim
order         int         # position within its column
labels        list[str]
dependencies  list[str]   # blocked_by
links         dict        # {pr, commit, doc, source, kedb_ref, ...}
meta          dict        # kind-specific: incident.severity, change.cab, problem.kedb_ref
archived      bool        # folded from an "archive" event; board filters this out
archived_at   str | None
archived_by   str | None
created_at    str
updated_at    str         # = timestamp of last folded event
```

### 3.2 Columns vs swimlanes

- **Columns = lifecycle stage** (shared by every kind):
  `backlog → ready → doing → review → done` (+ off-board `archived`).
  - `backlog` unlimited; `ready` = refined + acceptance criteria complete,
    pullable; split `doing`/`review` because "agent finished" != "verified".
  - `done` is a holding column; **archived** is off-board.
- **Swimlanes = kind / urgency** (horizontal rows cutting across columns):
  - `feature`, `bug`, `expedite` (incidents bypass WIP), `change` (may gate on
    CAB before entering `doing`), `problem`.
  - ITIL states map onto the shared columns: incident
    `New→Investigating→Resolved→Closed ≈ ready→doing→review→done`; the
    ITIL-native status string is preserved in `meta` but the board is driven by
    the shared `status`.

### 3.3 Events (the write API)

Each mutation is one appended event `{seq, ts, writer, action, ...}`:

```
create      (writes core.json)
claim       owner=<agent>, lease_expires=<ts>     # atomic CAS: only if unowned/expired
release     owner=None
move        status=<column>, order=<int>          # drag-drop
comment     note=<text>
link        links[<key>]=<value>                  # attach PR/commit/doc/KEDB
label       add/remove label
priority    priority=<enum>
block       dependencies += / -=
archive     archived=True, archived_at, archived_by
reopen      archived=False, status=<column>
```

Fold order is deterministic (`sort by (ts, writer, seq)`), identical to the ITIL
fold, so concurrent writers converge. Idempotency key `(card_id, action, writer,
seq)` — retried tool calls do not double-apply.

### 3.4 Claiming (agent-native)

Claim is an atomic compare-and-set folded from a `claim` event: it "succeeds" for
exactly one agent because the fold resolves the earliest `claim` with an unexpired
lease as the winner (`owner`). Dead-agent reclaim: if `lease_expires` is past (or
the owning PID is dead), the next `claim` event wins. **Originator is never
overwritten** (accountability stays with the human/agent who created it), mirroring
Linear's "human stays primary assignee, agent added as contributor" pattern.

### 3.5 Views are projections, never stores

- `KanbanBoard.render()` folds all non-archived cards → groups by
  `(swimlane, status)` → orders by `order` → emits **kanban JSON**.
- `BOARD.md` = markdown render of that projection.
- **kanban HTML** = the visual board (Archon-style cards) rendered from the same
  JSON, servable at a URL.
- Optional **SQLite read-cache** rebuilt from the event files on startup for fast
  `WHERE archived=false ORDER BY order` queries + full-text — a cache, never the
  truth. "Delete it and rebuild, nothing is lost."

### 3.6 Aging / archival (fixes the 1219 pile-up)

- **Auto-archive** cards in `done` after N days (default 14): append an `archive`
  event; they leave the active board (board filters `archived=False`).
- **WIP age**: track time-in-column; flag cards aging past a threshold as likely
  blocked.
- **Backlog hygiene**: age stale `backlog`/`open` items to `someday`/archived on
  a timer.
- Board query is always bounded regardless of total history.

---

## 4. Migration (coord + ITIL → cards)

No big-bang. Cards is additive; coord and ITIL keep working through adapters.

1. **Importer** reads existing coord `Task` files + agent-file-derived status →
   writes one `cards/<id>/core.json` + a synthetic `events` line per task
   (`create` + a `move` to its derived column + `archive` for the 1219 done).
   ITIL incidents/problems/changes import as `kind=incident/problem/change`.
2. **Coord CLI/MCP become thin adapters** over the card engine
   (`coord create/claim/complete` → card `create/claim/move`).
   `Board.get_task_views()` keeps its signature, backed by the fold.
3. **ITIL CLI/MCP** likewise: `itil_incident_create` → card with `kind=incident`,
   `swimlane=expedite`; `_fold_incident` logic moves into the shared fold keyed by
   `kind`.
4. After parity is proven, the raw `tasks/*.json` + `agents/*.json` become a
   read-only legacy archive.

---

## 5. Recommended phasing (the order I recommend)

Safe on a fleet-critical system: value first, storage rewrite last.

- **Phase 1 — Card model + unified kanban projection (additive, zero migration).**
  `card.py`: the `Card` model + a `KanbanBoard` that reads BOTH existing stores
  (coord `get_task_views` + ITIL `list_*`) and maps them into cards grouped by
  column×swimlane. Emits kanban JSON + HTML. **Chef gets the kanban board
  immediately, zero risk.**
- **Phase 2 — Archival + aging for coord (the real cleanup).**
  Archive dir + auto-archive sweep for `done` older than N days + agent-file
  compaction. Board filters archived. Kills the 1219 pile-up and the unbounded
  read cost.
- **Phase 3 — Event-sourced card store (the true unification).**
  `cards/<id>/{core.json,events/*.jsonl}` write path + fold + kanban events;
  importer; coord/ITIL CLIs re-pointed as adapters.
- **Phase 4 — Cutover + polish.**
  BOARD.md as pure projection; WIP limits; expedite lane for incidents; retire the
  legacy raw stores; SQLite read-cache.

Each phase ships working, tested software on its own.

---

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Fleet-wide breakage (coord is used by every agent + autopilot + MCP) | Phases 1-2 are additive; Phase 3 keeps CLI/MCP signatures via adapters; migration is reversible (legacy files retained read-only) |
| Fold cost on 1700 cards | SQLite read-cache; archived cards excluded from active fold; per-card fold is cheap and cached |
| Syncthing write conflicts | Per-writer append-only event files (the ITIL invariant) — never two writers on one file |
| Losing coord's conflict-free property | It is *strengthened*: same per-writer model, now with a real event log instead of derived scans |

---

## 7. Sources

- Archon: github.com/coleam00/Archon (v1 branch `archive/v1-task-management-rag`);
  DeepWiki task-management page; `migration/complete_setup.sql` schema.
- Kanban: Atlassian WIP limits; Businessmap WIP; Leantime swimlanes; CodeLucky
  board setup.
- Agent-native: Linear for Agents; block/agent-task-queue (PID-lease claiming);
  github/update-project-action (CI→status).
- ITSM unification: Planview/LeanKit ITSM kanban templates; Freshservice;
  Joe The IT Guy (kanban for change).
- Event sourcing: SQLite event-sourcing patterns; append-only log + rebuildable
  read cache (matches our shipped skmem-pg + ITIL July-13 refactor).
