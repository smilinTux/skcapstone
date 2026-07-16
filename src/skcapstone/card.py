"""Unified kanban Card projection over coord tasks and ITIL tickets.

Phase 1 is read-only: a ``Card`` is a projection, never a stored record. The
sources of truth remain ``coordination/`` (tasks + agent files) and ``itil/``
(event-sourced records). Columns are the shared lifecycle; swimlanes are the
card ``kind``. See docs/superpowers/specs/2026-07-16-unified-kanban-card-model.md.
"""
from __future__ import annotations

import html
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from .coordination import Board, TaskStatus, TaskView


class Kind(str, Enum):
    """The type of work a card represents (drives its swimlane)."""

    TASK = "task"
    EPIC = "epic"
    INCIDENT = "incident"
    PROBLEM = "problem"
    CHANGE = "change"


class Column(str, Enum):
    """The kanban lifecycle stage (shared by every kind)."""

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


# ---------------------------------------------------------------------------
# coord TaskView -> Card
# ---------------------------------------------------------------------------

_STATUS_TO_COLUMN = {
    TaskStatus.OPEN: Column.BACKLOG,
    TaskStatus.CLAIMED: Column.READY,
    TaskStatus.IN_PROGRESS: Column.DOING,
    TaskStatus.REVIEW: Column.REVIEW,
    TaskStatus.DONE: Column.DONE,
    TaskStatus.BLOCKED: Column.DOING,
}


def _swimlane_for_tags(tags: list[str]) -> str:
    """Pick a swimlane for a coord task from its tags."""
    lowered = {t.lower() for t in tags}
    if "bug" in lowered:
        return "bug"
    if "security" in lowered:
        return "security"
    return "feature"


def card_from_taskview(view: TaskView) -> Card:
    """Project a coord ``TaskView`` into a kanban ``Card``."""
    t = view.task
    tags_lower = {x.lower() for x in t.tags}
    kind = Kind.EPIC if "epic" in tags_lower else Kind.TASK
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


# ---------------------------------------------------------------------------
# ITIL records -> Card
# ---------------------------------------------------------------------------

# Column maps keyed by the REAL itil.py enum ``.value`` strings.
_INCIDENT_COLUMN = {
    "detected": Column.DOING,
    "acknowledged": Column.DOING,
    "investigating": Column.DOING,
    "escalated": Column.DOING,
    "resolved": Column.REVIEW,
    "closed": Column.DONE,
}
_PROBLEM_COLUMN = {
    "identified": Column.READY,
    "analyzing": Column.DOING,
    "known_error": Column.REVIEW,
    "resolved": Column.DONE,
}
_CHANGE_COLUMN = {
    "proposed": Column.BACKLOG,
    "reviewing": Column.READY,
    "approved": Column.READY,
    "implementing": Column.DOING,
    "deployed": Column.REVIEW,
    "verified": Column.DONE,
    "failed": Column.DOING,
    "rejected": Column.DONE,
    "closed": Column.DONE,
}


def card_from_incident(inc) -> Card:
    """Project an ITIL ``Incident`` into a kanban ``Card`` (expedite lane)."""
    return Card(
        id=inc.id,
        kind=Kind.INCIDENT,
        title=inc.title,
        status=_INCIDENT_COLUMN.get(inc.status.value, Column.DOING),
        swimlane="expedite",
        priority="high",
        meta={"severity": inc.severity.value, "itil_status": inc.status.value},
        source="itil",
    )


def card_from_problem(p) -> Card:
    """Project an ITIL ``Problem`` into a kanban ``Card`` (problem lane)."""
    return Card(
        id=p.id,
        kind=Kind.PROBLEM,
        title=p.title,
        status=_PROBLEM_COLUMN.get(p.status.value, Column.DOING),
        swimlane="problem",
        meta={"itil_status": p.status.value},
        source="itil",
    )


def card_from_change(ch) -> Card:
    """Project an ITIL ``Change`` into a kanban ``Card`` (change lane)."""
    return Card(
        id=ch.id,
        kind=Kind.CHANGE,
        title=ch.title,
        status=_CHANGE_COLUMN.get(ch.status.value, Column.BACKLOG),
        swimlane="change",
        meta={"itil_status": ch.status.value},
        source="itil",
    )


# ---------------------------------------------------------------------------
# KanbanBoard projection
# ---------------------------------------------------------------------------

COLUMN_ORDER = [c.value for c in Column]  # backlog, ready, doing, review, done
LANE_ORDER = ["feature", "bug", "security", "expedite", "change", "problem"]
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class KanbanBoard:
    """Read-only kanban projection over the coord board and the ITIL store.

    Args:
        home: Path to the shared skcapstone root (``~/.skcapstone``).
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()

    def cards(self) -> list[Card]:
        """All active (non-archived) cards from both sources."""
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
        """Group active cards as ``grid[swimlane][column] -> [cards]``.

        Cards within a cell are ordered by priority then id.
        """
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


# ---------------------------------------------------------------------------
# HTML render (self-contained, both themes, escaped, no em/en dashes)
# ---------------------------------------------------------------------------

_LANE_META = {
    "feature": ("Feature", "kind: task / epic"),
    "bug": ("Bug", "kind: task"),
    "security": ("Security", "kind: task"),
    "expedite": ("Expedite", "kind: incident"),
    "change": ("Change", "kind: change"),
    "problem": ("Problem", "kind: problem"),
}
_COLUMN_LABEL = {
    "backlog": "Backlog",
    "ready": "Ready",
    "doing": "In Progress",
    "review": "Review",
    "done": "Done",
}

_HTML_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#eef1f7;--panel:#fff;--panel2:#f6f8fc;--lane:#e8ecf5;--ink:#182031;
--ink2:#48546b;--ink3:#7a869e;--hair:#d7dded;--hair2:#e6eaf3;--accent:#268aa2;
--accentsoft:#d3ecf2;--crit:#d43a3f;--high:#c8781f;--med:#8a93a8;--low:#9aa4b8;
--incident:#e85a37;--change:#4f7fe0;--problem:#9160e0;--done:#2f9e6b;color-scheme:light;}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d1119;--panel:#151b28;--panel2:#121824;
--lane:#10151f;--ink:#e7ecf6;--ink2:#a7b2c8;--ink3:#6f7c95;--hair:#26303f;--hair2:#1d2532;
--accent:#4bb8d1;--accentsoft:#123039;--crit:#f0575c;--high:#e39a44;--med:#7f8aa2;--low:#626d84;
--incident:#ff7a54;--change:#6b96f2;--problem:#a97cf0;--done:#45c288;color-scheme:dark;}}}}
:root[data-theme="light"]{{--bg:#eef1f7;--panel:#fff;--panel2:#f6f8fc;--lane:#e8ecf5;--ink:#182031;
--ink2:#48546b;--ink3:#7a869e;--hair:#d7dded;--hair2:#e6eaf3;--accent:#268aa2;--accentsoft:#d3ecf2;
--crit:#d43a3f;--high:#c8781f;--med:#8a93a8;--low:#9aa4b8;--incident:#e85a37;--change:#4f7fe0;
--problem:#9160e0;--done:#2f9e6b;color-scheme:light;}}
:root[data-theme="dark"]{{--bg:#0d1119;--panel:#151b28;--panel2:#121824;--lane:#10151f;--ink:#e7ecf6;
--ink2:#a7b2c8;--ink3:#6f7c95;--hair:#26303f;--hair2:#1d2532;--accent:#4bb8d1;--accentsoft:#123039;
--crit:#f0575c;--high:#e39a44;--med:#7f8aa2;--low:#626d84;--incident:#ff7a54;--change:#6b96f2;
--problem:#a97cf0;--done:#45c288;color-scheme:dark;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:14px;line-height:1.45;}}
.mono{{font-family:ui-monospace,"SF Mono","JetBrains Mono",monospace;font-variant-numeric:tabular-nums;}}
.wrap{{max-width:1500px;margin:0 auto;padding:22px 20px 48px;}}
header{{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 18px;padding-bottom:16px;margin-bottom:16px;border-bottom:1px solid var(--hair);}}
h1{{font-size:19px;margin:0;font-weight:680;letter-spacing:-.01em;}}
.sub{{color:var(--ink3);font-size:12.5px;}}
.spacer{{flex:1 1 40px;}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;}}
.stat{{background:var(--panel);border:1px solid var(--hair);border-radius:9px;padding:6px 11px;display:flex;flex-direction:column;min-width:70px;}}
.stat .n{{font-size:16px;font-weight:700;}}
.stat .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);}}
.board-scroll{{overflow-x:auto;padding-bottom:6px;}}
.board{{display:grid;grid-template-columns:148px repeat(5,minmax(216px,1fr));min-width:1180px;border:1px solid var(--hair);border-radius:14px;overflow:hidden;background:var(--panel2);}}
.corner{{background:var(--panel);border-bottom:1px solid var(--hair);border-right:1px solid var(--hair2);}}
.colhead{{background:var(--panel);border-bottom:1px solid var(--hair);border-right:1px solid var(--hair2);padding:11px 13px;display:flex;align-items:center;justify-content:space-between;gap:8px;}}
.colhead:last-child{{border-right:none;}}
.colhead .name{{font-size:12px;font-weight:650;text-transform:uppercase;letter-spacing:.06em;}}
.colhead.donecol .name{{color:var(--done);}}
.wip{{font-size:11px;color:var(--ink3);border:1px solid var(--hair);border-radius:20px;padding:1px 8px;}}
.lanelabel{{border-right:1px solid var(--hair);border-bottom:1px solid var(--hair2);padding:14px 12px;background:var(--lane);display:flex;flex-direction:column;gap:6px;}}
.lname{{font-weight:640;font-size:12.5px;}}
.lkind{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);font-family:ui-monospace,monospace;}}
.lanerow{{display:contents;}}
.cell{{border-right:1px solid var(--hair2);border-bottom:1px solid var(--hair2);padding:8px;display:flex;flex-direction:column;gap:8px;min-height:56px;}}
.cell:nth-child(6n){{border-right:none;}}
.cell.expedite{{background:color-mix(in srgb,var(--incident) 6%,transparent);}}
.card{{position:relative;background:var(--panel);border:1px solid var(--hair);border-radius:9px;padding:9px 10px 9px 12px;display:flex;flex-direction:column;gap:7px;transition:transform .12s ease,border-color .12s;}}
.card:hover{{transform:translateY(-1px);border-color:color-mix(in srgb,var(--accent) 40%,var(--hair));}}
.card::before{{content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;border-radius:3px;background:var(--stripe,var(--med));}}
.p-critical{{--stripe:var(--crit);}} .p-high{{--stripe:var(--high);}} .p-medium{{--stripe:var(--med);}} .p-low{{--stripe:var(--low);}}
.ctop{{display:flex;align-items:center;gap:6px;}}
.badge{{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;padding:2px 6px;border-radius:5px;}}
.badge.task{{color:var(--accent);background:var(--accentsoft);}}
.badge.epic{{color:#fff;background:var(--accent);}}
.badge.incident{{color:#fff;background:var(--incident);}}
.badge.change{{color:#fff;background:var(--change);}}
.badge.problem{{color:#fff;background:var(--problem);}}
.cid{{margin-left:auto;font-size:10.5px;color:var(--ink3);}}
.sev{{font-size:9.5px;font-weight:700;padding:1px 5px;border-radius:4px;color:#fff;}}
.ctitle{{font-size:12.7px;line-height:1.3;font-weight:520;text-wrap:pretty;}}
.cfoot{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}}
.owner{{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--ink2);}}
.ava{{width:17px;height:17px;border-radius:50%;display:grid;place-items:center;font-size:9px;font-weight:700;color:#fff;background:var(--accent);}}
.tag{{font-size:10px;color:var(--ink3);background:var(--panel2);border:1px solid var(--hair2);border-radius:5px;padding:1px 6px;font-family:ui-monospace,monospace;}}
.cell.done .card{{opacity:.82;}}
.note{{margin-top:22px;padding:14px 16px;border-radius:11px;background:var(--panel);border:1px solid var(--hair);color:var(--ink2);font-size:12.5px;line-height:1.55;}}
.note b{{color:var(--ink);}}
.card:focus-visible{{outline:2px solid var(--accent);outline-offset:2px;}}
@media(prefers-reduced-motion:reduce){{.card{{transition:none;}}}}
</style></head><body><div class="wrap">
"""


def _sev_bg(sev: str) -> str:
    """Background var for a severity chip."""
    return "var(--incident)" if sev in ("sev1", "sev2") else "var(--med)"


def _clean(text: str) -> str:
    """Escape for HTML and normalize em/en dashes to a plain hyphen.

    Card titles come from live coord/ITIL data that may contain typographic
    dashes; the generated board keeps the house rule of plain hyphens only.
    """
    return html.escape(text).replace("—", "-").replace("–", "-")


def _render_card(c: Card) -> str:
    """Render one card to escaped HTML."""
    stripe = f"p-{html.escape(c.priority)}"
    badge = f'<span class="badge {c.kind.value}">{c.kind.value}</span>'
    sev = ""
    sev_val = c.meta.get("severity")
    if sev_val:
        sev = f'<span class="sev" style="background:{_sev_bg(sev_val)}">{html.escape(str(sev_val)).upper()}</span>'
    cid = f'<span class="cid mono">#{html.escape(c.id)}</span>'
    title = f'<div class="ctitle">{_clean(c.title)}</div>'
    foot = ""
    if c.owner:
        initial = html.escape(c.owner[:1].upper())
        foot = (
            f'<div class="cfoot"><span class="owner">'
            f'<span class="ava">{initial}</span>{_clean(c.owner)}</span></div>'
        )
    elif c.labels:
        foot = f'<div class="cfoot"><span class="tag">{_clean(c.labels[0])}</span></div>'
    stripe_style = ""
    if c.kind == Kind.INCIDENT:
        stripe_style = ' style="--stripe:var(--incident)"'
    elif c.kind == Kind.CHANGE:
        stripe_style = ' style="--stripe:var(--change)"'
    elif c.kind == Kind.PROBLEM:
        stripe_style = ' style="--stripe:var(--problem)"'
    return (
        f'<div class="card {stripe}"{stripe_style} tabindex="0">'
        f'<div class="ctop">{badge}{sev}{cid}</div>{title}{foot}</div>'
    )


def render_html(kb: "KanbanBoard", title: str = "SKBoard") -> str:
    """Render the kanban board as a self-contained HTML document.

    The output styles both light and dark themes, HTML-escapes every dynamic
    string, and contains no em or en dashes.
    """
    grid = kb.grid()
    all_cards = kb.cards()
    active = len([c for c in all_cards if c.status != Column.DONE])
    done = len([c for c in all_cards if c.status == Column.DONE])
    itil_n = len([c for c in all_cards if c.source == "itil"])

    parts = [_HTML_HEAD.format(title=html.escape(title))]
    parts.append(
        '<header><div><h1>SKBoard</h1>'
        '<div class="sub mono">cards/ &middot; kind in {task, epic, incident, problem, change}</div></div>'
        '<div class="spacer"></div><div class="stats">'
        f'<div class="stat"><span class="n mono">{active}</span><span class="l">Active</span></div>'
        f'<div class="stat"><span class="n mono">{itil_n}</span><span class="l">ITIL</span></div>'
        f'<div class="stat"><span class="n mono">{done}</span><span class="l">Done</span></div>'
        '</div></header>'
    )

    parts.append('<div class="board-scroll"><div class="board">')
    # column header row
    parts.append('<div class="corner"></div>')
    for col in COLUMN_ORDER:
        total = sum(len(grid[lane][col]) for lane in LANE_ORDER)
        donecls = " donecol" if col == "done" else ""
        parts.append(
            f'<div class="colhead{donecls}"><span class="name">{_COLUMN_LABEL[col]}</span>'
            f'<span class="wip mono">{total}</span></div>'
        )
    # lane rows (skip empty lanes to keep the board tight)
    for lane in LANE_ORDER:
        lane_cards = sum(len(grid[lane][col]) for col in COLUMN_ORDER)
        if lane_cards == 0:
            continue
        name, kind_label = _LANE_META[lane]
        parts.append('<div class="lanerow">')
        parts.append(
            f'<div class="lanelabel"><span class="lname">{name}</span>'
            f'<span class="lkind">{kind_label}</span></div>'
        )
        for col in COLUMN_ORDER:
            expe = " expedite" if lane == "expedite" else ""
            donecls = " done" if col == "done" else ""
            cards_html = "".join(_render_card(c) for c in grid[lane][col])
            parts.append(f'<div class="cell{expe}{donecls}">{cards_html}</div>')
        parts.append('</div>')
    parts.append('</div></div>')

    parts.append(
        '<div class="note"><b>Projection of live data.</b> Columns are the shared '
        'lifecycle, swimlanes are the card kind. The Expedite lane carries incidents. '
        'This board, BOARD.md, and the JSON view are all projections of one fold, so '
        'they cannot drift.</div>'
    )
    parts.append('</div></body></html>')
    return "".join(parts)
