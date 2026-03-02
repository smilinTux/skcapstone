"""Tests for the enhanced skcapstone status command and doctor CLI.

Covers:
- _probe_llm_backends() helper
- _get_daemon_info() helper
- _get_last_conversation() helper
- status --help
- status CLI output (sections: daemon, backends, disk, conversation)
- doctor CLI (smoke tests)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Fully initialised agent home with minimal required files."""
    home = tmp_path / ".skcapstone"
    for d in [
        "identity", "memory", "trust", "security", "sync", "config",
        "memory/short-term", "memory/mid-term", "memory/long-term",
    ]:
        (home / d).mkdir(parents=True, exist_ok=True)

    (home / "manifest.json").write_text(json.dumps({
        "name": "TestAgent", "version": "0.1.0",
    }))
    (home / "identity" / "identity.json").write_text(json.dumps({
        "name": "TestAgent",
        "fingerprint": "DEADBEEF12345678",
        "capauth_managed": True,
    }))
    (home / "config" / "config.yaml").write_text(
        yaml.dump({"agent_name": "TestAgent"})
    )
    return home


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestProbeLlmBackends:
    """Tests for _probe_llm_backends()."""

    def test_ollama_available_when_reachable(self):
        """ollama marked True when HTTP probe succeeds."""
        from skcapstone.cli.status import _probe_llm_backends

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _probe_llm_backends()

        assert result["ollama"] is True

    def test_ollama_unavailable_when_unreachable(self):
        """ollama marked False when HTTP probe raises."""
        from skcapstone.cli.status import _probe_llm_backends

        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _probe_llm_backends()

        assert result["ollama"] is False

    def test_cloud_backends_via_env(self, monkeypatch):
        """Cloud backends detected via environment variables."""
        from skcapstone.cli.status import _probe_llm_backends

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

        with patch("urllib.request.urlopen", side_effect=OSError):
            result = _probe_llm_backends()

        assert result["anthropic"] is True
        assert result["grok"] is True
        assert result["kimi"] is False
        assert result["nvidia"] is False

    def test_returns_all_expected_keys(self):
        """Result always contains the five expected backend keys."""
        from skcapstone.cli.status import _probe_llm_backends

        with patch("urllib.request.urlopen", side_effect=OSError):
            result = _probe_llm_backends()

        assert set(result.keys()) == {"ollama", "anthropic", "grok", "kimi", "nvidia"}


class TestGetDaemonInfo:
    """Tests for _get_daemon_info()."""

    def test_stopped_when_no_pid(self, agent_home: Path):
        """Returns running=False when no PID file exists."""
        from skcapstone.cli.status import _get_daemon_info

        with patch("skcapstone.cli.status._get_daemon_info.__module__"):
            pass  # ensure module is imported

        with patch("skcapstone.daemon.read_pid", return_value=None):
            result = _get_daemon_info(agent_home)

        assert result["running"] is False

    def test_running_with_pid(self, agent_home: Path):
        """Returns running=True with PID when daemon is alive."""
        from skcapstone.cli.status import _get_daemon_info

        with patch("skcapstone.daemon.read_pid", return_value=12345), \
             patch("skcapstone.daemon.get_daemon_status", return_value=None):
            result = _get_daemon_info(agent_home)

        assert result["running"] is True
        assert result["pid"] == 12345

    def test_uptime_formatting_seconds(self, agent_home: Path):
        """Uptime < 60s shown as Xs."""
        from skcapstone.cli.status import _get_daemon_info

        with patch("skcapstone.daemon.read_pid", return_value=1), \
             patch("skcapstone.daemon.get_daemon_status", return_value={"uptime_seconds": 45}):
            result = _get_daemon_info(agent_home)

        assert result["uptime"] == "45s"

    def test_uptime_formatting_minutes(self, agent_home: Path):
        """Uptime 60–3599s shown as Xm Ys."""
        from skcapstone.cli.status import _get_daemon_info

        with patch("skcapstone.daemon.read_pid", return_value=1), \
             patch("skcapstone.daemon.get_daemon_status", return_value={"uptime_seconds": 125}):
            result = _get_daemon_info(agent_home)

        assert result["uptime"] == "2m 5s"

    def test_uptime_formatting_hours(self, agent_home: Path):
        """Uptime >= 3600s shown as Xh Ym."""
        from skcapstone.cli.status import _get_daemon_info

        with patch("skcapstone.daemon.read_pid", return_value=1), \
             patch("skcapstone.daemon.get_daemon_status", return_value={"uptime_seconds": 7320}):
            result = _get_daemon_info(agent_home)

        assert result["uptime"] == "2h 2m"

    def test_message_count_included(self, agent_home: Path):
        """messages key present when daemon reports > 0 messages."""
        from skcapstone.cli.status import _get_daemon_info

        with patch("skcapstone.daemon.read_pid", return_value=1), \
             patch("skcapstone.daemon.get_daemon_status", return_value={
                 "uptime_seconds": 60, "messages_received": 7,
             }):
            result = _get_daemon_info(agent_home)

        assert result["messages"] == 7


class TestGetLastConversation:
    """Tests for _get_last_conversation()."""

    def test_none_when_no_conv_dir(self, agent_home: Path):
        """Returns None when conversations/ directory does not exist."""
        from skcapstone.cli.status import _get_last_conversation

        result = _get_last_conversation(agent_home)
        assert result is None

    def test_none_when_empty_conv_dir(self, agent_home: Path):
        """Returns None when conversations/ is empty."""
        from skcapstone.cli.status import _get_last_conversation

        (agent_home / "conversations").mkdir()
        result = _get_last_conversation(agent_home)
        assert result is None

    def test_returns_most_recent_peer(self, agent_home: Path):
        """Returns the peer name of the most-recently-touched file."""
        from skcapstone.cli.status import _get_last_conversation

        conv_dir = agent_home / "conversations"
        conv_dir.mkdir()
        old_file = conv_dir / "alice.json"
        new_file = conv_dir / "bob.json"
        old_file.write_text("[]")
        time.sleep(0.02)  # ensure mtime difference
        new_file.write_text("[]")

        result = _get_last_conversation(agent_home)
        assert result is not None
        assert result["peer"] == "bob"
        assert "when" in result

    def test_recent_convo_shows_minutes(self, agent_home: Path):
        """A freshly written file shows time in minutes."""
        from skcapstone.cli.status import _get_last_conversation

        conv_dir = agent_home / "conversations"
        conv_dir.mkdir()
        (conv_dir / "charlie.json").write_text("[]")

        result = _get_last_conversation(agent_home)
        assert result is not None
        assert "m ago" in result["when"] or "0m ago" in result["when"]


# ---------------------------------------------------------------------------
# CLI integration tests (via CliRunner)
# ---------------------------------------------------------------------------


class TestStatusCLI:
    """CLI integration tests for the status command."""

    def test_help(self):
        """status --help exits 0 and shows description."""
        from skcapstone.cli import main

        result = CliRunner().invoke(main, ["status", "--help"])
        assert result.exit_code == 0
        assert "sovereign" in result.output.lower()

    def test_missing_home_exits_nonzero(self, tmp_path: Path):
        """status on a nonexistent home exits with code 1."""
        from skcapstone.cli import main

        result = CliRunner().invoke(main, ["status", "--home", str(tmp_path / "nope")])
        assert result.exit_code != 0

    def test_daemon_stopped_shown(self, agent_home: Path):
        """'STOPPED' appears when daemon is not running."""
        from skcapstone.cli import main

        with patch("skcapstone.cli.status._get_daemon_info", return_value={"running": False}), \
             patch("skcapstone.cli.status._probe_llm_backends", return_value={
                 "ollama": False, "anthropic": False, "grok": False,
                 "kimi": False, "nvidia": False,
             }), \
             patch("skcapstone.cli.status._print_consciousness_metrics"), \
             patch("skcapstone.cli.status._get_last_conversation", return_value=None):
            result = CliRunner().invoke(main, ["status", "--home", str(agent_home)])

        assert result.exit_code == 0
        assert "STOPPED" in result.output

    def test_daemon_running_shown(self, agent_home: Path):
        """'RUNNING' appears with PID when daemon is alive."""
        from skcapstone.cli import main

        with patch("skcapstone.cli.status._get_daemon_info", return_value={
                 "running": True, "pid": 42, "uptime": "5m 3s",
             }), \
             patch("skcapstone.cli.status._probe_llm_backends", return_value={
                 "ollama": True, "anthropic": False, "grok": False,
                 "kimi": False, "nvidia": False,
             }), \
             patch("skcapstone.cli.status._print_consciousness_metrics"), \
             patch("skcapstone.cli.status._get_last_conversation", return_value=None):
            result = CliRunner().invoke(main, ["status", "--home", str(agent_home)])

        assert result.exit_code == 0
        assert "RUNNING" in result.output
        assert "42" in result.output

    def test_backends_section_present(self, agent_home: Path):
        """'Backends:' line appears in status output."""
        from skcapstone.cli import main

        with patch("skcapstone.cli.status._get_daemon_info", return_value={"running": False}), \
             patch("skcapstone.cli.status._probe_llm_backends", return_value={
                 "ollama": True, "anthropic": False, "grok": False,
                 "kimi": False, "nvidia": False,
             }), \
             patch("skcapstone.cli.status._print_consciousness_metrics"), \
             patch("skcapstone.cli.status._get_last_conversation", return_value=None):
            result = CliRunner().invoke(main, ["status", "--home", str(agent_home)])

        assert result.exit_code == 0
        assert "Backends:" in result.output

    def test_last_conversation_shown(self, agent_home: Path):
        """Last convo peer and time appear when conversation data exists."""
        from skcapstone.cli import main

        with patch("skcapstone.cli.status._get_daemon_info", return_value={"running": False}), \
             patch("skcapstone.cli.status._probe_llm_backends", return_value={
                 "ollama": False, "anthropic": False, "grok": False,
                 "kimi": False, "nvidia": False,
             }), \
             patch("skcapstone.cli.status._print_consciousness_metrics"), \
             patch("skcapstone.cli.status._get_last_conversation", return_value={
                 "peer": "alice", "when": "3m ago",
             }):
            result = CliRunner().invoke(main, ["status", "--home", str(agent_home)])

        assert result.exit_code == 0
        assert "alice" in result.output
        assert "3m ago" in result.output

    def test_disk_warning_when_low(self, agent_home: Path):
        """Disk warning printed when free space < 5 GB."""
        from skcapstone.cli import main

        low_usage = MagicMock()
        low_usage.free = int(2.5 * 1024 ** 3)  # 2.5 GB

        with patch("skcapstone.cli.status._get_daemon_info", return_value={"running": False}), \
             patch("skcapstone.cli.status._probe_llm_backends", return_value={
                 "ollama": False, "anthropic": False, "grok": False,
                 "kimi": False, "nvidia": False,
             }), \
             patch("skcapstone.cli.status._print_consciousness_metrics"), \
             patch("skcapstone.cli.status._get_last_conversation", return_value=None), \
             patch("shutil.disk_usage", return_value=low_usage):
            result = CliRunner().invoke(main, ["status", "--home", str(agent_home)])

        assert result.exit_code == 0
        assert "WARNING" in result.output
        assert "2.5" in result.output

    def test_no_disk_warning_when_ample(self, agent_home: Path):
        """No disk warning when free space >= 5 GB."""
        from skcapstone.cli import main

        big_usage = MagicMock()
        big_usage.free = int(50 * 1024 ** 3)  # 50 GB

        with patch("skcapstone.cli.status._get_daemon_info", return_value={"running": False}), \
             patch("skcapstone.cli.status._probe_llm_backends", return_value={
                 "ollama": False, "anthropic": False, "grok": False,
                 "kimi": False, "nvidia": False,
             }), \
             patch("skcapstone.cli.status._print_consciousness_metrics"), \
             patch("skcapstone.cli.status._get_last_conversation", return_value=None), \
             patch("shutil.disk_usage", return_value=big_usage):
            result = CliRunner().invoke(main, ["status", "--home", str(agent_home)])

        assert result.exit_code == 0
        assert "WARNING" not in result.output


class TestDoctorCLI:
    """Smoke tests for the doctor command."""

    def test_doctor_help(self):
        """doctor --help exits 0."""
        from skcapstone.cli import main

        result = CliRunner().invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "Diagnose" in result.output

    def test_doctor_on_agent_home(self, agent_home: Path):
        """doctor produces output with at least one section heading."""
        from skcapstone.cli import main

        result = CliRunner().invoke(main, ["doctor", "--home", str(agent_home)])
        assert result.exit_code == 0
        # At least one category must be rendered
        assert any(
            kw in result.output
            for kw in ["Python Packages", "Agent Home", "Identity", "Memory"]
        )

    def test_doctor_json_output(self, agent_home: Path):
        """doctor --json-out emits valid JSON with expected keys."""
        from skcapstone.cli import main

        result = CliRunner().invoke(main, ["doctor", "--home", str(agent_home), "--json-out"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "passed" in data
        assert "failed" in data
        assert isinstance(data["checks"], list)
