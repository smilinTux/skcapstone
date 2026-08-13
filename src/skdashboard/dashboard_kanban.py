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

from skcoord.card import COLUMN_ORDER, LANE_ORDER, Column
from skcoord.card_store import CardStore

logger = logging.getLogger("skcapstone.dashboard.kanban")

_VALID_COLUMNS = {c.value for c in Column}
_MUTATIONS = {"move", "assign", "unassign", "add_label", "remove_label", "priority", "note"}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _card_brief(c, home: Path | None = None) -> dict:
    """A compact card dict for the board face.

    Change-mgmt P2.4: change cards (``c.kind.value == "change"``) additionally
    carry the raw P1.1 fold fields (``itil_status``, ``prepared_pr``,
    ``prepared_by``, ``validation``, ``scheduled_window``) plus three derived
    chip payloads (``chips.cab``/``chips.validation``/``chips.window``) so any
    client renders the card face without a second fetch of the raw ITIL
    record. ``home`` is required to compute the CAB tally chip (CAB votes are
    a separate per-agent file set, not part of the folded ``Change``); it is
    optional only so existing non-kanban callers of this function keep
    working, in which case the CAB chip degrades to an all-zero tally.
    """
    run = c.meta.get("agent_run") or {}
    brief = {
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
    if c.kind.value == "change":
        brief["itil_status"] = c.meta.get("itil_status")
        brief["prepared_pr"] = c.meta.get("prepared_pr")
        brief["prepared_by"] = c.meta.get("prepared_by")
        brief["validation"] = c.meta.get("validation")
        brief["scheduled_window"] = c.meta.get("scheduled_window")
        brief["chips"] = _change_chips(c, home)
    return brief


def _cab_chip(change_id: str, home: Path | None) -> dict:
    """CAB tally chip: ``{approved, rejected, abstain, human_decision}``.

    ``human_decision`` is the "human" identity's own vote decision
    (``"approved"``/``"rejected"``/``"abstain"``), or ``None`` when the human
    seat has not voted yet - a more specific marker than a bare boolean,
    matching how ``_fold_change``'s CAB derivation itself treats the
    ``human`` voter as the deciding identity (skcoord.itil).
    """
    tally = {"approved": 0, "rejected": 0, "abstain": 0, "human_decision": None}
    if home is None:
        return tally
    try:
        from skcoord.itil import ITILManager

        votes = ITILManager(home).get_cab_votes(change_id)
    except Exception:  # noqa: BLE001 - chip rendering must never break the board
        return tally
    for v in votes:
        tally[v.decision.value] = tally.get(v.decision.value, 0) + 1
        if v.agent == "human":
            tally["human_decision"] = v.decision.value
    return tally


def _validation_chip(validation: dict | None, prepared_pr: dict | None) -> dict | None:
    """Validation verdict chip: ``{passed, check_count, stale}`` or ``None``
    when the change has never been validated.

    ``stale`` compares the verdict's ``head_sha`` against the change's
    current known PR head (``prepared_pr["head_sha"]``, the best proxy this
    read-only projection has without a live `gh` call) - the same freshness
    check the (later) deploy executor performs before merging (design doc
    section 5.2 step 4).
    """
    if not validation:
        return None
    checks = validation.get("checks") or []
    current_sha = (prepared_pr or {}).get("head_sha")
    verdict_sha = validation.get("head_sha")
    stale = bool(current_sha and verdict_sha and current_sha != verdict_sha)
    return {
        "passed": bool(validation.get("passed")),
        "check_count": len(checks),
        "stale": stale,
    }


def _window_chip(window: dict | None, window_missed: bool) -> dict:
    """Window chip: ``{label, asap}`` where ``label`` is ``"ASAP"``, a
    formatted window start (``"Fri 02:00Z"``), ``"MISSED"``, or ``"none"``.
    """
    if window:
        if window.get("asap"):
            return {"label": "ASAP", "asap": True}
        start = window.get("window_start")
        if start:
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                return {"label": dt.strftime("%a %H:%MZ"), "asap": False}
            except ValueError:
                return {"label": str(start), "asap": False}
        return {"label": "none", "asap": False}
    if window_missed:
        return {"label": "MISSED", "asap": False}
    return {"label": "none", "asap": False}


def _change_chips(c, home: Path | None) -> dict:
    """The three change-card chips: CAB tally / validation verdict / window."""
    return {
        "cab": _cab_chip(c.id, home),
        "validation": _validation_chip(c.meta.get("validation"), c.meta.get("prepared_pr")),
        "window": _window_chip(c.meta.get("scheduled_window"), bool(c.meta.get("window_missed"))),
    }


def get_kanban(home: Path) -> dict:
    """The full board grouped by lane and column, with WIP status."""
    from skcoord.card import KanbanBoard

    kb = KanbanBoard(home)
    grid = kb.grid()
    lanes = []
    for lane in LANE_ORDER:
        cols = {col: [_card_brief(c, home) for c in grid[lane][col]] for col in COLUMN_ORDER}
        if sum(len(v) for v in cols.values()) == 0:
            continue
        lanes.append({"key": lane, "columns": cols})
    return {"columns": COLUMN_ORDER, "lanes": lanes, "wip": kb.wip_report()}


def get_card(home: Path, card_id: str) -> dict:
    """A folded card plus its raw event stream (the activity log).

    Tolerant of ITIL ids (inc-/prb-/chg-): materializes the card from the ITIL
    record if it is not yet in the store, so any item opens into its detail.
    """
    try:
        from skcapstone import agent_run

        agent_run.ensure_card(home, card_id)
    except Exception:  # noqa: BLE001
        pass
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
