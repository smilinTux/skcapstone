"""Event-sourced Card store (Phase 4): the unified storage substrate.

One work item = one directory ``cards/<id>/`` with an immutable ``core.json``
(birth facts, write-once via O_EXCL) plus append-only per-writer event logs
``events/<agent>@<host>.jsonl``. Current state is folded on read, never stored.
This is the same conflict-free pattern proven in ``itil.py`` (the July-13
refactor), generalized with a ``kind`` discriminator so tasks, epics, and ITIL
tickets share one engine.

Phase 4 ships flag-gated (``SKCOORD_CARD_STORE``); see
docs/superpowers/plans/2026-07-16-cards-storage-cutover-phase4-SHELVED.md.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .card import Card, Column, Kind

logger = logging.getLogger(__name__)

_HOSTNAME = socket.gethostname()

# Column reached by a claim/complete convenience event, to mirror coord.
# coord's claim_task sets current_task, so a claim = in_progress = doing (not ready).
_CLAIM_COLUMN = Column.DOING
_COMPLETE_COLUMN = Column.DONE


def _now_iso() -> str:
    """UTC now as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class CardCore(BaseModel):
    """Immutable birth-facts of a card (written once to core.json)."""

    id: str
    kind: str = Kind.TASK.value
    title: str
    description: str = ""
    created_by: str = ""
    created_at: str = Field(default_factory=_now_iso)
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    initial_priority: str = "medium"
    initial_swimlane: str = "feature"
    initial_labels: list[str] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


# Sanctioned legacy overlay actions (coordination/card_events/*.jsonl) mapped
# onto the store fold's action vocabulary. Anything unmapped is ignored.
_OVERLAY_TO_STORE_ACTION = {
    "move": "move",
    "set_priority": "priority",
    "set_swimlane": "swimlane",
    "add_label": "add_label",
    "remove_label": "remove_label",
    "link": "link",
    "assign": "assign",
    "unassign": "unassign",
}

_OVERLAY_PAYLOAD_KEYS = (
    "column", "order", "priority", "swimlane", "label",
    "link_key", "link_value", "owner",
)


def load_legacy_mutations(home: Path) -> dict[str, list[dict]]:
    """Synthesize fold events from the sanctioned legacy append-only paths.

    Two legacy write paths remain live post-cutover (as the hot backup) and can
    carry mutations the store's own logs never saw (flag unset in that process,
    e.g. cron sweeps, or a best-effort mirror failure):

    - ``coordination/archive/<host>.jsonl`` (``Board.archive_task``) becomes an
      ``archive`` event stamped with its ``archived_at`` timestamp.
    - ``coordination/card_events/*.jsonl`` (the kanban overlay) becomes the
      equivalent store action per ``_OVERLAY_TO_STORE_ACTION``.

    Both are per-writer append-only, so merging them into the fold keeps the
    conflict-free invariant: no file is ever rewritten, ordering stays
    ``(ts, writer, seq)``, and a mutation mirrored into BOTH sides simply
    applies twice idempotently.

    Returns:
        dict: card_id -> list of synthetic event dicts (fold-shaped).
    """
    out: dict[str, list[dict]] = {}

    archive_dir = Path(home).expanduser() / "coordination" / "archive"
    if archive_dir.exists():
        for f in sorted(archive_dir.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping unreadable archive index %s: %s", f.name, exc)
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                tid = entry.get("id")
                if not tid:
                    continue
                out.setdefault(tid, []).append({
                    "ts": entry.get("archived_at", ""),
                    "writer": entry.get("archived_by") or "archive",
                    "seq": 0,
                    "action": "archive",
                    "origin": "legacy-archive",
                })

    from .card import CardEventLog

    for e in CardEventLog(home).read_all():
        action = _OVERLAY_TO_STORE_ACTION.get(e.action)
        if action is None:
            continue
        ev: dict = {
            "ts": e.ts,
            "writer": e.writer,
            "seq": e.seq,
            "action": action,
            "origin": "legacy-overlay",
        }
        for k in _OVERLAY_PAYLOAD_KEYS:
            v = getattr(e, k, None)
            if v is not None:
                ev[k] = v
        out.setdefault(e.card_id, []).append(ev)
    return out


class CardStore:
    """Event-sourced store for unified work-item cards.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.cards_dir = self.home / "cards"
        # Per-instance cache of legacy mutations (archive index + overlay).
        # Instances are short-lived (one per CLI/MCP call), so a single load
        # keeps list_cards() O(files) instead of rescanning per card.
        self._legacy_cache: Optional[dict[str, list[dict]]] = None

    def ensure_dirs(self) -> None:
        self.cards_dir.mkdir(parents=True, exist_ok=True)

    def _writer_id(self, agent: str) -> str:
        safe = (agent or "unknown").replace("/", "-").replace("@", "-")
        return f"{safe}@{_HOSTNAME}"

    # ── writes ────────────────────────────────────────────────────────────

    def create(self, core: CardCore) -> str:
        """Write ``cards/<id>/core.json`` write-once. Returns the card id.

        Uses O_CREAT|O_EXCL so a concurrent create on the same id is safe (the
        loser sees the existing core).
        """
        self.ensure_dirs()
        rec_dir = self.cards_dir / core.id
        rec_dir.mkdir(parents=True, exist_ok=True)
        core_path = rec_dir / "core.json"
        payload = (core.model_dump_json(indent=2) + "\n").encode("utf-8")
        try:
            fd = os.open(str(core_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return core.id
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return core.id

    def append_event(self, card_id: str, action: str, agent: str, **payload: Any) -> None:
        """Append one event line to this writer's own log (flock-guarded)."""
        rec_dir = self.cards_dir / card_id
        events_dir = rec_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{self._writer_id(agent)}.jsonl"
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                seq = sum(1 for _ in fh)
                event = {
                    "event_id": uuid.uuid4().hex,
                    "ts": _now_iso(),
                    "writer": agent,
                    "node": _HOSTNAME,
                    "seq": seq,
                    "action": action,
                }
                event.update(payload)
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(event, default=str) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # ── reads ─────────────────────────────────────────────────────────────

    def _load_core(self, card_id: str) -> Optional[dict]:
        core_path = self.cards_dir / card_id / "core.json"
        if not core_path.exists():
            return None
        try:
            return json.loads(core_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bad core.json for card %s: %s", card_id, exc)
            return None

    def _read_events(self, card_id: str) -> list[dict]:
        events_dir = self.cards_dir / card_id / "events"
        out: list[dict] = []
        if not events_dir.exists():
            return out
        for f in sorted(events_dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
        # Deterministic order: ts, then writer, then per-writer seq.
        out.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
        return out

    def _legacy_events(self, card_id: str) -> list[dict]:
        """Legacy mutations (archive index + overlay) for one card, cached."""
        if self._legacy_cache is None:
            try:
                self._legacy_cache = load_legacy_mutations(self.home)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Legacy mutation load failed: %s", exc)
                self._legacy_cache = {}
        return self._legacy_cache.get(card_id, [])

    def fold(self, card_id: str) -> Optional[Card]:
        """Fold core + events into the current ``Card`` state.

        The event stream is the union of this card's own store logs AND the
        sanctioned legacy paths (archive index + card_events overlay), merged
        in ``(ts, writer, seq)`` order. That is the fold-drift fix (card
        ba4af853): a mutation that only reached a legacy file (mirror off or
        failed) still folds into the served state, so ``coord status`` cannot
        overcount open cards post-cutover.
        """
        core = self._load_core(card_id)
        if core is None:
            return None
        try:
            kind = Kind(core.get("kind", "task"))
        except ValueError:
            kind = Kind.TASK
        card = Card(
            id=core["id"],
            kind=kind,
            title=core.get("title", ""),
            description=core.get("description", ""),
            status=Column.BACKLOG,
            swimlane=core.get("initial_swimlane", "feature"),
            priority=core.get("initial_priority", "medium"),
            originator=core.get("created_by", ""),
            labels=list(core.get("initial_labels", [])),
            dependencies=list(core.get("dependencies", [])),
            meta=dict(core.get("meta", {})),
            created_at=core.get("created_at", ""),
            source="cards",
        )
        events = self._read_events(card_id)
        legacy_events = self._legacy_events(card_id)
        if legacy_events:
            events = events + legacy_events
            events.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""),
                                       e.get("seq", 0)))
        for e in events:
            action = e.get("action")
            if action == "move":
                col = e.get("column")
                if col in {c.value for c in Column}:
                    card.status = Column(col)
                if e.get("order") is not None:
                    card.order = e["order"]
            elif action == "assign":
                card.owner = e.get("owner")
            elif action == "unassign":
                card.owner = None
            elif action == "claim":
                card.owner = e.get("owner")
                card.status = _CLAIM_COLUMN
            elif action == "complete":
                card.status = _COMPLETE_COLUMN
                # coord drops a completed task from claimed_tasks, so its derived
                # claimed_by is None. Match that so parity holds on done cards.
                card.owner = None
            elif action == "priority" and e.get("priority"):
                card.priority = e["priority"]
            elif action == "swimlane" and e.get("swimlane"):
                card.swimlane = e["swimlane"]
            elif action == "add_label" and e.get("label") and e["label"] not in card.labels:
                card.labels.append(e["label"])
            elif action == "remove_label" and e.get("label") in card.labels:
                card.labels.remove(e["label"])
            elif action == "link" and e.get("link_key") is not None:
                card.links[e["link_key"]] = e.get("link_value")
            elif action == "note" and e.get("text"):
                card.meta.setdefault("comments", []).append(
                    {"ts": e.get("ts"), "writer": e.get("writer"), "text": e["text"]}
                )
            elif action == "agent_run_request":
                card.meta["agent_run"] = {
                    "run_id": e.get("run_id"),
                    "state": "queued",
                    "instruction": e.get("instruction", ""),
                    "agent": e.get("run_agent"),
                    "mode": e.get("mode", "propose"),
                    "kind": e.get("kind"),
                    "requester": e.get("writer"),
                    "created_at": e.get("ts"),
                    "activity": [],
                    "attempts": 0,
                    "links": {},
                }
            elif action == "agent_run_claim":
                r = card.meta.get("agent_run")
                if r and r.get("run_id") == e.get("run_id"):
                    r["state"] = "running"
                    r["worker"] = e.get("worker")
                    r["lease_expires"] = e.get("lease_expires")
                    r["attempts"] = r.get("attempts", 0) + 1
            elif action == "agent_run_activity":
                r = card.meta.get("agent_run")
                if r and r.get("run_id") == e.get("run_id"):
                    r.setdefault("activity", []).append({
                        "ts": e.get("ts"), "atype": e.get("atype"),
                        "text": e.get("text"), "writer": e.get("writer"),
                    })
            elif action == "agent_run_state":
                r = card.meta.get("agent_run")
                if r and r.get("run_id") == e.get("run_id"):
                    r["state"] = e.get("state", r.get("state"))
                    if e.get("last_error"):
                        r["last_error"] = e.get("last_error")
                    for k in ("pr", "commit", "branch", "transcript"):
                        if e.get(k):
                            r.setdefault("links", {})[k] = e.get(k)
            elif action == "archive":
                card.archived = True
                card.meta["archived_at"] = e.get("ts")
                card.meta["archived_by"] = e.get("writer")
            elif action == "reopen":
                card.archived = False
                col = e.get("column")
                if col in {c.value for c in Column}:
                    card.status = Column(col)
            card.updated_at = e.get("ts", card.updated_at)
        return card

    def list_card_ids(self) -> list[str]:
        if not self.cards_dir.exists():
            return []
        return sorted(
            p.name for p in self.cards_dir.iterdir()
            if p.is_dir() and (p / "core.json").exists()
        )

    def list_cards(self, include_archived: bool = False) -> list[Card]:
        """Fold every card. Archived excluded unless requested."""
        out: list[Card] = []
        for cid in self.list_card_ids():
            card = self.fold(cid)
            if card is None:
                continue
            if card.archived and not include_archived:
                continue
            out.append(card)
        return out


# ---------------------------------------------------------------------------
# Importer + parity (Phase 4b / 4c)
# ---------------------------------------------------------------------------


def import_from_legacy(home: Path, dry_run: bool = False) -> dict:
    """Import the live legacy board (coord + ITIL + overlay) into the CardStore.

    Idempotent: a card whose ``core.json`` already exists is skipped, so a
    re-run is a no-op. Reproduces each card's column, owner, and archived state
    by emitting create + move + assign + archive events.

    Returns:
        dict: ``{"imported": n, "skipped": m, "total": t}``.
    """
    from .card import KanbanBoard

    store = CardStore(home)
    # Force the LEGACY projection even post-cutover (flag=1), otherwise
    # KanbanBoard would serve the store back to us and every card legacy-only
    # card would look "already present" (i.e. migrate could never import it).
    saved = os.environ.pop("SKCOORD_CARD_STORE", None)
    try:
        legacy = KanbanBoard(home).cards(include_archived=True)
    finally:
        if saved is not None:
            os.environ["SKCOORD_CARD_STORE"] = saved
    imported = 0
    skipped = 0
    for c in legacy:
        if store._load_core(c.id) is not None:
            skipped += 1
            continue
        if dry_run:
            imported += 1
            continue
        store.create(CardCore(
            id=c.id,
            kind=c.kind.value,
            title=c.title,
            description=c.description,
            created_by=c.originator,
            created_at=c.created_at or _now_iso(),
            dependencies=list(c.dependencies),
            initial_priority=c.priority,
            initial_swimlane=c.swimlane,
            initial_labels=list(c.labels),
            meta=dict(c.meta),
        ))
        writer = c.originator or "import"
        store.append_event(c.id, "move", writer, column=c.status.value, order=c.order)
        if c.owner:
            store.append_event(c.id, "assign", writer, owner=c.owner)
        if c.archived:
            store.append_event(c.id, "archive", writer)
        imported += 1
    return {"imported": imported, "skipped": skipped, "total": len(legacy)}


def card_store_write_enabled() -> bool:
    """True when coord writes should mirror into the CardStore (soak/cutover)."""
    return os.environ.get("SKCOORD_CARD_STORE") in ("1", "dual")


def card_store_read_enabled() -> bool:
    """True when reads should be served from the CardStore (post-cutover)."""
    return os.environ.get("SKCOORD_CARD_STORE") == "1"


# Reverse of card._STATUS_TO_COLUMN, to reconstruct coord TaskViews from cards.
_COLUMN_TO_STATUS = {
    "backlog": "open",
    "ready": "claimed",
    "doing": "in_progress",
    "review": "review",
    "done": "done",
}


def task_views_from_store(home: Path, include_archived: bool = False) -> list:
    """Reconstruct coord ``TaskView`` objects from the CardStore.

    Used by ``Board.get_task_views`` when reads are cut over
    (``SKCOORD_CARD_STORE=1``), so the dashboard, ``coord status``, and claim
    validation all serve from the event-sourced store while legacy keeps being
    written as a hot backup.
    """
    from .coordination import Task, TaskPriority, TaskStatus, TaskView

    store = CardStore(home)
    views = []
    for c in store.list_cards(include_archived=include_archived):
        # get_task_views is the COORD task board: coord-origin kinds only.
        # ITIL cards (incident/problem/change) live in the kanban view, not here.
        if c.kind.value not in ("task", "epic"):
            continue
        try:
            priority = TaskPriority(c.priority)
        except ValueError:
            priority = TaskPriority.MEDIUM
        task = Task(
            id=c.id,
            title=c.title,
            description=c.description,
            priority=priority,
            tags=list(c.labels),
            created_by=c.originator,
            created_at=c.created_at,
            dependencies=list(c.dependencies),
            meta=dict(c.meta),
        )
        status = TaskStatus(_COLUMN_TO_STATUS.get(c.status.value, "open"))
        views.append(TaskView(task=task, status=status, claimed_by=c.owner))
    return views


def mirror_coord_create(home: Path, task) -> None:
    """Mirror a coord Task creation into the CardStore (best-effort)."""
    from .card import _swimlane_for_tags

    tags_lower = {t.lower() for t in task.tags}
    kind = "epic" if "epic" in tags_lower else "task"
    CardStore(home).create(CardCore(
        id=task.id,
        kind=kind,
        title=task.title,
        description=task.description,
        created_by=task.created_by,
        created_at=task.created_at,
        acceptance_criteria=list(getattr(task, "acceptance_criteria", []) or []),
        dependencies=list(task.dependencies),
        initial_priority=task.priority.value,
        initial_swimlane=_swimlane_for_tags(task.tags),
        initial_labels=list(task.tags),
        meta=dict(task.meta),
    ))


def mirror_coord_claim(home: Path, task_id: str, agent: str) -> None:
    """Mirror a coord claim into the CardStore."""
    CardStore(home).append_event(task_id, "claim", agent, owner=agent)


def mirror_coord_complete(home: Path, task_id: str, agent: str) -> None:
    """Mirror a coord completion into the CardStore."""
    CardStore(home).append_event(task_id, "complete", agent)


def mirror_coord_move(home: Path, task_id: str, column: str, agent: str,
                      order: Optional[int] = None) -> None:
    """Mirror a kanban move into the CardStore."""
    CardStore(home).append_event(task_id, "move", agent or "mcp", column=column, order=order)


def mirror_coord_archive(home: Path, task_id: str, agent: str) -> None:
    """Mirror a coord archival into the CardStore."""
    CardStore(home).append_event(task_id, "archive", agent or "archive")


# Store-served open count may lag legacy by a few cards mid-sync; anything
# beyond this is drift worth alerting on (card ba4af853 was legacy ~310 vs
# store 427).
OPEN_DRIFT_THRESHOLD = 5


def _open_count(cards: dict) -> int:
    """Count coord-board OPEN cards (what ``coord status`` reports as open).

    Open = a task/epic card, not archived, still in the backlog column.
    """
    return sum(
        1 for c in cards.values()
        if not c.archived
        and c.kind.value in ("task", "epic")
        and c.status.value == "backlog"
    )


def parity_check(home: Path, open_drift_threshold: int = OPEN_DRIFT_THRESHOLD) -> dict:
    """Diff the legacy board against the CardStore fold.

    Compares every card on (status, owner, archived, priority, swimlane), and
    computes the PARITY ALERT: whether the store-served open-count diverges
    from legacy by more than ``open_drift_threshold``.

    Returns:
        dict: ``{"checked", "matched", "mismatches", "missing",
        "open_legacy", "open_store", "open_drift", "open_drift_threshold",
        "open_alert"}``.
    """
    from .card import KanbanBoard

    store = CardStore(home)
    # Force the LEGACY projection for the comparison side, even post-cutover when
    # the flag is 1 (otherwise KanbanBoard would return the store and we would be
    # comparing the store to itself). This keeps parity a real drift detector for
    # the legacy hot-backup vs the authoritative store.
    saved = os.environ.pop("SKCOORD_CARD_STORE", None)
    try:
        legacy = {c.id: c for c in KanbanBoard(home).cards(include_archived=True)}
    finally:
        if saved is not None:
            os.environ["SKCOORD_CARD_STORE"] = saved
    stored = {c.id: c for c in store.list_cards(include_archived=True)}

    # Coarse lifecycle bucket: legacy coord can only derive todo/active/done from
    # its claim files, so kanban-native column moves (ready<->doing<->review) made
    # on the board live only in the store and must NOT read as backup drift. The
    # monitor still catches real divergence (a card done/archived in one but not
    # the other, or a different owner).
    def _bucket(status_value: str) -> str:
        return {
            "backlog": "todo", "ready": "active", "doing": "active",
            "review": "active", "done": "done",
        }.get(status_value, status_value)

    mismatches: list[dict] = []
    missing: list[str] = []
    matched = 0
    for cid, lc in legacy.items():
        sc = stored.get(cid)
        if sc is None:
            missing.append(cid)
            continue
        diff = {}
        if _bucket(lc.status.value) != _bucket(sc.status.value):
            diff["status"] = [lc.status.value, sc.status.value]
        if (lc.owner or None) != (sc.owner or None):
            diff["owner"] = [lc.owner, sc.owner]
        if lc.archived != sc.archived:
            diff["archived"] = [lc.archived, sc.archived]
        if lc.priority != sc.priority:
            diff["priority"] = [lc.priority, sc.priority]
        if lc.swimlane != sc.swimlane:
            diff["swimlane"] = [lc.swimlane, sc.swimlane]
        if diff:
            mismatches.append({"id": cid, "diff": diff})
        else:
            matched += 1
    open_legacy = _open_count(legacy)
    open_store = _open_count(stored)
    open_drift = abs(open_legacy - open_store)
    return {
        "checked": len(legacy),
        "matched": matched,
        "mismatches": mismatches,
        "missing": missing,
        "open_legacy": open_legacy,
        "open_store": open_store,
        "open_drift": open_drift,
        "open_drift_threshold": open_drift_threshold,
        "open_alert": open_drift > open_drift_threshold,
    }


def reconcile_from_legacy(home: Path, dry_run: bool = True) -> dict:
    """One-time repair: append corrective store events where the fold still
    diverges from the authoritative legacy board.

    The fold now consumes the legacy archive index and the card_events overlay
    directly, so the only residual drift is state that lives ONLY in mutable
    legacy files with no per-event timestamps: claims/completions recorded in
    ``agents/*.json`` before the mirror was enabled (status + owner). This
    walks ``parity_check`` mismatches and appends move/assign/unassign/archive
    events (writer ``reconcile``) to converge the store on legacy.

    Priority/swimlane diffs are intentionally NOT touched: the dashboard
    writes those store-only, so there legacy is the stale side.

    Additive and idempotent: pure appends, and a second run finds no diffs.

    Returns:
        dict: ``{"fixed": n}`` or ``{"would_fix": n}`` when dry_run.
    """
    par = parity_check(home)
    store = CardStore(home)
    count = 0
    for m in par["mismatches"]:
        cid = m["id"]
        diff = m["diff"]
        actions: list[tuple[str, dict]] = []
        if "archived" in diff:
            legacy_archived = diff["archived"][0]
            if legacy_archived:
                actions.append(("archive", {}))
            else:
                actions.append(("reopen", {"column": diff.get("status", [None])[0]}))
        if "status" in diff:
            legacy_col = diff["status"][0]
            if legacy_col in {c.value for c in Column}:
                actions.append(("move", {"column": legacy_col}))
        if "owner" in diff:
            legacy_owner = diff["owner"][0]
            if legacy_owner:
                actions.append(("assign", {"owner": legacy_owner}))
            else:
                actions.append(("unassign", {}))
        if not actions:
            continue
        count += 1
        if dry_run:
            continue
        for action, payload in actions:
            store.append_event(cid, action, "reconcile", **payload)
    return {"would_fix": count} if dry_run else {"fixed": count}
