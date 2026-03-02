"""Tests for multi-agent daemon isolation.

Covers:
- Per-agent home directory resolution (opus → agents/opus/, jarvis → agents/jarvis/)
- Per-agent port assignment (opus=7777, jarvis=7778, unknown → next available)
- Default (no-agent) mode keeps backward-compatible home and port
- SKCAPSTONE_AGENT env var propagation
- DaemonConfig accepts distinct homes and ports for simultaneous agents
- PID files are isolated per agent home
- is_running / read_pid are home-scoped (no cross-agent interference)
- CLI --agent option resolves correct home path
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_home(tmp_path: Path, agent: str) -> Path:
    """Create a minimal agent home inside tmp_path/agents/<agent>/."""
    home = tmp_path / "agents" / agent
    home.mkdir(parents=True)
    return home


# ---------------------------------------------------------------------------
# 1. _resolve_agent_home  — home directory isolation
# ---------------------------------------------------------------------------


class TestResolveAgentHome:
    def test_named_agent_uses_agents_subdir(self, tmp_path: Path):
        """--agent opus → ~/.skcapstone/agents/opus/"""
        from skcapstone.cli.daemon import _resolve_agent_home

        with patch("skcapstone.cli.daemon.SKCAPSTONE_ROOT", str(tmp_path)):
            result = _resolve_agent_home("opus", str(tmp_path))

        assert result == (tmp_path / "agents" / "opus").expanduser()

    def test_jarvis_uses_own_subdir(self, tmp_path: Path):
        """--agent jarvis → ~/.skcapstone/agents/jarvis/"""
        from skcapstone.cli.daemon import _resolve_agent_home

        with patch("skcapstone.cli.daemon.SKCAPSTONE_ROOT", str(tmp_path)):
            result = _resolve_agent_home("jarvis", str(tmp_path))

        assert result == (tmp_path / "agents" / "jarvis").expanduser()

    def test_no_agent_uses_home_arg(self, tmp_path: Path):
        """No --agent flag → use the --home value directly (backward compat)."""
        from skcapstone.cli.daemon import _resolve_agent_home

        custom_home = str(tmp_path / "custom")
        result = _resolve_agent_home(None, custom_home)
        assert result == Path(custom_home).expanduser()

    def test_opus_and_jarvis_homes_are_distinct(self, tmp_path: Path):
        """Opus and Jarvis home paths must not overlap."""
        from skcapstone.cli.daemon import _resolve_agent_home

        with patch("skcapstone.cli.daemon.SKCAPSTONE_ROOT", str(tmp_path)):
            opus_home = _resolve_agent_home("opus", str(tmp_path))
            jarvis_home = _resolve_agent_home("jarvis", str(tmp_path))

        assert opus_home != jarvis_home
        assert "opus" in str(opus_home)
        assert "jarvis" in str(jarvis_home)


# ---------------------------------------------------------------------------
# 2. _resolve_agent_port  — port isolation
# ---------------------------------------------------------------------------


class TestResolveAgentPort:
    def test_opus_gets_7777(self):
        """opus always gets port 7777."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("opus", None) == 7777

    def test_jarvis_gets_7778(self):
        """jarvis always gets port 7778."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("jarvis", None) == 7778

    def test_explicit_port_overrides_agent_default(self):
        """Explicit --port always wins over the agent default."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("opus", 9999) == 9999
        assert _resolve_agent_port("jarvis", 8000) == 8000

    def test_no_agent_defaults_to_7777(self):
        """Single-agent / no-flag mode uses 7777."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port(None, None) == 7777

    def test_unknown_agent_gets_next_port(self):
        """An agent not in AGENT_PORTS gets max(ports)+1."""
        from skcapstone import AGENT_PORTS
        from skcapstone.cli.daemon import _resolve_agent_port

        expected = max(AGENT_PORTS.values()) + 1
        result = _resolve_agent_port("brandnew", None)
        assert result == expected

    def test_opus_and_jarvis_ports_differ(self):
        """Opus and Jarvis must listen on different ports."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("opus", None) != _resolve_agent_port("jarvis", None)


# ---------------------------------------------------------------------------
# 3. AGENT_PORTS registry in __init__
# ---------------------------------------------------------------------------


class TestAgentPortsRegistry:
    def test_opus_registered(self):
        from skcapstone import AGENT_PORTS

        assert "opus" in AGENT_PORTS
        assert AGENT_PORTS["opus"] == 7777

    def test_jarvis_registered(self):
        from skcapstone import AGENT_PORTS

        assert "jarvis" in AGENT_PORTS
        assert AGENT_PORTS["jarvis"] == 7778

    def test_all_ports_unique(self):
        from skcapstone import AGENT_PORTS

        ports = list(AGENT_PORTS.values())
        assert len(ports) == len(set(ports)), "Duplicate ports in AGENT_PORTS"


# ---------------------------------------------------------------------------
# 4. PID-file isolation — is_running / read_pid are home-scoped
# ---------------------------------------------------------------------------


class TestPidIsolation:
    def test_pid_file_written_to_agent_home(self, tmp_path: Path):
        """PID file is created inside the agent's own home directory."""
        from skcapstone.daemon import DaemonConfig, DaemonService

        opus_home = _make_agent_home(tmp_path, "opus")
        config = DaemonConfig(home=opus_home, port=7777)

        svc = DaemonService(config)
        # Call _write_pid directly without starting the full daemon.
        svc._write_pid()

        pid_file = opus_home / "daemon.pid"
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_pid_files_are_isolated_between_agents(self, tmp_path: Path):
        """Writing opus PID does not affect jarvis PID file."""
        from skcapstone.daemon import DaemonConfig, DaemonService, read_pid

        opus_home = _make_agent_home(tmp_path, "opus")
        jarvis_home = _make_agent_home(tmp_path, "jarvis")

        opus_svc = DaemonService(DaemonConfig(home=opus_home, port=7777))
        opus_svc._write_pid()

        # Jarvis home has no PID file → read_pid returns None.
        assert read_pid(jarvis_home) is None

    def test_is_running_false_without_pid_file(self, tmp_path: Path):
        """is_running returns False when no PID file exists."""
        from skcapstone.daemon import is_running

        empty_home = _make_agent_home(tmp_path, "nobody")
        assert is_running(empty_home) is False

    def test_read_pid_returns_current_pid_after_write(self, tmp_path: Path):
        """read_pid returns the PID we just wrote."""
        from skcapstone.daemon import DaemonConfig, DaemonService, read_pid

        home = _make_agent_home(tmp_path, "opus")
        svc = DaemonService(DaemonConfig(home=home, port=7777))
        svc._write_pid()

        assert read_pid(home) == os.getpid()


# ---------------------------------------------------------------------------
# 5. DaemonConfig — simultaneous distinct configs
# ---------------------------------------------------------------------------


class TestDaemonConfigMultiAgent:
    def test_two_configs_have_distinct_homes_and_ports(self, tmp_path: Path):
        """Two DaemonConfig instances for opus/jarvis stay isolated."""
        from skcapstone.daemon import DaemonConfig

        opus_home = _make_agent_home(tmp_path, "opus")
        jarvis_home = _make_agent_home(tmp_path, "jarvis")

        opus_cfg = DaemonConfig(home=opus_home, port=7777)
        jarvis_cfg = DaemonConfig(home=jarvis_home, port=7778)

        assert opus_cfg.home != jarvis_cfg.home
        assert opus_cfg.port != jarvis_cfg.port
        assert opus_cfg.port == 7777
        assert jarvis_cfg.port == 7778

    def test_log_files_are_in_respective_homes(self, tmp_path: Path):
        """Each agent's log file lives under its own home."""
        from skcapstone.daemon import DaemonConfig

        opus_home = _make_agent_home(tmp_path, "opus")
        jarvis_home = _make_agent_home(tmp_path, "jarvis")

        opus_cfg = DaemonConfig(home=opus_home, port=7777)
        jarvis_cfg = DaemonConfig(home=jarvis_home, port=7778)

        assert str(opus_cfg.log_file).startswith(str(opus_home))
        assert str(jarvis_cfg.log_file).startswith(str(jarvis_home))
        assert opus_cfg.log_file != jarvis_cfg.log_file


# ---------------------------------------------------------------------------
# 6. SKCAPSTONE_AGENT env-var path derivation in __init__
# ---------------------------------------------------------------------------


class TestAgentHomeEnvVar:
    def test_env_var_produces_agents_subdir(self, monkeypatch):
        """SKCAPSTONE_AGENT=opus → AGENT_HOME includes agents/opus."""
        import importlib

        monkeypatch.setenv("SKCAPSTONE_AGENT", "opus")
        monkeypatch.setenv("SKCAPSTONE_ROOT", "/tmp/sk")

        import skcapstone as pkg
        importlib.reload(pkg)

        assert "agents/opus" in pkg.AGENT_HOME or "agents\\opus" in pkg.AGENT_HOME

    def test_no_env_var_uses_root_directly(self, monkeypatch):
        """Without SKCAPSTONE_AGENT, AGENT_HOME == SKCAPSTONE_ROOT."""
        import importlib

        monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
        monkeypatch.setenv("SKCAPSTONE_ROOT", "/tmp/sk")

        import skcapstone as pkg
        importlib.reload(pkg)

        assert pkg.AGENT_HOME == pkg.SHARED_ROOT
