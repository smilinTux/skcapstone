"""
Sovereign agent morning briefing.

One screen. Everything you need to know. The first command of
every session. Gathers data from every pillar and presents a
compact, information-dense overview.

Usage:
    skcapstone summary
    skcapstone summary --json-out
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def gather_briefing(home: Path) -> dict:
    """Gather all data for the morning briefing.

    Pulls from runtime, memory, coordination board, peers,
    backups, and doctor diagnostics to build a single dict.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        dict: Complete briefing data.
    """
    briefing = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": _agent_info(home),
        "pillars": _pillar_summary(home),
        "memory": _memory_summary(home),
        "board": _board_summary(home),
        "peers": _peer_summary(home),
        "backups": _backup_summary(home),
        "health": _health_summary(home),
        "journal": _journal_summary(),
    }
    return briefing


def _agent_info(home: Path) -> dict:
    """Load basic agent identity and consciousness."""
    try:
        from .runtime import get_runtime

        runtime = get_runtime(home)
        m = runtime.manifest

        if m.is_singular:
            consciousness = "SINGULAR"
        elif m.is_conscious:
            consciousness = "CONSCIOUS"
        else:
            consciousness = "AWAKENING"

        return {
            "name": m.name,
            "version": m.version,
            "consciousness": consciousness,
            "home": str(m.home),
        }
    except Exception:
        return {"name": "unknown", "consciousness": "UNKNOWN", "home": str(home)}


def _pillar_summary(home: Path) -> dict:
    """Get one-line status for each pillar."""
    try:
        from .runtime import get_runtime

        runtime = get_runtime(home)
        m = runtime.manifest
        return {k: v.value for k, v in m.pillar_summary.items()}
    except Exception:
        return {}


def _memory_summary(home: Path) -> dict:
    """Get memory counts and recent entries."""
    try:
        from .memory_engine import get_stats, list_memories
        from .models import MemoryLayer

        stats = get_stats(home)
        recent = list_memories(home, limit=3)
        recent_titles = []
        for entry in recent:
            preview = entry.content[:60] + "..." if len(entry.content) > 60 else entry.content
            recent_titles.append(preview)

        return {
            "total": stats.total_memories,
            "short_term": stats.short_term,
            "mid_term": stats.mid_term,
            "long_term": stats.long_term,
            "recent": recent_titles,
        }
    except Exception:
        return {"total": 0, "recent": []}


def _board_summary(home: Path) -> dict:
    """Get coordination board stats and active tasks."""
    try:
        from .coordination import Board

        board = Board(home)
        views = board.get_task_views()

        done = sum(1 for v in views if v.status.value == "done")
        open_count = sum(1 for v in views if v.status.value == "open")
        in_progress = sum(1 for v in views if v.status.value in ("in_progress", "claimed"))

        active_tasks = [
            {"title": v.task.title[:50], "assignee": v.claimed_by or "unassigned"}
            for v in views
            if v.status.value in ("in_progress", "claimed", "open")
        ][:5]

        return {
            "total": len(views),
            "done": done,
            "open": open_count,
            "in_progress": in_progress,
            "active_tasks": active_tasks,
        }
    except Exception:
        return {"total": 0, "done": 0, "open": 0, "in_progress": 0, "active_tasks": []}


def _peer_summary(home: Path) -> dict:
    """Count known peers."""
    try:
        from .peers import list_peers

        peers = list_peers(skcapstone_home=home)
        names = [p.name for p in peers[:5]]
        return {"count": len(peers), "names": names}
    except Exception:
        peers_dir = home / "peers"
        if peers_dir.exists():
            count = sum(1 for f in peers_dir.glob("*.json"))
            return {"count": count, "names": []}
        return {"count": 0, "names": []}


def _backup_summary(home: Path) -> dict:
    """Get last backup info."""
    try:
        from .backup import list_backups

        backups = list_backups(home)
        if backups:
            latest = backups[0]
            return {
                "count": len(backups),
                "latest": latest.get("created_at", "")[:19],
                "encrypted": latest.get("encrypted", False),
            }
        return {"count": 0, "latest": None}
    except Exception:
        return {"count": 0, "latest": None}


def _health_summary(home: Path) -> dict:
    """Get doctor pass/fail counts."""
    try:
        from .doctor import run_diagnostics

        report = run_diagnostics(home)
        return {
            "passed": report.passed_count,
            "failed": report.failed_count,
            "total": report.total_count,
            "all_passed": report.all_passed,
        }
    except Exception:
        return {"passed": 0, "failed": 0, "total": 0, "all_passed": False}


def _journal_summary() -> dict:
    """Get journal entry count and latest title."""
    try:
        from skmemory.journal import Journal

        journal = Journal()
        count = journal.count_entries()
        latest = ""
        if count > 0:
            text = journal.read_latest(1)
            for line in text.split("\n"):
                if line.startswith("## "):
                    latest = line[3:].strip()
                    break

        return {"entries": count, "latest_title": latest}
    except Exception:
        return {"entries": 0, "latest_title": ""}
