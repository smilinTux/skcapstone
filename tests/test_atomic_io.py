"""Tests for crash-safe atomic store writes.

Covers the shared ``atomic_write_text`` helper plus its integration into the
coordination (Board) and ITIL stores. The failure cases prove that an
interrupted write never leaves a torn file on disk: the original contents
survive intact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone import atomic_io
from skcapstone.coordination import AgentFile, AgentState, Board, Task
from skcapstone.itil import ITILManager


class TestAtomicWriteText:
    """Direct tests for the atomic_write_text helper."""

    def test_happy_path_replaces_file(self, tmp_path: Path):
        """A normal write lands the full new payload atomically."""
        target = tmp_path / "data.json"
        target.write_text("old", encoding="utf-8")

        atomic_io.atomic_write_text(target, "new-contents")

        assert target.read_text(encoding="utf-8") == "new-contents"

    def test_creates_new_file(self, tmp_path: Path):
        """Writing to a missing target creates it."""
        target = tmp_path / "fresh.json"

        atomic_io.atomic_write_text(target, "hello")

        assert target.read_text(encoding="utf-8") == "hello"

    def test_no_temp_files_left_behind(self, tmp_path: Path):
        """A successful write leaves only the target, no ``.tmp`` litter."""
        target = tmp_path / "data.json"

        atomic_io.atomic_write_text(target, "payload")

        assert [p.name for p in tmp_path.iterdir()] == ["data.json"]

    def test_partial_write_failure_preserves_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If the write is interrupted, the original file is untouched.

        A plain ``write_text`` truncates in place, so a crash here would leave
        a torn/empty file. The atomic helper writes to a temp file first, so a
        failure before ``os.replace`` must leave the old contents fully intact
        and must not leave a stray temp file.
        """
        target = tmp_path / "data.json"
        original = json.dumps({"important": "keep-me"})
        target.write_text(original, encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise OSError("simulated disk failure mid-write")

        # Fail during the temp-file write, before os.replace can run.
        monkeypatch.setattr(atomic_io.os, "fsync", boom)

        with pytest.raises(OSError):
            atomic_io.atomic_write_text(target, json.dumps({"important": "torn"}))

        # Original survives byte-for-byte.
        assert target.read_text(encoding="utf-8") == original
        # No leftover temp file.
        assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


class TestCoordinationAtomicWrites:
    """Board store writers must be crash-safe."""

    def test_save_agent_atomic(self, tmp_path: Path):
        board = Board(tmp_path)
        board.ensure_dirs()
        agent = AgentFile(agent="lumina", state=AgentState.ACTIVE)

        path = board.save_agent(agent)

        assert json.loads(path.read_text(encoding="utf-8"))["agent"] == "lumina"
        # No temp litter in the agents dir.
        assert not list(board.agents_dir.glob("*.tmp"))

    def test_save_agent_interrupted_preserves_prior(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A failed re-save leaves the previously saved agent file intact."""
        board = Board(tmp_path)
        board.ensure_dirs()
        board.save_agent(AgentFile(agent="lumina", state=AgentState.ACTIVE))
        path = board.agents_dir / "lumina.json"
        good = path.read_text(encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise OSError("simulated failure")

        monkeypatch.setattr(atomic_io.os, "fsync", boom)
        with pytest.raises(OSError):
            board.save_agent(AgentFile(agent="lumina", state=AgentState.IDLE))

        assert path.read_text(encoding="utf-8") == good
        assert not list(board.agents_dir.glob("*.tmp"))

    def test_create_task_atomic(self, tmp_path: Path):
        board = Board(tmp_path)
        board.ensure_dirs()
        task = Task(title="Atomic task")

        path = board.create_task(task)

        assert json.loads(path.read_text(encoding="utf-8"))["title"] == "Atomic task"
        assert not list(board.tasks_dir.glob("*.tmp"))


class TestITILAtomicWrites:
    """ITIL store writers must be crash-safe."""

    def test_cab_vote_atomic(self, tmp_path: Path):
        store = ITILManager(tmp_path)
        store.ensure_dirs()

        vote = store.submit_cab_vote("chg-1234", "lumina", decision="approved")

        path = store.cab_dir / "chg-1234-lumina.json"
        assert json.loads(path.read_text(encoding="utf-8"))["change_id"] == "chg-1234"
        assert vote.agent == "lumina"
        assert not list(store.cab_dir.glob("*.tmp"))

    def test_cab_vote_interrupted_preserves_prior(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A failed re-vote leaves the prior vote file intact and untorn."""
        store = ITILManager(tmp_path)
        store.ensure_dirs()
        store.submit_cab_vote("chg-1234", "lumina", decision="approved")
        path = store.cab_dir / "chg-1234-lumina.json"
        good = path.read_text(encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise OSError("simulated failure")

        monkeypatch.setattr(atomic_io.os, "fsync", boom)
        with pytest.raises(OSError):
            store.submit_cab_vote("chg-1234", "lumina", decision="rejected")

        assert path.read_text(encoding="utf-8") == good
        assert not list(store.cab_dir.glob("*.tmp"))

    def test_kedb_entry_atomic(self, tmp_path: Path):
        store = ITILManager(tmp_path)
        store.ensure_dirs()

        entry = store.create_kedb_entry(title="Known error", symptoms=["boom"], root_cause="bug")

        path = store.kedb_dir / f"{entry.id}.json"
        assert json.loads(path.read_text(encoding="utf-8"))["title"] == "Known error"
        assert not list(store.kedb_dir.glob("*.tmp"))
