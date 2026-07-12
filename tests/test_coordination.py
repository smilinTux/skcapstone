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
    get_briefing_json,
    get_briefing_text,
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

    def test_load_tasks_skips_malformed_and_logs(self, board: Board, caplog):
        """A malformed task file is skipped (not crashed on) and logged loudly.

        Regression: notes-as-string used to fail Pydantic validation, and
        load_tasks swallowed the error silently — dropping the task from the
        board entirely (invisible: neither open nor done). The valid task must
        still load; the bad one must produce a warning, not a silent vanish.
        """
        import json
        import logging

        good = board.create_task(Task(title="Good task"))
        assert good.exists()
        # Hand-write a malformed task file (notes as a string, not a list).
        (board.tasks_dir / "bad.json").write_text(
            json.dumps({"id": "badtask1", "title": "Bad", "notes": "oops a string"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = board.load_tasks()
        titles = {t.title for t in loaded}
        assert "Good task" in titles
        assert "Bad" not in titles
        assert any("bad.json" in r.message for r in caplog.records)

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


class TestTaskMeta:
    """Tests for the new Task.meta field (autopilot back-compat)."""

    def test_meta_defaults_empty(self):
        t = Task(title="No meta")
        assert t.meta == {}

    def test_meta_roundtrip(self):
        t = Task(title="With meta", meta={"autopilot": {"phase": "grade"}})
        t2 = Task.model_validate(t.model_dump())
        assert t2.meta == {"autopilot": {"phase": "grade"}}

    def test_legacy_task_file_without_meta_loads(self, board: Board):
        """A task file written before meta existed must still load with meta == {}."""
        (board.tasks_dir / "legacy1-old.json").write_text(
            json.dumps({"id": "legacy1", "title": "Legacy"}),
            encoding="utf-8",
        )
        tasks = board.load_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "legacy1"
        assert tasks[0].meta == {}


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


class TestBriefing:
    """Tests for the tool-agnostic briefing functions."""

    def test_briefing_text_contains_protocol(self, tmp_path: Path):
        text = get_briefing_text(tmp_path)
        assert "SKCapstone Agent Coordination Protocol" in text
        assert "skcapstone coord briefing" in text
        assert "Conflict-Free Design" in text

    def test_briefing_text_includes_live_tasks(self, board: Board):
        board.create_task(Task(id="t1", title="Test task"))
        text = get_briefing_text(board.home)
        assert "Current Board Snapshot" in text
        assert "t1" in text
        assert "Test task" in text

    def test_briefing_text_includes_agents(self, board: Board):
        board.create_task(Task(id="t2", title="Another"))
        board.save_agent(AgentFile(agent="tester", current_task="t2"))
        text = get_briefing_text(board.home)
        assert "tester" in text

    def test_briefing_text_empty_board(self, tmp_path: Path):
        text = get_briefing_text(tmp_path)
        assert "Current Board Snapshot" not in text

    def test_briefing_json_valid(self, tmp_path: Path):
        raw = get_briefing_json(tmp_path)
        data = json.loads(raw)
        assert data["protocol_version"] == "1.0"
        assert "commands" in data
        assert "rules" in data
        assert "agent_names" in data

    def test_briefing_json_includes_tasks(self, board: Board):
        board.create_task(Task(id="j1", title="JSON task", priority=TaskPriority.HIGH))
        raw = get_briefing_json(board.home)
        data = json.loads(raw)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "j1"
        assert data["tasks"][0]["priority"] == "high"

    def test_briefing_json_includes_agents(self, board: Board):
        board.save_agent(AgentFile(agent="bot", state=AgentState.ACTIVE))
        raw = get_briefing_json(board.home)
        data = json.loads(raw)
        assert len(data["agents"]) == 1
        assert data["agents"][0]["name"] == "bot"
        assert data["agents"][0]["state"] == "active"

    def test_briefing_text_mentions_tool_agnostic(self, tmp_path: Path):
        text = get_briefing_text(tmp_path)
        assert "Cursor" in text
        assert "Claude Code" in text
        assert "Aider" in text
        assert "Windsurf" in text


class TestWriteTaskRaw:
    """Tests for the atomic single-writer raw-dict helper."""

    def test_preserves_unknown_keys(self, board: Board):
        """A key the Task model does not know about must survive a raw write."""
        path = board.create_task(Task(id="abc12345", title="Raw"))
        d = json.loads(path.read_text(encoding="utf-8"))
        d["legacy_unknown"] = {"kept": True}
        path.write_text(json.dumps(d), encoding="utf-8")

        board._write_task_raw("abc12345", lambda x: x.__setitem__("touched", 1))

        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["legacy_unknown"] == {"kept": True}
        assert after["touched"] == 1
        assert after["title"] == "Raw"

    def test_returns_path_and_no_tmp_left(self, board: Board):
        board.create_task(Task(id="def45678", title="Tmp"))
        p = board._write_task_raw("def45678", lambda d: d.setdefault("meta", {}).__setitem__("x", 1))
        assert p.exists()
        assert list(board.tasks_dir.glob("*.tmp")) == []

    def test_missing_task_raises(self, board: Board):
        with pytest.raises(FileNotFoundError):
            board._write_task_raw("nope", lambda d: None)


class TestScoreTask:
    """Tests for Board.score_task."""

    def test_appends_score_and_shape(self, board: Board):
        board.create_task(Task(id="aa11bb22", title="Score me"))
        board.score_task("aa11bb22", round=1, score=4, notes="thin tests",
                         harness="claude_code", phase="grade")
        t = {x.id: x for x in board.load_tasks()}["aa11bb22"]
        ap = t.meta["autopilot"]
        assert ap["phase"] == "grade"
        assert len(ap["scores"]) == 1
        s = ap["scores"][0]
        assert s["round"] == 1 and s["score"] == 4
        assert s["notes"] == "thin tests" and s["harness"] == "claude_code"
        assert "ts" in s

    def test_ref_routes_pr_vs_artifact(self, board: Board):
        board.create_task(Task(id="cc33dd44", title="Ref"))
        board.score_task("cc33dd44", round=1, score=5, ref="https://gh/pr/1")
        board.score_task("cc33dd44", round=2, score=5, ref="worktree/xyz")
        ap = {x.id: x for x in board.load_tasks()}["cc33dd44"].meta["autopilot"]
        assert ap["pr"] == "https://gh/pr/1"
        assert ap["artifact"] == "worktree/xyz"

    def test_idempotent_same_round_harness(self, board: Board):
        board.create_task(Task(id="ee55ff66", title="Idem"))
        board.score_task("ee55ff66", round=1, score=3, harness="h1")
        board.score_task("ee55ff66", round=1, score=5, harness="h1")  # re-grade
        scores = {x.id: x for x in board.load_tasks()}["ee55ff66"].meta["autopilot"]["scores"]
        assert len(scores) == 1
        assert scores[0]["score"] == 5


class TestUpdateTask:
    """Tests for Board.update_task (reversible autonomous edits)."""

    def test_updates_and_snapshots_edits(self, board: Board):
        board.create_task(Task(id="11aa22bb", title="Edit me",
                               description="old", tags=["x"]))
        board.update_task("11aa22bb", description="new",
                          acceptance_criteria=["ac1"], add_tags=["y"],
                          run_id="run-1")
        t = {x.id: x for x in board.load_tasks()}["11aa22bb"]
        assert t.description == "new"
        assert t.acceptance_criteria == ["ac1"]
        assert t.tags == ["x", "y"]
        edits = t.meta["autopilot"]["edits"]
        by_field = {e["field"]: e for e in edits}
        assert by_field["description"]["old"] == "old"
        assert by_field["description"]["new"] == "new"
        assert by_field["tags"]["old"] == ["x"]
        assert by_field["tags"]["new"] == ["x", "y"]
        assert all(e["run_id"] == "run-1" and "ts" in e for e in edits)

    def test_add_tags_dedupes_and_no_noop_edit(self, board: Board):
        board.create_task(Task(id="33cc44dd", title="Tags", tags=["x"]))
        board.update_task("33cc44dd", add_tags=["x"])  # already present -> no change
        t = {x.id: x for x in board.load_tasks()}["33cc44dd"]
        assert t.tags == ["x"]
        assert t.meta.get("autopilot", {}).get("edits", []) == []

    def test_none_args_leave_fields_untouched(self, board: Board):
        board.create_task(Task(id="55ee66ff", title="Keep", description="keep"))
        board.update_task("55ee66ff", acceptance_criteria=["only-ac"])
        t = {x.id: x for x in board.load_tasks()}["55ee66ff"]
        assert t.description == "keep"
        assert t.acceptance_criteria == ["only-ac"]


class TestCloseTaskObsolete:
    """Tests for Board.close_task_obsolete."""

    def test_marks_obsolete_meta_and_note(self, board: Board):
        board.create_task(Task(id="77aa88bb", title="Stale work"))
        board.close_task_obsolete("77aa88bb", "superseded by epic X", run_id="run-9")
        t = {x.id: x for x in board.load_tasks()}["77aa88bb"]
        ob = t.meta["autopilot"]["obsolete"]
        assert ob["reason"] == "superseded by epic X"
        assert ob["run_id"] == "run-9"
        assert "ts" in ob
        assert any("superseded by epic X" in n for n in t.notes)

    def test_preserves_existing_scores(self, board: Board):
        board.create_task(Task(id="99cc00dd", title="Had a score"))
        board.score_task("99cc00dd", round=1, score=2)
        board.close_task_obsolete("99cc00dd", "not worth pursuing")
        ap = {x.id: x for x in board.load_tasks()}["99cc00dd"].meta["autopilot"]
        assert len(ap["scores"]) == 1
        assert ap["obsolete"]["reason"] == "not worth pursuing"


class TestUnblockedTaskIds:
    """Tests for Board.unblocked_task_ids."""

    def test_no_deps_is_unblocked(self, board: Board):
        board.create_task(Task(id="aaa11111", title="Free"))
        assert "aaa11111" in board.unblocked_task_ids()

    def test_blocked_until_dep_completed(self, board: Board):
        board.create_task(Task(id="bbb22222", title="Dep"))
        board.create_task(Task(id="ccc33333", title="Needs dep",
                               dependencies=["bbb22222"]))
        assert "ccc33333" not in board.unblocked_task_ids()
        board.claim_task("jarvis", "bbb22222")
        board.complete_task("jarvis", "bbb22222")
        unblocked = board.unblocked_task_ids()
        assert "ccc33333" in unblocked
        assert "bbb22222" in unblocked

    def test_union_across_agents(self, board: Board):
        board.create_task(Task(id="ddd44444", title="d1"))
        board.create_task(Task(id="eee55555", title="d2"))
        board.create_task(Task(id="fff66666", title="needs both",
                               dependencies=["ddd44444", "eee55555"]))
        board.claim_task("jarvis", "ddd44444")
        board.complete_task("jarvis", "ddd44444")
        board.claim_task("opus", "eee55555")
        board.complete_task("opus", "eee55555")
        assert "fff66666" in board.unblocked_task_ids()
