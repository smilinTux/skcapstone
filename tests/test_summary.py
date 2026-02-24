"""Tests for the skcapstone summary morning briefing.

Covers:
- gather_briefing returns all expected sections
- Each section handles missing data gracefully
- CLI command produces output
- JSON output is valid
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.summary import gather_briefing


@pytest.fixture
def agent_home(tmp_path):
    """Create a minimal agent home for testing."""
    home = tmp_path / ".skcapstone"
    for d in ["identity", "memory", "memory/short-term", "memory/mid-term",
              "memory/long-term", "trust", "security", "sync", "sync/outbox",
              "sync/inbox", "config", "coordination", "coordination/tasks",
              "coordination/agents", "peers"]:
        (home / d).mkdir(parents=True, exist_ok=True)

    (home / "manifest.json").write_text(json.dumps({
        "name": "BriefBot", "version": "0.1.0",
    }))
    (home / "identity" / "identity.json").write_text(json.dumps({
        "name": "BriefBot", "fingerprint": "BRIEF1234", "capauth_managed": False,
    }))
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": "BriefBot"}))
    (home / "memory" / "index.json").write_text("{}")
    (home / "memory" / "short-term" / "m1.json").write_text(
        json.dumps({"memory_id": "m1", "content": "Test memory content here",
                     "tags": [], "source": "test", "importance": 0.5,
                     "layer": "short-term", "created_at": "2026-02-24T00:00:00Z",
                     "access_count": 0, "accessed_at": None, "metadata": {}})
    )

    return home


class TestGatherBriefing:
    """Test the briefing data gatherer."""

    def test_returns_all_sections(self, agent_home):
        """Briefing contains all expected top-level keys."""
        b = gather_briefing(agent_home)

        assert "timestamp" in b
        assert "agent" in b
        assert "pillars" in b
        assert "memory" in b
        assert "board" in b
        assert "peers" in b
        assert "backups" in b
        assert "health" in b
        assert "journal" in b

    def test_agent_info(self, agent_home):
        """Agent section has name and consciousness."""
        b = gather_briefing(agent_home)
        assert b["agent"]["name"] == "BriefBot"
        assert b["agent"]["consciousness"] in ("SINGULAR", "CONSCIOUS", "AWAKENING", "UNKNOWN")

    def test_memory_has_counts(self, agent_home):
        """Memory section has layer counts."""
        b = gather_briefing(agent_home)
        assert "total" in b["memory"]
        assert b["memory"]["total"] >= 1

    def test_board_has_stats(self, agent_home):
        """Board section has done/open/in_progress."""
        b = gather_briefing(agent_home)
        assert "done" in b["board"]
        assert "open" in b["board"]
        assert "total" in b["board"]

    def test_health_has_counts(self, agent_home):
        """Health section has pass/fail counts."""
        b = gather_briefing(agent_home)
        assert "passed" in b["health"]
        assert "total" in b["health"]
        assert b["health"]["total"] > 0

    def test_handles_empty_home(self, tmp_path):
        """Briefing generates without crashing on empty home."""
        b = gather_briefing(tmp_path / "nope")
        assert "agent" in b
        assert "memory" in b

    def test_json_serializable(self, agent_home):
        """Briefing is fully JSON-serializable."""
        b = gather_briefing(agent_home)
        json_str = json.dumps(b, indent=2, default=str)
        restored = json.loads(json_str)
        assert restored["agent"]["name"] == "BriefBot"


class TestCLI:
    """Test the summary CLI command."""

    def test_summary_help(self):
        """summary --help works."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["summary", "--help"])
        assert result.exit_code == 0
        assert "briefing" in result.output.lower() or "Morning" in result.output

    def test_summary_json(self, agent_home):
        """summary --json-out produces valid JSON."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["summary", "--home", str(agent_home), "--json-out"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "agent" in data
        assert "memory" in data

    def test_summary_human(self, agent_home):
        """summary without flags shows Rich output."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["summary", "--home", str(agent_home)])
        assert result.exit_code == 0
        assert "BriefBot" in result.output
        assert "Pillars" in result.output or "Memory" in result.output
