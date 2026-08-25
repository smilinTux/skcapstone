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
import base64
import hashlib
import json
import logging
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from skcoord.card import COLUMN_ORDER, LANE_ORDER, Column
from skcoord.card_store import CardStore

logger = logging.getLogger("skcapstone.dashboard.kanban")

_VALID_COLUMNS = {c.value for c in Column}
_MUTATIONS = {
    "move",
    "assign",
    "unassign",
    "add_label",
    "remove_label",
    "priority",
    "note",
    "describe",
}

_TERMINAL_ITIL_STATUSES = {
    "incident": {"closed"},
    "problem": {"resolved"},
    "change": {"verified", "closed", "rejected"},
}


def _authoritative_itil_cards(home: Path) -> dict[str, object]:
    """Project the current ITIL folds into cards keyed by record id.

    CardStore entries for ITIL records are intentionally only shadow cards;
    their immutable core captures the state at first materialization.  This
    helper keeps the dashboard projection anchored to the event-sourced ITIL
    record rather than treating that birth snapshot as current truth.
    """
    from skcoord.card import card_from_change, card_from_incident, card_from_problem
    from skcoord.itil import ITILManager

    mgr = ITILManager(Path(home).expanduser())
    out = {inc.id: card_from_incident(inc) for inc in mgr.list_incidents()}
    out.update({problem.id: card_from_problem(problem) for problem in mgr.list_problems()})
    for change in mgr.list_changes():
        try:
            events = mgr._read_events(mgr.changes_dir, change.id)
        except Exception:  # noqa: BLE001 - status projection remains useful without chips
            events = None
        out[change.id] = card_from_change(change, events=events)
    return out


def _overlay_authoritative_itil(card, authoritative):
    """Overlay mutable ITIL fields while preserving dashboard-only metadata."""
    card.title = authoritative.title
    card.description = authoritative.description
    card.status = authoritative.status
    card.swimlane = authoritative.swimlane
    card.priority = authoritative.priority
    card.source = "itil"
    card.meta.update(authoritative.meta)
    return card


def _sync_itil_shadow_cards(home: Path, authoritative: dict[str, object] | None = None) -> dict:
    """Reconcile existing ITIL shadows with authoritative ITIL lifecycle state.

    Reconciliation is append-only.  A lifecycle mismatch emits one ``move``
    event.  If the ITIL record is terminal, any queued/running AgentRun is
    canceled before a newly enabled runner can execute work for a closed
    record.  Dynamic ITIL metadata is overlaid by read functions because
    CardStore core metadata is immutable by design.
    """
    records = authoritative if authoritative is not None else _authoritative_itil_cards(home)
    store = CardStore(home)
    for card_id, source_card in records.items():
        shadow = store.fold(card_id)
        if shadow is None:
            continue
        if shadow.status != source_card.status:
            store.append_event(
                card_id,
                "move",
                "dashboard-itil-sync",
                column=source_card.status.value,
                order=shadow.order,
            )
            shadow = store.fold(card_id) or shadow
        status = source_card.meta.get("itil_status")
        terminal = status in _TERMINAL_ITIL_STATUSES.get(source_card.kind.value, set())
        run = shadow.meta.get("agent_run") or {}
        if terminal and run.get("state") in {"queued", "running"} and run.get("run_id"):
            store.append_event(
                card_id,
                "agent_run_state",
                "dashboard-itil-sync",
                run_id=run["run_id"],
                state="canceled",
                last_error=f"Authoritative ITIL record is {status}; run canceled",
            )
    return records


def itil_card_runnable(home: Path, card_id: str) -> tuple[bool, str | None]:
    """Reject new dashboard runs for authoritative terminal ITIL records."""
    if not card_id.startswith(("inc-", "prb-", "chg-")):
        return True, None
    source_card = _authoritative_itil_cards(home).get(card_id)
    if source_card is None:
        return False, "authoritative ITIL record not found"
    status = source_card.meta.get("itil_status")
    terminal = status in _TERMINAL_ITIL_STATUSES.get(source_card.kind.value, set())
    if terminal:
        return False, f"authoritative ITIL record is {status}; AI run not queued"
    return True, None


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


def _pir_chip(change_id: str, home: Path | None) -> dict | None:
    """PIR (post-implementation review) chip: ``{note, agent, ts}`` once the
    change has completed ``deployed -> verified`` (CM P3.3, design doc
    section 3), or ``None`` before that.

    Unlike the other three chips, the PIR note is not part of the card's
    meta projection (``card_from_change`` carries ``itil_status`` but not
    the timeline) - it lives on the folded ``Change`` record's timeline
    entry for the gated ``status:deployed->verified`` transition
    (``skcoord.itil._fold_change``). This re-folds the full record via
    ``home``, the same extra read the CAB tally chip already performs for
    its own vote data.
    """
    if home is None:
        return None
    try:
        from skcoord.itil import Change, ITILManager

        mgr = ITILManager(home)
        rid = mgr._resolve_id(mgr.changes_dir, change_id)
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
    except Exception:  # noqa: BLE001 - chip rendering must never break the board
        return None
    if chg is None or chg.status.value not in ("verified", "closed"):
        return None
    verified_rows = [
        row
        for row in chg.timeline
        if row.get("action") == "status:deployed->verified" and not row.get("conflicted")
    ]
    if not verified_rows:
        return None
    row = verified_rows[-1]
    return {"note": row.get("note", ""), "agent": row.get("agent", ""), "ts": row.get("ts", "")}


def _change_chips(c, home: Path | None) -> dict:
    """The four change-card chips: CAB tally / validation verdict / window / PIR."""
    return {
        "cab": _cab_chip(c.id, home),
        "validation": _validation_chip(c.meta.get("validation"), c.meta.get("prepared_pr")),
        "window": _window_chip(c.meta.get("scheduled_window"), bool(c.meta.get("window_missed"))),
        "pir": _pir_chip(c.id, home),
    }


def get_kanban(home: Path) -> dict:
    """The full board grouped by lane and column, with WIP status."""
    from skcoord.card import KanbanBoard

    authoritative = _sync_itil_shadow_cards(home)
    kb = KanbanBoard(home)
    grid = kb.grid()
    lanes = []
    for lane in LANE_ORDER:
        cols = {
            col: [
                _card_brief(
                    _overlay_authoritative_itil(c, authoritative[c.id])
                    if c.id in authoritative
                    else c,
                    home,
                )
                for c in grid[lane][col]
            ]
            for col in COLUMN_ORDER
        }
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
    authoritative = _sync_itil_shadow_cards(home)
    store = CardStore(home)
    card = store.fold(card_id)
    if card is None:
        return {"error": "card not found", "id": card_id}
    if card_id in authoritative:
        card = _overlay_authoritative_itil(card, authoritative[card_id])
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
    elif action == "describe":
        # SPE P3.1: title/description are folded, not frozen. Only the fields
        # actually supplied are written, so editing one never blanks the other,
        # and an empty string stays a deliberate clear. core.json is untouched.
        payload = {k: fields[k] for k in ("title", "description") if fields.get(k) is not None}
        if not payload:
            return {"error": "title or description required"}
        store.append_event(card_id, "describe", actor, **payload)

    return {"ok": True, "card": store.fold(card_id).model_dump()}


# ---------------------------------------------------------------------------
# SSE bus (in-process pub/sub + background event-store poll)
# ---------------------------------------------------------------------------


SSE_BOUND = 200  # Matches the frozen API's maximum bounded result size.
SSE_MAX_EVENT_BYTES = 64 * 1024  # Matches the existing bounded request ceiling.
SSE_HEARTBEAT_SECONDS = 20  # Preserves the existing dashboard SSE interval.
PUBLIC_STREAM_LANE = "public"


@dataclass(frozen=True)
class StreamEvent:
    sequence: int
    event_id: str
    event: str
    data: str


@dataclass(frozen=True)
class StreamReset:
    reason: str


@dataclass
class StreamLane:
    replay: deque[StreamEvent]
    subscribers: dict[asyncio.Queue, frozenset[str]]
    resume_floor: int | None = None


@dataclass
class StreamSubscription:
    bus: "Bus"
    replay: tuple[StreamEvent, ...]
    queue: asyncio.Queue | None
    boundary: str = "default"
    reset: StreamReset | None = None

    def close(self) -> None:
        if self.queue is not None:
            self.bus.close_stream(self.queue, self.boundary)


class Bus:
    """Bounded async fan-out partitioned by an authorized policy boundary."""

    def __init__(self, *, stream_id: str | None = None) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._lanes: OrderedDict[str, StreamLane] = OrderedDict()
        self._stream_id = stream_id or uuid4().hex
        self._sequence = 0

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to the bounded legacy refresh stream."""
        if self.subscriber_count >= SSE_BOUND:
            raise RuntimeError("event stream capacity reached")
        q: asyncio.Queue = asyncio.Queue(maxsize=SSE_BOUND)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @staticmethod
    def _boundary_id(boundary: str) -> str:
        return (
            base64.urlsafe_b64encode(hashlib.sha256(boundary.encode()).digest())
            .decode()
            .rstrip("=")
        )

    def _lane(self, boundary: str) -> StreamLane:
        if not isinstance(boundary, str) or not boundary or len(boundary) > 512:
            raise ValueError("event policy boundary is invalid")
        lane = self._lanes.get(boundary)
        if lane is None:
            if len(self._lanes) >= SSE_BOUND:
                inactive = next(
                    (key for key, value in self._lanes.items() if not value.subscribers), None
                )
                if inactive is None:
                    raise RuntimeError("event stream capacity reached")
                self._lanes.pop(inactive)
            lane = StreamLane(deque(maxlen=SSE_BOUND), {})
            self._lanes[boundary] = lane
        self._lanes.move_to_end(boundary)
        return lane

    def _event_id(self, sequence: int, boundary: str) -> str:
        value = f"sse:v2:{self._stream_id}:{self._boundary_id(boundary)}:{sequence}".encode()
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def _decode_event_id(raw: str) -> tuple[str, str, int]:
        if len(raw) > 512:
            raise ValueError("event cursor is too long")
        try:
            decoded = base64.b64decode(
                raw.encode("ascii") + b"=" * (-len(raw) % 4),
                altchars=b"-_",
                validate=True,
            ).decode("ascii")
            if base64.urlsafe_b64encode(decoded.encode()).decode().rstrip("=") != raw:
                raise ValueError
            parts = decoded.split(":")
            marker, version = parts[:2]
            if version == "v1" and len(parts) == 4:
                _marker, _version, stream_id, sequence_text = parts
                boundary_id = ""
            elif version == "v2" and len(parts) == 5:
                _marker, _version, stream_id, boundary_id, sequence_text = parts
            else:
                raise ValueError
            sequence = int(sequence_text)
            if (
                marker != "sse"
                or len(stream_id) != 32
                or (version == "v2" and len(boundary_id) != 43)
                or sequence < 1
            ):
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise ValueError("event cursor is invalid") from exc
        return stream_id, boundary_id, sequence

    def open_stream(
        self,
        cursor: str | None = None,
        topics: tuple[str, ...] = (),
        *,
        boundary: str = "default",
    ) -> StreamSubscription:
        lane = self._lane(boundary)
        if cursor:
            stream_id, boundary_id, sequence = self._decode_event_id(cursor)
            known_cursor = lane.resume_floor == sequence or any(
                event.sequence == sequence for event in lane.replay
            )
            if (
                stream_id != self._stream_id
                or boundary_id != self._boundary_id(boundary)
                or sequence > self._sequence
                or not known_cursor
            ):
                return StreamSubscription(
                    self, (), None, boundary, StreamReset("replay window unavailable")
                )
            replay = tuple(
                event
                for event in lane.replay
                if event.sequence > sequence and (not topics or event.event in topics)
            )
        else:
            replay = ()
        if self.subscriber_count >= SSE_BOUND:
            raise RuntimeError("event stream capacity reached")
        queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_BOUND)
        lane.subscribers[queue] = frozenset(topics)
        return StreamSubscription(self, replay, queue, boundary)

    def close_stream(self, queue: asyncio.Queue, boundary: str = "default") -> None:
        lane = self._lanes.get(boundary)
        if lane is not None:
            lane.subscribers.pop(queue, None)

    @staticmethod
    def _replace_with_reset(queue: asyncio.Queue, reason: str) -> None:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(StreamReset(reason))

    def _reset_streams(self, reason: str, boundary: str = "default") -> None:
        lane = self._lanes.get(boundary)
        if lane is not None:
            for queue in tuple(lane.subscribers):
                self._replace_with_reset(queue, reason)
                lane.subscribers.pop(queue, None)

    def _trim_replay(self) -> None:
        while self.replay_size > SSE_BOUND:
            lane = min(
                (value for value in self._lanes.values() if value.replay),
                key=lambda value: value.replay[0].sequence,
            )
            lane.resume_floor = lane.replay.popleft().sequence

    def publish(
        self,
        message: dict,
        *,
        boundary: str | None = None,
        public: bool = False,
    ) -> StreamEvent | None:
        if public == (boundary is not None) or boundary == PUBLIC_STREAM_LANE:
            raise ValueError("event must name exactly one public or protected lane")
        key = PUBLIC_STREAM_LANE if public else boundary
        try:
            event = message.get("type", "message")
            if (
                not isinstance(message, dict)
                or not isinstance(event, str)
                or not event
                or len(event) > 64
                or "\n" in event
                or "\r" in event
            ):
                raise ValueError
            data = json.dumps(message, sort_keys=True, separators=(",", ":"))
            if len(data.encode()) > SSE_MAX_EVENT_BYTES:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            self._reset_streams("producer failure", key)
            return None

        self._sequence += 1
        lane = self._lane(key)
        stored = StreamEvent(self._sequence, self._event_id(self._sequence, key), event, data)
        lane.replay.append(stored)
        self._trim_replay()
        for queue, topics in tuple(lane.subscribers.items()):
            if topics and event not in topics:
                continue
            if queue.full():
                self._replace_with_reset(queue, "slow consumer")
                lane.subscribers.pop(queue, None)
            else:
                queue.put_nowait(stored)
        if public:
            for queue in tuple(self._subs):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(message)
        return stored

    @property
    def replay_size(self) -> int:
        return sum(len(lane.replay) for lane in self._lanes.values())

    @property
    def subscriber_count(self) -> int:
        return len(self._subs) + sum(len(lane.subscribers) for lane in self._lanes.values())


async def stream_sse(
    subscription: StreamSubscription,
    *,
    heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
    authority=None,
    currentness_seconds: float | None = None,
):
    """Serialize one bounded subscription until reset, authority loss, or cancellation."""

    def frame(event: StreamEvent) -> str:
        return f"id: {event.event_id}\nevent: {event.event}\ndata: {event.data}\n\n"

    def authority_is_current() -> bool:
        return authority is None or authority.check()

    def authority_reset() -> str:
        subscription.close()
        return 'event: reset-required\ndata: {"reason":"authority unavailable"}\n\n'

    if currentness_seconds is None:
        currentness_seconds = heartbeat_seconds
    timeout = min(heartbeat_seconds, currentness_seconds)
    elapsed = heartbeat_seconds
    try:
        if subscription.reset is not None:
            if not authority_is_current():
                yield authority_reset()
                return
            yield (
                "event: reset-required\n"
                f"data: {json.dumps({'reason': subscription.reset.reason}, separators=(',', ':'))}\n\n"
            )
            if not authority_is_current():
                yield authority_reset()
                return
            yield ": heartbeat\n\n"
            return
        for event in subscription.replay:
            if not authority_is_current():
                yield authority_reset()
                return
            yield frame(event)
        if not authority_is_current():
            yield authority_reset()
            return
        yield ": heartbeat\n\n"
        elapsed = 0
        while subscription.queue is not None:
            try:
                item = await asyncio.wait_for(subscription.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                elapsed += timeout
                if not authority_is_current():
                    yield authority_reset()
                    return
                if elapsed < heartbeat_seconds:
                    continue
                elapsed = 0
                yield ": heartbeat\n\n"
                continue
            if not authority_is_current():
                yield authority_reset()
                return
            if isinstance(item, StreamReset):
                yield (
                    "event: reset-required\n"
                    f"data: {json.dumps({'reason': item.reason}, separators=(',', ':'))}\n\n"
                )
                return
            yield frame(item)
    finally:
        subscription.close()
        if authority is not None:
            authority.close()


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
                BUS.publish({"type": "board_changed"}, public=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("event-store poll error: %s", exc)
