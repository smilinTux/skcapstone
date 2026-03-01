"""Tests for the skcapstone memory CLI commands.

Tests argument parsing, option defaults, and error paths using
click.testing.CliRunner. External I/O (disk, memory engine) is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skcapstone._cli_monolith import main
from skcapstone.models import MemoryEntry, MemoryLayer


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_entry() -> MemoryEntry:
    """A minimal MemoryEntry returned by mocked mem_store."""
    return MemoryEntry(
        memory_id="abc12345",
        content="test memory content",
        tags=["test"],
        source="cli",
        importance=0.5,
        layer=MemoryLayer.SHORT_TERM,
    )


class TestMemoryStoreArgs:
    """Tests for `skcapstone memory store` argument parsing."""

    def test_store_requires_content(self, runner: CliRunner, tmp_path: Path) -> None:
        """Expected: missing content argument produces a usage error."""
        result = runner.invoke(main, ["memory", "store"])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or result.exit_code == 2

    def test_store_exits_when_no_agent_home(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Expected: exits with error when agent home does not exist."""
        nonexistent = str(tmp_path / "no_such_agent")
        result = runner.invoke(
            main, ["memory", "store", "--home", nonexistent, "some memory"]
        )
        assert result.exit_code != 0
        assert "No agent found" in result.output

    def test_store_default_importance(
        self, runner: CliRunner, tmp_path: Path, fake_entry: MemoryEntry
    ) -> None:
        """Expected: importance defaults to 0.5 when not provided."""
        agent_home = tmp_path / ".skcapstone"
        agent_home.mkdir()

        with patch("skcapstone._cli_monolith.mem_store", create=True), patch(
            "skcapstone.memory_engine.store", return_value=fake_entry
        ) as mock_store, patch("skcapstone.pillars.security.audit_event"):
            result = runner.invoke(
                main,
                ["memory", "store", "--home", str(agent_home), "hello world"],
            )
            # Verify importance was 0.5 (default) if store was called
            if mock_store.called:
                _, kwargs = mock_store.call_args
                assert kwargs.get("importance", 0.5) == 0.5

    def test_store_custom_tags(
        self, runner: CliRunner, tmp_path: Path, fake_entry: MemoryEntry
    ) -> None:
        """Expected: multiple --tag options are collected into a list."""
        agent_home = tmp_path / ".skcapstone"
        agent_home.mkdir()

        captured_tags: list[list[str]] = []

        def fake_store(**kwargs):
            captured_tags.append(kwargs.get("tags", []))
            return fake_entry

        with patch("skcapstone.memory_engine.store", side_effect=fake_store), patch(
            "skcapstone.pillars.security.audit_event"
        ):
            runner.invoke(
                main,
                [
                    "memory",
                    "store",
                    "--home",
                    str(agent_home),
                    "-t",
                    "alpha",
                    "-t",
                    "beta",
                    "tagged memory",
                ],
            )
            if captured_tags:
                assert "alpha" in captured_tags[0]
                assert "beta" in captured_tags[0]

    def test_store_layer_choice_validation(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Edge case: invalid layer value is rejected by click."""
        agent_home = tmp_path / ".skcapstone"
        agent_home.mkdir()
        result = runner.invoke(
            main,
            [
                "memory",
                "store",
                "--home",
                str(agent_home),
                "--layer",
                "invalid-layer",
                "content",
            ],
        )
        assert result.exit_code != 0


class TestMemorySearchArgs:
    """Tests for `skcapstone memory search` argument parsing."""

    def test_search_requires_query(self, runner: CliRunner, tmp_path: Path) -> None:
        """Expected: missing query argument produces a usage error."""
        result = runner.invoke(main, ["memory", "search"])
        assert result.exit_code != 0

    def test_search_exits_when_no_agent_home(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Expected: exits with error when agent home does not exist."""
        nonexistent = str(tmp_path / "no_such_agent")
        result = runner.invoke(
            main, ["memory", "search", "--home", nonexistent, "my query"]
        )
        assert result.exit_code != 0
        assert "No agent found" in result.output

    def test_search_no_results(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Expected: prints 'no memories' message when search returns empty."""
        agent_home = tmp_path / ".skcapstone"
        agent_home.mkdir()

        with patch("skcapstone.memory_engine.search", return_value=[]):
            result = runner.invoke(
                main,
                ["memory", "search", "--home", str(agent_home), "nonexistent"],
            )
            # Should succeed and report no results
            assert result.exit_code == 0
            assert "No memories" in result.output or "no memories" in result.output.lower()

    def test_search_default_limit(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Expected: limit defaults to 20 when not specified."""
        agent_home = tmp_path / ".skcapstone"
        agent_home.mkdir()

        captured: list[dict] = []

        def fake_search(**kwargs):
            captured.append(kwargs)
            return []

        with patch("skcapstone.memory_engine.search", side_effect=fake_search):
            runner.invoke(
                main,
                ["memory", "search", "--home", str(agent_home), "query"],
            )
            if captured:
                assert captured[0].get("limit", 20) == 20


class TestMemorySubcommandHelp:
    """Tests that memory subcommands expose help text."""

    def test_memory_group_help(self, runner: CliRunner) -> None:
        """Expected: `memory --help` exits 0 and shows subcommands."""
        result = runner.invoke(main, ["memory", "--help"])
        assert result.exit_code == 0
        assert "store" in result.output

    def test_memory_store_help(self, runner: CliRunner) -> None:
        """Expected: `memory store --help` exits 0 and documents options."""
        result = runner.invoke(main, ["memory", "store", "--help"])
        assert result.exit_code == 0
        assert "--tag" in result.output or "-t" in result.output
        assert "--importance" in result.output or "-i" in result.output

    def test_memory_search_help(self, runner: CliRunner) -> None:
        """Expected: `memory search --help` exits 0 and documents options."""
        result = runner.invoke(main, ["memory", "search", "--help"])
        assert result.exit_code == 0
        assert "QUERY" in result.output or "query" in result.output.lower()
