"""GTD (Getting Things Done) inbox capture and management tools."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response, _shared_root

logger = logging.getLogger(__name__)

# ── GTD directory under coordination ──────────────────────────────────

_GTD_LISTS = {
    "inbox": "inbox.json",
    "next-actions": "next-actions.json",
    "projects": "projects.json",
    "waiting-for": "waiting-for.json",
    "someday-maybe": "someday-maybe.json",
}

# SPE P1.4 (card 3df69da1): ONE file-set constant for the whole store.
# archive.json is not a list you file into, but it IS part of the store: it is
# in the dedupe universe, so it must be in the lookup universe too, or an
# archived item with a source_ref is un-findable AND un-recapturable at once.
# Everything that walks the store (here, agent_run, skos adapters) reads these
# names; no module defines its own copy.
GTD_ARCHIVE = "archive"
GTD_ARCHIVE_FILE = "archive.json"
GTD_FILES = {**_GTD_LISTS, GTD_ARCHIVE: GTD_ARCHIVE_FILE}
GTD_STORE_FILES = tuple(GTD_FILES.values())

_VALID_SOURCES = {"manual", "telegram", "email", "voice", "itil"}
_VALID_PRIVACY = {"private", "team", "community", "public"}
_VALID_STATUSES = {"inbox", "next", "project", "waiting", "someday", "reference", "done"}
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}
_VALID_ENERGIES = {"high", "medium", "low"}
_VALID_STEPS = {"single", "multi"}
_DESTINATION_MAP = {
    "next": "next-actions",
    "project": "projects",
    "waiting": "waiting-for",
    "someday": "someday-maybe",
    "reference": "someday-maybe",  # reference shares someday-maybe list
    "done": "archive",
}
_STATUS_FROM_DEST = {
    "next": "next",
    "project": "project",
    "waiting": "waiting",
    "someday": "someday",
    "reference": "reference",
    "done": "done",
}


def _gtd_dir() -> Path:
    """Return the GTD directory, creating it and seed files if needed."""
    d = _shared_root() / "coordination" / "gtd"
    d.mkdir(parents=True, exist_ok=True)
    for fname in GTD_STORE_FILES:
        p = d / fname
        if not p.exists():
            p.write_text("[]", encoding="utf-8")
    return d


# ── shared locked / atomic sink ───────────────────────────────────────
# The unified GTD store has three concurrent writers: this MCP path, the
# skos.gtd_ingest sink (cron/email/itil/order adapters), and legacy tooling.
# They must serialize on ONE store lock and never leave a half-written file,
# or updates get lost / corrupted. skos already shipped that locked, atomic,
# deduped sink (skos.gtd_ingest: _store_lock / _save / capture, whole-store
# (source, source_ref) dedupe) and soft-imports our _gtd_dir the other way, so
# we soft-import its exact mechanism back here. When skos is unavailable we
# fall back to a byte-for-byte mirror keyed on the SAME .gtd.lock path, so
# cross-process mutual exclusion with skos holds either way.
try:  # pragma: no cover - both branches exercised across environments
    from skos.gtd_ingest import GtdCapture as _GtdCapture
    from skos.gtd_ingest import _save as _skos_atomic_save
    from skos.gtd_ingest import _store_lock as _skos_store_lock
    from skos.gtd_ingest import capture as _skos_capture

    _HAVE_SKOS_SINK = True
except Exception:  # skos not installed: standalone skcapstone
    _GtdCapture = None
    _skos_atomic_save = None
    _skos_store_lock = None
    _skos_capture = None
    _HAVE_SKOS_SINK = False


@contextmanager
def _store_lock():
    """Advisory flock over the whole GTD store, held across each
    load-modify-save cycle so concurrent writers (this MCP path, the skos sink,
    cron) cannot lose updates. Delegates to skos's lock when present so both
    sides share one lock object; the fallback locks the SAME .gtd.lock file, so
    mutual exclusion still holds cross-process. Not reentrant."""
    if _skos_store_lock is not None:
        with _skos_store_lock():
            yield
        return
    lock_path = _gtd_dir() / ".gtd.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path: Path, items: list[dict]) -> None:
    """Crash-safe save: write a temp file in the same dir, fsync, os.replace
    over the target, fsync the dir. The target is never truncated in place, so
    a crash leaves either the whole old file or the whole new file, never a
    partial one. Uses skos.gtd_ingest._save directly when available (every
    target lives in _gtd_dir()); otherwise mirrors it exactly."""
    if _skos_atomic_save is not None:
        _skos_atomic_save(path.name, items)
        return
    d = path.parent
    payload = json.dumps(items, indent=2, ensure_ascii=False, default=str)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


_ALL_STORE_FILES = list(GTD_STORE_FILES)


def _seen_refs() -> set[tuple[str | None, str]]:
    """All (source, source_ref) pairs already present anywhere in the store.
    Mirrors skos.gtd_ingest._seen_refs so dedupe is identical on both write
    paths. Used only by the local fallback capture (skos's capture() dedupes
    itself)."""
    seen: set[tuple[str | None, str]] = set()
    for fname in _ALL_STORE_FILES:
        try:
            items = json.loads((_gtd_dir() / fname).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            ref = it.get("source_ref")
            if ref:
                seen.add((it.get("source"), ref))
    return seen


def _load_archive() -> list[dict]:
    """Load the archive list."""
    path = _gtd_dir() / "archive.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def _save_archive(items: list[dict]) -> None:
    """Persist the archive list atomically (crash-safe; see _atomic_write_json).
    Callers must hold _store_lock() around the load-modify-save cycle."""
    _atomic_write_json(_gtd_dir() / "archive.json", items)


def _find_item_across_lists(item_id: str) -> tuple[str | None, dict | None, int | None]:
    """Find an item by ID anywhere in the store. Returns (list_name, item, index).

    The archive is searched last, so a live item always shadows a same-id
    archived one. Searching it at all is the P1.4 fix: the archive was already
    in the dedupe universe, so leaving it out of lookup made an archived item
    with a source_ref both invisible and un-recapturable.
    """
    for list_name in GTD_FILES:
        items = _load_list(list_name)
        for idx, item in enumerate(items):
            if item.get("id") == item_id:
                return list_name, item, idx
    return None, None, None


def _remove_item_from_list(list_name: str, item_id: str) -> dict | None:
    """Remove an item from a list by ID. Returns the removed item or None."""
    items = _load_list(list_name)
    for idx, item in enumerate(items):
        if item.get("id") == item_id:
            removed = items.pop(idx)
            _save_list(list_name, items)
            return removed
    return None


def _load_list(name: str) -> list[dict]:
    """Load a GTD store file by key name (``archive`` included)."""
    path = _gtd_dir() / GTD_FILES[name]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def _save_list(name: str, items: list[dict]) -> None:
    """Persist a GTD store file atomically (crash-safe; see _atomic_write_json).
    Callers must hold _store_lock() around the load-modify-save cycle."""
    _atomic_write_json(_gtd_dir() / GTD_FILES[name], items)


def _journal(action: str, item_id: str, item: dict, to: str, src: str | None = None) -> None:
    """Record one mutation in the append-only journal (SPE P1.1).

    Best-effort by design: the store write has already succeeded by the time
    this runs, so a journal failure must not turn a landed mutation into an
    error. It is logged, never swallowed silently.
    """
    try:
        from ..gtd_journal import append as _append

        _append(action, item_id, item, to=to, src=src)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GTD journal append failed for %s (%s): %s", item_id, action, exc)


def _make_item(
    text: str,
    source: str = "manual",
    privacy: str = "private",
    context: str | None = None,
    status: str = "inbox",
) -> dict:
    """Create a new GTD item with the canonical schema."""
    return {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "source": source if source in _VALID_SOURCES else "manual",
        "privacy": privacy if privacy in _VALID_PRIVACY else "private",
        "context": context,
        "priority": None,
        "energy": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status if status in _VALID_STATUSES else "inbox",
    }


# ═══════════════════════════════════════════════════════════
# Tool Definitions
# ═══════════════════════════════════════════════════════════

TOOLS: list[Tool] = [
    Tool(
        name="gtd_capture",
        description=(
            "Capture an item to the GTD inbox. Quick-add anything that needs processing "
            "later. Returns confirmation with item ID."
        ),
        inputSchema={
            "properties": {
                "context": {
                    "description": "GTD context tag, e.g. @computer, @phone, @home",
                    "type": "string",
                },
                "privacy": {
                    "description": "Privacy level (default: private)",
                    "enum": ["private", "team", "community", "public"],
                    "type": "string",
                },
                "source": {
                    "description": "Where this item came from (default: manual)",
                    "enum": ["manual", "telegram", "email", "voice"],
                    "type": "string",
                },
                "text": {"description": "The item text to capture", "type": "string"},
            },
            "required": ["text"],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_inbox",
        description=(
            "List current GTD inbox items, sorted newest first. Shows items awaiting "
            "clarification and processing."
        ),
        inputSchema={
            "properties": {
                "limit": {
                    "description": "Maximum items to return (default: 20)",
                    "type": "integer",
                }
            },
            "required": [],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_status",
        description=(
            "Summary of all GTD lists: inbox count, next-actions count, projects count, "
            "waiting-for count, someday-maybe count."
        ),
        inputSchema={"properties": {}, "required": [], "type": "object"},
    ),
    Tool(
        name="gtd_clarify",
        description=(
            "Clarify and organize a GTD inbox item. Determines whether the item is "
            "actionable, single/multi-step, and routes it to the appropriate list."
        ),
        inputSchema={
            "properties": {
                "actionable": {"description": "Is this item actionable?", "type": "boolean"},
                "context": {
                    "description": "GTD context tag, e.g. @computer, @phone, @home",
                    "type": "string",
                },
                "delegate_to": {"description": "Person or agent to delegate to", "type": "string"},
                "energy": {
                    "description": "Energy level required",
                    "enum": ["high", "medium", "low"],
                    "type": "string",
                },
                "item_id": {"description": "ID of the inbox item to clarify", "type": "string"},
                "priority": {
                    "description": "Priority level",
                    "enum": ["critical", "high", "medium", "low"],
                    "type": "string",
                },
                "steps": {
                    "description": "Single action or multi-step project",
                    "enum": ["single", "multi"],
                    "type": "string",
                },
            },
            "required": ["item_id", "actionable"],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_move",
        description="Manually move a GTD item from its current list to another list.",
        inputSchema={
            "properties": {
                "destination": {
                    "description": "Destination list",
                    "enum": ["next", "project", "waiting", "someday", "reference", "done"],
                    "type": "string",
                },
                "item_id": {"description": "ID of the item to move", "type": "string"},
            },
            "required": ["item_id", "destination"],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_done",
        description=(
            "Mark any GTD item as done regardless of which list it is in. Moves it to the "
            "archive with a completed_at timestamp."
        ),
        inputSchema={
            "properties": {
                "item_id": {"description": "ID of the item to mark as done", "type": "string"}
            },
            "required": ["item_id"],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_reopen",
        description=(
            "Reopen an archived GTD item, restoring it to the list it came from under its "
            "ORIGINAL id. The undo for gtd_done: recorded as a reversing event, never an "
            "edit of history."
        ),
        inputSchema={
            "properties": {
                "item_id": {"description": "ID of the archived item to reopen", "type": "string"},
                "destination": {
                    "description": (
                        "Where to restore it. Defaults to the list it was archived from."
                    ),
                    "enum": ["next", "project", "waiting", "someday", "reference"],
                    "type": "string",
                },
            },
            "required": ["item_id"],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_review",
        description=(
            "Generate a GTD weekly review summary. Shows counts per list, oldest items, "
            "longest-waiting items, and stale projects."
        ),
        inputSchema={"properties": {}, "required": [], "type": "object"},
    ),
    Tool(
        name="gtd_next",
        description=(
            "View next actions filtered by context, energy level, and/or priority. Returns a "
            "sorted list (highest priority first, then oldest first)."
        ),
        inputSchema={
            "properties": {
                "context": {
                    "description": "Filter by GTD context tag, e.g. @computer, @phone, @home",
                    "type": "string",
                },
                "energy": {
                    "description": "Filter by energy level required",
                    "enum": ["high", "medium", "low"],
                    "type": "string",
                },
                "limit": {
                    "description": "Maximum items to return (default: 10)",
                    "type": "integer",
                },
                "priority": {
                    "description": "Filter by priority level",
                    "enum": ["critical", "high", "medium", "low"],
                    "type": "string",
                },
            },
            "required": [],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_projects",
        description=(
            "View GTD projects with their status. Can filter by active or stale (no activity "
            "in 7+ days). Shows the next action for each project."
        ),
        inputSchema={
            "properties": {
                "limit": {
                    "description": "Maximum items to return (default: 10)",
                    "type": "integer",
                },
                "status": {
                    "description": "Filter by project status (default: all)",
                    "enum": ["active", "stale", "all"],
                    "type": "string",
                },
            },
            "required": [],
            "type": "object",
        },
    ),
    Tool(
        name="gtd_waiting",
        description=(
            "View waiting-for items sorted by longest waiting first. Shows who/what you are "
            "waiting on and how long."
        ),
        inputSchema={
            "properties": {
                "limit": {
                    "description": "Maximum items to return (default: 10)",
                    "type": "integer",
                }
            },
            "required": [],
            "type": "object",
        },
    ),
]


# ═══════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_gtd_capture(args: dict) -> list[TextContent]:
    """Capture an item to the GTD inbox through the shared locked/atomic sink.

    Prefer skos.gtd_ingest.capture() (Option A): one call does whole-store
    (source, source_ref) dedupe + flock + atomic save, so the MCP path and the
    cron/email/itil adapters share exactly one sink. When skos is unavailable,
    fall back to a local mirror under the SAME store lock. Dedupe only engages
    when a source_ref is supplied; manual quick-adds without one are always
    captured (unchanged behavior)."""
    text = args.get("text", "").strip()
    if not text:
        return _error_response("text is required")

    source = args.get("source", "manual")
    source = source if source in _VALID_SOURCES else "manual"
    privacy = args.get("privacy", "private")
    privacy = privacy if privacy in _VALID_PRIVACY else "private"
    context = args.get("context")
    source_ref = (args.get("source_ref") or "").strip()

    if _HAVE_SKOS_SINK:
        new_id = _skos_capture(
            _GtdCapture(
                text=text,
                source=source,
                source_ref=source_ref,
                context=context,
                privacy=privacy,
                status="inbox",
            )
        )
        inbox = _load_list("inbox")
        if new_id is None:  # duplicate (source, source_ref) already in store
            return _json_response(
                {
                    "captured": False,
                    "duplicate": True,
                    "source": source,
                    "source_ref": source_ref,
                    "inbox_count": len(inbox),
                }
            )
        item = next((it for it in inbox if it.get("id") == new_id), {})
        _journal("capture", new_id, item, to="inbox")
        return _json_response(
            {
                "captured": True,
                "id": new_id,
                "text": item.get("text") or text,
                "source": item.get("source", source),
                "privacy": item.get("privacy", privacy),
                "context": item.get("context", context),
                "created_at": item.get("created_at", ""),
                "inbox_count": len(inbox),
            }
        )

    # Fallback: skos not installed. Local locked, atomic, deduped write.
    with _store_lock():
        if source_ref and (source, source_ref) in _seen_refs():
            inbox = _load_list("inbox")
            return _json_response(
                {
                    "captured": False,
                    "duplicate": True,
                    "source": source,
                    "source_ref": source_ref,
                    "inbox_count": len(inbox),
                }
            )
        item = _make_item(text=text, source=source, privacy=privacy, context=context)
        if source_ref:
            item["source_ref"] = source_ref
        inbox = _load_list("inbox")
        inbox.append(item)
        _save_list("inbox", inbox)
        _journal("capture", item["id"], item, to="inbox")

    return _json_response(
        {
            "captured": True,
            "id": item["id"],
            "text": item.get("text") or item.get("title") or "",
            "source": item["source"],
            "privacy": item["privacy"],
            "context": item["context"],
            "created_at": item["created_at"],
            "inbox_count": len(inbox),
        }
    )


async def _handle_gtd_inbox(args: dict) -> list[TextContent]:
    """List current inbox items, newest first."""
    limit = args.get("limit", 20)
    if not isinstance(limit, int) or limit < 1:
        limit = 20

    inbox = _load_list("inbox")
    # Sort newest first by created_at
    inbox.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    items = inbox[:limit]

    return _json_response(
        {
            "items": items,
            "total": len(inbox),
            "showing": len(items),
        }
    )


async def _handle_gtd_status(_args: dict) -> list[TextContent]:
    """Summary counts across all GTD lists."""
    counts = {}
    for list_name in _GTD_LISTS:
        items = _load_list(list_name)
        counts[list_name] = len(items)

    return _json_response(
        {
            "counts": counts,
            "total": sum(counts.values()),
            "gtd_dir": str(_gtd_dir()),
        }
    )


async def _handle_gtd_clarify(args: dict) -> list[TextContent]:
    """Clarify an inbox item and route it to the appropriate GTD list."""
    item_id = args.get("item_id", "").strip()
    if not item_id:
        return _error_response("item_id is required")

    actionable = args.get("actionable", False)
    steps = args.get("steps", "single")
    context = args.get("context")
    priority = args.get("priority", "medium")
    energy = args.get("energy", "medium")
    delegate_to = args.get("delegate_to")

    # Validate enum values
    if steps not in _VALID_STEPS:
        steps = "single"
    if priority not in _VALID_PRIORITIES:
        priority = "medium"
    if energy not in _VALID_ENERGIES:
        energy = "medium"

    # Whole find-move-save cycle under the shared store lock so a concurrent
    # skos-sink / cron write cannot be lost between our load and save.
    with _store_lock():
        # Find the item in the inbox
        inbox = _load_list("inbox")
        item = None
        for idx, it in enumerate(inbox):
            if it.get("id") == item_id:
                item = inbox.pop(idx)
                break

        if item is None:
            return _error_response(f"Item '{item_id}' not found in inbox")

        # Update item fields
        item["context"] = context or item.get("context")
        item["priority"] = priority
        item["energy"] = energy
        item["clarified_at"] = datetime.now(timezone.utc).isoformat()

        # Route based on clarification
        if actionable and delegate_to:
            # Delegated → waiting-for
            item["status"] = "waiting"
            item["delegate_to"] = delegate_to
            dest_name = "waiting-for"
            dest_list = _load_list("waiting-for")
            dest_list.append(item)
            _save_list("waiting-for", dest_list)
        elif actionable and steps == "multi":
            # Multi-step → projects
            item["status"] = "project"
            dest_name = "projects"
            dest_list = _load_list("projects")
            dest_list.append(item)
            _save_list("projects", dest_list)
        elif actionable:
            # Single action → next-actions
            item["status"] = "next"
            dest_name = "next-actions"
            dest_list = _load_list("next-actions")
            dest_list.append(item)
            _save_list("next-actions", dest_list)
        else:
            # Not actionable → someday-maybe
            item["status"] = "someday"
            dest_name = "someday-maybe"
            dest_list = _load_list("someday-maybe")
            dest_list.append(item)
            _save_list("someday-maybe", dest_list)

        # Save updated inbox (item removed)
        _save_list("inbox", inbox)
        _journal("clarify", item_id, item, to=dest_name, src="inbox")

    return _json_response(
        {
            "clarified": True,
            "id": item["id"],
            "text": item.get("text") or item.get("title") or "",
            "destination": dest_name,
            "status": item["status"],
            "priority": item.get("priority"),
            "energy": item.get("energy"),
            "context": item.get("context"),
            "delegate_to": item.get("delegate_to"),
        }
    )


async def _handle_gtd_move(args: dict) -> list[TextContent]:
    """Move a GTD item from its current list to a new destination."""
    item_id = args.get("item_id", "").strip()
    destination = args.get("destination", "").strip()

    if not item_id:
        return _error_response("item_id is required")
    if destination not in _DESTINATION_MAP:
        return _error_response(
            f"Invalid destination '{destination}'. "
            f"Valid: {', '.join(sorted(_DESTINATION_MAP.keys()))}"
        )

    # Whole find-remove-add cycle under the shared store lock so a concurrent
    # writer cannot lose the source-list update or the destination append.
    with _store_lock():
        # Find the item across all lists
        source_list, item, _ = _find_item_across_lists(item_id)
        if source_list is None or item is None:
            return _error_response(f"Item '{item_id}' not found in any GTD list")

        # Update status
        item["status"] = _STATUS_FROM_DEST[destination]
        item["moved_at"] = datetime.now(timezone.utc).isoformat()

        # P1.3: destination first, source second. A crash in the window between
        # the two saves must leave a recoverable duplicate, never a hole.
        if destination == "done":
            item["completed_at"] = datetime.now(timezone.utc).isoformat()
            dest_name = GTD_ARCHIVE
        else:
            dest_name = _DESTINATION_MAP[destination]
        dest_list = _load_list(dest_name)
        dest_list.append(item)
        _save_list(dest_name, dest_list)

        # Remove from source
        _remove_item_from_list(source_list, item_id)
        _journal("move", item_id, item, to=dest_name, src=source_list)

    return _json_response(
        {
            "moved": True,
            "id": item["id"],
            "text": item.get("text") or item.get("title") or "",
            "from": source_list,
            "to": dest_name,
            "status": item["status"],
        }
    )


async def _handle_gtd_done(args: dict) -> list[TextContent]:
    """Mark any GTD item as done and move it to the archive."""
    item_id = args.get("item_id", "").strip()
    if not item_id:
        return _error_response("item_id is required")

    # Whole find-remove-archive cycle under the shared store lock so a
    # concurrent writer cannot lose the source-list or archive update.
    with _store_lock():
        # Find the item across all lists
        source_list, item, _ = _find_item_across_lists(item_id)
        if source_list is None or item is None:
            return _error_response(f"Item '{item_id}' not found in any GTD list")
        if source_list == GTD_ARCHIVE:
            # P1.4 made the archive visible to lookup; done must not re-archive
            # an already-archived item into a duplicate.
            return _error_response(f"Item '{item_id}' is already archived")

        # Mark done and archive. P1.3: archive first, THEN drop the source, so a
        # crash between the two saves duplicates the item instead of losing it.
        item["status"] = "done"
        item["completed_at"] = datetime.now(timezone.utc).isoformat()

        archive = _load_list(GTD_ARCHIVE)
        archive.append(item)
        _save_list(GTD_ARCHIVE, archive)

        # Remove from source
        _remove_item_from_list(source_list, item_id)
        _journal("done", item_id, item, to=GTD_ARCHIVE, src=source_list)

    return _json_response(
        {
            "done": True,
            "id": item["id"],
            "text": item.get("text") or item.get("title") or "",
            "from": source_list,
            "completed_at": item["completed_at"],
            "archive_count": len(archive),
        }
    )


async def _handle_gtd_reopen(args: dict) -> list[TextContent]:
    """Restore an archived item to its prior list, under its original id.

    SPE P1.2 (card 0ef48ec9). ``done`` used to be a one-way door: recovery meant
    hand-reading archive.json and recapturing under a NEW id, orphaning every
    reference to the old one. This is the reversal, and it reverses the SPE way:
    one more appended event, no stored event edited, no history deleted.

    The destination is the list the item came from per the journal (P1.1). If
    the journal has no record (the item predates it), the item's own status is
    used, and failing that the inbox. An explicit ``destination`` overrides.
    """
    item_id = args.get("item_id", "").strip()
    if not item_id:
        return _error_response("item_id is required")

    destination = (args.get("destination") or "").strip()
    if destination and destination not in _DESTINATION_MAP:
        return _error_response(
            f"Invalid destination '{destination}'. "
            f"Valid: {', '.join(sorted(k for k in _DESTINATION_MAP if k != 'done'))}"
        )
    if destination == "done":
        return _error_response("Cannot reopen an item to 'done'")

    with _store_lock():
        source_list, item, _ = _find_item_across_lists(item_id)
        if source_list is None or item is None:
            return _error_response(f"Item '{item_id}' not found in the GTD store")
        if source_list != GTD_ARCHIVE:
            return _error_response(f"Item '{item_id}' is not archived (it is in {source_list})")

        dest_name = _DESTINATION_MAP[destination] if destination else _prior_list_for(item)

        item["status"] = _STATUS_FROM_DEST.get(destination) or _status_for_list(dest_name)
        item.pop("completed_at", None)
        item["reopened_at"] = datetime.now(timezone.utc).isoformat()

        # Destination first, archive second (P1.3): a crash duplicates.
        dest_list = _load_list(dest_name)
        dest_list.append(item)
        _save_list(dest_name, dest_list)
        _remove_item_from_list(GTD_ARCHIVE, item_id)
        _journal("reopen", item_id, item, to=dest_name, src=GTD_ARCHIVE)

    return _json_response(
        {
            "reopened": True,
            "id": item["id"],
            "text": item.get("text") or item.get("title") or "",
            "to": dest_name,
            "status": item["status"],
            "reopened_at": item["reopened_at"],
        }
    )


# Which list a status belongs in, for restoring an item whose journal entry is
# missing. The inverse of _STATUS_FROM_DEST via _DESTINATION_MAP.
_LIST_FOR_STATUS = {
    "inbox": "inbox",
    "next": "next-actions",
    "project": "projects",
    "waiting": "waiting-for",
    "someday": "someday-maybe",
    "reference": "someday-maybe",
}
_STATUS_FOR_LIST = {
    "inbox": "inbox",
    "next-actions": "next",
    "projects": "project",
    "waiting-for": "waiting",
    "someday-maybe": "someday",
}


def _status_for_list(list_name: str) -> str:
    """The item status that belongs with a list."""
    return _STATUS_FOR_LIST.get(list_name, "inbox")


def _prior_list_for(item: dict) -> str:
    """Where an archived item should go back to.

    The journal is authoritative: the event that archived it recorded which
    list it left. Items archived before the journal existed fall back to the
    status they carried, and finally to the inbox.
    """
    item_id = item.get("id", "")
    try:
        from ..gtd_journal import read_all

        for e in reversed(read_all()):
            if e.get("item_id") != item_id:
                continue
            src = e.get("from")
            if e.get("action") in ("done", "move") and src in _GTD_LISTS:
                return src
    except Exception as exc:  # noqa: BLE001
        logger.warning("GTD journal unreadable while reopening %s: %s", item_id, exc)

    prior_status = item.get("prior_status") or item.get("status")
    if prior_status in _LIST_FOR_STATUS and prior_status != "done":
        return _LIST_FOR_STATUS[prior_status]
    return "inbox"


async def _handle_gtd_review(_args: dict) -> list[TextContent]:
    """Generate a GTD weekly review summary."""
    now = datetime.now(timezone.utc)
    review: dict = {"generated_at": now.isoformat(), "counts": {}, "total": 0}

    # Counts per list
    all_items: dict[str, list[dict]] = {}
    for list_name in _GTD_LISTS:
        items = _load_list(list_name)
        all_items[list_name] = items
        review["counts"][list_name] = len(items)
        review["total"] += len(items)

    # Archive count
    archive = _load_archive()
    review["counts"]["archive"] = len(archive)

    # Oldest items across all lists (top 5)
    every_item = []
    for list_name, items in all_items.items():
        for it in items:
            every_item.append({**it, "_list": list_name})

    every_item.sort(key=lambda x: x.get("created_at", ""))
    review["oldest_items"] = [
        {
            "id": it["id"],
            "text": it.get("text", "")[:60],
            "list": it["_list"],
            "created_at": it.get("created_at", ""),
        }
        for it in every_item[:5]
    ]

    # Items waiting longest (from waiting-for)
    waiting = all_items.get("waiting-for", [])
    waiting_sorted = sorted(waiting, key=lambda x: x.get("created_at", ""))
    review["longest_waiting"] = [
        {
            "id": it["id"],
            "text": it.get("text", "")[:60],
            "delegate_to": it.get("delegate_to", ""),
            "created_at": it.get("created_at", ""),
        }
        for it in waiting_sorted[:5]
    ]

    # Stale projects (no activity in 7+ days)
    projects = all_items.get("projects", [])
    stale = []
    for proj in projects:
        last_touch = proj.get("moved_at") or proj.get("clarified_at") or proj.get("created_at", "")
        if last_touch:
            try:
                ts = datetime.fromisoformat(last_touch.replace("Z", "+00:00"))
                age_days = (now - ts).days
                if age_days >= 7:
                    stale.append(
                        {
                            "id": proj["id"],
                            "text": proj.get("text", "")[:60],
                            "days_stale": age_days,
                        }
                    )
            except (ValueError, TypeError):
                pass
    stale.sort(key=lambda x: x["days_stale"], reverse=True)
    review["stale_projects"] = stale[:5]

    # Inbox items needing clarification
    review["inbox_needs_clarify"] = review["counts"].get("inbox", 0)

    return _json_response(review)


_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


async def _handle_gtd_next(args: dict) -> list[TextContent]:
    """View next actions filtered by context, energy, and/or priority."""
    context_filter = args.get("context")
    energy_filter = args.get("energy")
    priority_filter = args.get("priority")
    limit = args.get("limit", 10)
    if not isinstance(limit, int) or limit < 1:
        limit = 10

    items = _load_list("next-actions")

    # Apply filters
    if context_filter:
        items = [it for it in items if it.get("context") == context_filter]
    if energy_filter:
        if energy_filter not in _VALID_ENERGIES:
            return _error_response(
                f"Invalid energy '{energy_filter}'. Valid: {', '.join(sorted(_VALID_ENERGIES))}"
            )
        items = [it for it in items if it.get("energy") == energy_filter]
    if priority_filter:
        if priority_filter not in _VALID_PRIORITIES:
            return _error_response(
                f"Invalid priority '{priority_filter}'. Valid: {', '.join(sorted(_VALID_PRIORITIES))}"  # noqa: E501
            )
        items = [it for it in items if it.get("priority") == priority_filter]

    # Sort: priority (critical > high > medium > low), then oldest first
    items.sort(
        key=lambda x: (
            _PRIORITY_ORDER.get(x.get("priority", "low"), 3),
            x.get("created_at", ""),
        )
    )

    total = len(items)
    items = items[:limit]

    return _json_response(
        {
            "items": items,
            "total": total,
            "showing": len(items),
            "filters": {
                "context": context_filter,
                "energy": energy_filter,
                "priority": priority_filter,
            },
        }
    )


async def _handle_gtd_projects(args: dict) -> list[TextContent]:
    """View GTD projects filtered by status."""
    status_filter = args.get("status", "all")
    limit = args.get("limit", 10)
    if not isinstance(limit, int) or limit < 1:
        limit = 10
    if status_filter not in ("active", "stale", "all"):
        status_filter = "all"

    now = datetime.now(timezone.utc)
    projects = _load_list("projects")

    result_items = []
    for proj in projects:
        last_touch = proj.get("moved_at") or proj.get("clarified_at") or proj.get("created_at", "")
        days_since = None
        is_stale = False
        if last_touch:
            try:
                ts = datetime.fromisoformat(last_touch.replace("Z", "+00:00"))
                days_since = (now - ts).days
                is_stale = days_since >= 7
            except (ValueError, TypeError):
                pass

        proj_status = "stale" if is_stale else "active"

        if status_filter != "all" and proj_status != status_filter:
            continue

        result_items.append(
            {
                "id": proj.get("id", ""),
                "text": proj.get("text", ""),
                "status": proj_status,
                "priority": proj.get("priority"),
                "energy": proj.get("energy"),
                "context": proj.get("context"),
                "days_since_activity": days_since,
                "created_at": proj.get("created_at", ""),
                "next_action": proj.get("text", "")[:60],
            }
        )

    total = len(result_items)
    result_items = result_items[:limit]

    return _json_response(
        {
            "projects": result_items,
            "total": total,
            "showing": len(result_items),
            "filter": status_filter,
        }
    )


async def _handle_gtd_waiting(args: dict) -> list[TextContent]:
    """View waiting-for items sorted by longest waiting."""
    limit = args.get("limit", 10)
    if not isinstance(limit, int) or limit < 1:
        limit = 10

    now = datetime.now(timezone.utc)
    items = _load_list("waiting-for")

    # Sort oldest first (longest waiting)
    items.sort(key=lambda x: x.get("created_at", ""))

    result_items = []
    for it in items:
        created = it.get("created_at", "")
        waiting_days = None
        if created:
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                waiting_days = (now - ts).days
            except (ValueError, TypeError):
                pass

        result_items.append(
            {
                "id": it.get("id", ""),
                "text": it.get("text", ""),
                "delegate_to": it.get("delegate_to", ""),
                "context": it.get("context"),
                "priority": it.get("priority"),
                "created_at": created,
                "waiting_days": waiting_days,
                "waiting_since": (
                    f"{waiting_days} day(s)" if waiting_days is not None else "unknown"
                ),
            }
        )

    total = len(result_items)
    result_items = result_items[:limit]

    return _json_response(
        {
            "items": result_items,
            "total": total,
            "showing": len(result_items),
        }
    )


HANDLERS: dict = {
    "gtd_capture": _handle_gtd_capture,
    "gtd_inbox": _handle_gtd_inbox,
    "gtd_status": _handle_gtd_status,
    "gtd_clarify": _handle_gtd_clarify,
    "gtd_move": _handle_gtd_move,
    "gtd_done": _handle_gtd_done,
    "gtd_reopen": _handle_gtd_reopen,
    "gtd_review": _handle_gtd_review,
    "gtd_next": _handle_gtd_next,
    "gtd_projects": _handle_gtd_projects,
    "gtd_waiting": _handle_gtd_waiting,
}
