"""
Memory Adapter — bridge between skcapstone's MemoryEntry and skmemory's Memory.

Provides a unified memory backend that routes through skmemory's three-tier
architecture (SQLite + Qdrant + FalkorDB) while keeping the existing JSON
engine as a fallback for offline/minimal deployments.

Environment variables:
    SKMEMORY_QDRANT_URL  — Qdrant server URL (enables semantic search)
    SKMEMORY_FALKORDB_URL — FalkorDB/Redis URL (enables graph traversal)
"""

from __future__ import annotations

import functools
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .models import MemoryEntry, MemoryLayer

logger = logging.getLogger("skcapstone.memory_adapter")


@functools.lru_cache(maxsize=1)
def _skmemory_available() -> bool:
    """Check if skmemory is importable (cached)."""
    try:
        import skmemory  # noqa: F401

        return True
    except ImportError:
        return False


# Layer mapping: skcapstone uses SHORT_TERM/MID_TERM/LONG_TERM,
# skmemory uses SHORT/MID/LONG
_LAYER_TO_SKMEMORY = {
    MemoryLayer.SHORT_TERM: "short-term",
    MemoryLayer.MID_TERM: "mid-term",
    MemoryLayer.LONG_TERM: "long-term",
}

_LAYER_FROM_SKMEMORY = {
    "short-term": MemoryLayer.SHORT_TERM,
    "mid-term": MemoryLayer.MID_TERM,
    "long-term": MemoryLayer.LONG_TERM,
}


def _get_store() -> Optional["skmemory.MemoryStore"]:
    """Create a MemoryStore with all available backends based on env vars.

    Returns:
        MemoryStore or None if skmemory is not available.
    """
    if not _skmemory_available():
        return None

    try:
        from skmemory.store import MemoryStore
        from skmemory.backends.sqlite_backend import SQLiteBackend

        vector = None
        qdrant_url = os.environ.get("SKMEMORY_QDRANT_URL")
        if qdrant_url:
            try:
                from skmemory.backends.qdrant_backend import QdrantBackend

                vector = QdrantBackend(url=qdrant_url)
                logger.info("Qdrant backend enabled at %s", qdrant_url)
            except Exception as e:
                logger.warning("Could not initialize Qdrant backend: %s", e)

        graph = None
        falkordb_url = os.environ.get("SKMEMORY_FALKORDB_URL")
        if falkordb_url:
            try:
                from skmemory.backends.falkordb_backend import FalkorDBBackend

                graph = FalkorDBBackend(url=falkordb_url)
                logger.info("FalkorDB backend enabled at %s", falkordb_url)
            except Exception as e:
                logger.warning("Could not initialize FalkorDB backend: %s", e)

        store = MemoryStore(primary=SQLiteBackend(), vector=vector, graph=graph)
        logger.info("Unified memory backend active")
        return store
    except Exception as e:
        logger.warning("Failed to create unified MemoryStore: %s", e)
        return None


# Module-level lazy singleton
_unified_store: Optional[object] = None
_unified_checked: bool = False


def get_unified() -> Optional["skmemory.MemoryStore"]:
    """Get or create the unified MemoryStore singleton.

    Returns None if skmemory is not available or store creation fails.
    """
    global _unified_store, _unified_checked
    if not _unified_checked:
        _unified_store = _get_store()
        _unified_checked = True
    return _unified_store


def entry_to_memory(entry: MemoryEntry) -> "skmemory.Memory":
    """Convert a skcapstone MemoryEntry to a skmemory Memory.

    Args:
        entry: The skcapstone MemoryEntry.

    Returns:
        skmemory Memory object.
    """
    from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer as SKLayer

    layer_value = _LAYER_TO_SKMEMORY.get(entry.layer, "short-term")
    sk_layer = SKLayer(layer_value)

    return Memory(
        id=entry.memory_id,
        title=entry.content[:80] if entry.content else "untitled",
        content=entry.content,
        layer=sk_layer,
        tags=entry.tags,
        source=entry.source,
        created_at=entry.created_at.isoformat() if entry.created_at else datetime.now(timezone.utc).isoformat(),
        emotional=EmotionalSnapshot(
            intensity=entry.importance * 10,
        ),
        metadata={
            "access_count": entry.access_count,
            "importance": entry.importance,
            "soul_context": entry.soul_context,
            **(entry.metadata or {}),
        },
    )


def memory_to_entry(memory: "skmemory.Memory") -> MemoryEntry:
    """Convert a skmemory Memory to a skcapstone MemoryEntry.

    Args:
        memory: The skmemory Memory object.

    Returns:
        skcapstone MemoryEntry.
    """
    layer = _LAYER_FROM_SKMEMORY.get(memory.layer.value, MemoryLayer.SHORT_TERM)

    importance = memory.metadata.get("importance", memory.emotional.intensity / 10)
    importance = max(0.0, min(1.0, importance))

    created_at = datetime.now(timezone.utc)
    if memory.created_at:
        try:
            created_at = datetime.fromisoformat(memory.created_at)
        except (ValueError, TypeError):
            pass

    meta = dict(memory.metadata)
    access_count = meta.pop("access_count", 0)
    soul_context = meta.pop("soul_context", None)

    return MemoryEntry(
        memory_id=memory.id,
        content=memory.content,
        tags=memory.tags,
        source=memory.source,
        layer=layer,
        created_at=created_at,
        access_count=access_count,
        importance=importance,
        soul_context=soul_context,
        metadata=meta,
    )


def verify_sync() -> dict:
    """Compare memory counts across primary/vector/graph backends.

    Returns:
        Dict with counts per backend and sync status.
    """
    store = get_unified()
    if store is None:
        return {"synced": False, "reason": "skmemory not available"}

    health = store.health()
    result = {"synced": True, "backends": {}}

    primary_count = None
    if "primary" in health:
        stats = store.primary.stats() if hasattr(store.primary, "stats") else {}
        primary_count = stats.get("total", None)
        result["backends"]["sqlite"] = {
            "ok": health["primary"].get("ok", False),
            "count": primary_count,
        }

    if "vector" in health:
        result["backends"]["qdrant"] = {
            "ok": health["vector"].get("ok", False),
            "count": health["vector"].get("point_count", None),
        }

    if "graph" in health:
        result["backends"]["falkordb"] = {
            "ok": health["graph"].get("ok", False),
            "count": health["graph"].get("node_count", None),
        }

    counts = [
        v["count"]
        for v in result["backends"].values()
        if v.get("count") is not None
    ]
    if len(counts) >= 2 and len(set(counts)) > 1:
        result["synced"] = False
        result["reason"] = f"Count mismatch across backends: {dict((k, v['count']) for k, v in result['backends'].items() if v.get('count') is not None)}"

    return result


def reindex_all() -> dict:
    """Rebuild vector and graph indexes from SQLite primary.

    Returns:
        Dict with reindex results.
    """
    store = get_unified()
    if store is None:
        return {"ok": False, "reason": "skmemory not available"}

    all_memories = store.list_memories(limit=10000)
    vector_count = 0
    graph_count = 0
    errors = []

    for mem in all_memories:
        if store.vector:
            try:
                store.vector.save(mem)
                vector_count += 1
            except Exception as e:
                errors.append(f"vector:{mem.id}:{e}")

        if store.graph:
            try:
                store.graph.index_memory(mem)
                graph_count += 1
            except Exception as e:
                errors.append(f"graph:{mem.id}:{e}")

    return {
        "ok": len(errors) == 0,
        "total": len(all_memories),
        "vector_indexed": vector_count,
        "graph_indexed": graph_count,
        "errors": errors[:20],
    }
