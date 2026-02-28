"""Tests for the Changelog Generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.changelog import (
    TAG_CATEGORIES,
    _categorize,
    _parse_date,
    generate_changelog,
    write_changelog,
)


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


class TestCategorize:
    """Tests for tag-based categorization."""

    def test_feature_tags(self) -> None:
        """Feature tags categorize correctly."""
        assert _categorize(["skcapstone", "memory"]) == "feature"

    def test_security_tags(self) -> None:
        """Security tags categorize correctly."""
        assert _categorize(["encryption", "security"]) == "security"

    def test_infrastructure_tags(self) -> None:
        """Infrastructure tags categorize correctly."""
        assert _categorize(["docker", "ci"]) == "infrastructure"

    def test_documentation_tags(self) -> None:
        """Documentation tags categorize correctly."""
        assert _categorize(["docs", "readme"]) == "documentation"

    def test_empty_tags(self) -> None:
        """Empty tags return 'other'."""
        assert _categorize([]) == "other"

    def test_unknown_tags(self) -> None:
        """Unrecognized tags return 'other'."""
        assert _categorize(["random", "unknown"]) == "other"

    def test_best_match_wins(self) -> None:
        """When multiple categories match, the one with most matches wins."""
        result = _categorize(["skcapstone", "skchat", "security"])
        assert result == "feature"  # 2 feature matches vs 1 security

    def test_case_insensitive(self) -> None:
        """Tags are case-insensitive."""
        assert _categorize(["SKCAPSTONE"]) == "feature"

    def test_emotional_tags(self) -> None:
        """Emotional/soul tags categorize correctly."""
        assert _categorize(["cloud9", "soul", "trust"]) == "emotional"


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


class TestParseDate:
    """Tests for ISO date extraction."""

    def test_iso_datetime(self) -> None:
        """Parses full ISO datetime."""
        assert _parse_date("2026-02-24T12:00:00+00:00") == "2026-02-24"

    def test_date_only(self) -> None:
        """Parses date-only string."""
        assert _parse_date("2026-01-15") == "2026-01-15"

    def test_invalid_string(self) -> None:
        """Invalid string returns today's date."""
        result = _parse_date("not-a-date")
        # Should be YYYY-MM-DD format
        assert len(result) == 10
        assert result[4] == "-"

    def test_none_input(self) -> None:
        """None input returns today's date."""
        result = _parse_date(None)
        assert len(result) == 10


# ---------------------------------------------------------------------------
# Changelog generation
# ---------------------------------------------------------------------------


def _setup_board(home: Path, tasks: list[dict]) -> None:
    """Create a minimal coordination board with tasks and agent files.

    Status is derived from agent files, not the task JSON.  Tasks whose
    ``status`` field is ``"done"`` are added to a test agent's
    ``completed_tasks`` list so the Board marks them as done.
    """
    coord_dir = home / "coordination" / "tasks"
    coord_dir.mkdir(parents=True)
    agents_dir = home / "coordination" / "agents"
    agents_dir.mkdir(parents=True)

    completed_ids: list[str] = []
    for task in tasks:
        # Strip the non-schema "status" key before writing the task file.
        task_data = {k: v for k, v in task.items() if k != "status"}
        task_file = coord_dir / f"{task['id']}-test.json"
        task_file.write_text(json.dumps(task_data), encoding="utf-8")

        if task.get("status") == "done":
            completed_ids.append(task["id"])

    # Create an agent file so the Board can derive "done" status.
    if completed_ids:
        agent_file = agents_dir / "test-agent.json"
        agent_file.write_text(
            json.dumps({
                "agent": "test-agent",
                "completed_tasks": completed_ids,
                "claimed_tasks": completed_ids,
            }),
            encoding="utf-8",
        )


class TestGenerateChangelog:
    """Tests for changelog generation."""

    def test_empty_board(self, tmp_path: Path) -> None:
        """Empty board generates minimal changelog."""
        (tmp_path / "coordination" / "tasks").mkdir(parents=True)
        (tmp_path / "coordination" / "agents").mkdir(parents=True)

        result = generate_changelog(tmp_path)
        assert "# SKCapstone Changelog" in result
        assert "Total completed: 0" in result

    def test_completed_tasks_appear(self, tmp_path: Path) -> None:
        """Completed tasks appear in the changelog."""
        _setup_board(tmp_path, [
            {
                "id": "abc123",
                "title": "Add encrypted messaging",
                "status": "done",
                "priority": "high",
                "tags": ["skchat", "encryption"],
                "created_at": "2026-02-24T12:00:00+00:00",
            },
        ])
        result = generate_changelog(tmp_path)
        assert "Add encrypted messaging" in result

    def test_open_tasks_excluded(self, tmp_path: Path) -> None:
        """Open tasks do not appear in the changelog."""
        _setup_board(tmp_path, [
            {
                "id": "abc123",
                "title": "Pending feature",
                "status": "open",
                "priority": "medium",
                "tags": ["skcapstone"],
                "created_at": "2026-02-24T12:00:00+00:00",
            },
        ])
        result = generate_changelog(tmp_path)
        assert "Pending feature" not in result

    def test_grouped_by_date(self, tmp_path: Path) -> None:
        """Tasks are grouped by date."""
        _setup_board(tmp_path, [
            {
                "id": "t1",
                "title": "Task One",
                "status": "done",
                "priority": "medium",
                "tags": ["skcapstone"],
                "created_at": "2026-02-24T12:00:00+00:00",
            },
            {
                "id": "t2",
                "title": "Task Two",
                "status": "done",
                "priority": "medium",
                "tags": ["skcapstone"],
                "created_at": "2026-02-25T12:00:00+00:00",
            },
        ])
        result = generate_changelog(tmp_path)
        assert "## 2026-02-24" in result
        assert "## 2026-02-25" in result

    def test_custom_title(self, tmp_path: Path) -> None:
        """Custom title is used."""
        (tmp_path / "coordination" / "tasks").mkdir(parents=True)
        (tmp_path / "coordination" / "agents").mkdir(parents=True)

        result = generate_changelog(tmp_path, title="My Custom Changelog")
        assert "# My Custom Changelog" in result

    def test_without_agents(self, tmp_path: Path) -> None:
        """Agent attribution can be disabled."""
        _setup_board(tmp_path, [
            {
                "id": "t1",
                "title": "Some Task",
                "status": "done",
                "priority": "medium",
                "tags": ["skcapstone"],
                "created_at": "2026-02-24T12:00:00+00:00",
            },
        ])
        result = generate_changelog(tmp_path, include_agents=False)
        assert "Some Task" in result
        assert "(@" not in result.split("Some Task")[1].split("\n")[0]


# ---------------------------------------------------------------------------
# Write changelog
# ---------------------------------------------------------------------------


class TestWriteChangelog:
    """Tests for writing changelog to file."""

    def test_writes_to_default_path(self, tmp_path: Path, monkeypatch) -> None:
        """write_changelog creates CHANGELOG.md."""
        (tmp_path / "coordination" / "tasks").mkdir(parents=True)
        (tmp_path / "coordination" / "agents").mkdir(parents=True)

        output = tmp_path / "CHANGELOG.md"
        result = write_changelog(tmp_path, output=output)
        assert result == output
        assert output.exists()
        content = output.read_text(encoding="utf-8")
        assert "SKCapstone Changelog" in content

    def test_writes_to_custom_path(self, tmp_path: Path) -> None:
        """write_changelog respects custom output path."""
        (tmp_path / "coordination" / "tasks").mkdir(parents=True)
        (tmp_path / "coordination" / "agents").mkdir(parents=True)

        output = tmp_path / "custom" / "changes.md"
        output.parent.mkdir(parents=True)
        result = write_changelog(tmp_path, output=output)
        assert result == output
        assert output.exists()
