"""Tests for the SKCapstone coordination module."""

import json
from pathlib import Path

import pytest

from skcapstone.coordination import (
    AgentFile,
    AgentState,
    Board,
    Task,
    TaskPriority,
    TaskStatus,
    TaskView,
)


@pytest.fixture
def board(tmp_path: Path) -> Board:
    """Create a board with a temporary home directory."""
    b = Board(tmp_path)
    b.ensure_dirs()
    return b


class TestTask:
    """Tests for the Task model."""

    def test_defaults(self):
        t = Task(title="Test task")
        assert t.title == "Test task"
        assert len(t.id) == 8
        assert t.priority == TaskPriority.MEDIUM
        assert t.tags == []
        assert t.created_at is not None

    def test_full_construction(self):
        t = Task(
            id="abc12345",
            title="Build CLI",
            description="Implement capauth CLI",
            priority=TaskPriority.HIGH,
            tags=["capauth", "cli"],
            created_by="jarvis",
            acceptance_criteria=["capauth init works"],
            dependencies=["dep001"],
        )
        assert t.id == "abc12345"
        assert t.priority == TaskPriority.HIGH
        assert "capauth" in t.tags
        assert len(t.dependencies) == 1

    def test_serialization_roundtrip(self):
        t = Task(title="Roundtrip", tags=["test"])
        data = t.model_dump()
        t2 = Task.model_validate(data)
        assert t2.title == t.title
        assert t2.id == t.id
        assert t2.tags == t.tags


class TestAgentFile:
    """Tests for the AgentFile model."""

    def test_defaults(self):
        af = AgentFile(agent="jarvis")
        assert af.agent == "jarvis"
        assert af.state == AgentState.ACTIVE
        assert af.current_task is None
        assert af.claimed_tasks == []
        assert af.completed_tasks == []

    def test_with_claims(self):
        af = AgentFile(
            agent="opus",
            current_task="task001",
            claimed_tasks=["task001", "task002"],
            capabilities=["python", "docs"],
        )
        assert af.current_task == "task001"
        assert len(af.claimed_tasks) == 2
        assert "python" in af.capabilities


class TestBoard:
    """Tests for the Board coordination logic."""

    def test_ensure_dirs(self, board: Board):
        assert board.tasks_dir.exists()
        assert board.agents_dir.exists()

    def test_create_and_load_task(self, board: Board):
        task = Task(title="Test task", tags=["test"])
        path = board.create_task(task)
        assert path.exists()
        loaded = board.load_tasks()
        assert len(loaded) == 1
        assert loaded[0].title == "Test task"

    def test_save_and_load_agent(self, board: Board):
        agent = AgentFile(agent="jarvis", capabilities=["python"])
        board.save_agent(agent)
        loaded = board.load_agent("jarvis")
        assert loaded is not None
        assert loaded.agent == "jarvis"
        assert "python" in loaded.capabilities

    def test_load_nonexistent_agent(self, board: Board):
        assert board.load_agent("nobody") is None

    def test_load_all_agents(self, board: Board):
        board.save_agent(AgentFile(agent="jarvis"))
        board.save_agent(AgentFile(agent="opus"))
        agents = board.load_agents()
        assert len(agents) == 2
        names = {a.agent for a in agents}
        assert names == {"jarvis", "opus"}

    def test_task_views_open(self, board: Board):
        board.create_task(Task(id="t1", title="Open task"))
        views = board.get_task_views()
        assert len(views) == 1
        assert views[0].status == TaskStatus.OPEN
        assert views[0].claimed_by is None

    def test_task_views_claimed(self, board: Board):
        board.create_task(Task(id="t1", title="Claimable"))
        board.claim_task("jarvis", "t1")
        views = board.get_task_views()
        assert views[0].status == TaskStatus.IN_PROGRESS
        assert views[0].claimed_by == "jarvis"

    def test_task_views_completed(self, board: Board):
        board.create_task(Task(id="t1", title="Completable"))
        board.claim_task("jarvis", "t1")
        board.complete_task("jarvis", "t1")
        views = board.get_task_views()
        assert views[0].status == TaskStatus.DONE

    def test_claim_task(self, board: Board):
        board.create_task(Task(id="t1", title="Claim me"))
        agent = board.claim_task("opus", "t1")
        assert "t1" in agent.claimed_tasks
        assert agent.current_task == "t1"

    def test_claim_nonexistent_task(self, board: Board):
        with pytest.raises(ValueError, match="not found"):
            board.claim_task("jarvis", "nonexistent")

    def test_claim_already_claimed_by_other(self, board: Board):
        board.create_task(Task(id="t1", title="Contested"))
        board.claim_task("jarvis", "t1")
        with pytest.raises(ValueError, match="already"):
            board.claim_task("opus", "t1")

    def test_claim_idempotent_same_agent(self, board: Board):
        board.create_task(Task(id="t1", title="Idempotent"))
        board.claim_task("jarvis", "t1")
        agent = board.claim_task("jarvis", "t1")
        assert agent.claimed_tasks.count("t1") == 1

    def test_complete_task(self, board: Board):
        board.create_task(Task(id="t1", title="Complete me"))
        board.claim_task("jarvis", "t1")
        agent = board.complete_task("jarvis", "t1")
        assert "t1" not in agent.claimed_tasks
        assert "t1" in agent.completed_tasks
        assert agent.current_task is None

    def test_complete_advances_current(self, board: Board):
        """Completing current task moves to next claimed task."""
        board.create_task(Task(id="t1", title="First"))
        board.create_task(Task(id="t2", title="Second"))
        board.claim_task("jarvis", "t1")
        board.claim_task("jarvis", "t2")
        agent = board.complete_task("jarvis", "t1")
        assert agent.current_task == "t2"

    def test_multiple_agents_independent(self, board: Board):
        """Two agents can work on different tasks simultaneously."""
        board.create_task(Task(id="t1", title="Jarvis work"))
        board.create_task(Task(id="t2", title="Opus work"))
        board.claim_task("jarvis", "t1")
        board.claim_task("opus", "t2")
        views = board.get_task_views()
        status_map = {v.task.id: (v.status, v.claimed_by) for v in views}
        assert status_map["t1"] == (TaskStatus.IN_PROGRESS, "jarvis")
        assert status_map["t2"] == (TaskStatus.IN_PROGRESS, "opus")


class TestBoardMd:
    """Tests for BOARD.md generation."""

    def test_empty_board(self, board: Board):
        md = board.generate_board_md()
        assert "Coordination Board" in md

    def test_board_with_tasks(self, board: Board):
        board.create_task(Task(id="t1", title="Open task", tags=["test"]))
        board.create_task(Task(id="t2", title="Done task"))
        board.claim_task("jarvis", "t2")
        board.complete_task("jarvis", "t2")
        md = board.generate_board_md()
        assert "Open task" in md
        assert "Done task" in md
        assert "jarvis" in md

    def test_write_board_md(self, board: Board):
        board.create_task(Task(id="t1", title="File test"))
        path = board.write_board_md()
        assert path.exists()
        assert "File test" in path.read_text()

    def test_board_shows_agents(self, board: Board):
        board.save_agent(
            AgentFile(agent="opus", notes="Building tokens")
        )
        md = board.generate_board_md()
        assert "opus" in md
        assert "Building tokens" in md


class TestCorruptFiles:
    """Edge cases: malformed JSON, missing fields."""

    def test_corrupt_task_file_skipped(self, board: Board):
        (board.tasks_dir / "bad.json").write_text("not json{{{")
        board.create_task(Task(id="good", title="Valid task"))
        tasks = board.load_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "good"

    def test_corrupt_agent_file_skipped(self, board: Board):
        (board.agents_dir / "bad.json").write_text("{broken")
        board.save_agent(AgentFile(agent="good"))
        agents = board.load_agents()
        assert len(agents) == 1
        assert agents[0].agent == "good"
