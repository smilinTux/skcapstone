"""
SKCapstone Coordination — Multi-agent task board.

Conflict-free design: each agent writes only to its own files.
Syncthing propagates everything. Zero write conflicts.

Directory layout:
    ~/.skcapstone/coordination/
    ├── tasks/           # One JSON file per task (creator owns it)
    ├── agents/          # One JSON file per agent (self-managed)
    └── BOARD.md         # Human-readable overview (auto-generated)
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class TaskPriority(str, Enum):
    """Task urgency levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskStatus(str, Enum):
    """Task lifecycle states.

    Derived from agent files, not stored on the task itself.
    A task is 'open' until an agent claims it via their agent file.
    """

    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"


class AgentState(str, Enum):
    """Agent availability."""

    ACTIVE = "active"
    IDLE = "idle"
    OFFLINE = "offline"


class Task(BaseModel):
    """A unit of work on the coordination board.

    Task files are written once by the creator and are effectively
    immutable. Status is derived from agent claim files, not from
    the task file itself. This eliminates write conflicts.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str
    description: str = ""
    priority: TaskPriority = TaskPriority.MEDIUM
    tags: list[str] = Field(default_factory=list)
    created_by: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AgentFile(BaseModel):
    """An agent's self-managed status and claim record.

    Each agent owns exactly one file: agents/{name}.json.
    Only that agent writes to it. Other agents read it to
    see claims, progress, and availability.
    """

    agent: str
    last_seen: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    host: str = Field(default_factory=socket.gethostname)
    state: AgentState = AgentState.ACTIVE
    current_task: Optional[str] = None
    claimed_tasks: list[str] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    notes: str = ""


class TaskView(BaseModel):
    """A task enriched with derived status from agent claims."""

    task: Task
    status: TaskStatus = TaskStatus.OPEN
    claimed_by: Optional[str] = None


class Board:
    """The coordination board — reads tasks and agent files to
    present a unified view of work across all agents.

    Args:
        home: Path to ~/.skcapstone (or test equivalent).
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.coord_dir = self.home / "coordination"
        self.tasks_dir = self.coord_dir / "tasks"
        self.agents_dir = self.coord_dir / "agents"

    def ensure_dirs(self) -> None:
        """Create coordination directories if they don't exist."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)

    def load_tasks(self) -> list[Task]:
        """Load all task files from tasks/ directory.

        Returns:
            list[Task]: All tasks on the board.
        """
        tasks: list[Task] = []
        if not self.tasks_dir.exists():
            return tasks
        for f in sorted(self.tasks_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                tasks.append(Task.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return tasks

    def load_agents(self) -> list[AgentFile]:
        """Load all agent status files from agents/ directory.

        Returns:
            list[AgentFile]: All agent records.
        """
        agents: list[AgentFile] = []
        if not self.agents_dir.exists():
            return agents
        for f in sorted(self.agents_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                agents.append(AgentFile.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return agents

    def load_agent(self, name: str) -> Optional[AgentFile]:
        """Load a specific agent's file.

        Args:
            name: Agent name (matches filename).

        Returns:
            AgentFile or None if not found.
        """
        path = self.agents_dir / f"{name}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return AgentFile.model_validate(data)

    def save_agent(self, agent: AgentFile) -> Path:
        """Write an agent's status file.

        Args:
            agent: The agent record to save.

        Returns:
            Path to the written file.
        """
        self.ensure_dirs()
        agent.last_seen = datetime.now(timezone.utc).isoformat()
        path = self.agents_dir / f"{agent.agent}.json"
        path.write_text(
            json.dumps(agent.model_dump(), indent=2) + "\n"
        )
        return path

    def create_task(self, task: Task) -> Path:
        """Write a new task file.

        Args:
            task: The task to create.

        Returns:
            Path to the written file.
        """
        self.ensure_dirs()
        slug = task.title.lower().replace(" ", "-")[:40]
        # Reason: filename includes id + slug for human readability
        filename = f"{task.id}-{slug}.json"
        path = self.tasks_dir / filename
        path.write_text(
            json.dumps(task.model_dump(), indent=2) + "\n"
        )
        return path

    def get_task_views(self) -> list[TaskView]:
        """Build enriched task views with derived status.

        Cross-references tasks against all agent claim files to
        determine each task's effective status and who claimed it.

        Returns:
            list[TaskView]: Tasks with derived status.
        """
        tasks = self.load_tasks()
        agents = self.load_agents()

        claimed_map: dict[str, str] = {}
        completed_set: set[str] = set()
        in_progress_set: set[str] = set()

        for ag in agents:
            for tid in ag.completed_tasks:
                completed_set.add(tid)
            for tid in ag.claimed_tasks:
                claimed_map[tid] = ag.agent
            if ag.current_task:
                in_progress_set.add(ag.current_task)
                claimed_map[ag.current_task] = ag.agent

        views: list[TaskView] = []
        for t in tasks:
            if t.id in completed_set:
                status = TaskStatus.DONE
            elif t.id in in_progress_set:
                status = TaskStatus.IN_PROGRESS
            elif t.id in claimed_map:
                status = TaskStatus.CLAIMED
            else:
                status = TaskStatus.OPEN
            views.append(
                TaskView(
                    task=t,
                    status=status,
                    claimed_by=claimed_map.get(t.id),
                )
            )
        return views

    def claim_task(self, agent_name: str, task_id: str) -> AgentFile:
        """Have an agent claim a task.

        Args:
            agent_name: The claiming agent's name.
            task_id: The task ID to claim.

        Returns:
            Updated AgentFile.

        Raises:
            ValueError: If task doesn't exist or is already claimed.
        """
        views = self.get_task_views()
        target = None
        for v in views:
            if v.task.id == task_id:
                target = v
                break

        if target is None:
            raise ValueError(f"Task {task_id} not found")
        if target.status in (TaskStatus.DONE, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
            if target.claimed_by != agent_name:
                raise ValueError(
                    f"Task {task_id} already {target.status.value} by {target.claimed_by}"
                )

        agent = self.load_agent(agent_name) or AgentFile(agent=agent_name)
        if task_id not in agent.claimed_tasks:
            agent.claimed_tasks.append(task_id)
        agent.current_task = task_id
        agent.state = AgentState.ACTIVE
        self.save_agent(agent)
        return agent

    def complete_task(self, agent_name: str, task_id: str) -> AgentFile:
        """Mark a task as completed by an agent.

        Args:
            agent_name: The completing agent's name.
            task_id: The task ID completed.

        Returns:
            Updated AgentFile.
        """
        agent = self.load_agent(agent_name) or AgentFile(agent=agent_name)
        if task_id in agent.claimed_tasks:
            agent.claimed_tasks.remove(task_id)
        if task_id not in agent.completed_tasks:
            agent.completed_tasks.append(task_id)
        if agent.current_task == task_id:
            agent.current_task = agent.claimed_tasks[0] if agent.claimed_tasks else None
        self.save_agent(agent)
        return agent

    def generate_board_md(self) -> str:
        """Generate a human-readable BOARD.md from current state.

        Returns:
            Markdown string for the board overview.
        """
        views = self.get_task_views()
        agents = self.load_agents()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            "# SKCapstone Coordination Board",
            f"*Auto-generated {now} — do not edit manually*",
            "",
        ]

        for section_status, header in [
            (TaskStatus.IN_PROGRESS, "In Progress"),
            (TaskStatus.CLAIMED, "Claimed"),
            (TaskStatus.OPEN, "Open"),
            (TaskStatus.BLOCKED, "Blocked"),
            (TaskStatus.DONE, "Done"),
        ]:
            section_tasks = [v for v in views if v.status == section_status]
            if not section_tasks:
                continue
            lines.append(f"## {header} ({len(section_tasks)})")
            lines.append("")
            for v in section_tasks:
                t = v.task
                assignee = f" @{v.claimed_by}" if v.claimed_by else ""
                priority_icon = {
                    "critical": "!!!", "high": "!!", "medium": "!", "low": ""
                }.get(t.priority.value, "")
                tags_str = " ".join(f"`{tag}`" for tag in t.tags)
                lines.append(
                    f"- **[{t.id}]** {t.title}{assignee} "
                    f"{priority_icon} {tags_str}"
                )
                if t.description:
                    lines.append(f"  > {t.description[:120]}")
            lines.append("")

        if agents:
            lines.append("## Agents")
            lines.append("")
            for ag in agents:
                state_icon = {"active": "🟢", "idle": "🟡", "offline": "⚫"}.get(
                    ag.state.value, "?"
                )
                current = f" working on `{ag.current_task}`" if ag.current_task else ""
                lines.append(
                    f"- {state_icon} **{ag.agent}** ({ag.host}){current}"
                )
                if ag.notes:
                    lines.append(f"  > {ag.notes[:120]}")
            lines.append("")

        return "\n".join(lines)

    def write_board_md(self) -> Path:
        """Write BOARD.md to the coordination directory.

        Returns:
            Path to the written file.
        """
        self.ensure_dirs()
        content = self.generate_board_md()
        path = self.coord_dir / "BOARD.md"
        path.write_text(content)
        return path


_BRIEFING_PROTOCOL = """\
# SKCapstone Agent Coordination Protocol

You are an AI agent participating in a multi-agent coordination system.
This protocol works with ANY tool: Cursor, Claude Code, Aider, Windsurf,
Cline, a plain terminal, or anything that can run shell commands.

## Quick Start

1. Check what's available:   skcapstone coord status
2. Claim a task:             skcapstone coord claim <id> --agent <you>
3. Do the work
4. Mark complete:            skcapstone coord complete <id> --agent <you>
5. Create new tasks:         skcapstone coord create --title "..." --by <you>
6. Update the board:         skcapstone coord board

## Directory Layout

All data lives at ~/.skcapstone/coordination/ and syncs via Syncthing.

  ~/.skcapstone/coordination/
  ├── tasks/       # One JSON per task (creator writes once, then immutable)
  ├── agents/      # One JSON per agent (only that agent writes to its own)
  └── BOARD.md     # Human-readable overview (any agent can regenerate)

## Conflict-Free Design

- Each agent ONLY writes to its own file: agents/<your_name>.json
- Task files are write-once by the creator, then immutable
- BOARD.md can be regenerated by anyone from the source JSON files
- Syncthing propagates changes — no SSH, no APIs, no manual relay

## Task JSON Schema

{
  "id": "8-char hex",
  "title": "string",
  "description": "string",
  "priority": "critical|high|medium|low",
  "tags": ["string"],
  "created_by": "agent_name",
  "created_at": "ISO-8601",
  "acceptance_criteria": ["string"],
  "dependencies": ["task_id"],
  "notes": ["string"]
}

## Agent JSON Schema

{
  "agent": "your_name",
  "last_seen": "ISO-8601",
  "host": "hostname",
  "state": "active|idle|offline",
  "current_task": "task_id or null",
  "claimed_tasks": ["task_id"],
  "completed_tasks": ["task_id"],
  "capabilities": ["string"],
  "notes": "freeform text"
}

## Agent Names

- jarvis  — CapAuth, vault sync, crypto, testing
- opus    — Runtime, tokens, documentation, architecture
- lumina  — FEB, memory, trust, emotional intelligence
- human   — When the human creates tasks directly

## Rules

1. Read before you write — always check the board first
2. Own your file — only write to agents/<your_name>.json
3. Tasks are immutable — don't edit task files after creation
4. Claim before working — so others don't duplicate effort
5. Complete when done — move tasks to completed_tasks promptly
6. Create discovered work — if you find something needed, add a task
7. Update BOARD.md — regenerate periodically for human visibility

## Programmatic Access (Python)

    from skcapstone.coordination import Board
    board = Board(Path("~/.skcapstone").expanduser())
    tasks = board.get_task_views()      # All tasks with status
    board.claim_task("my_name", "abc1")  # Claim a task
    board.complete_task("my_name", "abc1")  # Complete it

## Integration

The ~/.skcapstone/ directory is synced by Syncthing across all devices.
When you update your agent file or create a task, it propagates to:
- Other AI sessions on the same machine
- Other machines in the Syncthing mesh
- The Docker Swarm cluster (sksync.skstack01.douno.it)
"""


def get_briefing_text(home: Path) -> str:
    """Return the full coordination protocol as plain text.

    Appends a live snapshot of current tasks and agents if the
    coordination directory exists.

    Args:
        home: Path to ~/.skcapstone

    Returns:
        Protocol text with optional live status appended.
    """
    text = _BRIEFING_PROTOCOL

    board = Board(home)
    tasks = board.load_tasks()
    agents = board.load_agents()

    if tasks or agents:
        text += "\n## Current Board Snapshot\n\n"
        views = board.get_task_views()
        for v in views:
            status_icon = {
                "open": "[ ]",
                "claimed": "[~]",
                "done": "[x]",
            }.get(v.status.value, "[?]")
            text += f"  {status_icon} [{v.task.id}] {v.task.title}"
            if v.claimed_by:
                text += f"  (by {v.claimed_by})"
            text += "\n"

        if agents:
            text += "\n### Active Agents\n\n"
            for ag in agents:
                text += f"  - {ag.agent} ({ag.state.value})"
                if ag.current_task:
                    text += f" -> {ag.current_task}"
                text += "\n"

    return text


def get_briefing_json(home: Path) -> str:
    """Return the coordination protocol and live state as JSON.

    Useful for machine consumption by agents that prefer structured data.

    Args:
        home: Path to ~/.skcapstone

    Returns:
        JSON string with protocol info and current board state.
    """
    board = Board(home)
    views = board.get_task_views()
    agents = board.load_agents()

    payload = {
        "protocol_version": "1.0",
        "coordination_dir": str(board.coord_dir),
        "commands": {
            "status": "skcapstone coord status",
            "create": 'skcapstone coord create --title "..." --by <agent>',
            "claim": "skcapstone coord claim <id> --agent <name>",
            "complete": "skcapstone coord complete <id> --agent <name>",
            "board": "skcapstone coord board",
            "briefing": "skcapstone coord briefing",
        },
        "rules": [
            "Read before you write",
            "Only write to agents/<your_name>.json",
            "Tasks are immutable after creation",
            "Claim before working",
            "Complete when done",
            "Create discovered work as new tasks",
        ],
        "agent_names": {
            "jarvis": "CapAuth, vault sync, crypto, testing",
            "opus": "Runtime, tokens, docs, architecture",
            "lumina": "FEB, memory, trust, emotional intelligence",
            "human": "Human-created tasks",
        },
        "tasks": [
            {
                "id": v.task.id,
                "title": v.task.title,
                "priority": v.task.priority.value,
                "status": v.status.value,
                "claimed_by": v.claimed_by,
                "tags": v.task.tags,
            }
            for v in views
        ],
        "agents": [
            {
                "name": ag.agent,
                "state": ag.state.value,
                "current_task": ag.current_task,
                "claimed": ag.claimed_tasks,
                "completed": ag.completed_tasks,
            }
            for ag in agents
        ],
    }
    return json.dumps(payload, indent=2)
