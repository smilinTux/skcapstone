"""
Memory pillar — sovereign memory initialization.

Persistent context across platforms and sessions.
The agent remembers. Always. Everywhere.

The built-in memory engine (memory_engine.py) provides full
store/search/recall/list/gc capabilities. The optional external
`skmemory` package adds legacy compatibility but is NOT required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..models import MemoryLayer, MemoryState, PillarStatus


def initialize_memory(home: Path, memory_home: Optional[Path] = None) -> MemoryState:
    """Initialize memory for the agent.

    Creates the memory directory structure with layer subdirs.
    The built-in memory engine handles all operations; no
    external package required.

    Args:
        home: Agent home directory (~/.skcapstone).
        memory_home: Unused, kept for backward compatibility.

    Returns:
        MemoryState after initialization.
    """
    memory_dir = home / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    for layer in MemoryLayer:
        (memory_dir / layer.value).mkdir(parents=True, exist_ok=True)

    state = MemoryState(store_path=memory_dir, status=PillarStatus.ACTIVE)
    return state


def get_memory_stats(home: Path) -> dict:
    """Get current memory statistics from the built-in engine.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        Dict with memory layer counts.
    """
    from ..memory_engine import get_stats

    stats = get_stats(home)
    return {
        "short_term": stats.short_term,
        "mid_term": stats.mid_term,
        "long_term": stats.long_term,
        "total": stats.total_memories,
    }
