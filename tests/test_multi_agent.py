"""Tests for multi-agent daemon isolation.

Covers:
- Per-agent home directory resolution (opus → agents/opus/, jarvis → agents/jarvis/)
- Default daemon port behavior under the profile-agnostic runtime
- Default (no-agent) mode keeps backward-compatible home and port
- SKCAPSTONE_AGENT env var propagation
- DaemonConfig accepts distinct homes and ports for simultaneous agents
- PID files are isolated per agent home
- is_running / read_pid are home-scoped (no cross-agent interference)
- CLI --agent option resolves correct home path
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone import _detect_active_agent as detect_active_agent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_daemon_shared_root(monkeypatch, tmp_path):
    """Keep read_pid/is_running from falling back to the live ~/.skcapstone.

    read_pid checks the passed home first, then falls back to the module-level
    AGENT_HOME (real ~/.skcapstone) where a running daemon's PID file may live.
    Point that fallback at an isolated empty tmp dir so the home-scoped PID
    isolation these tests assert holds regardless of a live daemon on the host.
    """
    import skcapstone.daemon as daemon_mod

    isolated_root = tmp_path / "isolated-shared-root"
    isolated_root.mkdir()
    monkeypatch.setattr(daemon_mod, "AGENT_HOME", str(isolated_root))


def _make_agent_home(tmp_path: Path, agent: str) -> Path:
    """Create a minimal agent home inside tmp_path/agents/<agent>/."""
    home = tmp_path / "agents" / agent
    home.mkdir(parents=True)
    return home


# ---------------------------------------------------------------------------
# 1. _resolve_agent_home  - home directory isolation
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


class TestActiveAgentDetection:
    """The shared resolver must not guess between fleet identities."""

    def test_explicit_environment_wins(self, tmp_path: Path, monkeypatch):
        _make_agent_home(tmp_path, "jarvis")
        _make_agent_home(tmp_path, "lumina")
        monkeypatch.setenv("SKAGENT", "jarvis")

        assert detect_active_agent(str(tmp_path)) == "jarvis"

    def test_single_installed_agent_is_safe_fallback(self, tmp_path: Path, monkeypatch):
        import skcapstone

        _make_agent_home(tmp_path, "jarvis")
        monkeypatch.delenv("SKAGENT", raising=False)
        monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
        monkeypatch.setattr(skcapstone, "DEFAULT_AGENT", "")

        assert detect_active_agent(str(tmp_path)) == "jarvis"

    def test_multiple_agents_require_explicit_selection(self, tmp_path: Path, monkeypatch):
        import skcapstone

        _make_agent_home(tmp_path, "jarvis")
        _make_agent_home(tmp_path, "lumina")
        monkeypatch.delenv("SKAGENT", raising=False)
        monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
        monkeypatch.setattr(skcapstone, "DEFAULT_AGENT", "")

        assert detect_active_agent(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# 2. _resolve_agent_port  - port isolation
# ---------------------------------------------------------------------------


class TestResolveAgentPort:
    def test_known_agent_uses_registered_port(self):
        """Known agents use their explicit registered port."""
        from skcapstone import AGENT_PORTS
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("opus", None) == AGENT_PORTS["opus"]

    def test_second_known_agent_uses_registered_port(self):
        """Jarvis uses its own explicit registered port."""
        from skcapstone import AGENT_PORTS
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("jarvis", None) == AGENT_PORTS["jarvis"]

    def test_known_agents_resolve_to_distinct_ports(self):
        """opus, lumina, and jarvis each resolve to a different port."""
        from skcapstone.cli.daemon import _resolve_agent_port

        ports = {
            _resolve_agent_port("opus", None),
            _resolve_agent_port("lumina", None),
            _resolve_agent_port("jarvis", None),
        }
        assert len(ports) == 3

    def test_no_known_agent_lands_on_skcomms_port(self):
        """No known agent (and no default) may resolve onto skcomms' 9384."""
        from skcapstone import AGENT_PORTS
        from skcapstone.cli.daemon import _resolve_agent_port

        assert 9384 not in AGENT_PORTS.values()
        for agent in ("opus", "lumina", "jarvis"):
            assert _resolve_agent_port(agent, None) != 9384

    def test_explicit_port_overrides_agent_default(self):
        """Explicit --port always wins over the agent default."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("opus", 9999) == 9999
        assert _resolve_agent_port("jarvis", 8000) == 8000

    def test_no_agent_defaults_to_default_port(self):
        """Single-agent / no-flag mode uses the package default port."""
        from skcapstone import DEFAULT_PORT
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port(None, None) == DEFAULT_PORT

    def test_unknown_agent_gets_dynamic_range_port(self):
        """An unknown agent gets a port in the dedicated dynamic range."""
        from skcapstone import DYNAMIC_PORT_BASE, DYNAMIC_PORT_SPAN
        from skcapstone.cli.daemon import _resolve_agent_port

        result = _resolve_agent_port("brandnew", None)
        assert DYNAMIC_PORT_BASE <= result < DYNAMIC_PORT_BASE + DYNAMIC_PORT_SPAN

    def test_unknown_agent_port_is_deterministic(self):
        """The same unknown agent always resolves to the same port (restart-stable)."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("scholar", None) == _resolve_agent_port("scholar", None)

    def test_unknown_agent_never_uses_reserved_fleet_port(self):
        """An unknown agent never lands on a documented fleet port."""
        from skcapstone import AGENT_PORTS, FLEET_RESERVED_PORTS
        from skcapstone.cli.daemon import _resolve_agent_port

        for agent in ("artisan", "herald", "sentinel", "architect", "steward", "coder"):
            port = _resolve_agent_port(agent, None)
            assert port not in FLEET_RESERVED_PORTS
            # ...and never silently reuses another *known* agent's port either.
            assert port not in set(AGENT_PORTS.values())

    def test_distinct_unknown_agents_get_distinct_ports(self):
        """Different unknown agents get different ports (no collision)."""
        from skcapstone.cli.daemon import _resolve_agent_port

        a = _resolve_agent_port("herald", None)
        b = _resolve_agent_port("sentinel", None)
        assert a != b

    def test_two_agents_on_one_host_never_collide(self):
        """No two of the known + representative unknown agents share a port."""
        from skcapstone.cli.daemon import _resolve_agent_port

        agents = ["opus", "lumina", "jarvis", "artisan", "herald", "scholar", "steward"]
        ports = [_resolve_agent_port(a, None) for a in agents]
        assert len(set(ports)) == len(ports)

    def test_explicit_ports_can_differ_for_isolated_agents(self):
        """Simultaneous agent daemons can still isolate by explicit port."""
        from skcapstone.cli.daemon import _resolve_agent_port

        assert _resolve_agent_port("opus", 7777) != _resolve_agent_port("jarvis", 7778)


# ---------------------------------------------------------------------------
# 3. AGENT_PORTS registry in __init__
# ---------------------------------------------------------------------------


class TestAgentPortsRegistry:
    def test_opus_registered(self):
        from skcapstone import AGENT_PORTS

        assert "opus" in AGENT_PORTS

    def test_jarvis_registered(self):
        from skcapstone import AGENT_PORTS

        assert "jarvis" in AGENT_PORTS

    def test_lumina_registered(self):
        from skcapstone import AGENT_PORTS

        assert "lumina" in AGENT_PORTS

    def test_registered_ports_are_distinct(self):
        """Every known agent has a unique port (no shared bind)."""
        from skcapstone import AGENT_PORTS

        values = list(AGENT_PORTS.values())
        assert len(set(values)) == len(values)

    def test_no_registered_port_is_a_fleet_port(self):
        """No known agent is assigned a reserved fleet port."""
        from skcapstone import AGENT_PORTS, FLEET_RESERVED_PORTS

        assert not (set(AGENT_PORTS.values()) & FLEET_RESERVED_PORTS)

    def test_all_ports_are_ints(self):
        from skcapstone import AGENT_PORTS

        assert AGENT_PORTS
        assert all(isinstance(port, int) for port in AGENT_PORTS.values())


# ---------------------------------------------------------------------------
# 4. PID-file isolation - is_running / read_pid are home-scoped
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
# 5. DaemonConfig - simultaneous distinct configs
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
    def test_env_var_keeps_shared_root_and_agent_home_resolves_subdir(self, monkeypatch):
        """SKCAPSTONE_AGENT keeps AGENT_HOME at root and agent_home() resolves the agent subdir."""
        import importlib

        monkeypatch.setenv("SKCAPSTONE_AGENT", "opus")
        monkeypatch.setenv("SKCAPSTONE_HOME", "/tmp/sk")

        import skcapstone as pkg

        importlib.reload(pkg)

        assert pkg.AGENT_HOME == "/tmp/sk"
        assert "agents/opus" in str(pkg.agent_home("opus")) or "agents\\opus" in str(
            pkg.agent_home("opus")
        )

    def test_no_env_var_uses_root_directly(self, monkeypatch):
        """Without SKCAPSTONE_AGENT, AGENT_HOME stays at the shared root."""
        import importlib

        monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
        monkeypatch.setenv("SKCAPSTONE_HOME", "/tmp/sk")

        import skcapstone as pkg

        importlib.reload(pkg)

        assert pkg.AGENT_HOME == pkg.SHARED_ROOT


# ---------------------------------------------------------------------------
# 7. hashed_agent_port - stable, non-fleet dynamic assignment
# ---------------------------------------------------------------------------


class TestHashedAgentPort:
    def test_lands_in_dynamic_range(self):
        from skcapstone import DYNAMIC_PORT_BASE, DYNAMIC_PORT_SPAN, hashed_agent_port

        for agent in ("artisan", "herald", "sentinel", "architect", "scholar"):
            port = hashed_agent_port(agent)
            assert DYNAMIC_PORT_BASE <= port < DYNAMIC_PORT_BASE + DYNAMIC_PORT_SPAN

    def test_deterministic_across_calls(self):
        from skcapstone import hashed_agent_port

        assert hashed_agent_port("coder") == hashed_agent_port("coder")

    def test_stable_not_process_salted(self):
        """Must NOT depend on Python's per-process-salted hash()."""
        # Known SHA-256-derived value: recompute the expected port independently.
        import hashlib

        from skcapstone import (
            AGENT_PORTS,
            DYNAMIC_PORT_BASE,
            DYNAMIC_PORT_SPAN,
            FLEET_RESERVED_PORTS,
            hashed_agent_port,
        )

        agent = "scholar"
        digest = hashlib.sha256(agent.encode("utf-8")).digest()
        offset = int.from_bytes(digest[:4], "big") % DYNAMIC_PORT_SPAN
        taken = FLEET_RESERVED_PORTS | set(AGENT_PORTS.values())
        while (DYNAMIC_PORT_BASE + offset) in taken:
            offset = (offset + 1) % DYNAMIC_PORT_SPAN
        assert hashed_agent_port(agent) == DYNAMIC_PORT_BASE + offset

    def test_never_returns_fleet_port(self):
        from skcapstone import FLEET_RESERVED_PORTS, hashed_agent_port

        for agent in ("a", "b", "c", "skcomms", "jarvis-heartbeat", "x" * 40):
            assert hashed_agent_port(agent) not in FLEET_RESERVED_PORTS


# ---------------------------------------------------------------------------
# 8. Bind-time behavior - collision fallback + loud degraded health
# ---------------------------------------------------------------------------


class TestApiServerBind:
    def _service(self, tmp_path: Path):
        from skcapstone.daemon import DaemonConfig, DaemonService

        home = _make_agent_home(tmp_path, "opus")
        return DaemonService(DaemonConfig(home=home, port=7777))

    def test_binds_preferred_port_when_free(self, tmp_path: Path):
        import socket as _socket
        from http.server import BaseHTTPRequestHandler

        # Find a free port to use as the preferred one.
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()

        svc = self._service(tmp_path)
        server, bound = svc._bind_api_server(free_port, BaseHTTPRequestHandler)
        try:
            assert bound == free_port
        finally:
            server.server_close()

    def test_falls_back_to_dynamic_range_on_collision(self, tmp_path: Path):
        import socket as _socket
        from http.server import BaseHTTPRequestHandler

        from skcapstone import DYNAMIC_PORT_BASE, DYNAMIC_PORT_SPAN

        # Occupy a port so the preferred bind collides (EADDRINUSE).
        blocker = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken_port = blocker.getsockname()[1]

        svc = self._service(tmp_path)
        try:
            server, bound = svc._bind_api_server(taken_port, BaseHTTPRequestHandler)
            try:
                assert bound != taken_port
                assert DYNAMIC_PORT_BASE <= bound < DYNAMIC_PORT_BASE + DYNAMIC_PORT_SPAN
            finally:
                server.server_close()
        finally:
            blocker.close()

    def test_record_api_server_ok(self, tmp_path: Path):
        svc = self._service(tmp_path)
        svc.state.record_api_server("ok", port=9383)
        snap = svc.state.snapshot()
        assert snap["api_server"]["status"] == "ok"
        assert snap["api_server"]["port"] == 9383
        assert svc.state.is_degraded() is False

    def test_rebound_marks_degraded(self, tmp_path: Path):
        svc = self._service(tmp_path)
        svc.state.record_api_server("rebound", port=9400, detail="collision")
        assert svc.state.is_degraded() is True
        assert svc.state.snapshot()["api_server"]["status"] == "rebound"

    def test_down_marks_degraded(self, tmp_path: Path):
        svc = self._service(tmp_path)
        svc.state.record_api_server("down", detail="bind failed")
        assert svc.state.is_degraded() is True

    def test_emit_api_alert_pushes_alert_event(self, tmp_path: Path):
        from unittest.mock import patch as _patch

        svc = self._service(tmp_path)
        with _patch("skcapstone.daemon._activity.push") as mock_push:
            svc._emit_api_alert("down", 9383, "bind failed")
        assert mock_push.called
        event_type, payload = mock_push.call_args.args
        assert event_type == "alert"
        assert payload["severity"] == "alert"
        assert payload["component"] == "api_server"
