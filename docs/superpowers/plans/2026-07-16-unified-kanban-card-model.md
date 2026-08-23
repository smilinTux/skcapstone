# Unified Kanban Card Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SKCapstone one kanban view over coord tasks + ITIL tickets, and stop the coord board from growing without bound, without a risky storage rewrite.

**Architecture:** Phases 1-2 are purely additive. A new `card.py` defines a `Card` projection model and a `KanbanBoard` that reads BOTH existing stores (coord `Board.get_task_views` + ITIL `ITILManager.list_*`), maps them into cards grouped by column x swimlane, and renders kanban JSON + HTML. Then archival/aging is added to the coord `Board` so `done` cards age off the active board. No migration, no changed storage, existing CLI/MCP untouched.

**Tech Stack:** Python 3.11, pydantic v2, pytest. Pure-stdlib HTML render (no new deps). Editable install at `~/.skenv/`.

## Global Constraints

- Run tests with `~/.skenv/bin/python -m pytest tests/ -q` from the repo root.
- NO em dashes or en dashes anywhere (code, comments, docstrings, commit messages, HTML copy). Use commas, parentheses, periods, or a plain hyphen. This is a hard project rule.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- Do NOT edit existing coord `Task` files or ITIL `core.json` files; Phases 1-2 only read them (Phase 2 adds a separate archive index, it does not mutate task files).
- Google-style docstrings, type hints everywhere, black formatting, line length 99.
- The coord board is used fleet-wide by every agent + autopilot + MCP. Preserve every existing public signature in `coordination.py` and `itil.py`.
- Conflict-free invariant: any new file a running agent writes must be per-writer (never a file another agent also writes).

---

## File Structure

- Create: `src/skcapstone/card.py` — `Card`, `Kind`, `Column`, `Swimlane` models + `to_card_*` adapters + `KanbanBoard` projection + `render_html`.
- Create: `tests/test_card.py` — model + adapter + projection tests.
- Create: `tests/test_card_kanban_html.py` — HTML render tests.
- Modify: `src/skcapstone/coordination.py` — add `archive_done_tasks(older_than_days)` + an archive index reader; make `load_tasks`/`get_task_views` skip archived ids.
- Create: `tests/test_coord_archival.py` — archival/aging tests.
- Modify: `src/skcapstone/cli.py` (or the coord CLI module) — add `coord kanban [--html PATH]` and `coord archive-done [--days N] [--dry-run]`.
- Create: `tests/test_cli_kanban.py` — CLI smoke tests.

---

## Phase 1: Card model + unified kanban projection

### Task 1: Card model + enums

**Files:**
- Create: `src/skcapstone/card.py`
- Test: `tests/test_card.py`

**Interfaces:**
- Produces: `Kind` (Enum: `TASK, EPIC, INCIDENT, PROBLEM, CHANGE`), `Column` (Enum: `BACKLOG, READY, DOING, REVIEW, DONE`), `Card` (pydantic model). `Card` fields: `id: str`, `kind: Kind`, `title: str`, `description: str = ""`, `status: Column`, `swimlane: str`, `priority: str = "medium"`, `originator: str = ""`, `owner: str | None = None`, `order: int = 0`, `labels: list[str] = []`, `dependencies: list[str] = []`, `links: dict = {}`, `meta: dict = {}`, `archived: bool = False`, `created_at: str = ""`, `updated_at: str = ""`, `source: str = "coord"` (provenance: `coord` | `itil`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card.py
from skcapstone.card import Card, Kind, Column


def test_card_defaults_and_roundtrip():
    c = Card(id="abc123", kind=Kind.TASK, title="Do the thing", status=Column.READY,
             swimlane="feature")
    assert c.priority == "medium"
    assert c.archived is False
    assert c.source == "coord"
    # pydantic round-trip keeps enum values as strings
    dumped = c.model_dump()
    assert dumped["kind"] == "task"
    assert dumped["status"] == "ready"
    assert Card(**dumped).title == "Do the thing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py::test_card_defaults_and_roundtrip -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'skcapstone.card'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/card.py
"""Unified kanban Card projection over coord tasks and ITIL tickets.

Phase 1 is read-only: a Card is a projection, never a stored record. The
sources of truth remain coordination/ (tasks + agent files) and itil/
(event-sourced records). See docs/superpowers/specs/2026-07-16-unified-kanban-card-model.md.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Kind(str, Enum):
    TASK = "task"
    EPIC = "epic"
    INCIDENT = "incident"
    PROBLEM = "problem"
    CHANGE = "change"


class Column(str, Enum):
    BACKLOG = "backlog"
    READY = "ready"
    DOING = "doing"
    REVIEW = "review"
    DONE = "done"


class Card(BaseModel):
    """A single work item projected onto the kanban board."""

    id: str
    kind: Kind
    title: str
    description: str = ""
    status: Column
    swimlane: str
    priority: str = "medium"
    originator: str = ""
    owner: str | None = None
    order: int = 0
    labels: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    links: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""
    source: str = "coord"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py::test_card_defaults_and_roundtrip -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card.py
git commit -m "feat(card): Card projection model + Kind/Column enums

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 2: coord TaskView to Card adapter

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card.py`

**Interfaces:**
- Consumes: `TaskView` from `coordination.py` (fields `task: Task`, `status: TaskStatus`, `claimed_by: str | None`); `TaskStatus` values `open, claimed, in_progress, review, done, blocked`.
- Produces: `card_from_taskview(view) -> Card`. Status map: `open -> BACKLOG`, `claimed -> READY`, `in_progress -> DOING`, `review -> REVIEW`, `done -> DONE`, `blocked -> DOING` (kept on board, flagged via `meta["blocked"]=True`). Swimlane defaults to `"feature"` unless `"bug"` or `"security"` in tags. `kind=EPIC` if `"epic"` in tags else `TASK`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card.py (append)
from skcapstone.card import card_from_taskview
from skcapstone.coordination import Task, TaskStatus, TaskView, TaskPriority


def test_card_from_taskview_maps_status_and_swimlane():
    t = Task(id="t1", title="Fix login", priority=TaskPriority.HIGH,
             created_by="opus", tags=["bug", "auth"])
    view = TaskView(task=t, status=TaskStatus.IN_PROGRESS, claimed_by="lumina")
    c = card_from_taskview(view)
    assert c.status.value == "doing"
    assert c.swimlane == "bug"
    assert c.owner == "lumina"
    assert c.originator == "opus"
    assert c.priority == "high"
    assert c.source == "coord"


def test_card_from_taskview_blocked_stays_on_board():
    t = Task(id="t2", title="Blocked thing", created_by="opus")
    view = TaskView(task=t, status=TaskStatus.BLOCKED)
    c = card_from_taskview(view)
    assert c.status.value == "doing"
    assert c.meta.get("blocked") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py -q -k taskview`
Expected: FAIL with `ImportError: cannot import name 'card_from_taskview'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/card.py (append)
from .coordination import TaskStatus, TaskView

_STATUS_TO_COLUMN = {
    TaskStatus.OPEN: Column.BACKLOG,
    TaskStatus.CLAIMED: Column.READY,
    TaskStatus.IN_PROGRESS: Column.DOING,
    TaskStatus.REVIEW: Column.REVIEW,
    TaskStatus.DONE: Column.DONE,
    TaskStatus.BLOCKED: Column.DOING,
}


def _swimlane_for_tags(tags: list[str]) -> str:
    lowered = {t.lower() for t in tags}
    if "bug" in lowered:
        return "bug"
    if "security" in lowered:
        return "security"
    return "feature"


def card_from_taskview(view: TaskView) -> Card:
    """Project a coord TaskView into a kanban Card."""
    t = view.task
    kind = Kind.EPIC if "epic" in {x.lower() for x in t.tags} else Kind.TASK
    meta = dict(t.meta)
    if view.status == TaskStatus.BLOCKED:
        meta["blocked"] = True
    return Card(
        id=t.id,
        kind=kind,
        title=t.title,
        description=t.description,
        status=_STATUS_TO_COLUMN[view.status],
        swimlane=_swimlane_for_tags(t.tags),
        priority=t.priority.value,
        originator=t.created_by,
        owner=view.claimed_by,
        labels=list(t.tags),
        dependencies=list(t.dependencies),
        meta=meta,
        created_at=t.created_at,
        source="coord",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py -q -k taskview`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card.py
git commit -m "feat(card): coord TaskView to Card adapter

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 3: ITIL record to Card adapters

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card.py`

**Interfaces:**
- Consumes: `Incident` (fields `id, title, severity, status`), `Problem` (`id, title, status`), `Change` (`id, title, status`) from `itil.py`. Incident status values `detected, investigating, mitigated, resolved, closed`; Problem `identified, investigating, known_error, resolved, closed`; Change `proposed, approved, implementing, review, closed, rejected`.
- Produces: `card_from_incident(inc) -> Card` (swimlane `"expedite"`, kind `INCIDENT`, `meta["severity"]=inc.severity.value`), `card_from_problem(p) -> Card` (swimlane `"problem"`), `card_from_change(ch) -> Card` (swimlane `"change"`). Column maps per §3.2 of the spec: incident `detected/investigating -> DOING`, `mitigated -> REVIEW`, `resolved/closed -> DONE`; problem `identified -> READY`, `investigating/known_error -> DOING`, `resolved/closed -> DONE`; change `proposed -> BACKLOG`, `approved -> READY`, `implementing -> DOING`, `review -> REVIEW`, `closed -> DONE`, `rejected -> DONE`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card.py (append)
from skcapstone.card import card_from_incident
from skcapstone.itil import Incident, Severity, IncidentStatus


def test_card_from_incident_is_expedite_lane():
    inc = Incident(id="inc-1", title="skmem-pg down", severity=Severity.SEV2,
                   status=IncidentStatus.INVESTIGATING)
    c = card_from_incident(inc)
    assert c.kind.value == "incident"
    assert c.swimlane == "expedite"
    assert c.status.value == "doing"
    assert c.meta["severity"] == "sev2"
    assert c.source == "itil"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py -q -k incident`
Expected: FAIL with `ImportError: cannot import name 'card_from_incident'`

- [ ] **Step 3: Write minimal implementation**

Read the actual enum member names in `itil.py` first (they may differ from the guesses above; use the real values). Then:

```python
# src/skcapstone/card.py (append)
from .itil import Change, Incident, Problem

# Fill these maps from the REAL enum values in itil.py, do not guess.
_INCIDENT_COLUMN = {
    "detected": Column.DOING, "investigating": Column.DOING,
    "mitigated": Column.REVIEW, "resolved": Column.DONE, "closed": Column.DONE,
}
_PROBLEM_COLUMN = {
    "identified": Column.READY, "investigating": Column.DOING,
    "known_error": Column.DOING, "resolved": Column.DONE, "closed": Column.DONE,
}
_CHANGE_COLUMN = {
    "proposed": Column.BACKLOG, "approved": Column.READY, "implementing": Column.DOING,
    "review": Column.REVIEW, "closed": Column.DONE, "rejected": Column.DONE,
}


def card_from_incident(inc: Incident) -> Card:
    return Card(
        id=inc.id, kind=Kind.INCIDENT, title=inc.title, status=_INCIDENT_COLUMN[inc.status.value],
        swimlane="expedite", priority="high",
        meta={"severity": inc.severity.value, "itil_status": inc.status.value},
        source="itil",
    )


def card_from_problem(p: Problem) -> Card:
    return Card(id=p.id, kind=Kind.PROBLEM, title=p.title, status=_PROBLEM_COLUMN[p.status.value],
                swimlane="problem", meta={"itil_status": p.status.value}, source="itil")


def card_from_change(ch: Change) -> Card:
    return Card(id=ch.id, kind=Kind.CHANGE, title=ch.title, status=_CHANGE_COLUMN[ch.status.value],
                swimlane="change", meta={"itil_status": ch.status.value}, source="itil")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py -q -k incident`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card.py
git commit -m "feat(card): ITIL incident/problem/change to Card adapters

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 4: KanbanBoard projection (columns x swimlanes)

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card.py`

**Interfaces:**
- Consumes: `Board` from `coordination.py` (`get_task_views()`), `ITILManager` from `itil.py` (`list_incidents()`, `list_problems()`, `list_changes()`).
- Produces: `KanbanBoard(home: Path)` with `.cards() -> list[Card]` (all active cards from both sources, archived excluded), and `.grid() -> dict[str, dict[str, list[Card]]]` keyed `[swimlane][column_value] -> [cards ordered by (priority_rank, id)]`. Swimlane display order: `feature, bug, security, expedite, change, problem`. `LANE_ORDER` and `COLUMN_ORDER` module constants exported for the renderer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card.py (append)
from pathlib import Path
from skcapstone.card import KanbanBoard, COLUMN_ORDER, LANE_ORDER
from skcapstone.coordination import Board, Task


def test_kanban_grid_groups_by_lane_and_column(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="k1", title="Backlog item", created_by="opus"))
    kb = KanbanBoard(tmp_path)
    grid = kb.grid()
    assert "feature" in grid
    assert any(c.id == "k1" for c in grid["feature"]["backlog"])
    assert COLUMN_ORDER[0] == "backlog"
    assert LANE_ORDER[0] == "feature"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py -q -k grid`
Expected: FAIL with `ImportError: cannot import name 'KanbanBoard'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/card.py (append)
from pathlib import Path

from .coordination import Board

COLUMN_ORDER = [c.value for c in Column]  # backlog, ready, doing, review, done
LANE_ORDER = ["feature", "bug", "security", "expedite", "change", "problem"]
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class KanbanBoard:
    """Read-only kanban projection over coord + ITIL."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()

    def cards(self) -> list[Card]:
        out: list[Card] = []
        board = Board(self.home)
        for view in board.get_task_views():
            out.append(card_from_taskview(view))
        try:
            from .itil import ITILManager
            mgr = ITILManager(self.home)
            out += [card_from_incident(i) for i in mgr.list_incidents()]
            out += [card_from_problem(p) for p in mgr.list_problems()]
            out += [card_from_change(c) for c in mgr.list_changes()]
        except Exception:  # ITIL store may be absent; projection stays task-only
            pass
        return [c for c in out if not c.archived]

    def grid(self) -> dict[str, dict[str, list[Card]]]:
        grid: dict[str, dict[str, list[Card]]] = {
            lane: {col: [] for col in COLUMN_ORDER} for lane in LANE_ORDER
        }
        for c in self.cards():
            lane = c.swimlane if c.swimlane in grid else "feature"
            grid[lane][c.status.value].append(c)
        for lane in grid.values():
            for col in lane.values():
                col.sort(key=lambda c: (_PRIORITY_RANK.get(c.priority, 2), c.id))
        return grid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card.py -q -k grid`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card.py
git commit -m "feat(card): KanbanBoard projection grouping by lane x column

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 5: Kanban HTML render

**Files:**
- Modify: `src/skcapstone/card.py`
- Test: `tests/test_card_kanban_html.py`

**Interfaces:**
- Consumes: `KanbanBoard.grid()`, `COLUMN_ORDER`, `LANE_ORDER`.
- Produces: `render_html(kb: KanbanBoard, title: str = "SKBoard") -> str` returning a self-contained HTML string (inline CSS, both themes, columns as grid + swimlane rows + cards with priority stripe, kind badge, owner, id). Every user-facing string HTML-escaped via `html.escape`. WIP count per column in the header. No em/en dashes in the output.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_card_kanban_html.py
from skcapstone.card import KanbanBoard, render_html
from skcapstone.coordination import Board, Task


def test_render_html_contains_card_and_no_dashes(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="h1", title="Render me", created_by="opus", tags=["bug"]))
    html_out = render_html(KanbanBoard(tmp_path), title="SKBoard")
    assert "<!doctype html" in html_out.lower() or "<style" in html_out.lower()
    assert "Render me" in html_out
    assert "backlog" in html_out.lower()
    # hard project rule: no em or en dashes in generated output
    assert "—" not in html_out
    assert "–" not in html_out


def test_render_html_escapes_titles(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="h2", title="<script>x</script>", created_by="opus"))
    html_out = render_html(KanbanBoard(tmp_path))
    assert "<script>x</script>" not in html_out
    assert "&lt;script&gt;" in html_out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_card_kanban_html.py -q`
Expected: FAIL with `ImportError: cannot import name 'render_html'`

- [ ] **Step 3: Write minimal implementation**

Implement `render_html` building the same board structure as the approved mockup
(`docs/superpowers/specs/` sibling artifact): a `.board` CSS grid with a lane-label
column plus five data columns, one `.lane-row` per lane in `LANE_ORDER`, one `.cell`
per column in `COLUMN_ORDER`, and one `.card` per card with a priority-stripe class
`p-<priority>`, a kind badge, escaped title, `#<id>`, and owner. Use `html.escape` on
every dynamic string. Keep the palette token-driven for both themes. Do not emit any
`—`/`–`; use plain hyphens or restructure. Return the full string starting
with `<!doctype html>`.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_card_kanban_html.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/card.py tests/test_card_kanban_html.py
git commit -m "feat(card): self-contained kanban HTML render (both themes, escaped)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 6: `coord kanban` CLI

**Files:**
- Modify: the coord CLI (find with `grep -n 'def kanban\|coord.*group\|@coord' src/skcapstone/cli.py` and the coord command module).
- Test: `tests/test_cli_kanban.py`

**Interfaces:**
- Consumes: `KanbanBoard`, `render_html`.
- Produces: CLI `skcapstone coord kanban` printing a text grid summary (lane x column counts) to stdout, and `--html PATH` writing the HTML board to PATH. `--json` dumps `KanbanBoard.grid()` as JSON.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_kanban.py
from click.testing import CliRunner
from skcapstone.coordination import Board, Task


def test_coord_kanban_html_written(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="c1", title="CLI card", created_by="opus"))
    from skcapstone.cli import cli
    out = tmp_path / "board.html"
    result = CliRunner().invoke(cli, ["coord", "kanban", "--html", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "CLI card" in out.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_cli_kanban.py -q`
Expected: FAIL (no `kanban` subcommand; nonzero exit)

- [ ] **Step 3: Write minimal implementation**

Add a `kanban` command under the existing `coord` group, resolving `home` the same
way the other coord commands do (respect `SKCAPSTONE_HOME`). Text mode prints
per-lane per-column counts; `--html` writes `render_html(...)`; `--json` writes
`json.dumps({lane: {col: [c.model_dump() for c in cards] ...}})`.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_cli_kanban.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/cli.py tests/test_cli_kanban.py
git commit -m "feat(cli): coord kanban command (text/--html/--json)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 2: Archival + aging (drain the 1219 pile-up)

### Task 7: Archive index reader + writer on Board

**Files:**
- Modify: `src/skcapstone/coordination.py`
- Test: `tests/test_coord_archival.py`

**Interfaces:**
- Produces on `Board`: `archived_ids() -> set[str]` (reads per-writer archive index files `coordination/archive/<host>.jsonl`, each line `{"id": ..., "archived_at": ..., "archived_by": ...}`), and `archive_task(task_id, by) -> None` (appends one line to this host's archive index; per-writer, conflict-free; never mutates the task file). `load_tasks()` and `get_task_views()` gain a `include_archived: bool = False` param and skip archived ids by default.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coord_archival.py
from skcapstone.coordination import Board, Task


def test_archive_task_hides_from_default_views(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="a1", title="Old done thing", created_by="opus"))
    board.archive_task("a1", by="opus")
    assert "a1" in board.archived_ids()
    assert all(v.task.id != "a1" for v in board.get_task_views())
    assert any(v.task.id == "a1" for v in board.get_task_views(include_archived=True))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_coord_archival.py -q -k hides`
Expected: FAIL with `AttributeError: 'Board' object has no attribute 'archive_task'`

- [ ] **Step 3: Write minimal implementation**

Add `self.archive_dir = self.coord_dir / "archive"` in `__init__` and create it in
`ensure_dirs`. Implement `archived_ids` (read every `archive/*.jsonl`, union the ids),
`archive_task` (append one JSON line to `archive/<socket.gethostname()>.jsonl`), and
thread `include_archived` through `load_tasks`/`get_task_views` (filter out
`archived_ids()` unless requested). Do not touch task files.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_coord_archival.py -q -k hides`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/coordination.py tests/test_coord_archival.py
git commit -m "feat(coord): per-writer archive index + include_archived filter

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 8: Age-off sweep for done tasks

**Files:**
- Modify: `src/skcapstone/coordination.py`
- Test: `tests/test_coord_archival.py`

**Interfaces:**
- Consumes: `archive_task`, `get_task_views`, `TaskStatus.DONE`.
- Produces on `Board`: `archive_done_tasks(older_than_days: int = 14, now: datetime | None = None, dry_run: bool = False) -> list[str]`. Returns the ids it archived (or would, if dry_run). "Age" is measured from the most recent completion timestamp available; if none, fall back to `task.created_at`. Only `DONE` tasks are eligible.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coord_archival.py (append)
from datetime import datetime, timedelta, timezone
from skcapstone.coordination import AgentFile


def test_archive_done_tasks_ages_off_old_done(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    board.create_task(Task(id="d1", title="Old done", created_by="opus", created_at=old))
    # mark done via an agent file listing it complete
    board.save_agent(AgentFile(agent="opus", completed_tasks=["d1"]))
    archived = board.archive_done_tasks(older_than_days=14)
    assert "d1" in archived
    assert "d1" in board.archived_ids()


def test_archive_done_tasks_keeps_recent(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="d2", title="Fresh done", created_by="opus"))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["d2"]))
    assert board.archive_done_tasks(older_than_days=14) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_coord_archival.py -q -k archive_done`
Expected: FAIL with `AttributeError: ... 'archive_done_tasks'`

- [ ] **Step 3: Write minimal implementation**

Iterate `get_task_views()`, select `status == DONE`, parse the completion/creation
timestamp, compare against `now - older_than_days`, and `archive_task` each eligible id
(unless `dry_run`). Return the list.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_coord_archival.py -q -k archive_done`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/coordination.py tests/test_coord_archival.py
git commit -m "feat(coord): archive_done_tasks age-off sweep (default 14d)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 9: `coord archive-done` CLI + one-shot drain of the backlog

**Files:**
- Modify: the coord CLI.
- Test: `tests/test_cli_kanban.py` (append).

**Interfaces:**
- Consumes: `Board.archive_done_tasks`.
- Produces: `skcapstone coord archive-done [--days 14] [--dry-run]` printing the count archived (or would-archive). Safe to run repeatedly (idempotent: already-archived ids are skipped by `get_task_views` default filter).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_kanban.py (append)
def test_coord_archive_done_dry_run(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    from skcapstone.coordination import Board, Task, AgentFile
    board = Board(tmp_path); board.ensure_dirs()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    board.create_task(Task(id="cd1", title="old", created_by="opus", created_at=old))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["cd1"]))
    from click.testing import CliRunner
    from skcapstone.cli import cli
    result = CliRunner().invoke(cli, ["coord", "archive-done", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "1" in result.output
    assert "cd1" not in board.archived_ids()  # dry-run does not write
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_cli_kanban.py -q -k archive_done_dry`
Expected: FAIL (no `archive-done` subcommand)

- [ ] **Step 3: Write minimal implementation**

Add the `archive-done` command under `coord`; call `archive_done_tasks(days, dry_run=...)`
and print the count.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_cli_kanban.py -q -k archive_done_dry`
Expected: PASS

- [ ] **Step 5: Run the full suite + a real dry-run on the live board**

```bash
~/.skenv/bin/python -m pytest tests/ -q
~/.skenv/bin/skcapstone coord archive-done --dry-run   # expect ~1219 candidates
```

- [ ] **Step 6: Commit**

```bash
git add src/skcapstone/cli.py tests/test_cli_kanban.py
git commit -m "feat(cli): coord archive-done command (dry-run + drain backlog)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** Phase 1 (kanban view: Tasks 1-6) + Phase 2 (archival/aging: Tasks 7-9) map to spec §3.5 (projections), §3.6 (aging), §5 (phasing). Phase 3-4 (event-sourced store cutover + WIP/expedite enforcement) are explicitly deferred to a follow-up plan and are NOT in this plan's scope.
- **Placeholder scan:** Tasks 3, 5, 6, 8, 9 describe implementation prose instead of full code where the code is either mechanical (CLI wiring) or must be reconciled against the real `itil.py` enum values / the approved mockup HTML. The implementer must read the real enums (Task 3) and the mockup (Task 5) rather than trust guessed strings. This is deliberate, not a gap.
- **Type consistency:** `Card`, `Kind`, `Column`, `KanbanBoard`, `render_html`, `card_from_taskview/incident/problem/change`, `COLUMN_ORDER`, `LANE_ORDER`, `Board.archive_task/archived_ids/archive_done_tasks` names are used identically across tasks.
- **Constraint check:** every generated string is escaped and dash-free (Task 5 asserts it); every new writer file is per-host/per-writer (Task 7); no existing public signature changes (new params default to preserving old behavior).
