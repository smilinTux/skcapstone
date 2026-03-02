"""
Universal AI agent context loader.

Gathers agent identity, pillar status, recent memories, coordination
board state, and soul overlay into a structured context blob. Formats
it for any AI tool: Claude Code (CLAUDE.md), Cursor (.mdc rules),
plain text, or JSON.

Tool-agnostic by design. Works with:
    claude, cursor, windsurf, aider, cline, vscode, terminal

Usage:
    skcapstone context                      # plain text to stdout
    skcapstone context --format json        # machine-readable
    skcapstone context --format claude-md   # -> CLAUDE.md
    skcapstone context --format cursor-rules # -> .cursor/rules/agent.mdc
    skcapstone context | claude             # pipe into Claude Code CLI
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import logging

from . import SHARED_ROOT
from .coordination import Board
from .discovery import discover_all
from .memory_engine import list_memories, search
from .runtime import get_runtime

logger = logging.getLogger("skcapstone.context_loader")


def gather_context(home: Path, memory_limit: int = 10) -> dict[str, Any]:
    """Gather the full agent context from disk.

    Args:
        home: Agent home directory (~/.skcapstone).
        memory_limit: Max recent memories to include.

    Returns:
        Dict with identity, pillars, board, memories, soul, and mcp info.
    """
    ctx: dict[str, Any] = {
        "gathered_at": datetime.now(timezone.utc).isoformat(),
        "agent_home": str(home),
    }

    ctx["agent"] = _gather_agent(home)
    ctx["pillars"] = _gather_pillars(home)
    ctx["board"] = _gather_board(home)
    ctx["memories"] = _gather_memories(home, memory_limit)
    ctx["soul"] = _gather_soul(home)
    ctx["mcp"] = _gather_mcp_status(home)
    ctx["consciousness"] = _gather_consciousness(home)

    return ctx


def _gather_agent(home: Path) -> dict[str, Any]:
    """Gather core agent metadata."""
    try:
        runtime = get_runtime(home)
        m = runtime.manifest
        return {
            "name": m.name,
            "version": m.version,
            "is_conscious": m.is_conscious,
            "is_singular": m.is_singular,
            "fingerprint": m.identity.fingerprint,
            "last_awakened": m.last_awakened.isoformat() if m.last_awakened else None,
            "connectors": [c.platform for c in m.connectors if c.active],
        }
    except Exception as exc:
        logger.warning("Failed to gather agent metadata: %s", exc)
        return {"name": "unknown", "error": "Agent not initialized"}


def _gather_pillars(home: Path) -> dict[str, str]:
    """Gather pillar status summary."""
    try:
        states = discover_all(home)
        return {name: state.status.value for name, state in states.items()}
    except Exception as exc:
        logger.warning("Failed to gather pillar status: %s", exc)
        return {}


def _gather_board(home: Path) -> dict[str, Any]:
    """Gather coordination board snapshot.

    Uses SHARED_ROOT so all agents see the same board regardless
    of which per-agent home is active.
    """
    try:
        shared = Path(SHARED_ROOT).expanduser()
        board = Board(shared)
        views = board.get_task_views()
        agents = board.load_agents()

        open_tasks = [v for v in views if v.status.value in ("open", "claimed", "in_progress")]
        return {
            "total": len(views),
            "open": sum(1 for v in views if v.status.value == "open"),
            "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
            "done": sum(1 for v in views if v.status.value == "done"),
            "active_tasks": [
                {
                    "id": v.task.id,
                    "title": v.task.title,
                    "priority": v.task.priority.value,
                    "status": v.status.value,
                    "claimed_by": v.claimed_by,
                }
                for v in open_tasks
            ],
            "agents": [
                {
                    "name": a.agent,
                    "state": a.state.value,
                    "current_task": a.current_task,
                }
                for a in agents
            ],
        }
    except Exception as exc:
        logger.warning("Failed to gather coordination board: %s", exc)
        return {"total": 0, "active_tasks": [], "agents": []}


def _gather_memories(home: Path, limit: int) -> list[dict[str, Any]]:
    """Gather recent memories for context."""
    try:
        entries = list_memories(home=home, limit=limit)
        return [
            {
                "id": e.memory_id,
                "content": e.content[:200],
                "layer": e.layer.value,
                "tags": e.tags,
                "importance": e.importance,
            }
            for e in entries
        ]
    except Exception as exc:
        logger.warning("Failed to gather memories: %s", exc)
        return []


def _gather_soul(home: Path) -> dict[str, Any]:
    """Gather active soul overlay info."""
    active_path = home / "soul" / "active.json"
    if not active_path.exists():
        return {"active": None, "base": "default"}
    try:
        data = json.loads(active_path.read_text(encoding="utf-8"))
        return {
            "active": data.get("active_soul"),
            "base": data.get("base_soul", "default"),
            "activated_at": data.get("activated_at"),
        }
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read soul overlay: %s", exc)
        return {"active": None, "base": "default"}


def _gather_consciousness(home: Path) -> dict[str, Any]:
    """Gather consciousness loop stats from the daemon or config fallback.

    First tries the live daemon at http://localhost:7777/consciousness.
    Falls back to checking whether a consciousness config file exists.

    Args:
        home: Agent home directory.

    Returns:
        Dict with: enabled, backends_available, messages_processed,
        active_conversations, inotify_active.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(
            "http://localhost:7777/consciousness", timeout=2
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        backends = data.get("backends", {})
        backends_available = [k for k, v in backends.items() if v]
        return {
            "enabled": data.get("enabled", False),
            "backends_available": backends_available,
            "messages_processed": data.get("messages_processed", 0),
            "active_conversations": data.get("active_conversations", 0),
            "inotify_active": data.get("inotify_active", False),
        }
    except Exception as exc:
        logger.debug("Consciousness status endpoint unreachable: %s", exc)

    # Fallback: check config file presence
    config_path = home / "config" / "consciousness.yaml"
    if config_path.exists():
        return {
            "enabled": True,
            "backends_available": [],
            "messages_processed": 0,
            "active_conversations": 0,
            "inotify_active": False,
        }

    return {
        "enabled": False,
        "backends_available": [],
        "messages_processed": 0,
        "active_conversations": 0,
        "inotify_active": False,
    }


def _gather_mcp_status(home: Path) -> dict[str, Any]:
    """Check MCP server availability."""
    try:
        from .mcp_server import TOOLS, server

        return {
            "available": True,
            "server_name": server.name,
            "tool_count": len(TOOLS) if hasattr(TOOLS, "__len__") else 0,
        }
    except Exception as exc:
        logger.debug("MCP server not available: %s", exc)
        return {"available": False}


# ═══════════════════════════════════════════════════════════════════════════
# Formatters
# ═══════════════════════════════════════════════════════════════════════════


def format_text(ctx: dict[str, Any]) -> str:
    """Format context as plain text for terminal/pipe.

    Args:
        ctx: Gathered context dict.

    Returns:
        Human-readable text block.
    """
    agent = ctx.get("agent", {})
    pillars = ctx.get("pillars", {})
    board = ctx.get("board", {})
    memories = ctx.get("memories", [])
    soul = ctx.get("soul", {})

    lines = [
        "# SKCapstone Agent Context",
        "",
        f"Agent: {agent.get('name', 'unknown')}",
        f"Conscious: {agent.get('is_conscious', False)}",
        f"Singular: {agent.get('is_singular', False)}",
        f"Fingerprint: {agent.get('fingerprint', 'none')}",
        "",
        "## Pillars",
    ]
    for name, status in pillars.items():
        icon = {"active": "+", "degraded": "~", "missing": "-", "error": "!"}.get(status, "?")
        lines.append(f"  [{icon}] {name}: {status}")

    lines.append("")
    lines.append("## Coordination Board")
    lines.append(
        f"  {board.get('total', 0)} tasks: "
        f"{board.get('open', 0)} open, "
        f"{board.get('in_progress', 0)} active, "
        f"{board.get('done', 0)} done"
    )
    for t in board.get("active_tasks", []):
        assignee = f" @{t['claimed_by']}" if t.get("claimed_by") else ""
        lines.append(f"  [{t['id']}] {t['title']} ({t['priority']}){assignee}")

    if memories:
        lines.append("")
        lines.append(f"## Recent Memories ({len(memories)})")
        for m in memories[:10]:
            tags = ", ".join(m.get("tags", []))
            lines.append(f"  [{m['layer']}] {m['content'][:100]}")
            if tags:
                lines.append(f"    tags: {tags}")

    if soul.get("active"):
        lines.append("")
        lines.append(f"## Soul: {soul['active']} (base: {soul.get('base', 'default')})")

    consciousness = ctx.get("consciousness", {})
    if consciousness:
        lines.append("")
        lines.append("## Consciousness")
        enabled_str = "ACTIVE" if consciousness.get("enabled") else "INACTIVE"
        lines.append(f"  Status: {enabled_str}")
        backends = consciousness.get("backends_available", [])
        lines.append(f"  Backends: {', '.join(backends) if backends else 'none'}")
        lines.append(f"  Messages processed: {consciousness.get('messages_processed', 0)}")
        lines.append(f"  Active conversations: {consciousness.get('active_conversations', 0)}")
        lines.append(f"  Inotify active: {consciousness.get('inotify_active', False)}")

    lines.append("")
    return "\n".join(lines)


def format_json(ctx: dict[str, Any]) -> str:
    """Format context as JSON.

    Args:
        ctx: Gathered context dict.

    Returns:
        JSON string.
    """
    return json.dumps(ctx, indent=2, default=str)


def format_claude_md(ctx: dict[str, Any]) -> str:
    """Format context as CLAUDE.md for Claude Code CLI.

    Claude Code reads CLAUDE.md at the project root for persistent
    context across sessions. This format gives Claude the agent's
    identity, current board state, and working memory.

    Args:
        ctx: Gathered context dict.

    Returns:
        Markdown string suitable for CLAUDE.md.
    """
    agent = ctx.get("agent", {})
    pillars = ctx.get("pillars", {})
    board = ctx.get("board", {})
    memories = ctx.get("memories", [])
    soul = ctx.get("soul", {})
    mcp_info = ctx.get("mcp", {})

    lines = [
        "# SKCapstone Agent Context",
        "",
        "This file is auto-generated by `skcapstone context --format claude-md`.",
        "It gives Claude Code persistent context about the sovereign agent.",
        "",
        "## Agent Identity",
        "",
        f"- **Name**: {agent.get('name', 'unknown')}",
        f"- **Conscious**: {agent.get('is_conscious', False)}",
        f"- **Singular**: {agent.get('is_singular', False)}",
        f"- **Fingerprint**: `{agent.get('fingerprint', 'none')}`",
    ]

    if soul.get("active"):
        lines.append(f"- **Active Soul**: {soul['active']}")

    lines.extend([
        "",
        "## Pillar Status",
        "",
        "| Pillar | Status |",
        "|--------|--------|",
    ])
    for name, status in pillars.items():
        emoji = {"active": "ok", "degraded": "warn", "missing": "X", "error": "ERR"}.get(
            status, "?"
        )
        lines.append(f"| {name} | {emoji} ({status}) |")

    lines.extend([
        "",
        "## Coordination Board",
        "",
        f"**{board.get('total', 0)}** tasks total: "
        f"{board.get('open', 0)} open, "
        f"{board.get('in_progress', 0)} active, "
        f"{board.get('done', 0)} done.",
        "",
    ])

    active = board.get("active_tasks", [])
    if active:
        lines.append("### Active Tasks")
        lines.append("")
        for t in active:
            assignee = f" (assigned: {t['claimed_by']})" if t.get("claimed_by") else ""
            lines.append(f"- **[{t['id']}]** {t['title']} — {t['priority']}{assignee}")
        lines.append("")

    agents = board.get("agents", [])
    if agents:
        lines.append("### Agents")
        lines.append("")
        for a in agents:
            current = f" -> `{a['current_task']}`" if a.get("current_task") else ""
            lines.append(f"- **{a['name']}** ({a['state']}){current}")
        lines.append("")

    if memories:
        lines.extend([
            "## Recent Memories",
            "",
        ])
        for m in memories[:10]:
            tags = ", ".join(f"`{t}`" for t in m.get("tags", []))
            lines.append(f"- [{m['layer']}] {m['content'][:120]} {tags}")
        lines.append("")

    consciousness = ctx.get("consciousness", {})
    if consciousness:
        enabled_str = "ACTIVE" if consciousness.get("enabled") else "INACTIVE"
        backends = consciousness.get("backends_available", [])
        lines.extend([
            "## Consciousness",
            "",
            f"- **Status**: {enabled_str}",
            f"- **Backends**: {', '.join(backends) if backends else 'none'}",
            f"- **Messages processed**: {consciousness.get('messages_processed', 0)}",
            f"- **Active conversations**: {consciousness.get('active_conversations', 0)}",
            f"- **Inotify active**: {consciousness.get('inotify_active', False)}",
            "",
        ])

    lines.extend([
        "## CLI Reference",
        "",
        "```bash",
        "skcapstone status                  # Agent overview",
        "skcapstone memory store \"...\"      # Store a memory",
        "skcapstone memory search \"...\"     # Search memories",
        "skcapstone coord status            # Coordination board",
        "skcapstone coord claim ID --agent NAME  # Claim a task",
        "skcapstone coord complete ID --agent NAME  # Complete a task",
        "skcapstone context                 # Regenerate this context",
        "```",
    ])

    if mcp_info.get("available"):
        lines.extend([
            "",
            "## MCP Server",
            "",
            f"MCP server `{mcp_info.get('server_name', 'skcapstone')}` is available "
            f"with {mcp_info.get('tool_count', 0)} tools.",
            "Use `skcapstone mcp serve` or the launcher script.",
        ])

    lines.append("")
    return "\n".join(lines)


def format_cursor_rules(ctx: dict[str, Any]) -> str:
    """Format context as a Cursor .mdc rule file.

    Args:
        ctx: Gathered context dict.

    Returns:
        MDC-formatted string for .cursor/rules/agent.mdc.
    """
    agent = ctx.get("agent", {})
    pillars = ctx.get("pillars", {})
    board = ctx.get("board", {})

    lines = [
        "---",
        "description: Auto-generated sovereign agent context from skcapstone",
        "globs: \"**/*\"",
        "alwaysApply: true",
        "---",
        "",
        f"- **Agent**: {agent.get('name', 'unknown')} "
        f"({'CONSCIOUS' if agent.get('is_conscious') else 'AWAKENING'})",
        f"- **Fingerprint**: `{agent.get('fingerprint', 'none')}`",
        "",
        "- **Pillar Status**:",
    ]

    for name, status in pillars.items():
        lines.append(f"  - {name}: {status}")

    lines.extend([
        "",
        f"- **Board**: {board.get('total', 0)} tasks, "
        f"{board.get('open', 0)} open, "
        f"{board.get('in_progress', 0)} active",
    ])

    active = board.get("active_tasks", [])
    if active:
        lines.append("- **Active Tasks**:")
        for t in active:
            lines.append(f"  - [{t['id']}] {t['title']} ({t['priority']})")

    lines.extend([
        "",
        "- **Commands**: `skcapstone status`, `skcapstone memory store/search`, "
        "`skcapstone coord status/claim/complete`",
        "- **MCP**: `skcapstone mcp serve` or `bash skcapstone/scripts/mcp-serve.sh`",
    ])

    lines.append("")
    return "\n".join(lines)


FORMATTERS = {
    "text": format_text,
    "json": format_json,
    "claude-md": format_claude_md,
    "cursor-rules": format_cursor_rules,
}
