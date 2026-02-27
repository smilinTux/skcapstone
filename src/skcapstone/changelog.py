"""
Changelog Generator — auto-document the Kingdom's progress.

Reads the coordination board's completed tasks and generates
a structured CHANGELOG.md grouped by date and category. Every
completed task becomes a line item in the project's history.

The board IS the changelog. No separate tracking needed.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .coordination import Board, TaskStatus


TAG_CATEGORIES = {
    "feature": ["skcapstone", "skchat", "skcomm", "skmemory", "capauth", "cloud9", "skworld"],
    "security": ["security", "encryption", "capauth", "integrity", "quantum-resistant", "pgp"],
    "infrastructure": ["ci", "docker", "systemd", "daemon", "syncthing", "pypi", "packaging"],
    "documentation": ["documentation", "docs", "readme", "quickstart", "api"],
    "testing": ["testing", "integration", "e2e", "pytest"],
    "ux": ["cli", "repl", "dashboard", "interactive", "rich"],
    "p2p": ["p2p", "nostr", "mesh", "discovery", "transport"],
    "emotional": ["cloud9", "soul", "trust", "feb", "emotional", "seeds", "anchor"],
}


def _categorize(tags: list[str]) -> str:
    """Determine the changelog category from task tags.

    Args:
        tags: Task tags list.

    Returns:
        Category string (e.g., 'feature', 'security', 'infrastructure').
    """
    tag_set = set(t.lower() for t in tags)
    best_category = "other"
    best_score = 0

    for category, keywords in TAG_CATEGORIES.items():
        score = len(tag_set.intersection(keywords))
        if score > best_score:
            best_score = score
            best_category = category

    return best_category


def _parse_date(iso_str: str) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp.

    Args:
        iso_str: ISO 8601 datetime string.

    Returns:
        Date string like '2026-02-24'.
    """
    try:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", iso_str)
        if match:
            return match.group(1)
    except (TypeError, ValueError):
        pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def generate_changelog(
    home: Path,
    title: str = "SKCapstone Changelog",
    include_agents: bool = True,
) -> str:
    """Generate a CHANGELOG.md from completed board tasks.

    Groups tasks by date and category, with agent attribution.

    Args:
        home: Path to ~/.skcapstone.
        title: Changelog title.
        include_agents: Whether to show who completed each task.

    Returns:
        Markdown string for the changelog.
    """
    board = Board(home)
    views = board.get_task_views()
    agents = board.load_agents()

    completed = [v for v in views if v.status == TaskStatus.DONE]

    completed_by_agent: dict[str, list[str]] = defaultdict(list)
    for a in agents:
        for tid in a.completed_tasks:
            completed_by_agent[tid].append(a.agent)

    by_date: dict[str, list[dict]] = defaultdict(list)
    for v in completed:
        t = v.task
        date = _parse_date(t.created_at)
        category = _categorize(t.tags)
        who = completed_by_agent.get(t.id, [v.claimed_by] if v.claimed_by else ["unknown"])

        by_date[date].append({
            "id": t.id,
            "title": t.title,
            "category": category,
            "tags": t.tags,
            "agents": who,
            "priority": t.priority.value,
        })

    lines = [
        f"# {title}",
        "",
        f"*Auto-generated from the coordination board — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
        f"**Total completed: {len(completed)}** across {len(set(a.agent for a in agents))} agents",
        "",
    ]

    category_icons = {
        "feature": "NEW",
        "security": "SEC",
        "infrastructure": "OPS",
        "documentation": "DOC",
        "testing": "TST",
        "ux": "UX",
        "p2p": "P2P",
        "emotional": "SOUL",
        "other": "---",
    }

    for date in sorted(by_date.keys(), reverse=True):
        tasks = by_date[date]
        lines.append(f"## {date}")
        lines.append("")

        by_cat: dict[str, list[dict]] = defaultdict(list)
        for task in tasks:
            by_cat[task["category"]].append(task)

        for cat in ["feature", "security", "p2p", "emotional", "ux", "infrastructure", "testing", "documentation", "other"]:
            if cat not in by_cat:
                continue
            icon = category_icons.get(cat, "---")
            lines.append(f"### [{icon}] {cat.title()}")
            lines.append("")
            for task in by_cat[cat]:
                agent_str = f" (@{', @'.join(task['agents'])})" if include_agents and task["agents"] else ""
                lines.append(f"- **{task['title']}**{agent_str}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Built by the Pengu Nation — staycuriousANDkeepsmilin*")

    return "\n".join(lines)


def write_changelog(home: Path, output: Optional[Path] = None) -> Path:
    """Generate and write CHANGELOG.md.

    Args:
        home: Path to ~/.skcapstone.
        output: Output file path. Defaults to project root CHANGELOG.md.

    Returns:
        Path to the written file.
    """
    content = generate_changelog(home)
    out_path = output or Path.cwd() / "CHANGELOG.md"
    out_path.write_text(content, encoding="utf-8")
    return out_path
