"""Tests for the interactive REPL shell."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from skcapstone.shell import (
    COMMANDS,
    DISPATCH,
    _handle_capture,
    _handle_context,
    _handle_coord,
    _handle_help,
    _handle_memory,
    _handle_status,
    _handle_sync,
    _handle_trust,
)


def _init_agent(home: Path, name: str = "shell-test") -> None:
    """Set up a full agent for testing."""
    from skcapstone.pillars.identity import generate_identity
    from skcapstone.pillars.memory import initialize_memory
    from skcapstone.pillars.security import initialize_security
    from skcapstone.pillars.sync import initialize_sync
    from skcapstone.pillars.trust import initialize_trust

    generate_identity(home, name)
    initialize_memory(home)
    initialize_trust(home)
    initialize_security(home)
    initialize_sync(home)
    manifest = {"name": name, "version": "0.1.0", "created_at": "2026-01-01T00:00:00Z", "connectors": []}
    (home / "manifest.json").write_text(json.dumps(manifest))
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": name}))


class TestDispatchTable:
    """Tests for the command dispatch structure."""

    def test_all_commands_have_handlers(self):
        """Every listed command has a dispatch handler."""
        for cmd in COMMANDS:
            if cmd in ("exit", "quit"):
                continue
            assert cmd in DISPATCH, f"Missing handler for '{cmd}'"

    def test_all_handlers_callable(self):
        """All dispatch entries are callable."""
        for name, handler in DISPATCH.items():
            assert callable(handler), f"Handler '{name}' is not callable"


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_no_agent(self, tmp_agent_home: Path, capsys):
        """Status reports error when no agent exists."""
        with patch("skcapstone.shell._home", return_value=tmp_agent_home / "nope"):
            _handle_status()

    def test_status_with_agent(self, tmp_agent_home: Path, capsys):
        """Status shows agent name for initialized agent."""
        _init_agent(tmp_agent_home, "test-status")
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_status()


class TestMemoryCommand:
    """Tests for memory subcommands."""

    def test_store_and_search(self, tmp_agent_home: Path):
        """Store a memory then find it via search."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_memory(["store", "The", "sovereign", "agent", "remembers"])
            _handle_memory(["search", "sovereign"])
            _handle_memory(["list"])
            _handle_memory(["stats"])

    def test_empty_usage(self, tmp_agent_home: Path):
        """No args shows usage."""
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_memory([])


class TestCaptureCommand:
    """Tests for the capture command."""

    def test_capture_stores_memories(self, tmp_agent_home: Path):
        """Capture auto-extracts and stores memories."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_capture(["We", "decided", "to", "use", "Ed25519", "for", "all", "agent", "PGP", "keys"])

    def test_capture_empty(self, tmp_agent_home: Path):
        """Empty capture shows usage."""
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_capture([])


class TestContextCommand:
    """Tests for the context command."""

    def test_context_text(self, tmp_agent_home: Path):
        """Context in text format doesn't crash."""
        _init_agent(tmp_agent_home, "ctx-test")
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_context(["text"])

    def test_context_json(self, tmp_agent_home: Path):
        """Context in JSON format doesn't crash."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_context(["json"])

    def test_context_default(self, tmp_agent_home: Path):
        """Default context format (text) works."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_context([])


class TestTrustCommand:
    """Tests for trust subcommands."""

    def test_trust_graph_table(self, tmp_agent_home: Path):
        """Trust graph in table format works."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_trust(["graph"])

    def test_trust_graph_json(self, tmp_agent_home: Path):
        """Trust graph in JSON format works."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_trust(["graph", "json"])

    def test_trust_status(self, tmp_agent_home: Path):
        """Trust status without trust.json doesn't crash."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_trust(["status"])


class TestCoordCommand:
    """Tests for coordination subcommands."""

    def test_coord_status(self, tmp_agent_home: Path):
        """Coord status shows board info."""
        _init_agent(tmp_agent_home)
        from skcapstone.coordination import Board, Task
        board = Board(tmp_agent_home)
        board.ensure_dirs()
        board.create_task(Task(title="Shell test task"))

        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            with patch("skcapstone.shell._agent_name", return_value="shell-test"):
                _handle_coord(["status"])

    def test_coord_create(self, tmp_agent_home: Path):
        """Coord create makes a new task."""
        _init_agent(tmp_agent_home)
        from skcapstone.coordination import Board
        Board(tmp_agent_home).ensure_dirs()

        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            with patch("skcapstone.shell._agent_name", return_value="shell-test"):
                _handle_coord(["create", "Test", "task", "from", "shell"])


class TestSyncCommand:
    """Tests for sync subcommands."""

    def test_sync_status(self, tmp_agent_home: Path):
        """Sync status doesn't crash."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_sync(["status"])

    def test_sync_pull_empty(self, tmp_agent_home: Path):
        """Sync pull with empty inbox works."""
        _init_agent(tmp_agent_home)
        with patch("skcapstone.shell._home", return_value=tmp_agent_home):
            _handle_sync(["pull"])


class TestHelpCommand:
    """Tests for the help command."""

    def test_help_shows_all_commands(self):
        """Help output mentions key commands."""
        import io
        from rich.console import Console as TestConsole

        buf = io.StringIO()
        test_console = TestConsole(file=buf)
        with patch("skcapstone.shell.console", test_console):
            _handle_help()

        output = buf.getvalue()
        for keyword in ["status", "memory", "capture", "context", "trust", "coord", "sync", "help", "exit"]:
            assert keyword in output, f"Help missing '{keyword}'"
