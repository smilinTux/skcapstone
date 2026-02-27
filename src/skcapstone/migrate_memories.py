"""
Memory Migration — move JSON memories to the unified three-tier backend.

Scans ~/.skcapstone/memory/{short-term,mid-term,long-term}/*.json,
converts each MemoryEntry to a skmemory Memory, and writes to
SQLite (primary) + Qdrant (vector) + FalkorDB (graph).

Safe to re-run: deduplicates by memory_id.

Usage:
    skcapstone memory migrate              # execute
    skcapstone memory migrate --dry-run    # preview only
    skcapstone memory migrate --verify     # confirm integrity after migration
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .models import MemoryEntry, MemoryLayer

logger = logging.getLogger("skcapstone.migrate")


def _scan_json_memories(home: Path) -> list[MemoryEntry]:
    """Scan all JSON memory files from the agent's memory directory.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        List of all MemoryEntry objects found.
    """
    memory_dir = home / "memory"
    entries: list[MemoryEntry] = []

    for layer in MemoryLayer:
        layer_dir = memory_dir / layer.value
        if not layer_dir.exists():
            continue
        for f in sorted(layer_dir.glob("*.json")):
            if f.name == "index.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                entry = MemoryEntry(**data)
                entries.append(entry)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Skipping invalid file %s: %s", f, e)

    return entries


def migrate(
    home: Path,
    dry_run: bool = False,
    verify: bool = False,
) -> dict:
    """Migrate JSON memories to the unified skmemory backend.

    Args:
        home: Agent home directory (~/.skcapstone).
        dry_run: If True, scan and report without writing.
        verify: If True, compare JSON memories against unified backend.

    Returns:
        Dict with migration results.
    """
    from .memory_adapter import entry_to_memory, get_unified

    store = get_unified()
    if store is None:
        return {
            "ok": False,
            "error": "skmemory not available. Install with: pip install skmemory[all]",
        }

    entries = _scan_json_memories(home)
    result = {
        "ok": True,
        "total_json": len(entries),
        "migrated": 0,
        "skipped_existing": 0,
        "errors": [],
    }

    if dry_run:
        result["dry_run"] = True
        logger.info("DRY RUN: Found %d JSON memories to migrate", len(entries))
        return result

    if verify:
        return _verify_migration(entries, store, result)

    # Deduplicate by memory_id — check what already exists in primary
    existing_ids: set[str] = set()
    try:
        existing = store.list_memories(limit=10000)
        existing_ids = {m.id for m in existing}
    except Exception:
        pass

    for entry in entries:
        if entry.memory_id in existing_ids:
            result["skipped_existing"] += 1
            continue

        try:
            memory = entry_to_memory(entry)
            memory.seal()
            store.primary.save(memory)

            if store.vector:
                try:
                    store.vector.save(memory)
                except Exception as e:
                    logger.debug("Vector index failed for %s: %s", entry.memory_id, e)

            if store.graph:
                try:
                    store.graph.index_memory(memory)
                except Exception as e:
                    logger.debug("Graph index failed for %s: %s", entry.memory_id, e)

            result["migrated"] += 1
        except Exception as e:
            result["errors"].append(f"{entry.memory_id}: {e}")
            logger.warning("Migration failed for %s: %s", entry.memory_id, e)

    logger.info(
        "Migration complete: %d migrated, %d skipped, %d errors",
        result["migrated"],
        result["skipped_existing"],
        len(result["errors"]),
    )
    return result


def _verify_migration(
    entries: list[MemoryEntry],
    store: "skmemory.MemoryStore",
    result: dict,
) -> dict:
    """Verify all JSON memories exist in the unified backend.

    Args:
        entries: JSON-sourced MemoryEntry list.
        store: The unified MemoryStore.
        result: Result dict to populate.

    Returns:
        Updated result dict with verification info.
    """
    result["verify"] = True
    missing: list[str] = []

    for entry in entries:
        try:
            recalled = store.recall(entry.memory_id)
            if recalled is None:
                missing.append(entry.memory_id)
        except Exception:
            missing.append(entry.memory_id)

    result["verified"] = len(entries) - len(missing)
    result["missing"] = missing
    result["ok"] = len(missing) == 0

    if missing:
        logger.warning(
            "Verification found %d missing memories: %s",
            len(missing),
            missing[:10],
        )
    else:
        logger.info("Verification passed: all %d memories present in unified backend", len(entries))

    return result
