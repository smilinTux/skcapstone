"""Tests for the native SKCapstone session briefing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.session_briefing import (
    build_session_briefing,
    format_session_briefing_text,
    load_hammertime_briefing,
)


def test_load_hammertime_briefing_respects_disable_env(monkeypatch, tmp_path: Path) -> None:
    """It returns None when HammerTime briefing is explicitly disabled."""
    monkeypatch.setenv("SK_INCLUDE_HAMMERTIME_BRIEFING", "0")
    assert load_hammertime_briefing(root=tmp_path) is None


def test_load_hammertime_briefing_parses_json(monkeypatch, tmp_path: Path) -> None:
    """It parses JSON output from the HammerTime briefing script."""
    script = tmp_path / "scripts" / "case-briefing.py"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    payload = {"summary": {"queue_size": 2}, "alert_count": 1}

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.delenv("SK_INCLUDE_HAMMERTIME_BRIEFING", raising=False)
    monkeypatch.setattr("skcapstone.session_briefing.subprocess.run", fake_run)

    assert load_hammertime_briefing(root=tmp_path) == payload


def test_build_session_briefing_includes_skcapstone_and_hammertime(monkeypatch, tmp_path: Path) -> None:
    """It builds a combined payload for startup consumers."""
    ctx = {"agent": {"name": "Aster"}, "memories": []}
    briefing = {"summary": {"queue_size": 1}, "alert_count": 0}

    monkeypatch.setattr("skcapstone.session_briefing.gather_context", lambda home, memory_limit=10: ctx)
    monkeypatch.setattr(
        "skcapstone.session_briefing.load_hammertime_briefing",
        lambda python_bin=None: briefing,
    )

    payload = build_session_briefing(tmp_path, memory_limit=3)

    assert payload["agent_home"] == str(tmp_path)
    assert payload["skcapstone_context"] == ctx
    assert payload["hammertime_briefing"] == briefing
    assert "generated_at" in payload


def test_format_session_briefing_text_contains_hammertime_section() -> None:
    """It renders a readable summary including the HammerTime section."""
    payload = {
        "generated_at": "2026-04-09T00:00:00+00:00",
        "agent_home": "/tmp/aster",
        "skcapstone_context": {
            "agent": {"name": "Aster", "is_conscious": True, "fingerprint": "abc123"},
            "pillars": {},
            "board": {"total": 0},
            "memories": [],
            "soul": {"active": None},
            "mcp": {"available": False},
            "gathered_at": "2026-04-09T00:00:00+00:00",
        },
        "hammertime_briefing": {
            "alert_count": 1,
            "summary": {"queue_size": 2},
            "top_priority": {
                "incident_id": "INC-001",
                "problem_slug": "example-problem",
                "action": "File claim of exemption",
                "status": "in-progress",
            },
            "focus_items": [
                {
                    "incident_id": "INC-001",
                    "action": "Review preferred filing",
                    "status": "in-progress",
                }
            ],
        },
    }

    output = format_session_briefing_text(payload)

    assert "# SKCapstone Session Briefing" in output
    assert "## hammertime briefing" in output
    assert "INC-001" in output
    assert "File claim of exemption" in output


def test_session_briefing_cli_json(monkeypatch, tmp_path: Path) -> None:
    """The CLI exposes the combined payload as JSON."""
    runner = CliRunner()
    payload = {
        "generated_at": "2026-04-09T00:00:00+00:00",
        "agent_home": str(tmp_path),
        "skcapstone_context": {"agent": {"name": "Aster"}},
        "hammertime_briefing": {"summary": {"queue_size": 1}, "alert_count": 0},
    }

    monkeypatch.setattr(
        "skcapstone.session_briefing.build_session_briefing",
        lambda home, memory_limit=10: payload,
    )

    result = runner.invoke(
        main,
        ["session", "briefing", "--home", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["skcapstone_context"]["agent"]["name"] == "Aster"
    assert parsed["hammertime_briefing"]["summary"]["queue_size"] == 1
