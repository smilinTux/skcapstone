"""Incremental, signature-keyed folds for the fleet dispatcher's pool_fold phase.

Measured on the production board (about 8,400 card directories, 4,300 not
archived) the dispatcher spent most of its pool_fold phase re-folding every card
over and over: ``LiveCardStoreGateway.read_card`` calls ``CardStore.list_cards``
for every review card it inspects, and each call folds the whole board again.
One cycle made 18 such calls, roughly 175,000 card folds. This module makes the
fold incremental:

* ``card_signature`` is a cheap, symlink-refusing change signature for one card
  (``core.json`` plus every ``events/*.jsonl`` file: inode, size, mtime, ctime).
* ``SigCache`` maps a card to a value under a key derived from that signature and
  any other input the value depends on. A key mismatch is a miss; nothing is ever
  patched in place.
* ``CardIndex`` is the board-wide sibling scan: it returns exactly what
  ``CardStore.list_cards()`` returns, re-folding only cards whose key changed.

Safety rules, each one an invalidation path:

* Any unsafe or unreadable path (symlink, non-regular file, vanished entry, bad
  card identifier) makes the signature ``None`` and the caller uses the original
  uncached read. The cache never widens what the strict reader would accept.
* Entries whose files were touched within ``RACY_NS`` of being recorded are not
  stored, so a rewrite inside the filesystem timestamp granularity cannot be
  mistaken for "unchanged".
* The persisted file carries a schema and the skcoord version; any difference,
  any parse problem, or any malformed entry discards the whole file.
* ``audit`` re-folds a random sample of persisted hits and discards everything on
  the first mismatch.
* ``SKFLEET_POOL_FOLD_CACHE=0`` turns the whole thing off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import stat
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

CACHE_SCHEMA = 1
#: A file touched this recently when recorded may still change within the same
#: filesystem timestamp tick, so it is not trusted (same idea as git's racy index).
RACY_NS = 2_000_000_000
AUDIT_SAMPLE = 24

#: Signature of a directory entry that has no core.json (not a card).
ABSENT = ("absent",)


def enabled() -> bool:
    return os.environ.get("SKFLEET_POOL_FOLD_CACHE", "1").strip() != "0"


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def card_signature(cards_dir: str | os.PathLike[str], card_id: str,
                   memo: Optional[dict] = None, now_ns: Optional[int] = None,
                   dir_checked: bool = False) -> Optional[tuple]:
    """Return a hashable change signature for one card, or ``None`` when unsafe.

    ``ABSENT`` means the directory has no ``core.json`` (``CardStore`` skips such
    directories). ``None`` means the layout is not the plain one the strict reader
    would accept (symlink, non-regular file, unreadable): callers must fall back to
    the uncached read for that card.

    ``memo`` is an optional per-process map used to skip re-listing an ``events``
    directory whose own inode and timestamps have not moved (creating, removing or
    renaming an entry always moves them); every known file is still stat'ed, so an
    append is seen. A directory touched within ``RACY_NS`` is always re-listed.
    ``dir_checked`` means the caller already proved the card directory is a real
    directory (its own ``scandir`` entry), so that ``lstat`` is skipped.
    """
    base = os.path.join(os.fspath(cards_dir), card_id)
    try:
        if not dir_checked and not stat.S_ISDIR(os.lstat(base).st_mode):
            return None
        try:
            core = os.lstat(os.path.join(base, "core.json"))
        except FileNotFoundError:
            return ABSENT
        if not stat.S_ISREG(core.st_mode):
            return None
        events_path = os.path.join(base, "events")
        try:
            events_dir = os.lstat(events_path)
        except FileNotFoundError:
            return (_stat_key(core), ())
        if not stat.S_ISDIR(events_dir.st_mode):
            return None
        dir_key = (events_dir.st_ino, events_dir.st_mtime_ns, events_dir.st_ctime_ns)
        names = None
        if memo is not None:
            known = memo.get(card_id)
            if known is not None and known[0] == dir_key:
                names = known[1]
        files: list[tuple] = []
        if names is not None:
            try:
                for name in names:
                    info = os.lstat(os.path.join(events_path, name))
                    if not stat.S_ISREG(info.st_mode):
                        return None
                    files.append((name, *_stat_key(info)))
            except FileNotFoundError:
                names = None
                files = []
        if names is None:
            with os.scandir(events_path) as entries:
                for entry in entries:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        return None
                    files.append((entry.name, *_stat_key(info)))
            files.sort()
            if memo is not None:
                moment = time.time_ns() if now_ns is None else now_ns
                if max(dir_key[1], dir_key[2]) < moment - RACY_NS:
                    memo[card_id] = (dir_key, tuple(f[0] for f in files))
                else:
                    memo.pop(card_id, None)
        return (_stat_key(core), tuple(files))
    except OSError:
        return None


def newest_ns(signature: tuple) -> int:
    """Newest mtime/ctime recorded in a signature (0 for ``ABSENT``)."""
    if signature == ABSENT:
        return 0
    core, files = signature
    newest = max(core[2], core[3])
    for item in files:
        newest = max(newest, item[3], item[4])
    return newest


def is_racy(signature: tuple, now_ns: int) -> bool:
    return newest_ns(signature) >= now_ns - RACY_NS


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def make_key(signature: tuple, *extra: Any) -> str:
    """Derive a cache key from a signature plus every other input of the value."""
    return _digest([signature, *extra])


class LegacyDigests:
    """Per-card digests of a legacy-mutation mapping, memoized by mapping identity."""

    def __init__(self) -> None:
        self._source: Optional[dict] = None
        self._memo: dict[str, str] = {}

    def digest(self, mapping: dict, card_id: str) -> str:
        if self._source is not mapping:
            self._source = mapping
            self._memo = {}
        found = self._memo.get(card_id)
        if found is None:
            rows = mapping.get(card_id)
            found = _digest(rows) if rows else ""
            self._memo[card_id] = found
        return found


class SigCache:
    """A card id to ``(key, JSON text)`` map with an optional private on-disk copy."""

    def __init__(self, path: str | os.PathLike[str] | None, namespace: str,
                 stamp: str, clock: Callable[[], int] = time.time_ns) -> None:
        self.path = os.fspath(path) if path else None
        self.namespace = namespace
        self.stamp = stamp
        self.clock = clock
        self.entries: dict[str, tuple[str, str]] = {}
        self.persisted: set[str] = set()
        self.dirty = False
        self.loaded = 0
        self.hits = 0
        self.misses = 0
        self.load()

    def load(self) -> None:
        self.entries = {}
        self.persisted = set()
        self.loaded = 0
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != CACHE_SCHEMA
                or raw.get("namespace") != self.namespace
                or raw.get("stamp") != self.stamp
                or not isinstance(raw.get("entries"), dict)
            ):
                raise ValueError("cache header mismatch")
            entries: dict[str, tuple[str, str]] = {}
            for card_id, item in raw["entries"].items():
                if (
                    not isinstance(card_id, str)
                    or not isinstance(item, list)
                    or len(item) != 2
                    or not isinstance(item[0], str)
                    or not isinstance(item[1], str)
                ):
                    raise ValueError("cache entry malformed")
                entries[card_id] = (item[0], item[1])
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
            logger.warning("pool fold cache %s discarded: %s", self.path, exc)
            return
        self.entries = entries
        self.persisted = set(entries)
        self.loaded = len(entries)

    def get(self, card_id: str, key: str) -> Optional[str]:
        found = self.entries.get(card_id)
        if found is not None and found[0] == key:
            self.hits += 1
            return found[1]
        self.misses += 1
        return None

    def put(self, card_id: str, key: str, blob: str) -> None:
        current = self.entries.get(card_id)
        if current == (key, blob):
            return
        self.entries[card_id] = (key, blob)
        self.persisted.discard(card_id)
        self.dirty = True

    def drop(self, card_id: str) -> None:
        if self.entries.pop(card_id, None) is not None:
            self.persisted.discard(card_id)
            self.dirty = True

    def keep_only(self, card_ids: Iterable[str]) -> None:
        keep = set(card_ids)
        for card_id in [c for c in self.entries if c not in keep]:
            self.drop(card_id)

    def clear(self) -> None:
        if self.entries:
            self.dirty = True
        self.entries = {}
        self.persisted = set()

    def save(self) -> bool:
        """Write the cache atomically and privately; never raises."""
        if not self.path or not self.dirty:
            return False
        temporary = "%s.tmp.%d" % (self.path, os.getpid())
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            payload = {
                "schema": CACHE_SCHEMA,
                "namespace": self.namespace,
                "stamp": self.stamp,
                "entries": {c: [k, b] for c, (k, b) in self.entries.items()},
            }
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.warning("pool fold cache %s not saved: %s", self.path, exc)
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
        self.persisted = set(self.entries)
        self.dirty = False
        return True


def skcoord_stamp() -> str:
    try:
        import importlib.metadata

        return "skcoord-%s" % importlib.metadata.version("skcoord")
    except Exception:  # noqa: BLE001 - an unknown version just never matches
        return "skcoord-unknown"


class CardIndex:
    """``CardStore.list_cards()`` that re-folds only cards whose key changed.

    Returned ``Card`` objects are shared between calls. Callers must treat them as
    read-only snapshots (``LiveCardStoreGateway.read_card`` only reads them).
    """

    def __init__(self, home: Path, path: str | os.PathLike[str] | None = None,
                 clock: Callable[[], int] = time.time_ns,
                 rng: Optional[random.Random] = None) -> None:
        self.home = Path(home)
        self.clock = clock
        self.rng = rng or random.Random()
        self.cache = SigCache(path, "card-index", skcoord_stamp(), clock)
        self.objects: dict[str, tuple[str, Any]] = {}
        self.legacy = LegacyDigests()
        self.refolded = 0
        self.reused = 0
        self.fallbacks = 0
        self.audited = False
        self.audit_failures = 0
        self.scans = 0
        self.scan_seconds = 0.0
        self.dir_memo: dict[str, tuple] = {}

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _legacy_mapping(store: Any) -> dict:
        """Mirror ``CardStore._legacy_events`` loading exactly (including failure)."""
        if store._legacy_cache is None:
            from skcoord.card_store import load_legacy_mutations_cached

            try:
                store._legacy_cache = load_legacy_mutations_cached(store.home)
            except Exception as exc:  # noqa: BLE001 - same fail-soft as the store
                logger.warning("Legacy mutation load failed: %s", exc)
                store._legacy_cache = {}
        return store._legacy_cache

    def _card_ids(self, store: Any) -> Optional[list[str]]:
        """Sorted card directory names, or ``None`` when the layout is unsafe."""
        from skcoord.card_store import validate_card_lock_identifier

        names: list[str] = []
        try:
            with os.scandir(os.fspath(store.cards_dir)) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        return None
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    validate_card_lock_identifier(entry.name)
                    names.append(entry.name)
        except (OSError, ValueError):
            return None
        names.sort()
        return names

    def _materialize(self, card_id: str, key: str, blob: str) -> Optional[Any]:
        from skcoord.card import Card

        try:
            data = json.loads(blob)
            if data.get("archived_marker"):
                return None
            return Card.model_validate(data["card"])
        except Exception:  # noqa: BLE001 - a bad entry is just a miss
            self.cache.drop(card_id)
            return False  # type: ignore[return-value]

    def _blob(self, card: Any) -> Optional[str]:
        """Serialise a folded card, or ``None`` when it does not round-trip."""
        from skcoord.card import Card

        if card is None:
            return None
        try:
            dump = card.model_dump(mode="json")
            if Card.model_validate(dump) != card:
                return None
            return json.dumps({"card": dump}, sort_keys=True, separators=(",", ":"))
        except Exception:  # noqa: BLE001
            return None

    # -- public ----------------------------------------------------------

    def cards(self, store: Any, *, degrade_unreadable: bool = False) -> list:
        """Return what ``store.list_cards()`` returns, folding only changed cards."""
        started = time.perf_counter()
        try:
            return self._cards(store, degrade_unreadable)
        finally:
            self.scans += 1
            self.scan_seconds += time.perf_counter() - started

    def _cards(self, store: Any, degrade_unreadable: bool) -> list:
        if os.path.realpath(os.fspath(store.home)) != os.path.realpath(os.fspath(self.home)):
            return store.list_cards(degrade_unreadable=degrade_unreadable)
        names = self._card_ids(store)
        if names is None:
            self.fallbacks += 1
            return store.list_cards(degrade_unreadable=degrade_unreadable)
        legacy = self._legacy_mapping(store)
        cards_dir = os.fspath(store.cards_dir)
        plan: list[tuple[str, tuple, str]] = []
        now = self.clock()
        for name in names:
            signature = card_signature(cards_dir, name, self.dir_memo, now, True)
            if signature is None:
                self.fallbacks += 1
                return store.list_cards(degrade_unreadable=degrade_unreadable)
            if signature == ABSENT:
                continue
            plan.append((name, signature, make_key(signature, self.legacy.digest(legacy, name))))
        out = self._resolve(store, plan, cards_dir, degrade_unreadable)
        if out is None:  # the audit found a stale entry: rebuild without the cache
            self.cache.clear()
            self.objects = {}
            out = self._resolve(store, plan, cards_dir, degrade_unreadable)
        self.cache.keep_only(name for name, _signature, _key in plan)
        return out  # type: ignore[return-value]

    def _resolve(self, store: Any, plan: list, cards_dir: str,
                 degrade_unreadable: bool = False) -> Optional[list]:
        now = self.clock()
        out: list = []
        persisted_hits: list[tuple[str, str, Any]] = []
        for name, signature, key in plan:
            held = self.objects.get(name)
            if held is not None and held[0] == key:
                card = held[1]
                self.reused += 1
            else:
                blob = self.cache.get(name, key)
                card = False
                if blob is not None:
                    card = self._materialize(name, key, blob)
                    if card is not False:
                        if card is not None:  # None is an archived marker, not a Card
                            self.objects[name] = (key, card)
                        self.reused += 1
                        if name in self.cache.persisted:
                            persisted_hits.append((name, key, card))
                if card is False:
                    try:
                        card = store.fold(name)
                    except Exception as exc:  # noqa: BLE001 - one-card fault boundary
                        if not degrade_unreadable:
                            raise
                        logger.error("CardStore card %s is unreadable: %s", name, exc)
                        out.append(store._unreadable_card(name, exc))
                        self.objects.pop(name, None)
                        self.cache.drop(name)
                        continue
                    self.refolded += 1
                    if card is not None and not is_racy(signature, now):
                        if card_signature(cards_dir, name) == signature:
                            self.objects[name] = (key, card)
                            if card.archived:
                                self.cache.put(name, key, json.dumps({"archived_marker": True}))
                            else:
                                blob = self._blob(card)
                                if blob is not None:
                                    self.cache.put(name, key, blob)
                    else:
                        self.objects.pop(name, None)
                        if card is None:
                            self.cache.drop(name)
            if card is not None and not card.archived:
                out.append(card)
        if not self.audited and persisted_hits:
            self.audited = True
            sample = self.rng.sample(persisted_hits, min(AUDIT_SAMPLE, len(persisted_hits)))
            for name, _key, cached in sample:
                fresh = store.fold(name)
                # cached is None for an archived-card marker: it must still be archived.
                stale = (fresh is None or not fresh.archived) if cached is None else fresh != cached
                if stale:
                    self.audit_failures += 1
                    logger.error("pool fold cache audit mismatch on card %s", name)
                    return None
        return out

    def fold_one(self, store: Any, card_id: str) -> Optional[Any]:
        """``store.fold(card_id)`` reusing an unchanged card from this process.

        The returned ``Card`` is shared; callers must only read it. Any doubt (an
        unsafe layout, a racy file, a different home) folds through the store.
        """
        if os.path.realpath(os.fspath(store.home)) != os.path.realpath(os.fspath(self.home)):
            return store.fold(card_id)
        cards_dir = os.fspath(store.cards_dir)
        signature = card_signature(cards_dir, card_id, self.dir_memo, self.clock())
        if signature is None or signature == ABSENT:
            return store.fold(card_id)
        key = make_key(signature, self.legacy.digest(self._legacy_mapping(store), card_id))
        held = self.objects.get(card_id)
        if held is not None and held[0] == key:
            self.reused += 1
            return held[1]
        card = store.fold(card_id)
        self.refolded += 1
        if card_signature(cards_dir, card_id) == signature and not is_racy(signature, self.clock()):
            self.objects[card_id] = (key, card)
        return card

    def stats(self) -> dict:
        return {
            "refolded": self.refolded,
            "reused": self.reused,
            "fallbacks": self.fallbacks,
            "loaded": self.cache.loaded,
            "audit_failures": self.audit_failures,
            "scans": self.scans,
            "scan_seconds": round(self.scan_seconds, 2),
        }

    def save(self) -> bool:
        return self.cache.save()


# -- process-wide switch used by LiveCardStoreGateway.read_card --------------

_SHARED: dict[str, CardIndex] = {}


def enable(home: Path, path: str | os.PathLike[str] | None = None) -> Optional[CardIndex]:
    """Turn on the shared board index for this process (the dispatcher calls this)."""
    if not enabled():
        return None
    key = os.path.realpath(os.fspath(home))
    index = _SHARED.get(key)
    if index is None:
        index = CardIndex(Path(home), path)
        _SHARED[key] = index
    return index


def disable() -> None:
    _SHARED.clear()


def shared_index(home: Path) -> Optional[CardIndex]:
    return _SHARED.get(os.path.realpath(os.fspath(home)))


def sibling_cards(store: Any, *, degrade_unreadable: bool = False) -> list:
    """``store.list_cards()``, incremental when the dispatcher enabled the index."""
    index = shared_index(store.home)
    if index is None:
        return store.list_cards(degrade_unreadable=degrade_unreadable)
    return index.cards(store, degrade_unreadable=degrade_unreadable)


# -- soft deadline and fairness cursor for the dispatcher's card loop --------

#: Fraction of the cycle budget the pool fold may spend before it stops admitting.
SOFT_DEADLINE_FRACTION = 0.6
#: The second (unblock-count) pass is only an ordering hint, so it yields earlier.
UNBLOCKS_DEADLINE_FACTOR = 1.1


def soft_deadline_seconds(budget: float | None, override: str | None = None) -> Optional[float]:
    """Seconds since cycle start after which no more cards are admitted, or ``None``."""
    if override is not None and str(override).strip() != "":
        try:
            value = float(override)
        except ValueError:
            return None
        return value if value > 0 else None
    if not budget:
        return None
    return float(budget) * SOFT_DEADLINE_FRACTION


def read_cursor(path: str | os.PathLike[str]) -> Optional[str]:
    """Return the card id the previous truncated pass stopped at, if any."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle).get("next")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def write_cursor(path: str | os.PathLike[str], next_card: Optional[str]) -> None:
    """Persist where to resume, or remove the cursor after a complete pass."""
    try:
        if next_card is None:
            os.unlink(path)
            return
        temporary = "%s.tmp.%d" % (os.fspath(path), os.getpid())
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"next": next_card}, handle)
        os.replace(temporary, path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("pool fold cursor not written: %s", exc)


def rotate_from_cursor(paths: list[str], cursor: Optional[str]) -> list[str]:
    """Start the (sorted) card walk at ``cursor`` and wrap, so none is starved."""
    if not cursor:
        return paths
    names = [os.path.basename(p) for p in paths]
    for index, name in enumerate(names):
        if name >= cursor:
            return paths[index:] + paths[:index]
    return paths


def fold_one(store: Any, card_id: str) -> Optional[Any]:
    """``store.fold(card_id)``, reusing unchanged cards when the index is enabled."""
    index = shared_index(store.home)
    if index is None:
        return store.fold(card_id)
    return index.fold_one(store, card_id)
