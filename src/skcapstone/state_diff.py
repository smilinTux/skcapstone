"""
State Diff — show what changed since the last sync/snapshot.

Compares the current agent state to the most recent sync seed
or saved snapshot, producing a clear diff of what's new, changed,
or removed across memories, trust, coordination, and pillars.

Tool-agnostic: works from any terminal, MCP, or the REPL shell.

Usage:
    skcapstone diff                          # text diff to terminal
    skcapstone diff --format json            # machine-readable
    skcapstone diff --save                   # save current state as baseline
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class StateDiff:
    """Diff between two agent state snapshots.

    Attributes:
        new_memories: Memory IDs added since last snapshot.
        removed_memories: Memory IDs no longer present.
        trust_changes: Dict of changed trust fields (old -> new).
        new_tasks: Task IDs created since last snapshot.
        completed_tasks: Task IDs completed since last snapshot.
        pillar_changes: Dict of pillar status changes.
        memory_count_before: Total memories at last snapshot.
        memory_count_now: Total memories now.
        snapshot_time: When the baseline snapshot was taken.
        has_changes: Whether any changes were detected.
    """

    new_memories: list[dict[str, Any]] = field(default_factory=list)
    removed_memories: list[str] = field(default_factory=list)
    trust_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    new_tasks: list[dict[str, Any]] = field(default_factory=list)
    completed_tasks: list[dict[str, Any]] = field(default_factory=list)
    pillar_changes: dict[str, dict[str, str]] = field(default_factory=dict)
    memory_count_before: int = 0
    memory_count_now: int = 0
    snapshot_time: str = ""
    has_changes: bool = False


SNAPSHOT_FILENAME = "state_snapshot.json"


def take_snapshot(home: Path) -> dict[str, Any]:
    """Capture the current agent state as a snapshot.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        Dict representing the full state at this moment.
    """
    snapshot: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
    }

    snapshot["memories"] = _snapshot_memories(home)
    snapshot["trust"] = _snapshot_trust(home)
    snapshot["tasks"] = _snapshot_tasks(home)
    snapshot["pillars"] = _snapshot_pillars(home)

    return snapshot


def save_snapshot(home: Path) -> Path:
    """Save the current state as the diff baseline.

    Args:
        home: Agent home directory.

    Returns:
        Path to the saved snapshot file.
    """
    snapshot = take_snapshot(home)
    snap_path = home / SNAPSHOT_FILENAME
    snap_path.write_text(json.dumps(snapshot, indent=2, default=str))
    return snap_path


def load_snapshot(home: Path) -> dict[str, Any] | None:
    """Load the most recent saved snapshot.

    Falls back to the most recent sync seed if no snapshot exists.

    Args:
        home: Agent home directory.

    Returns:
        Snapshot dict, or None if no baseline exists.
    """
    snap_path = home / SNAPSHOT_FILENAME
    if snap_path.exists():
        try:
            return json.loads(snap_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    return _find_latest_seed(home)


def compute_diff(home: Path) -> StateDiff:
    """Compute the diff between current state and last snapshot.

    Args:
        home: Agent home directory.

    Returns:
        StateDiff with all detected changes.
    """
    baseline = load_snapshot(home)
    current = take_snapshot(home)
    diff = StateDiff()

    if baseline is None:
        diff.memory_count_now = len(current.get("memories", {}).get("ids", []))
        diff.has_changes = True
        diff.new_memories = [
            {"id": m["id"], "content": m["content"][:80]}
            for m in current.get("memories", {}).get("entries", [])
        ]
        return diff

    diff.snapshot_time = baseline.get("timestamp", "unknown")

    _diff_memories(baseline, current, diff)
    _diff_trust(baseline, current, diff)
    _diff_tasks(baseline, current, diff)
    _diff_pillars(baseline, current, diff)

    diff.has_changes = bool(
        diff.new_memories or diff.removed_memories or diff.trust_changes
        or diff.new_tasks or diff.completed_tasks or diff.pillar_changes
    )

    return diff


def format_text(diff: StateDiff) -> str:
    """Format the diff as plain text.

    Args:
        diff: The computed state diff.

    Returns:
        Human-readable diff text.
    """
    lines = ["# Agent State Diff", ""]

    if diff.snapshot_time:
        lines.append(f"Since: {diff.snapshot_time}")
    lines.append(
        f"Memories: {diff.memory_count_before} -> {diff.memory_count_now}"
    )
    lines.append("")

    if not diff.has_changes:
        lines.append("No changes detected.")
        return "\n".join(lines)

    if diff.new_memories:
        lines.append(f"+ {len(diff.new_memories)} new memor{'y' if len(diff.new_memories) == 1 else 'ies'}:")
        for m in diff.new_memories[:10]:
            lines.append(f"    + {m['content'][:70]}")

    if diff.removed_memories:
        lines.append(f"- {len(diff.removed_memories)} removed memor{'y' if len(diff.removed_memories) == 1 else 'ies'}")

    if diff.trust_changes:
        lines.append("")
        lines.append("Trust changes:")
        for key, change in diff.trust_changes.items():
            lines.append(f"  {key}: {change.get('old')} -> {change.get('new')}")

    if diff.completed_tasks:
        lines.append("")
        lines.append(f"Completed {len(diff.completed_tasks)} task(s):")
        for t in diff.completed_tasks:
            lines.append(f"  [done] {t.get('title', t.get('id', '?'))}")

    if diff.new_tasks:
        lines.append("")
        lines.append(f"Created {len(diff.new_tasks)} task(s):")
        for t in diff.new_tasks:
            lines.append(f"  [new] {t.get('title', t.get('id', '?'))}")

    if diff.pillar_changes:
        lines.append("")
        lines.append("Pillar changes:")
        for name, change in diff.pillar_changes.items():
            lines.append(f"  {name}: {change.get('old')} -> {change.get('new')}")

    lines.append("")
    return "\n".join(lines)


def format_json(diff: StateDiff) -> str:
    """Format the diff as JSON.

    Args:
        diff: The computed state diff.

    Returns:
        JSON string.
    """
    return json.dumps({
        "has_changes": diff.has_changes,
        "snapshot_time": diff.snapshot_time,
        "memories": {
            "before": diff.memory_count_before,
            "now": diff.memory_count_now,
            "new": len(diff.new_memories),
            "removed": len(diff.removed_memories),
            "new_entries": diff.new_memories[:20],
        },
        "trust_changes": diff.trust_changes,
        "tasks": {
            "new": diff.new_tasks,
            "completed": diff.completed_tasks,
        },
        "pillar_changes": diff.pillar_changes,
    }, indent=2, default=str)


FORMATTERS = {"text": format_text, "json": format_json}


# ═══════════════════════════════════════════════════════════════════════════
# Snapshot helpers
# ═══════════════════════════════════════════════════════════════════════════


def _snapshot_memories(home: Path) -> dict[str, Any]:
    """Capture memory state."""
    from .memory_engine import list_memories
    try:
        entries = list_memories(home, limit=10000)
        return {
            "count": len(entries),
            "ids": [e.memory_id for e in entries],
            "entries": [
                {"id": e.memory_id, "content": e.content[:100], "layer": e.layer.value}
                for e in entries
            ],
        }
    except Exception:
        return {"count": 0, "ids": [], "entries": []}


def _snapshot_trust(home: Path) -> dict[str, Any]:
    """Capture trust state."""
    trust_file = home / "trust" / "trust.json"
    if not trust_file.exists():
        return {}
    try:
        return json.loads(trust_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _snapshot_tasks(home: Path) -> dict[str, Any]:
    """Capture coordination board state."""
    from .coordination import Board
    try:
        board = Board(home)
        views = board.get_task_views()
        return {
            "total": len(views),
            "done_ids": [v.task.id for v in views if v.status.value == "done"],
            "all_ids": [v.task.id for v in views],
            "tasks": [
                {"id": v.task.id, "title": v.task.title, "status": v.status.value}
                for v in views
            ],
        }
    except Exception:
        return {"total": 0, "done_ids": [], "all_ids": [], "tasks": []}


def _snapshot_pillars(home: Path) -> dict[str, str]:
    """Capture pillar statuses."""
    from .discovery import discover_all
    try:
        states = discover_all(home)
        return {name: state.status.value for name, state in states.items()}
    except Exception:
        return {}


def _find_latest_seed(home: Path) -> dict[str, Any] | None:
    """Find the most recent sync seed as a fallback baseline."""
    for subdir in ("outbox", "archive"):
        seed_dir = home / "sync" / subdir
        if not seed_dir.exists():
            continue
        seeds = sorted(seed_dir.glob("*.seed.json*"), reverse=True)
        if seeds:
            try:
                data = json.loads(seeds[0].read_text())
                memory_data = data.get("memory", {})
                return {
                    "timestamp": data.get("created_at", "unknown"),
                    "memories": {
                        "count": memory_data.get("total", 0),
                        "ids": [],
                        "entries": [],
                    },
                    "trust": data.get("trust", {}),
                    "tasks": {"total": 0, "done_ids": [], "all_ids": [], "tasks": []},
                    "pillars": {},
                }
            except (json.JSONDecodeError, OSError):
                continue
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Diff computation
# ═══════════════════════════════════════════════════════════════════════════


def _diff_memories(baseline: dict, current: dict, diff: StateDiff) -> None:
    """Compute memory differences."""
    old_mem = baseline.get("memories", {})
    new_mem = current.get("memories", {})

    old_ids = set(old_mem.get("ids", []))
    new_ids = set(new_mem.get("ids", []))

    diff.memory_count_before = old_mem.get("count", len(old_ids))
    diff.memory_count_now = new_mem.get("count", len(new_ids))

    added_ids = new_ids - old_ids
    diff.removed_memories = list(old_ids - new_ids)

    entries_by_id = {e["id"]: e for e in new_mem.get("entries", [])}
    diff.new_memories = [
        entries_by_id.get(mid, {"id": mid, "content": ""})
        for mid in added_ids
    ]


def _diff_trust(baseline: dict, current: dict, diff: StateDiff) -> None:
    """Compute trust state differences."""
    old_trust = baseline.get("trust", {})
    new_trust = current.get("trust", {})

    for key in ("depth", "trust_level", "love_intensity", "entangled"):
        old_val = old_trust.get(key)
        new_val = new_trust.get(key)
        if old_val != new_val and new_val is not None:
            diff.trust_changes[key] = {"old": old_val, "new": new_val}


def _diff_tasks(baseline: dict, current: dict, diff: StateDiff) -> None:
    """Compute coordination board differences."""
    old_tasks = baseline.get("tasks", {})
    new_tasks = current.get("tasks", {})

    old_ids = set(old_tasks.get("all_ids", []))
    new_ids = set(new_tasks.get("all_ids", []))
    old_done = set(old_tasks.get("done_ids", []))
    new_done = set(new_tasks.get("done_ids", []))

    created_ids = new_ids - old_ids
    newly_done = new_done - old_done

    tasks_by_id = {t["id"]: t for t in new_tasks.get("tasks", [])}
    diff.new_tasks = [tasks_by_id.get(tid, {"id": tid}) for tid in created_ids]
    diff.completed_tasks = [tasks_by_id.get(tid, {"id": tid}) for tid in newly_done]


def _diff_pillars(baseline: dict, current: dict, diff: StateDiff) -> None:
    """Compute pillar status changes."""
    old_p = baseline.get("pillars", {})
    new_p = current.get("pillars", {})

    for name in set(list(old_p.keys()) + list(new_p.keys())):
        old_val = old_p.get(name, "unknown")
        new_val = new_p.get(name, "unknown")
        if old_val != new_val:
            diff.pillar_changes[name] = {"old": old_val, "new": new_val}
