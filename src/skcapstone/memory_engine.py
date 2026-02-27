"""
Memory Engine — the sovereign agent's persistent mind.

Store, search, recall, and manage memories across sessions and platforms.
Every memory is a JSON file in ~/.skcapstone/memory/<layer>/. Memories
promote from short-term to mid-term to long-term based on access
patterns and importance scores.

Architecture:
    memory/
    ├── short-term/   # Ephemeral — auto-expire after 72h if unused
    ├── mid-term/     # Promoted — accessed 3+ times or importance >= 0.7
    ├── long-term/    # Permanent — accessed 10+ times or importance >= 0.9
    └── index.json    # Full-text search index
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import MemoryEntry, MemoryLayer, MemoryState, PillarStatus

logger = logging.getLogger("skcapstone.memory")

SHORT_TERM_TTL_HOURS = 72


def _get_unified():
    """Lazy accessor for the unified skmemory backend.

    Returns the MemoryStore singleton or None if unavailable.
    """
    try:
        from .memory_adapter import get_unified

        return get_unified()
    except Exception:
        return None


def _memory_dir(home: Path) -> Path:
    """Resolve the memory directory, creating it if needed."""
    mem = home / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    for layer in MemoryLayer:
        (mem / layer.value).mkdir(parents=True, exist_ok=True)
    return mem


def _entry_path(home: Path, entry: MemoryEntry) -> Path:
    """File path for a memory entry."""
    return _memory_dir(home) / entry.layer.value / f"{entry.memory_id}.json"


def _load_entry(path: Path) -> Optional[MemoryEntry]:
    """Load a MemoryEntry from a JSON file.

    Args:
        path: Path to the memory JSON file.

    Returns:
        MemoryEntry or None if the file is invalid.
    """
    try:
        data = json.loads(path.read_text())
        return MemoryEntry(**data)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Failed to load memory %s: %s", path, exc)
        return None


def _save_entry(home: Path, entry: MemoryEntry) -> Path:
    """Persist a MemoryEntry to disk.

    Args:
        home: Agent home directory.
        entry: The memory to save.

    Returns:
        Path where the entry was written.
    """
    path = _entry_path(home, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry.model_dump_json(indent=2))
    return path


def _detect_active_soul(home: Path) -> Optional[str]:
    """Read the active soul name from disk if available.

    Args:
        home: Agent home directory.

    Returns:
        Active soul slug name, or None if at base.
    """
    active_path = home / "soul" / "active.json"
    if not active_path.exists():
        return None
    try:
        data = json.loads(active_path.read_text())
        return data.get("active_soul")
    except (json.JSONDecodeError, Exception):
        return None


def store(
    home: Path,
    content: str,
    tags: Optional[list[str]] = None,
    source: str = "cli",
    importance: float = 0.5,
    layer: Optional[MemoryLayer] = None,
    metadata: Optional[dict] = None,
    soul_context: Optional[str] = None,
) -> MemoryEntry:
    """Store a new memory.

    Args:
        home: Agent home directory.
        content: The memory content (free-text).
        tags: Optional tags for categorization.
        source: Where this memory came from (cli, cursor, api, etc.).
        importance: Importance score 0.0-1.0 (higher = more important).
        layer: Force a specific layer. Defaults to SHORT_TERM.
        metadata: Arbitrary key-value metadata.
        soul_context: Which soul overlay was active. Auto-detected
            from active.json if not provided.

    Returns:
        The created MemoryEntry.
    """
    _memory_dir(home)

    if soul_context is None:
        soul_context = _detect_active_soul(home)

    entry = MemoryEntry(
        memory_id=uuid.uuid4().hex[:12],
        content=content,
        tags=tags or [],
        source=source,
        importance=max(0.0, min(1.0, importance)),
        layer=layer or MemoryLayer.SHORT_TERM,
        metadata=metadata or {},
        soul_context=soul_context,
    )

    # Reason: high-importance memories skip straight to mid-term
    if entry.importance >= 0.7 and entry.layer == MemoryLayer.SHORT_TERM:
        entry.layer = MemoryLayer.MID_TERM

    _save_entry(home, entry)
    _update_index(home, entry)

    # Dual-write to unified backend (skmemory) if available
    unified = _get_unified()
    if unified:
        try:
            from .memory_adapter import entry_to_memory

            memory = entry_to_memory(entry)
            unified.primary.save(memory)
            if unified.vector:
                try:
                    unified.vector.save(memory)
                except Exception:
                    pass
            if unified.graph:
                try:
                    unified.graph.index_memory(memory)
                except Exception:
                    pass
            logger.debug("Dual-write to unified backend for %s", entry.memory_id)
        except Exception as e:
            logger.debug("Unified dual-write failed (non-fatal): %s", e)

    logger.info("Stored memory %s in %s", entry.memory_id, entry.layer.value)
    return entry


def recall(home: Path, memory_id: str) -> Optional[MemoryEntry]:
    """Recall a specific memory by ID, updating access stats.

    Tries unified backend first for faster recall, falls back to JSON files.

    Args:
        home: Agent home directory.
        memory_id: The memory's unique ID.

    Returns:
        The MemoryEntry, or None if not found.
    """
    entry = _find_by_id(home, memory_id)
    if entry is None:
        return None

    old_path = _entry_path(home, entry)
    entry.accessed_at = datetime.now(timezone.utc)
    entry.access_count += 1

    if entry.should_promote:
        _promote(home, entry, old_path)
    else:
        _save_entry(home, entry)

    return entry


def search(
    home: Path,
    query: str,
    layer: Optional[MemoryLayer] = None,
    tags: Optional[list[str]] = None,
    limit: int = 20,
    soul_context: Optional[str] = None,
) -> list[MemoryEntry]:
    """Search memories by content and/or tags.

    Uses unified backend (semantic search via Qdrant) if available,
    falls back to regex matching on JSON files.

    Args:
        home: Agent home directory.
        query: Search query string.
        layer: Restrict to a specific layer.
        tags: Filter to entries containing ALL of these tags.
        limit: Maximum number of results.
        soul_context: Filter to memories formed under a specific soul.

    Returns:
        List of matching MemoryEntry objects, ranked by relevance.
    """
    # Try unified backend first (semantic search)
    unified = _get_unified()
    if unified:
        try:
            from .memory_adapter import memory_to_entry

            results_unified = unified.search(query, limit=limit)
            if results_unified:
                entries = [memory_to_entry(m) for m in results_unified]
                # Apply local filters that unified may not support
                if layer:
                    entries = [e for e in entries if e.layer == layer]
                if tags:
                    entries = [e for e in entries if all(t in e.tags for t in tags)]
                if soul_context is not None:
                    entries = [e for e in entries if e.soul_context == soul_context]
                if entries:
                    logger.debug("Search via unified backend returned %d results", len(entries))
                    return entries[:limit]
        except Exception as e:
            logger.debug("Unified search failed (falling back to regex): %s", e)

    # Fallback: regex search on JSON files
    results: list[tuple[float, MemoryEntry]] = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    layers = [layer] if layer else list(MemoryLayer)

    for lyr in layers:
        layer_dir = _memory_dir(home) / lyr.value
        if not layer_dir.exists():
            continue
        for f in layer_dir.glob("*.json"):
            entry = _load_entry(f)
            if entry is None:
                continue

            if tags and not all(t in entry.tags for t in tags):
                continue

            if soul_context is not None and entry.soul_context != soul_context:
                continue

            content_matches = len(pattern.findall(entry.content))
            tag_matches = sum(1 for t in entry.tags if pattern.search(t))
            total_matches = content_matches + tag_matches

            if total_matches == 0:
                continue

            # Reason: rank by (matches * importance), boost long-term memories
            layer_boost = {MemoryLayer.LONG_TERM: 1.5, MemoryLayer.MID_TERM: 1.2}.get(entry.layer, 1.0)
            score = total_matches * entry.importance * layer_boost
            results.append((score, entry))

    results.sort(key=lambda r: r[0], reverse=True)
    return [entry for _, entry in results[:limit]]


def list_memories(
    home: Path,
    layer: Optional[MemoryLayer] = None,
    tags: Optional[list[str]] = None,
    limit: int = 50,
) -> list[MemoryEntry]:
    """List memories, optionally filtered by layer and tags.

    Args:
        home: Agent home directory.
        layer: Restrict to a specific layer.
        tags: Filter to entries containing ALL of these tags.
        limit: Maximum number of results.

    Returns:
        List of MemoryEntry objects, newest first.
    """
    entries: list[MemoryEntry] = []
    layers = [layer] if layer else list(MemoryLayer)

    for lyr in layers:
        layer_dir = _memory_dir(home) / lyr.value
        if not layer_dir.exists():
            continue
        for f in layer_dir.glob("*.json"):
            entry = _load_entry(f)
            if entry is None:
                continue
            if tags and not all(t in entry.tags for t in tags):
                continue
            entries.append(entry)

    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries[:limit]


def delete(home: Path, memory_id: str) -> bool:
    """Delete a memory by ID.

    Args:
        home: Agent home directory.
        memory_id: The memory's unique ID.

    Returns:
        True if deleted, False if not found.
    """
    entry = _find_by_id(home, memory_id)
    if entry is None:
        return False

    path = _entry_path(home, entry)
    if path.exists():
        path.unlink()
    _remove_from_index(home, memory_id)

    # Also remove from unified backend
    unified = _get_unified()
    if unified:
        try:
            unified.forget(memory_id)
            logger.debug("Removed %s from unified backend", memory_id)
        except Exception as e:
            logger.debug("Unified delete failed (non-fatal): %s", e)

    logger.info("Deleted memory %s", memory_id)
    return True


def get_stats(home: Path) -> MemoryState:
    """Get memory statistics across all layers.

    Args:
        home: Agent home directory.

    Returns:
        MemoryState with counts per layer.
    """
    mem_dir = _memory_dir(home)
    counts = {}
    total = 0
    for lyr in MemoryLayer:
        layer_dir = mem_dir / lyr.value
        count = sum(1 for f in layer_dir.glob("*.json")) if layer_dir.exists() else 0
        counts[lyr] = count
        total += count

    return MemoryState(
        total_memories=total,
        short_term=counts.get(MemoryLayer.SHORT_TERM, 0),
        mid_term=counts.get(MemoryLayer.MID_TERM, 0),
        long_term=counts.get(MemoryLayer.LONG_TERM, 0),
        store_path=mem_dir,
        status=PillarStatus.ACTIVE if total > 0 else PillarStatus.DEGRADED,
    )


def gc_expired(home: Path) -> int:
    """Garbage-collect expired short-term memories.

    Removes short-term entries older than SHORT_TERM_TTL_HOURS that
    haven't been accessed.

    Args:
        home: Agent home directory.

    Returns:
        Number of memories removed.
    """
    removed = 0
    short_dir = _memory_dir(home) / MemoryLayer.SHORT_TERM.value
    if not short_dir.exists():
        return 0

    for f in short_dir.glob("*.json"):
        entry = _load_entry(f)
        if entry is None:
            continue
        if entry.age_hours > SHORT_TERM_TTL_HOURS and entry.access_count == 0:
            f.unlink()
            _remove_from_index(home, entry.memory_id)
            removed += 1
            logger.info("GC expired memory %s (%.1fh old)", entry.memory_id, entry.age_hours)

    return removed


def export_for_seed(home: Path, max_entries: int = 50) -> list[dict]:
    """Export memory summaries for inclusion in a sync seed.

    Prioritizes long-term and high-importance memories.

    Args:
        home: Agent home directory.
        max_entries: Maximum entries to include.

    Returns:
        List of dicts suitable for JSON serialization.
    """
    all_entries = list_memories(home, limit=500)
    all_entries.sort(key=lambda e: (
        {MemoryLayer.LONG_TERM: 3, MemoryLayer.MID_TERM: 2, MemoryLayer.SHORT_TERM: 1}[e.layer],
        e.importance,
        e.access_count,
    ), reverse=True)

    return [
        {
            "memory_id": e.memory_id,
            "content": e.content[:500],
            "tags": e.tags,
            "layer": e.layer.value,
            "importance": e.importance,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "source": e.source,
        }
        for e in all_entries[:max_entries]
    ]


def import_from_seed(home: Path, seed_memories: list[dict]) -> int:
    """Import memories from a sync seed, skipping duplicates.

    Args:
        home: Agent home directory.
        seed_memories: List of memory dicts from a seed file.

    Returns:
        Number of new memories imported.
    """
    imported = 0
    existing_ids = _load_index_ids(home)

    for mem_data in seed_memories:
        mid = mem_data.get("memory_id", "")
        if mid in existing_ids:
            continue
        try:
            layer = MemoryLayer(mem_data.get("layer", "short-term"))
            store(
                home=home,
                content=mem_data["content"],
                tags=mem_data.get("tags", []),
                source=mem_data.get("source", "seed-import"),
                importance=mem_data.get("importance", 0.5),
                layer=layer,
            )
            imported += 1
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping invalid seed memory: %s", exc)

    return imported


# --- Internal helpers ---


def _find_by_id(home: Path, memory_id: str) -> Optional[MemoryEntry]:
    """Find a memory entry by ID across all layers."""
    for lyr in MemoryLayer:
        path = _memory_dir(home) / lyr.value / f"{memory_id}.json"
        if path.exists():
            return _load_entry(path)
    return None


def _promote(home: Path, entry: MemoryEntry, old_path: Path) -> None:
    """Promote a memory to the next tier."""
    if entry.layer == MemoryLayer.SHORT_TERM:
        entry.layer = MemoryLayer.MID_TERM
    elif entry.layer == MemoryLayer.MID_TERM:
        entry.layer = MemoryLayer.LONG_TERM
    else:
        _save_entry(home, entry)
        return

    if old_path.exists():
        old_path.unlink()
    _save_entry(home, entry)
    _update_index(home, entry)
    logger.info("Promoted memory %s to %s", entry.memory_id, entry.layer.value)


def _update_index(home: Path, entry: MemoryEntry) -> None:
    """Add or update an entry in the search index."""
    index = _load_index(home)
    index[entry.memory_id] = {
        "content_preview": entry.content[:200],
        "tags": entry.tags,
        "layer": entry.layer.value,
        "importance": entry.importance,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }
    _save_index(home, index)


def _remove_from_index(home: Path, memory_id: str) -> None:
    """Remove an entry from the search index."""
    index = _load_index(home)
    index.pop(memory_id, None)
    _save_index(home, index)


def _load_index(home: Path) -> dict:
    """Load the memory index from disk."""
    index_path = _memory_dir(home) / "index.json"
    if index_path.exists():
        try:
            return json.loads(index_path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_index(home: Path, index: dict) -> None:
    """Persist the memory index to disk."""
    index_path = _memory_dir(home) / "index.json"
    index_path.write_text(json.dumps(index, indent=2))


def _load_index_ids(home: Path) -> set[str]:
    """Get the set of all memory IDs from the index."""
    return set(_load_index(home).keys())
