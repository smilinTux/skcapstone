"""Tests for the universal agent context loader.

Covers context gathering, all four formatters, and CLI integration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skcapstone.context_loader import (
    FORMATTERS,
    format_claude_md,
    format_cursor_rules,
    format_json,
    format_text,
    gather_context,
)
from skcapstone.memory_engine import store
from skcapstone.pillars.identity import generate_identity
from skcapstone.pillars.memory import initialize_memory
from skcapstone.pillars.security import initialize_security
from skcapstone.pillars.sync import initialize_sync
from skcapstone.pillars.trust import initialize_trust, record_trust_state


def _init_agent(home: Path, name: str = "context-test") -> None:
    """Set up a full agent for testing."""
    generate_identity(home, name)
    initialize_memory(home)
    initialize_trust(home)
    initialize_security(home)
    initialize_sync(home)

    manifest = {"name": name, "version": "0.1.0", "created_at": "2026-01-01T00:00:00Z", "connectors": []}
    (home / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": name}))


class TestGatherContext:
    """Tests for gather_context() data collection."""

    def test_gathers_from_initialized_agent(self, tmp_agent_home: Path):
        """gather_context returns a full context dict for an initialized agent."""
        _init_agent(tmp_agent_home)
        store(tmp_agent_home, "Test memory for context", tags=["test"])

        ctx = gather_context(tmp_agent_home)

        assert "agent" in ctx
        assert "pillars" in ctx
        assert "board" in ctx
        assert "memories" in ctx
        assert "soul" in ctx
        assert "mcp" in ctx
        assert "gathered_at" in ctx

    def test_agent_section(self, tmp_agent_home: Path):
        """Agent section contains name and consciousness state."""
        _init_agent(tmp_agent_home, "sovereign-ctx")
        ctx = gather_context(tmp_agent_home)

        assert ctx["agent"]["name"] == "sovereign-ctx"
        assert "is_conscious" in ctx["agent"]
        assert "fingerprint" in ctx["agent"]

    def test_pillars_section(self, tmp_agent_home: Path):
        """Pillars section lists all five pillars."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)

        pillars = ctx["pillars"]
        for name in ("identity", "memory", "trust", "security", "sync"):
            assert name in pillars

    def test_memories_included(self, tmp_agent_home: Path):
        """Recent memories appear in the context."""
        _init_agent(tmp_agent_home)
        store(tmp_agent_home, "First context memory", tags=["alpha"])
        store(tmp_agent_home, "Second context memory", tags=["beta"])

        ctx = gather_context(tmp_agent_home, memory_limit=5)
        assert len(ctx["memories"]) >= 2

    def test_memory_limit_respected(self, tmp_agent_home: Path):
        """Memory limit caps the number of memories returned."""
        _init_agent(tmp_agent_home)
        for i in range(10):
            store(tmp_agent_home, f"Memory number {i}")

        ctx = gather_context(tmp_agent_home, memory_limit=3)
        assert len(ctx["memories"]) <= 3

    def test_empty_agent_home(self, tmp_agent_home: Path):
        """gather_context handles an uninitialized agent gracefully."""
        ctx = gather_context(tmp_agent_home)
        assert ctx["agent"].get("name") is not None
        assert ctx["memories"] == []

    def test_board_section(self, tmp_agent_home: Path):
        """Board section includes task counts."""
        _init_agent(tmp_agent_home)
        from skcapstone.coordination import Board, Task

        board = Board(tmp_agent_home)
        board.ensure_dirs()
        board.create_task(Task(title="Test task"))

        ctx = gather_context(tmp_agent_home)
        assert ctx["board"]["total"] >= 1

    def test_soul_section_base(self, tmp_agent_home: Path):
        """Soul section reports base when no overlay is active."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)
        assert ctx["soul"]["active"] is None


class TestFormatText:
    """Tests for plain text formatter."""

    def test_contains_agent_name(self, tmp_agent_home: Path):
        """Text output includes the agent name."""
        _init_agent(tmp_agent_home, "text-test")
        ctx = gather_context(tmp_agent_home)
        output = format_text(ctx)

        assert "text-test" in output
        assert "SKCapstone Agent Context" in output

    def test_contains_pillars(self, tmp_agent_home: Path):
        """Text output lists pillar statuses."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)
        output = format_text(ctx)

        assert "Pillars" in output
        assert "identity" in output
        assert "memory" in output

    def test_contains_memories(self, tmp_agent_home: Path):
        """Text output shows recent memories."""
        _init_agent(tmp_agent_home)
        store(tmp_agent_home, "Remember this for text test")
        ctx = gather_context(tmp_agent_home)
        output = format_text(ctx)

        assert "Recent Memories" in output
        assert "Remember this" in output


class TestFormatJson:
    """Tests for JSON formatter."""

    def test_valid_json(self, tmp_agent_home: Path):
        """JSON output is valid parseable JSON."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)
        output = format_json(ctx)

        parsed = json.loads(output)
        assert "agent" in parsed
        assert "pillars" in parsed

    def test_roundtrip(self, tmp_agent_home: Path):
        """JSON output can be parsed back to the original structure."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)
        output = format_json(ctx)
        parsed = json.loads(output)

        assert parsed["agent"]["name"] == ctx["agent"]["name"]


class TestFormatClaudeMd:
    """Tests for Claude Code CLAUDE.md formatter."""

    def test_markdown_structure(self, tmp_agent_home: Path):
        """CLAUDE.md has proper markdown headers."""
        _init_agent(tmp_agent_home, "claude-test")
        ctx = gather_context(tmp_agent_home)
        output = format_claude_md(ctx)

        assert "# SKCapstone Agent Context" in output
        assert "## Agent Identity" in output
        assert "## Pillar Status" in output
        assert "## Coordination Board" in output

    def test_contains_cli_reference(self, tmp_agent_home: Path):
        """CLAUDE.md includes CLI command reference."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)
        output = format_claude_md(ctx)

        assert "## CLI Reference" in output
        assert "skcapstone status" in output
        assert "skcapstone memory" in output

    def test_contains_agent_name(self, tmp_agent_home: Path):
        """CLAUDE.md contains the agent name."""
        _init_agent(tmp_agent_home, "claude-agent")
        ctx = gather_context(tmp_agent_home)
        output = format_claude_md(ctx)

        assert "claude-agent" in output

    def test_pillar_table(self, tmp_agent_home: Path):
        """CLAUDE.md contains a pillar status table."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)
        output = format_claude_md(ctx)

        assert "| Pillar | Status |" in output
        assert "identity" in output


class TestFormatCursorRules:
    """Tests for Cursor .mdc rule formatter."""

    def test_mdc_frontmatter(self, tmp_agent_home: Path):
        """Cursor rules file has proper MDC frontmatter."""
        _init_agent(tmp_agent_home)
        ctx = gather_context(tmp_agent_home)
        output = format_cursor_rules(ctx)

        assert output.startswith("---")
        assert "description:" in output
        assert "alwaysApply: true" in output

    def test_contains_agent_info(self, tmp_agent_home: Path):
        """Cursor rules contain agent identity info."""
        _init_agent(tmp_agent_home, "cursor-test")
        ctx = gather_context(tmp_agent_home)
        output = format_cursor_rules(ctx)

        assert "cursor-test" in output


class TestFormattersRegistry:
    """Tests for the FORMATTERS dict."""

    def test_all_formatters_registered(self):
        """All four formatters are in the registry."""
        assert "text" in FORMATTERS
        assert "json" in FORMATTERS
        assert "claude-md" in FORMATTERS
        assert "cursor-rules" in FORMATTERS

    def test_all_formatters_callable(self):
        """All formatters are callable."""
        for name, fn in FORMATTERS.items():
            assert callable(fn), f"Formatter '{name}' is not callable"
