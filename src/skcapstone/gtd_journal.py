"""Append-only per-writer journal for the unified GTD store (SPE P1.1).

Card ``3d927cda``, sprint ``83482526``, epic ``373a33ca``. The GTD store keeps
its state in flat JSON lists and nothing else, so a mutation left no record:
nothing said who moved an item, out of which list, or when. Reopen (P1.2) and
attribution (P2) both need that record first.

The shape is lifted from ``skcoord.card_store``, which has run it since the
July-13 ITIL refactor and survives Syncthing by construction:

* one file per writer, ``journal/<agent>@<host>.jsonl``, so two processes never
  append to the same file and a sync merge is a union, never a conflict;
* appends only, guarded by ``flock`` for the read-seq-then-append window;
* a deterministic read order of ``(ts, writer, seq)``.

Every event carries the item's POST-state and the list it landed in, so the
fold is simply "drop this id everywhere, then place the item where the event
says". That makes :func:`fold` reproduce the live lists exactly, and it makes a
reversal just one more event rather than an edit to an old one.

Writer identity here is the ``SKAGENT`` name. Resolving it through
``capauth.resolve_agent_identity`` and signing the envelope is P2/P3 work; P1
deliberately carries no crypto dependency so the incident fix ships alone.

Scope: this journals the skcapstone GTD write paths (the MCP handlers and the
CLI that calls them). Adapters writing straight into ``skos.gtd_ingest`` do not
appear here yet; enumerating every writer is P4.
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

logger = logging.getLogger(__name__)

JOURNAL_DIRNAME = "journal"

_HOSTNAME = socket.gethostname()

# Mutations worth a line. A read never writes one.
ACTIONS = ("capture", "clarify", "move", "done", "reopen")


def _now_iso() -> str:
    """UTC now as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def writer_name() -> str:
    """The acting agent, per the standard SKAGENT precedence."""
    for var in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return "unknown"


def writer_id(writer: str | None = None) -> str:
    """The per-writer file stem, ``<agent>@<host>``."""
    safe = (writer or writer_name()).replace("/", "-").replace("@", "-")
    return f"{safe}@{_HOSTNAME}"


def journal_dir() -> Path:
    """Return (and create) the journal directory beside the GTD lists."""
    from .mcp_tools.gtd_tools import _gtd_dir

    d = _gtd_dir() / JOURNAL_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def append(
    action: str,
    item_id: str,
    item: dict,
    to: str,
    src: str | None = None,
    writer: str | None = None,
) -> dict:
    """Append one mutation event to this writer's own log.

    Args:
        action: One of :data:`ACTIONS`.
        item_id: The GTD item's id.
        item: The item's state AFTER the mutation.
        to: The store list the item now lives in (``archive`` included).
        src: The list it came from, if any. ``None`` for a fresh capture.
        writer: Override the acting agent (defaults to :func:`writer_name`).

    Returns:
        dict: the event as written.

    Callers append AFTER the store write has succeeded and while still holding
    the store lock, so the journal never claims a mutation that did not land.
    """
    path = journal_dir() / f"{writer_id(writer)}.jsonl"
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            seq = sum(1 for _ in fh)
            event = {
                "event_id": uuid.uuid4().hex,
                "ts": _now_iso(),
                "writer": writer or writer_name(),
                "node": _HOSTNAME,
                "seq": seq,
                "action": action,
                "item_id": item_id,
                "from": src,
                "to": to,
                "item": item,
            }
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(event, default=str) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return event


def read_all() -> list[dict]:
    """Every event across all writers, in ``(ts, writer, seq)`` order.

    A line that will not parse is skipped, not fatal: a torn tail on one
    writer's file must not make the whole history unreadable.
    """
    out: list[dict] = []
    d = journal_dir()
    for f in sorted(d.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("Skipping unreadable journal %s: %s", f.name, exc)
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed journal line in %s", f.name)
    out.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
    return out


def fold(events: list[dict] | None = None) -> dict[str, list[dict]]:
    """Replay the journal into ``{list_name: [items]}``.

    Each event is "this id is now HERE, in this state", so replaying is a
    remove-everywhere then append. The result matches the live store for any
    history whose mutations all went through the journal.
    """
    from .mcp_tools.gtd_tools import GTD_FILES

    state: dict[str, list[dict]] = {name: [] for name in GTD_FILES}
    for e in read_all() if events is None else events:
        item_id = e.get("item_id")
        if not item_id:
            continue
        for items in state.values():
            for idx, it in enumerate(items):
                if it.get("id") == item_id:
                    items.pop(idx)
                    break
        dest = e.get("to")
        if dest in state and isinstance(e.get("item"), dict):
            state[dest].append(e["item"])
    return state


def last_event_for(item_id: str) -> dict | None:
    """The most recent event touching one item, or None."""
    for e in reversed(read_all()):
        if e.get("item_id") == item_id:
            return e
    return None
