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
import logging
import os
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """UTC now as an ISO-8601 string (shared by autopilot mutators)."""
    return datetime.now(timezone.utc).isoformat()


def _slugify_filename(text: str) -> str:
    """Convert text to a filesystem-safe slug.
    
    Removes or replaces characters that are illegal in filenames:
    - Forward slash (/) → dash (-)
    - Backslash (\\) → dash (-)
    - Colon (:) → dash (-)
    - Other special chars → removed
    
    Args:
        text: Input string (e.g., task title)
        
    Returns:
        Safe filename slug
    """
    slug = text.lower().strip()
    # Replace path separators and other illegal chars with dash
    slug = re.sub(r'[/\\:*?"<>|]', '-', slug)
    # Remove remaining non-word chars (except dash and space)
    slug = re.sub(r'[^\w\s-]', '', slug)
    # Convert spaces and underscores to single dash
    slug = re.sub(r'[\s_]+', '-', slug)
    # Remove leading/trailing dashes
    return slug.strip('-')


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
    meta: dict = Field(default_factory=dict)


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
    itil_claims: list[str] = Field(default_factory=list)
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
              In multi-agent mode, pass the shared root (not the
              per-agent home) so all agents see the same board.
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.coord_dir = self.home / "coordination"
        self.tasks_dir = self.coord_dir / "tasks"
        self.agents_dir = self.coord_dir / "agents"
        self.archive_dir = self.coord_dir / "archive"

    def ensure_dirs(self) -> None:
        """Create coordination directories if they don't exist."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def archived_ids(self) -> set[str]:
        """Union of task ids archived by any writer.

        Reads per-writer archive index files ``archive/<host>.jsonl`` (each
        line ``{"id", "archived_at", "archived_by"}``). Conflict-free: every
        writer appends only to its own host file.
        """
        ids: set[str] = set()
        if not self.archive_dir.exists():
            return ids
        for f in sorted(self.archive_dir.glob("*.jsonl")):
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    tid = json.loads(line).get("id")
                    if tid:
                        ids.add(tid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping malformed archive index %s: %s", f.name, exc)
                continue
        return ids

    def archive_task(self, task_id: str, by: str = "") -> None:
        """Archive a task by appending to this host's archive index.

        Never mutates the task file; the task stays on disk but drops out of
        the default board views.
        """
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        index = self.archive_dir / f"{socket.gethostname()}.jsonl"
        entry = {"id": task_id, "archived_at": _now_iso(), "archived_by": by}
        with index.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def load_tasks(self, include_archived: bool = False) -> list[Task]:
        """Load all task files from tasks/ directory.

        Args:
            include_archived: When False (default), tasks present in the
                archive index are omitted.

        Returns:
            list[Task]: All tasks on the board.
        """
        archived = set() if include_archived else self.archived_ids()
        tasks: list[Task] = []
        if not self.tasks_dir.exists():
            return tasks
        for f in sorted(self.tasks_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                task = Task.model_validate(data)
                if task.id in archived:
                    continue
                tasks.append(task)
            except Exception as exc:  # noqa: BLE001
                # A malformed task file must NOT vanish silently — a dropped task
                # is invisible to the board (it counts as neither open nor done).
                # Log loudly so schema drift (e.g. notes-as-string) is caught.
                logger.warning("Skipping malformed task file %s: %s", f.name, exc)
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
                data = json.loads(f.read_text(encoding="utf-8"))
                agents.append(AgentFile.model_validate(data))
            except Exception as exc:  # noqa: BLE001
                # A dropped agent file loses that agent's claims/completions from
                # the derived board status — surface it instead of swallowing.
                logger.warning("Skipping malformed agent file %s: %s", f.name, exc)
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
        data = json.loads(path.read_text(encoding="utf-8"))
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
        , encoding="utf-8")
        return path

    def create_task(self, task: Task) -> Path:
        """Write a new task file.

        Args:
            task: The task to create.

        Returns:
            Path to the written file.
        """
        self.ensure_dirs()
        slug = _slugify_filename(task.title)[:40]
        # Reason: filename includes id + slug for human readability
        filename = f"{task.id}-{slug}.json"
        path = self.tasks_dir / filename
        path.write_text(
            json.dumps(task.model_dump(), indent=2) + "\n"
        , encoding="utf-8")
        return path

    def _write_task_raw(self, task_id: str, mutate: Callable[[dict], None]) -> Path:
        """Atomically mutate a task's raw JSON dict, preserving all keys.

        This is the ONLY task-file mutation helper. It locates
        tasks/<id>-*.json, loads the raw dict (NOT through the Task model, so
        non-model keys such as meta.autopilot are never dropped), applies
        mutate(d) in place, and writes the result back atomically via a temp
        file plus os.replace.

        Single-writer safety is a hard precondition: only one process may call
        this at a time. autopilot-daily is pinned to a single node
        (nodes: [noroc2027]) for exactly this reason.

        Raises:
            FileNotFoundError: If no task file matches the id.
        """
        matches = sorted(self.tasks_dir.glob(f"{task_id}-*.json"))
        if not matches:
            exact = self.tasks_dir / f"{task_id}.json"
            if exact.exists():
                matches = [exact]
        if not matches:
            raise FileNotFoundError(
                f"No task file for id {task_id} in {self.tasks_dir}"
            )
        path = matches[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    def score_task(self, task_id: str, round: int, score: int, notes: str = "",
                   harness: str = "", phase: str | None = None,
                   ref: str | None = None) -> Path:
        """Record an autopilot grade on a task (meta.autopilot.scores[]).

        Idempotent: a re-grade of the same (round, harness) replaces that entry
        in place instead of appending a duplicate. Goes through _write_task_raw,
        so all other keys are preserved.
        """
        def _mutate(d: dict) -> None:
            ap = d.setdefault("meta", {}).setdefault("autopilot", {})
            scores = ap.setdefault("scores", [])
            entry = {
                "round": round,
                "score": score,
                "notes": notes,
                "ts": _now_iso(),
                "harness": harness,
            }
            for i, existing in enumerate(scores):
                if existing.get("round") == round and existing.get("harness") == harness:
                    scores[i] = entry
                    break
            else:
                scores.append(entry)
            if phase:
                ap["phase"] = phase
            if ref:
                ap["pr" if ref.startswith("http") else "artifact"] = ref

        return self._write_task_raw(task_id, _mutate)

    def update_task(self, task_id: str, description: str | None = None,
                    acceptance_criteria: list[str] | None = None,
                    add_tags: list[str] | None = None,
                    run_id: str | None = None) -> Path:
        """Rewrite task fields and snapshot each change for reversibility.

        Backs the Phase-0 stale action. A None argument leaves that field
        untouched. Every field that actually changes is snapshotted into
        meta.autopilot.edits[] as {field, old, new, ts, run_id}. Goes through
        the atomic raw-dict helper.
        """
        def _mutate(d: dict) -> None:
            ap = d.setdefault("meta", {}).setdefault("autopilot", {})
            edits = ap.setdefault("edits", [])
            ts = _now_iso()
            if description is not None and description != d.get("description", ""):
                edits.append({"field": "description", "old": d.get("description", ""),
                              "new": description, "ts": ts, "run_id": run_id})
                d["description"] = description
            if acceptance_criteria is not None and \
                    acceptance_criteria != d.get("acceptance_criteria", []):
                edits.append({"field": "acceptance_criteria",
                              "old": d.get("acceptance_criteria", []),
                              "new": acceptance_criteria, "ts": ts, "run_id": run_id})
                d["acceptance_criteria"] = acceptance_criteria
            if add_tags:
                existing = list(d.get("tags", []))
                merged = existing + [t for t in add_tags if t not in existing]
                if merged != existing:
                    edits.append({"field": "tags", "old": existing,
                                  "new": merged, "ts": ts, "run_id": run_id})
                    d["tags"] = merged

        return self._write_task_raw(task_id, _mutate)

    def close_task_obsolete(self, task_id: str, reason: str,
                            run_id: str | None = None) -> Path:
        """Mark a task obsolete on the task file itself.

        Task files carry no status field: done/claimed status is derived from
        agents/*.json, and autopilot is not the completing agent, so faking a
        done state via an agent file would misattribute completion. Instead
        obsolete is recorded as a machine-readable meta.autopilot.obsolete block
        plus a human-readable line appended to notes[]. Reversible and
        auditable, via the atomic raw-dict helper.
        """
        def _mutate(d: dict) -> None:
            ts = _now_iso()
            ap = d.setdefault("meta", {}).setdefault("autopilot", {})
            ap["obsolete"] = {"reason": reason, "run_id": run_id, "ts": ts}
            notes = d.setdefault("notes", [])
            if isinstance(notes, list):
                notes.append(f"[obsolete {ts}] {reason}")

        return self._write_task_raw(task_id, _mutate)

    def unblocked_task_ids(self) -> set[str]:
        """Task ids whose dependencies are all completed (Phase-0 compute).

        A task is unblocked when its dependencies are a subset of the union of
        every agent's completed_tasks. Tasks with no dependencies are trivially
        unblocked.

        Returns:
            set[str]: Task ids that are unblocked.
        """
        completed: set[str] = set()
        for ag in self.load_agents():
            completed.update(ag.completed_tasks)
        return {
            t.id for t in self.load_tasks()
            if set(t.dependencies).issubset(completed)
        }

    def release_stale_claims(self, agent: str, older_than_seconds: int) -> list[str]:
        """Release an agent's uncompleted claims if it has gone stale.

        AgentFile carries no per-claim timestamp, only last_seen, so staleness
        is keyed on last_seen: if the agent has not been seen for
        older_than_seconds, all of its claimed_tasks (uncompleted by
        construction) are released and current_task is cleared if it pointed at
        a released id. Returns the released ids (empty if the agent is unknown,
        fresh, or holds no claims).
        """
        af = self.load_agent(agent)
        if af is None:
            return []
        try:
            last_seen = datetime.fromisoformat(af.last_seen)
        except (ValueError, TypeError):
            return []
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last_seen).total_seconds()
        if age < older_than_seconds:
            return []
        released = list(af.claimed_tasks)
        if not released:
            return []
        af.claimed_tasks = []
        if af.current_task in released:
            af.current_task = None
        self.save_agent(af)
        return released

    def get_task_views(self, include_archived: bool = False) -> list[TaskView]:
        """Build enriched task views with derived status.

        Cross-references tasks against all agent claim files to
        determine each task's effective status and who claimed it.

        Args:
            include_archived: When False (default), archived tasks are omitted.

        Returns:
            list[TaskView]: Tasks with derived status.
        """
        tasks = self.load_tasks(include_archived=include_archived)
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

    def archive_done_tasks(
        self,
        older_than_days: int = 14,
        now: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> list[str]:
        """Age done tasks off the active board.

        A done task is eligible when its age (from the task's created_at, the
        only timestamp available on the derived-status model) exceeds
        ``older_than_days``. Archiving appends to this host's archive index and
        never mutates the task file.

        Args:
            older_than_days: Age threshold in days.
            now: Reference time (defaults to UTC now); injectable for tests.
            dry_run: When True, return the eligible ids without writing.

        Returns:
            list[str]: The task ids archived (or that would be, if dry_run).
        """
        ref = now or datetime.now(timezone.utc)
        cutoff = ref - timedelta(days=older_than_days)
        eligible: list[str] = []
        for view in self.get_task_views():
            if view.status != TaskStatus.DONE:
                continue
            created = view.task.created_at
            try:
                ts = datetime.fromisoformat(created)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                # No parseable timestamp: treat as old enough to archive.
                ts = cutoff - timedelta(days=1)
            if ts <= cutoff:
                eligible.append(view.task.id)
        if not dry_run:
            for tid in eligible:
                self.archive_task(tid, by="archive-done")
        return eligible

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

        # Mint Joules for completed task
        _mint_joules_for_task(self, task_id, agent_name)

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
        path.write_text(content, encoding="utf-8")
        return path


# Priority → (work_category, event_type, joules) mapping for Joule minting
_PRIORITY_JOULE_MAP: dict[str, tuple[str, str, int]] = {
    "critical": ("development", "bug_fix", 500),
    "high": ("development", "code_commit", 100),
    "medium": ("community", "support_ticket", 50),
    "low": ("development", "documentation", 25),
}


def _mint_joules_for_task(board: Board, task_id: str, agent_name: str) -> None:
    """Mint Joules via SKJoule when a task is completed.

    Looks up the task to get priority/title, maps priority to a work
    category and Joule amount, then calls JouleEngine.auto_tokenize_task().
    Failures are silently caught so tokenization never blocks task completion.

    Args:
        board: The Board instance (used to look up task data).
        task_id: ID of the completed task.
        agent_name: Agent who completed the task.
    """
    try:
        from .skjoule import JouleEngine

        # Find the task to get its metadata
        task_data: dict[str, object] = {"id": task_id, "completed_by": agent_name}
        for t in board.load_tasks():
            if t.id == task_id:
                task_data.update(t.model_dump())
                break

        priority = str(task_data.get("priority", "medium"))
        category, _event_type, joules = _PRIORITY_JOULE_MAP.get(
            priority, ("community", "support_ticket", 50)
        )

        # Inject tags so auto_tokenize_task picks up the right category
        tags = list(task_data.get("tags", []))  # type: ignore[arg-type]
        if category == "development":
            if "dev" not in tags and "development" not in tags:
                tags.append("dev")
        elif category == "community":
            if "community" not in tags:
                tags.append("community")
        task_data["tags"] = tags

        # Use assignee if available, else fall back to the active workspace agent.
        from . import active_agent_name

        worker = (
            task_data.get("completed_by")
            or task_data.get("created_by")
            or active_agent_name()
            or "agent"
        )
        task_data["completed_by"] = worker

        engine = JouleEngine()
        record = engine.auto_tokenize_task(task_data)

        if record:
            title = task_data.get("title", task_id)
            print(f"[SKJoule] Minted {record.joules} Joules for task: {title}")
    except Exception as exc:
        # Never let tokenization failure block task completion
        logger.warning("Joule tokenization failed for task %s (non-fatal): %s", task_id, exc)


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
7. Show this protocol:       skcapstone coord briefing
8. Machine-readable:         skcapstone coord briefing --format json

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
