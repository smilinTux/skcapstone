"""Single-owner GTD writes for hosts without skos (the chi fleet).

The GTD lists are whole-file JSON in a Syncthing-shared folder. flock and an
atomic rename only serialize writers on ONE host, so when two hosts each
load-modify-save ``inbox.json`` inside the sync window Syncthing keeps one
version and parks the other as a ``.sync-conflict`` copy, and every item only
the loser had drops out of the store.

skos fixed that (smilinTux/skos#35, #37): the host named in the synced
``OWNER`` file is the only one that rewrites the lists. Every other host
appends its ops to its own ``spool/<host>.jsonl`` (one writer per file,
append-only, so a sync is a plain copy and never conflicts) and the owner folds
the spools in under the store lock. This module is the same mechanism for
standalone skcapstone, with the SAME on-disk record format, spool location and
offsets file, so a skos owner can drain a skcapstone spool and the other way
round, and installing skos on a host later changes nothing on disk.

Records (one JSON object per line)::

    {"v": 1, "op": "capture"|"upsert", "id", "host", "ts", "capture": {...}}
    {"v": 1, "op": "clarify"|"move"|"done"|"reopen", "id", "host", "ts",
     "mutation": {"to", "src": [...] | null, "patch": {...}, "drop": [...]}}
    {"v": 1, "op": "append", "id", "host", "ts", "to", "item": {...}}

Replay is idempotent: captures skip a known id or (source, source_ref), appends
skip a known id (and, here, a known (source, source_ref)), mutations skip an
item that is missing, outside ``src``, already in place, or stamped at or after
the record's ``ts``. With no OWNER file, every host writes directly as before.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

OWNER_FILENAME = "OWNER"
SPOOL_DIRNAME = "spool"
MUTATION_OPS = ("clarify", "move", "done", "reopen")
_ALL_FILES = (
    "inbox.json",
    "next-actions.json",
    "projects.json",
    "waiting-for.json",
    "someday-maybe.json",
    "archive.json",
)
LIST_NAMES = tuple(f[: -len(".json")] for f in _ALL_FILES)
_MUTATION_STAMPS = ("clarified_at", "moved_at", "completed_at", "reopened_at", "updated_at")
# status -> list file, as in skos.gtd_ingest._LIST_FILE
_LIST_FILE = {
    "inbox": "inbox.json",
    "next": "next-actions.json",
    "project": "projects.json",
    "waiting": "waiting-for.json",
    "someday": "someday-maybe.json",
    "reference": "someday-maybe.json",
}


@dataclass
class GtdCapture:
    """Field-for-field the skos.gtd_ingest.GtdCapture record, so spooled
    captures written by either implementation replay on the other."""

    text: str
    source: str
    source_ref: str
    context: str = "@inbox"
    priority: str | None = None
    privacy: str = "private"
    status: str = "inbox"
    delegate_to: str | None = None
    meta: dict = field(default_factory=dict)


class CorruptStoreError(RuntimeError):
    """A store list is not valid JSON; the owner refuses to rewrite it."""


def _tools():
    from .mcp_tools import gtd_tools  # local: gtd_tools imports this module

    return gtd_tools


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── store location, owner, host ──────────────────────────────────────


def gtd_dir() -> Path:
    """Same precedence as skos.gtd_ingest.gtd_dir: SK_GTD_DIR, else skcapstone's."""
    env = os.environ.get("SK_GTD_DIR")
    if env:
        d = Path(env).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _tools()._gtd_dir()


def _hostname() -> str:
    return os.environ.get("SK_GTD_HOST") or socket.gethostname()


def owner_host() -> str | None:
    """The one host allowed to rewrite the store lists, or None (any host)."""
    env = (os.environ.get("SK_GTD_OWNER_HOST") or "").strip()
    if env:
        return env
    try:
        raw = (gtd_dir() / OWNER_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    return None


def is_owner() -> bool:
    """True when this host may write the store lists directly."""
    owner = owner_host()
    if owner is None:
        return True
    me = _hostname()
    return me == owner or me.split(".", 1)[0] == owner.split(".", 1)[0]


# ── store I/O ────────────────────────────────────────────────────────


def _load(fname: str) -> list[dict]:
    """A store list. Missing = empty; corrupt raises, so a drain never
    rewrites a file it could not read."""
    p = gtd_dir() / fname
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptStoreError(f"{p}: {exc}") from exc
    if not isinstance(data, list):
        raise CorruptStoreError(f"{p}: expected a JSON list, got {type(data).__name__}")
    return data


def _save(fname: str, items: list[dict]) -> None:
    _tools()._atomic_write_json(gtd_dir() / fname, items)


def _list_file(name: str) -> str:
    if name not in LIST_NAMES:
        raise ValueError(f"unknown GTD list {name!r} (valid: {', '.join(LIST_NAMES)})")
    return f"{name}.json"


def _all_ids() -> set[str]:
    return {it.get("id") for fname in _ALL_FILES for it in _load(fname)}


def _seen_refs() -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    for fname in _ALL_FILES:
        for it in _load(fname):
            ref = it.get("source_ref")
            if ref:
                seen.add((it.get("source"), ref))
    return seen


def _locate(item_id: str) -> tuple[str | None, int | None, dict | None, list | None]:
    """(list name, index, item, items) for ``item_id``; live lists shadow archive."""
    for fname in _ALL_FILES:
        items = _load(fname)
        for idx, it in enumerate(items):
            if it.get("id") == item_id:
                return fname[: -len(".json")], idx, it, items
    return None, None, None, None


def _find_by_ref(source: str, source_ref: str):
    for fname in _ALL_FILES:
        items = _load(fname)
        for idx, it in enumerate(items):
            if it.get("source") == source and it.get("source_ref") == source_ref:
                return fname, idx, it, items
    return None, None, None, None


# ── spool (non-owner side) ───────────────────────────────────────────


def _spool_path(host: str | None = None) -> Path:
    d = gtd_dir()
    _tools()._guard_estate(d)
    sp = d / SPOOL_DIRNAME
    sp.mkdir(parents=True, exist_ok=True)
    return sp / f"{(host or _hostname()).replace('/', '-')}.jsonl"


def _read_spool(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    """Complete records from ``offset`` on, plus the offset after the last one.
    A trailing line without a newline is still being written (or synced) and
    is left for the next pass."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    records: list[dict] = []
    consumed = 0
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        consumed += len(line)
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            logger.error("gtd spool: skipping unparseable line in %s", path)
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, offset + consumed


def _spool_record(rec: dict) -> None:
    """Append one complete record (one line) to this host's spool, fsynced."""
    path = _spool_path()
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    logger.info(
        "gtd store: %s spooled %s for owner %s", rec.get("op"), rec.get("id"), owner_host()
    )


def _spooled_refs() -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for rec in _read_spool(_spool_path())[0]:
        cap = rec.get("capture") or {}
        if rec.get("op") in ("capture", "upsert") and cap.get("source_ref"):
            refs.add((cap.get("source"), cap["source_ref"]))
        item = rec.get("item") or {}
        if rec.get("op") == "append" and item.get("source_ref"):
            refs.add((item.get("source"), item["source_ref"]))
    return refs


def spool_mutation(
    action: str,
    item_id: str,
    *,
    to: str,
    src: list[str] | None = None,
    patch: dict | None = None,
    drop: list[str] | tuple = (),
    ts: str | None = None,
) -> dict:
    """Queue a clarify/move/done/reopen of ``item_id`` for the owner."""
    if action not in MUTATION_OPS:
        raise ValueError(f"unknown GTD mutation {action!r} (valid: {', '.join(MUTATION_OPS)})")
    _list_file(to)
    for name in src or ():
        _list_file(name)
    rec = {
        "v": 1,
        "op": action,
        "id": item_id,
        "host": _hostname(),
        "ts": ts or _now(),
        "mutation": {
            "to": to,
            "src": list(src) if src is not None else None,
            "patch": dict(patch or {}),
            "drop": list(drop),
        },
    }
    _spool_record(rec)
    return rec


def spool_append(item: dict, to: str) -> dict:
    """Queue a whole, already-built item (with its final id) for list ``to``."""
    _list_file(to)
    if not item.get("id"):
        raise ValueError("spool_append needs an item with an id")
    rec = {
        "v": 1,
        "op": "append",
        "id": item["id"],
        "host": _hostname(),
        "ts": _now(),
        "to": to,
        "item": dict(item),
    }
    _spool_record(rec)
    return rec


def _spool_capture(op: str, c: GtdCapture, item_id: str) -> None:
    _spool_record(
        {"v": 1, "op": op, "id": item_id, "host": _hostname(), "ts": _now(), "capture": asdict(c)}
    )


# ── apply (owner side; caller holds the store lock) ──────────────────


def _last_touched(item: dict) -> str:
    return max((str(item.get(k)) for k in _MUTATION_STAMPS if item.get(k)), default="")


def _apply_mutation_locked(rec: dict) -> int:
    action, item_id, ts = rec.get("op"), rec.get("id"), str(rec.get("ts") or "")
    m = rec.get("mutation") or {}
    to, src = m.get("to"), m.get("src")
    patch, drop = m.get("patch") or {}, m.get("drop") or []
    if not item_id or to not in LIST_NAMES or not isinstance(patch, dict):
        logger.error("gtd spool: malformed %s record for %r, skipped", action, item_id)
        return 0
    cur, idx, item, items = _locate(item_id)
    if item is None:
        logger.warning("gtd spool: %s %s skipped: item not in the store", action, item_id)
        return 0
    if src is not None and cur not in src:
        logger.warning(
            "gtd spool: %s %s skipped: item is in %s, op expected %s", action, item_id, cur, src
        )
        return 0
    if ts and _last_touched(item) >= ts:
        logger.info("gtd spool: %s %s skipped: already applied or superseded", action, item_id)
        return 0
    updated = {**item, **patch}
    for k in drop:
        updated.pop(k, None)
    if cur == to and updated == item:
        return 0
    if cur == to:
        items[idx] = updated
        _save(_list_file(to), items)
    else:  # write-then-delete: a crash can only duplicate, never lose
        dest = [it for it in _load(_list_file(to)) if it.get("id") != item_id]
        dest.append(updated)
        _save(_list_file(to), dest)
        items.pop(idx)
        _save(_list_file(cur), items)
    logger.info(
        "gtd spool: applied %s %s (%s -> %s) from %s", action, item_id, cur, to, rec.get("host")
    )
    return 1


def _apply_append_locked(rec: dict) -> int:
    item, to = rec.get("item"), rec.get("to")
    if not isinstance(item, dict) or not item.get("id") or to not in LIST_NAMES:
        logger.error("gtd spool: malformed append record %r, skipped", rec.get("id"))
        return 0
    if item["id"] in _all_ids():
        return 0
    if item.get("source_ref") and (item.get("source"), item["source_ref"]) in _seen_refs():
        return 0  # two hosts raised the same ref (e.g. one ITIL incident)
    items = _load(_list_file(to))
    items.append(item)
    _save(_list_file(to), items)
    return 1


def _capture_locked(c: GtdCapture, item_id: str | None = None) -> str | None:
    if c.source_ref and (c.source, c.source_ref) in _seen_refs():
        return None
    if item_id and item_id in _all_ids():
        return None
    item = {
        "id": item_id or uuid.uuid4().hex[:12],
        "text": c.text,
        "source": c.source,
        "source_ref": c.source_ref,
        "privacy": c.privacy,
        "context": c.context,
        "priority": c.priority,
        "energy": None,
        "status": c.status,
        "delegate_to": c.delegate_to,
        "created_at": _now(),
    }
    for k, v in (c.meta or {}).items():
        item.setdefault(k, v)
    fname = _LIST_FILE.get(c.status, "inbox.json")
    items = _load(fname)
    items.append(item)
    _save(fname, items)
    return item["id"]


def _upsert_locked(c: GtdCapture, item_id: str | None = None) -> tuple[str, str]:
    if not c.source_ref:
        return (_capture_locked(c, item_id) or item_id or ""), "created"
    fname, idx, existing, items = _find_by_ref(c.source, c.source_ref)
    if existing is None:
        return (_capture_locked(c, item_id) or item_id or ""), "created"
    updated = dict(existing)
    changed = False
    for key, new_val in (
        ("text", c.text),
        ("status", c.status),
        ("priority", c.priority),
        ("context", c.context),
    ):
        if new_val is not None and updated.get(key) != new_val:
            updated[key] = new_val
            changed = True
    for k, v in (c.meta or {}).items():
        if updated.get(k) != v:
            updated[k] = v
            changed = True
    if not changed:
        return existing["id"], "unchanged"
    updated["updated_at"] = _now()
    terminal = c.status == "done"
    if terminal:
        updated["completed_at"] = updated["updated_at"]
    dest = (
        "archive.json"
        if terminal
        else _LIST_FILE.get(updated.get("status", "inbox"), "inbox.json")
    )
    if dest == fname:
        items[idx] = updated
        _save(fname, items)
    else:
        dest_items = [it for it in _load(dest) if it.get("id") != updated["id"]]
        dest_items.append(updated)
        _save(dest, dest_items)
        items.pop(idx)
        _save(fname, items)
    return updated["id"], ("completed" if terminal else "updated")


def _offsets_path() -> Path:
    """Per-host, NOT synced. Same file skos uses, so the two never double-apply."""
    base = os.environ.get("SK_GTD_SPOOL_STATE")
    if base:
        return Path(base).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return state / "skos" / "gtd-spool-offsets.json"


def drain_spool_locked() -> int:
    """Apply every spool record not yet applied; caller holds the store lock.
    Idempotent, so a lost offsets file only costs a re-scan."""
    d = gtd_dir() / SPOOL_DIRNAME
    if not d.is_dir():
        return 0
    op_path = _offsets_path()
    try:
        offsets = json.loads(op_path.read_text(encoding="utf-8"))
        if not isinstance(offsets, dict):
            offsets = {}
    except (OSError, json.JSONDecodeError):
        offsets = {}
    applied = 0
    changed = False
    for path in sorted(d.glob("*.jsonl")):
        if ".sync-conflict-" in path.name:
            continue
        start = int(offsets.get(path.name, 0))
        try:
            if path.stat().st_size < start:  # rotated or replaced: rescan
                start = 0
        except OSError:
            continue
        records, end = _read_spool(path, start)
        for rec in records:
            op = rec.get("op")
            if op in MUTATION_OPS:
                applied += _apply_mutation_locked(rec)
            elif op == "append":
                applied += _apply_append_locked(rec)
            elif op in ("capture", "upsert"):
                try:
                    cap = GtdCapture(**(rec.get("capture") or {}))
                except TypeError:
                    logger.error("gtd spool: bad capture record %s in %s", rec.get("id"), path)
                    continue
                if op == "upsert":
                    _, action = _upsert_locked(cap, item_id=rec.get("id"))
                    applied += action != "unchanged"
                elif _capture_locked(cap, item_id=rec.get("id")):
                    applied += 1
            else:
                logger.error("gtd spool: unknown op %r (%s) in %s", op, rec.get("id"), path)
        if end != start:
            offsets[path.name] = end
            changed = True
    if changed:
        op_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = op_path.with_name(op_path.name + ".tmp")
        tmp.write_text(json.dumps(offsets, indent=2), encoding="utf-8")
        os.replace(tmp, op_path)
    if applied:
        logger.info("gtd store: applied %d spooled op(s)", applied)
    return applied


def drain_spool() -> int:
    """Owner only: fold the other hosts' spools into the lists. 0 elsewhere."""
    if not is_owner():
        return 0
    with _tools()._store_lock_at(gtd_dir()):
        return drain_spool_locked()


# ── public sinks ─────────────────────────────────────────────────────


def capture(c: GtdCapture) -> str | None:
    """Dedupe by (source, source_ref) and add. Non-owner: spool with the id
    assigned now. Returns the id, or None for a duplicate."""
    if not is_owner():
        if c.source_ref and (
            (c.source, c.source_ref) in _seen_refs() or (c.source, c.source_ref) in _spooled_refs()
        ):
            return None
        item_id = uuid.uuid4().hex[:12]
        _spool_capture("capture", c, item_id)
        return item_id
    with _tools()._store_lock_at(gtd_dir()):
        drain_spool_locked()
        return _capture_locked(c)


def upsert(c: GtdCapture) -> tuple[str, str]:
    """Create-or-update by (source, source_ref). Non-owner: ``spooled``."""
    if not is_owner():
        existing = _find_by_ref(c.source, c.source_ref)[2] if c.source_ref else None
        item_id = existing["id"] if existing else uuid.uuid4().hex[:12]
        _spool_capture("upsert", c, item_id)
        return item_id, "spooled"
    with _tools()._store_lock_at(gtd_dir()):
        drain_spool_locked()
        return _upsert_locked(c)


def spool_list_rewrite(
    list_name: str,
    current: list[dict],
    new: list[dict],
    *,
    append=None,
    mutate=None,
) -> int:
    """Turn a whole-list save from a legacy direct writer into spool records.

    Callers that load a list, change it and save it (the skcoord ITIL fallback
    sink, its orphan reaper, ``_remove_item_from_list`` + ``_save_archive``)
    predate the spool. On a non-owner host their save becomes, per item:
    an ``append`` when the id is new to the store, a ``move`` (``done`` into
    the archive) when it currently sits in another list, an in-place ``move``
    patch when it changed here. An item dropped from the list is not spooled:
    there is no delete op, and every drop in this codebase is the source half of
    a move whose destination half is spooled. ``append``/``mutate`` default to
    this module's spool writers (skos's are passed when skos is installed).
    Returns the records written."""
    append = append or spool_append
    mutate = mutate or spool_mutation
    _list_file(list_name)
    cur_by_id = {it.get("id"): it for it in current if isinstance(it, dict) and it.get("id")}
    ts = _now()
    written = 0
    for it in new:
        if not isinstance(it, dict):
            continue
        iid = it.get("id")
        if not iid:
            logger.warning("gtd spool: item without id in %s save, not spooled", list_name)
            continue
        old = cur_by_id.get(iid)
        src = list_name
        if old is None:
            loc, _, old, _ = _locate(iid)
            if old is None:
                append(it, list_name)
                written += 1
                continue
            src = loc
        if old == it and src == list_name:
            continue
        patch = {k: v for k, v in it.items() if old.get(k) != v}
        drop = [k for k in old if k not in it]
        action = "done" if list_name == "archive" and src != "archive" else "move"
        mutate(action, iid, to=list_name, src=[src], patch=patch, drop=drop, ts=ts)
        written += 1
    dropped = set(cur_by_id) - {it.get("id") for it in new if isinstance(it, dict)}
    if dropped:
        logger.info(
            "gtd spool: %d item(s) dropped from %s on a non-owner host left to the owner",
            len(dropped),
            list_name,
        )
    return written
