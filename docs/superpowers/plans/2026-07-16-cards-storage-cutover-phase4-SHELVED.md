# Cards Storage Cutover (Phase 4) Implementation Plan — SHELVED

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans if this is ever green-lit. Steps use checkbox (`- [ ]`) syntax.

**Status:** SHELVED. Do NOT execute without Chef's explicit go. See the recommendation below.

**Goal:** Replace coord's Task-files + derived-status storage with one event-sourced `cards/<id>/{core.json, events/*.jsonl}` store shared by coord and ITIL, so every work item has a single source of truth.

---

## Recommendation: defer this (why it is shelved)

After Phases 1-3, the board is already unified (one kanban view over coord + ITIL),
bounded (archival + a daily maintenance job), and operable (move/label/link + WIP,
via an event overlay), and agents can drive it over MCP. Those phases delivered the
value Chef asked for.

Phase 4 replaces a **working, conflict-free, fleet-wide** write path (every agent,
the autopilot, the dashboard, `coord_*` MCP tools, joule minting, changelog, briefing)
for a **modest internal gain** (status becomes a folded event instead of a derived
scan). The blast radius is the entire coordination substrate on both .158 and .41
simultaneously. The overlay already gives event-sourcing where it changes the user
experience (kanban ops). So the honest engineering call is: **do not big-bang this.**

If it is ever wanted, do it as the strictly-incremental, reversible sequence below,
never as a single cutover. The key safety property is a long **parallel-run parity
window** where the new store is built and continuously diffed against the live board
before anything reads or writes it for real.

---

## Global Constraints (if executed)

- NO em or en dashes anywhere. Commit trailer `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- Reuse ITIL's proven engine (`itil.py`: immutable `core.json` + per-writer `events/<agent>@<host>.jsonl` + fold-on-read + deterministic dedup ids). Do NOT invent a second event engine; generalize that one with a `kind` field.
- Every step is reversible: legacy `tasks/*.json` + `agents/*.json` are retained read-only until the very last step, and the whole feature sits behind a flag (`SKCOORD_CARD_STORE`) defaulting OFF.
- Roll all fleet nodes together (same rule as the July-13 ITIL refactor); restart MCP + dashboard together.

## Phased sequence (each phase independently shippable and OFF by default)

### Phase 4a: Card store engine (additive, unused)
- Generalize the ITIL fold engine into a `CardStore` writing `cards/<id>/{core.json, events/*.jsonl}` with a `kind` discriminator. Fold produces a `Card` (the Phase 1 model).
- Events: `create, move, claim, release, comment, label, link, priority, archive, reopen`.
- Tests: fold determinism, concurrent per-writer merge, dedup ids. NOTHING reads it yet.

### Phase 4b: Importer (additive, idempotent)
- `import_legacy_to_cards()`: read coord Task files + agent-derived status + archive index + card_events overlay + ITIL records, and emit `create` + `move` + `archive` events per item. Idempotent (re-runnable; keyed by id).
- Verify: import the live board into a scratch dir, count parity vs `get_task_views()`.

### Phase 4c: Parallel-read parity (flagged, still OFF)
- `KanbanBoard` gains a `source` switch: legacy projection vs `CardStore` fold.
- A `coord parity` command diffs the two for every card (column, owner, archived) and reports mismatches. Run it for a real soak window (days) across .158 + .41 until zero drift.

### Phase 4d: Write cutover behind a flag (reversible)
- With `SKCOORD_CARD_STORE=1`, `coord create/claim/complete/move` and the `coord_*` MCP tools write `CardStore` events (adapters keep the exact same public signatures). With the flag OFF, behavior is unchanged.
- Dual-write option during bake: write both stores, read from legacy, diff continuously.
- Enable on one node first, watch, then the fleet.

### Phase 4e: Retire legacy (final, only after a clean soak)
- Flip the default flag ON. Move `tasks/*.json` + `agents/*.json` to a read-only `legacy/` archive. `BOARD.md` becomes a pure `render(fold(events))` projection. Delete the derived-status scan path.

## Rollback
At any phase before 4e: set `SKCOORD_CARD_STORE=0` and the legacy path is authoritative again. After 4e: restore `legacy/` and revert the flag default. The event log is append-only, so no history is ever lost.

## Self-Review
- This plan intentionally has no inline TDD code because it is shelved; when green-lit it should be re-expanded into bite-sized TDD tasks per the writing-plans skill, reusing the concrete engine in `itil.py` as the reference implementation.
