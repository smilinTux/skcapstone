"""Dashboard kanban API: board data, card detail, mutations, and the SSE bus.

Phase 2 of the interactive SKDashboard (see
docs/superpowers/specs/2026-07-16-skdashboard-itil-kanban-airunner.md).

Reads come from the event-sourced ``CardStore`` (the board is served post-cutover
from ``SKCOORD_CARD_STORE=1``). Every mutation appends an event to the CardStore
and publishes on an in-process bus so open dashboards refresh over SSE. A
background poll of the card-events directory catches writes by other agents / the
runner on this or other nodes (Syncthing-synced).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from .card import COLUMN_ORDER, LANE_ORDER, Column
from .card_store import CardStore

logger = logging.getLogger("skcapstone.dashboard.kanban")

_VALID_COLUMNS = {c.value for c in Column}
_MUTATIONS = {"move", "assign", "unassign", "add_label", "remove_label", "priority", "note"}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _card_brief(c) -> dict:
    """A compact card dict for the board face."""
    run = c.meta.get("agent_run") or {}
    return {
        "id": c.id,
        "kind": c.kind.value,
        "title": c.title,
        "status": c.status.value,
        "swimlane": c.swimlane,
        "priority": c.priority,
        "owner": c.owner,
        "labels": c.labels,
        "order": c.order,
        "severity": c.meta.get("severity"),
        "ai": run.get("state"),
    }


def get_kanban(home: Path) -> dict:
    """The full board grouped by lane and column, with WIP status."""
    from .card import KanbanBoard

    kb = KanbanBoard(home)
    grid = kb.grid()
    lanes = []
    for lane in LANE_ORDER:
        cols = {col: [_card_brief(c) for c in grid[lane][col]] for col in COLUMN_ORDER}
        if sum(len(v) for v in cols.values()) == 0:
            continue
        lanes.append({"key": lane, "columns": cols})
    return {"columns": COLUMN_ORDER, "lanes": lanes, "wip": kb.wip_report()}


def get_card(home: Path, card_id: str) -> dict:
    """A folded card plus its raw event stream (the activity log)."""
    store = CardStore(home)
    card = store.fold(card_id)
    if card is None:
        return {"error": "card not found", "id": card_id}
    events = store._read_events(card_id)
    return {"card": card.model_dump(), "activity": events}


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def apply_mutation(home: Path, card_id: str, action: str, actor: str, **fields) -> dict:
    """Append a mutation event to the CardStore and return the new card state.

    Args:
        home: Shared skcapstone root.
        card_id: Target card id.
        action: One of the allowed mutation actions.
        actor: Who performed it (audit); written as the event writer.
        **fields: action-specific (column/order/owner/label/priority/text).

    Returns:
        dict: ``{"ok": True, "card": {...}}`` or ``{"error": ...}``.
    """
    if action not in _MUTATIONS:
        return {"error": f"unknown action '{action}'"}
    store = CardStore(home)
    if store.fold(card_id) is None:
        return {"error": "card not found", "id": card_id}

    if action == "move":
        col = fields.get("column")
        if col not in _VALID_COLUMNS:
            return {"error": f"invalid column '{col}'"}
        store.append_event(card_id, "move", actor, column=col, order=fields.get("order"))
    elif action == "assign":
        store.append_event(card_id, "assign", actor, owner=fields.get("owner"))
    elif action == "unassign":
        store.append_event(card_id, "unassign", actor)
    elif action in ("add_label", "remove_label"):
        if not fields.get("label"):
            return {"error": "label required"}
        store.append_event(card_id, action, actor, label=fields["label"])
    elif action == "priority":
        if fields.get("priority") not in ("critical", "high", "medium", "low"):
            return {"error": "invalid priority"}
        store.append_event(card_id, "priority", actor, priority=fields["priority"])
    elif action == "note":
        if not (fields.get("text") or "").strip():
            return {"error": "note text required"}
        store.append_event(card_id, "note", actor, text=fields["text"])

    return {"ok": True, "card": store.fold(card_id).model_dump()}


# ---------------------------------------------------------------------------
# SSE bus (in-process pub/sub + background event-store poll)
# ---------------------------------------------------------------------------

class Bus:
    """Minimal async pub/sub for SSE fan-out."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, message: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(message)
            except Exception:  # noqa: BLE001
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


BUS = Bus()


def _cards_fingerprint(home: Path) -> int:
    """Cheap change signal: sum of mtimes of all card-event logs.

    Changes when any writer (dashboard, runner, or another node via Syncthing)
    appends an event, without reading file contents.
    """
    cards_dir = Path(home).expanduser() / "cards"
    if not cards_dir.exists():
        return 0
    total = 0
    for p in cards_dir.glob("*/events/*.jsonl"):
        try:
            total += int(p.stat().st_mtime)
        except OSError:
            continue
    return total


async def poll_event_store(home: Path, interval: float = 1.0) -> None:
    """Background task: publish ``board_changed`` when the event store changes.

    Catches mutations made outside this process (other agents, the runner). The
    operator's own dashboard mutations also publish directly for instant echo.
    """
    last = _cards_fingerprint(home)
    while True:
        await asyncio.sleep(interval)
        try:
            cur = _cards_fingerprint(home)
            if cur != last:
                last = cur
                BUS.publish({"type": "board_changed"})
        except Exception as exc:  # noqa: BLE001
            logger.debug("event-store poll error: %s", exc)
