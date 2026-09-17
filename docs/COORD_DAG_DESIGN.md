# COORD DAG — Design & Rollout Plan

**Status:** planned (Phase 1 ready to build)
**Author:** Fable design pass, verified against HEAD 2026-08-02 (lumina)
**Scope:** revive the already-stored dependency DAG so coord derives BLOCKED/READY,
schedules parallel work, and lets edges be amended after task birth.

---

## Verdict: the primitives are wired, but asleep

The board already stores a real DAG. Tasks carry a populated `dependencies` edge
list (`coordination.py:112`), authorable at birth via `coord create --dep`, and
~2000+ live task files already have edges. **Nothing reads them.** The readiness
function that would consume them, `unblocked_task_ids()` (`coordination.py:501`),
has **zero callers** anywhere in the tree — verified dead.

The fix is almost entirely read-side derivation, no new subsystem. Roughly
~150 lines, one extracted helper, two auction hooks, one fold clause, and the
already-authored edges become a working parallel scheduler.

### Verified anchors (HEAD, 2026-08-02)

| Claim | Anchor | State |
|---|---|---|
| Edges are stored | `coordination.py:112` `dependencies: list[str]` | ✅ real, populated |
| Readiness fn is dead | `coordination.py:501` `unblocked_task_ids()` | ✅ zero callers |
| Status derivation ignores deps | `coordination.py:547` `get_task_views()` | ✅ derives DONE/IN_PROGRESS/CLAIMED/OPEN only |
| **Two** return branches | `get_task_views()` CardStore read path + legacy path | ✅ overlay must apply after both |
| Topo-sort to extract | `team_engine.py:228` `resolve_deploy_order`, mask `:259` `& agent_keys`, raises `:265` | ✅ Kahn waves, raises on cycle |
| CardStore fold pattern | `card_store.py:204` `append_event()`, action map `:68` | ✅ append-only per-writer, folded on read |
| **Bonus bug** | `card.py:194` `TaskStatus.BLOCKED: Column.DOING` | 🐛 confirmed |

> Note: Fable's original doc placed these under a `coord/` subdir. Actual layout
> is flat under `src/skcapstone/`. Anchors above are corrected.

---

## The load-bearing moves

| # | Change | Why it matters |
|---|---|---|
| 1 | **Overlay `BLOCKED`/`READY` onto `get_task_views()`** via a pure `apply_dependency_status()` fn, applied **once after both** the legacy and CardStore branches | The single most important placement call — do it twice and the two backends diverge. Revives `unblocked_task_ids()` as the one source of readiness truth. |
| 2 | **Extract team_engine's Kahn topo-sort** into `graphutil.topo_waves()`, return-not-raise | Cycles among a few of thousands of tasks must never crash a board read. team_engine keeps its raising wrapper + green tests. |
| 3 | **`dep_add`/`dep_remove` events** in CardStore, mirroring the `add_label`/`remove_label` fold (`card_store.py:68`, `:204`) | "The graph you can never amend is the graph nobody maintains." Edges are *discovered* (autopilot decompose), not only declared at birth. |
| 4 | **`coord ready` / `blocked` / `graph --check`** CLI + auction stops bidding blocked work + `complete_task` fans out newly-unblocked dependents | The "stop waiting in line" mechanic — finishing a node *pushes* its ready dependents to idle agents instead of them polling. |
| 5 | **Worktree-per-node = agent-layer convention, not a coord feature** | Coord is Syncthing-synced multi-host; a worktree path is host-local, meaningless on another node. Bind via existing `coord link`. |

---

## The correctness trap that saves the rollout

**A dependency pointing at an absent task counts as satisfied, not unmet.**

`archive_done_tasks` sweeps DONE tasks after 14 days, so most historical dep
targets are archived and gone. Count those as unmet and you would **mass-block
the entire live board on day one.** Mirror the proven `team_engine.py:259`
`& agent_keys` mask: intersect the dependency set with *known* task ids before
testing satisfaction. Dangling edges get surfaced by `coord graph --check`, never
silently block.

---

## Rollout (zero-risk first)

### Phase 1 — read-only derivation (build first)
- Add `apply_dependency_status()` (pure fn) + wire it once after both branches of `get_task_views()`.
- Revive `unblocked_task_ids()` as the readiness source.
- Apply the dangling-edge mask (`& known_ids`).
- Fix the bonus bug: `card.py:194` `BLOCKED → Column.BACKLOG`.
- Gate behind `SKCOORD_DAG` (default on).
- **Run `coord graph --check` before merge** to measure dangling/cyclic edges in the live corpus.
- Blast radius: some OPEN tasks correctly show BLOCKED. No schema change, files read exactly as before.

### Phase 2 — enforcement
- Auction ready-filter (stop bidding blocked work).
- Claim guard (refuse claim on a blocked task).
- `complete_task` fan-out of newly-unblocked dependents to idle agents.

### Phase 3 — edge mutation
- `coord dep add` / `coord dep remove` (CardStore `dep_add`/`dep_remove` events, mirror the label fold).
- Cycle guard at create/mutate time (reuse `graphutil.topo_waves()`).

### Phase 4 — worktree convention
- Runner-side only. Worktree-per-node bound via `coord link`, never stored as a coord field.

---

## Bonus bug (ship in Phase 1)

`card.py:194` maps `TaskStatus.BLOCKED → Column.DOING`. Inert today (nothing emits
BLOCKED). The moment BLOCKED starts firing in Phase 1, every blocked card renders
as in-progress and eats the `doing` WIP limit. One-line fix: `→ Column.BACKLOG`.
