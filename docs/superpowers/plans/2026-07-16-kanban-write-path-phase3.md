# Kanban Write-Path (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the unified kanban board operable, not just viewable: explicit column moves, card ordering, WIP limits, and label/link edits, without rewriting coord's working conflict-free storage.

**Architecture:** An additive per-writer event overlay. New events live in `coordination/card_events/<host>.jsonl` (same conflict-free invariant as the Phase 2 archive index: each writer appends only to its own host file). The overlay is folded on read and applied on top of the base cards that Phase 1 already projects from coord + ITIL. An explicit `move` event is authoritative for a card's column and order; when absent, the derived-from-claims column (Phase 1) is the fallback. This keeps the existing claim/complete flow untouched.

**Tech Stack:** Python 3.11, pydantic v2, pytest. No new deps. Builds on `card.py` (Phase 1) and `coordination.py` (Phase 2).

## Global Constraints

- Run tests with `~/.skenv/bin/python -m pytest tests/ -q` from the repo root.
- NO em or en dashes anywhere (code, comments, docstrings, HTML, commits). Plain hyphens only.
- Commit trailer: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- Conflict-free invariant: every writer appends only to `card_events/<socket.gethostname()>.jsonl`. Never a shared file.
- Do not change any existing public signature in `coordination.py`, `itil.py`, or `card.py`. New params default to preserving current behavior.
- Overlay is authoritative for column/order only when a `move` event exists for that card; otherwise the Phase 1 derived column stands (backward compatible).

---

## File Structure

- Modify: `src/skcapstone/card.py` — add `CardEvent`, `CardEventLog` (append + fold), apply overlay in `KanbanBoard.cards()`, WIP config + `KanbanBoard.wip_report()`, surface WIP in `render_html`.
- Create: `tests/test_card_events.py` — overlay fold + apply + WIP tests.
- Modify: `src/skcapstone/cli/coord.py` — `coord move <id> <column>` and `coord label`/`coord link` commands.
- Modify: `tests/test_cli_kanban.py` — CLI tests for `coord move`.

---

## Task 1: CardEvent model + CardEventLog append

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card_events.py`

**Interfaces:**
- Produces: `CardEvent` (pydantic: `card_id: str`, `action: str`, `writer: str = ""`, `ts: str = _now_iso()`, `seq: int = 0`, `column: str | None`, `order: int | None`, `priority: str | None`, `swimlane: str | None`, `label: str | None`, `link_key: str | None`, `link_value: str | None`). `CardEventLog(home: Path)` with `append(event: CardEvent) -> None` (writes one JSON line to `coordination/card_events/<host>.jsonl`) and `read_all() -> list[CardEvent]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card_events.py
from skcapstone.card import CardEvent, CardEventLog


def test_card_event_log_append_and_read(tmp_path):
    log = CardEventLog(tmp_path)
    log.append(CardEvent(card_id="x1", action="move", column="review", order=3))
    events = log.read_all()
    assert len(events) == 1
    assert events[0].card_id == "x1"
    assert events[0].column == "review"
    assert events[0].writer  # host stamped on append
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k append`
Expected: FAIL with `ImportError: cannot import name 'CardEvent'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/card.py (append near the top-level helpers)
import json
import socket
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CardEvent(BaseModel):
    """One kanban overlay event (move, order, label, link, priority, swimlane)."""

    card_id: str
    action: str
    writer: str = ""
    ts: str = Field(default_factory=_now_iso)
    seq: int = 0
    column: str | None = None
    order: int | None = None
    priority: str | None = None
    swimlane: str | None = None
    label: str | None = None
    link_key: str | None = None
    link_value: str | None = None


class CardEventLog:
    """Per-writer append-only overlay log for kanban operations."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.dir = self.home / "coordination" / "card_events"

    def append(self, event: CardEvent) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not event.writer:
            event.writer = socket.gethostname()
        path = self.dir / f"{socket.gethostname()}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")

    def read_all(self) -> list[CardEvent]:
        out: list[CardEvent] = []
        if not self.dir.exists():
            return out
        for f in sorted(self.dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(CardEvent.model_validate_json(line))
                    except Exception:  # noqa: BLE001
                        continue
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k append`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card_events.py
git commit -m "feat(card): CardEvent + per-writer CardEventLog overlay store

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

## Task 2: Fold overlay into a per-card patch

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card_events.py`

**Interfaces:**
- Consumes: `CardEventLog.read_all()`.
- Produces: `fold_overlay(events: list[CardEvent]) -> dict[str, dict]`. Returns `overlay[card_id] = {column, order, priority, swimlane, labels: list[str], links: dict}`. Events applied in `(ts, writer, seq)` order: `move` sets column + order (last wins), `set_priority`/`set_swimlane` last wins, `add_label`/`remove_label` accumulate, `link` merges `links[link_key]=link_value`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card_events.py (append)
from skcapstone.card import fold_overlay


def test_fold_overlay_move_last_wins_and_labels_accumulate():
    events = [
        CardEvent(card_id="c", action="move", column="ready", order=1, ts="2026-07-16T01:00:00+00:00"),
        CardEvent(card_id="c", action="add_label", label="urgent", ts="2026-07-16T01:01:00+00:00"),
        CardEvent(card_id="c", action="move", column="review", order=2, ts="2026-07-16T01:02:00+00:00"),
        CardEvent(card_id="c", action="link", link_key="pr", link_value="#42", ts="2026-07-16T01:03:00+00:00"),
    ]
    ov = fold_overlay(events)["c"]
    assert ov["column"] == "review"
    assert ov["order"] == 2
    assert "urgent" in ov["labels"]
    assert ov["links"]["pr"] == "#42"


def test_fold_overlay_remove_label():
    events = [
        CardEvent(card_id="c", action="add_label", label="x", ts="2026-07-16T01:00:00+00:00"),
        CardEvent(card_id="c", action="remove_label", label="x", ts="2026-07-16T01:01:00+00:00"),
    ]
    assert "x" not in fold_overlay(events)["c"]["labels"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k fold`
Expected: FAIL with `ImportError: cannot import name 'fold_overlay'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/card.py (append)
def fold_overlay(events: list[CardEvent]) -> dict[str, dict]:
    """Fold overlay events into a per-card patch dict."""
    ordered = sorted(events, key=lambda e: (e.ts, e.writer, e.seq))
    overlay: dict[str, dict] = {}
    for e in ordered:
        patch = overlay.setdefault(
            e.card_id, {"column": None, "order": None, "priority": None,
                        "swimlane": None, "labels": [], "links": {}}
        )
        if e.action == "move":
            if e.column is not None:
                patch["column"] = e.column
            if e.order is not None:
                patch["order"] = e.order
        elif e.action == "set_priority" and e.priority is not None:
            patch["priority"] = e.priority
        elif e.action == "set_swimlane" and e.swimlane is not None:
            patch["swimlane"] = e.swimlane
        elif e.action == "add_label" and e.label and e.label not in patch["labels"]:
            patch["labels"].append(e.label)
        elif e.action == "remove_label" and e.label in patch["labels"]:
            patch["labels"].remove(e.label)
        elif e.action == "link" and e.link_key is not None:
            patch["links"][e.link_key] = e.link_value
    return overlay
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k fold`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card_events.py
git commit -m "feat(card): fold_overlay - deterministic per-card overlay patch

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

## Task 3: Apply overlay in KanbanBoard.cards()

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card_events.py`

**Interfaces:**
- Consumes: `CardEventLog`, `fold_overlay`, existing `KanbanBoard.cards()`.
- Produces: `KanbanBoard.cards()` applies the overlay to each base card: if `overlay[id]["column"]` is set, override `card.status` (must be a valid `Column` value, else ignore); set `card.order` from overlay; last-wins `priority`/`swimlane`; extend `labels`; merge `links`. Base cards unaffected when no overlay exists.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card_events.py (append)
from pathlib import Path
from skcapstone.card import KanbanBoard, CardEvent, CardEventLog
from skcapstone.coordination import Board, Task


def test_move_event_overrides_derived_column(tmp_path):
    board = Board(tmp_path); board.ensure_dirs()
    board.create_task(Task(id="m1", title="movable", created_by="opus"))  # derived: backlog
    CardEventLog(tmp_path).append(CardEvent(card_id="m1", action="move", column="review", order=5))
    kb = KanbanBoard(tmp_path)
    card = next(c for c in kb.cards() if c.id == "m1")
    assert card.status.value == "review"
    assert card.order == 5
    grid = kb.grid()
    assert any(c.id == "m1" for c in grid["feature"]["review"])
    assert all(c.id != "m1" for c in grid["feature"]["backlog"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k override`
Expected: FAIL (m1 still in backlog)

- [ ] **Step 3: Write minimal implementation**

In `KanbanBoard.cards()`, after building `out`, fold the overlay and apply it:

```python
# inside KanbanBoard.cards(), before the archived filter/return
overlay = fold_overlay(CardEventLog(self.home).read_all())
_valid_cols = {c.value for c in Column}
for c in out:
    patch = overlay.get(c.id)
    if not patch:
        continue
    if patch["column"] in _valid_cols:
        c.status = Column(patch["column"])
    if patch["order"] is not None:
        c.order = patch["order"]
    if patch["priority"]:
        c.priority = patch["priority"]
    if patch["swimlane"]:
        c.swimlane = patch["swimlane"]
    for lb in patch["labels"]:
        if lb not in c.labels:
            c.labels.append(lb)
    c.links.update(patch["links"])
```

Also update `grid()` ordering so explicit `order` sorts first: change the sort key to
`(c.order if c.order else 9999, _PRIORITY_RANK.get(c.priority, 2), c.id)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k override`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card_events.py
git commit -m "feat(card): apply overlay in KanbanBoard - explicit moves + order

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

## Task 4: WIP limits + report

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card_events.py`

**Interfaces:**
- Produces: module constant `WIP_LIMITS = {"ready": 8, "doing": 6, "review": 4}` (backlog/done unlimited). `KanbanBoard.wip_report() -> dict[str, dict]` returning per-column `{"count": n, "limit": l | None, "over": bool}` counting only non-expedite lanes (the expedite/incident lane bypasses WIP by design).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card_events.py (append)
def test_wip_report_flags_over_limit(tmp_path):
    board = Board(tmp_path); board.ensure_dirs()
    log = CardEventLog(tmp_path)
    for i in range(7):
        board.create_task(Task(id=f"w{i}", title=f"t{i}", created_by="o"))
        log.append(CardEvent(card_id=f"w{i}", action="move", column="doing"))
    report = KanbanBoard(tmp_path).wip_report()
    assert report["doing"]["count"] == 7
    assert report["doing"]["limit"] == 6
    assert report["doing"]["over"] is True
    assert report["backlog"]["limit"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k wip`
Expected: FAIL with `AttributeError: ... 'wip_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/card.py
WIP_LIMITS = {"ready": 8, "doing": 6, "review": 4}


# method on KanbanBoard:
    def wip_report(self) -> dict[str, dict]:
        counts = {col: 0 for col in COLUMN_ORDER}
        for c in self.cards():
            if c.swimlane == "expedite":
                continue  # incidents bypass WIP
            counts[c.status.value] += 1
        report = {}
        for col in COLUMN_ORDER:
            limit = WIP_LIMITS.get(col)
            report[col] = {
                "count": counts[col],
                "limit": limit,
                "over": limit is not None and counts[col] > limit,
            }
        return report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card_events.py -q -k wip`
Expected: PASS

- [ ] **Step 5: Surface WIP in render_html + commit**

Update the column header render to show `count / limit` and an `over` class when over.
Then:

```bash
git add src/skcapstone/card.py tests/test_card_events.py
git commit -m "feat(card): WIP limits + wip_report (expedite bypasses)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

## Task 5: `coord move` / `coord label` / `coord link` CLI

**Files:**
- Modify: `src/skcapstone/cli/coord.py`
- Test: `tests/test_cli_kanban.py`

**Interfaces:**
- Consumes: `CardEvent`, `CardEventLog`, `Column`.
- Produces: `skcapstone coord move <task_id> <column> [--order N] [--home]` appending a `move` event (validates column against `Column`); `coord label <task_id> <label> [--remove]`; `coord link <task_id> <key> <value>`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_kanban.py (append)
def test_coord_move_appends_event(tmp_path):
    from skcapstone.coordination import Board, Task
    from skcapstone.card import KanbanBoard
    board = Board(tmp_path); board.ensure_dirs()
    board.create_task(Task(id="mv1", title="move me", created_by="opus"))
    result = CliRunner().invoke(
        main, ["coord", "move", "mv1", "review", "--home", str(tmp_path), "--order", "2"]
    )
    assert result.exit_code == 0, result.output
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "mv1")
    assert card.status.value == "review"


def test_coord_move_rejects_bad_column(tmp_path):
    from skcapstone.coordination import Board, Task
    board = Board(tmp_path); board.ensure_dirs()
    board.create_task(Task(id="mv2", title="x", created_by="o"))
    result = CliRunner().invoke(main, ["coord", "move", "mv2", "nonsense", "--home", str(tmp_path)])
    assert result.exit_code != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_cli_kanban.py -q -k move`
Expected: FAIL (no `move` subcommand)

- [ ] **Step 3: Write minimal implementation**

Add `move`, `label`, `link` commands under the `coord` group. `move` validates the column
with `click.Choice([c.value for c in Column])`, then appends a `CardEvent`. Print a
confirmation.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_cli_kanban.py -q -k move`
Expected: PASS (2 tests)

- [ ] **Step 5: Full suite + commit**

```bash
~/.skenv/bin/python -m pytest tests/ -q -k "card or coord or kanban or itil"
git add src/skcapstone/cli/coord.py tests/test_cli_kanban.py
git commit -m "feat(cli): coord move/label/link - operate the kanban board

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Backlog aging for stale-open tasks

**Files:**
- Modify: `src/skcapstone/coordination.py`
- Modify: `src/skcapstone/cli/coord.py`
- Test: `tests/test_coord_archival.py`, `tests/test_cli_kanban.py`

**Interfaces:**
- Produces on `Board`: `age_stale_open(older_than_days: int = 90, now=None, dry_run=False) -> list[str]`. Archives OPEN tasks whose `created_at` is older than the threshold (unclaimed, unworked backlog rot), using the same archive index. Conservative default of 90 days. CLI: `coord age-backlog [--days 90] [--dry-run]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coord_archival.py (append)
def test_age_stale_open_archives_ancient_open(tmp_path):
    from datetime import datetime, timedelta, timezone
    board = Board(tmp_path); board.ensure_dirs()
    ancient = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    board.create_task(Task(id="s1", title="ancient open", created_by="o", created_at=ancient))
    board.create_task(Task(id="s2", title="fresh open", created_by="o"))
    aged = board.age_stale_open(older_than_days=90)
    assert aged == ["s1"]
    assert "s1" in board.archived_ids()
    assert "s2" not in board.archived_ids()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_coord_archival.py -q -k stale_open`
Expected: FAIL with `AttributeError: ... 'age_stale_open'`

- [ ] **Step 3: Write minimal implementation**

Mirror `archive_done_tasks` but select `status == TaskStatus.OPEN` and compare
`created_at` against the cutoff. Add the `coord age-backlog` CLI command.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_coord_archival.py -q -k stale_open`
Expected: PASS

- [ ] **Step 5: Full suite + commit**

```bash
~/.skenv/bin/python -m pytest tests/ -q -k "card or coord or kanban"
git add src/skcapstone/coordination.py src/skcapstone/cli/coord.py tests/
git commit -m "feat(coord): age-backlog - archive ancient unclaimed open tasks

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** Delivers the operable-kanban part of spec §3.3 (events), §3.2 (columns/swimlanes with explicit moves), and the WIP-limit / expedite-bypass behavior. The full event-sourced storage cutover (spec §4 migration, replacing task files with core.json+events) remains Phase 4, explicitly out of scope here.
- **Placeholder scan:** Tasks 3 (render WIP), 5, 6 give implementation prose plus the key code; the prose points at mechanical CLI wiring and a mirror of an existing method, not hidden logic.
- **Type consistency:** `CardEvent`, `CardEventLog`, `fold_overlay`, `WIP_LIMITS`, `KanbanBoard.wip_report`, `Board.age_stale_open` used identically across tasks.
- **Constraint check:** every new writer file is `card_events/<host>.jsonl` (per-writer); overlay only overrides column when a valid `move` exists; no existing signature changed.
