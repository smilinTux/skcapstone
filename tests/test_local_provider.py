"""Tests for LocalProvider — local process-backed agent deployment.

All subprocess and filesystem side effects are controlled via tmp_path
and unittest.mock so no real crush/claude binary is required.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.blueprints.schema import AgentRole, AgentSpec, ModelTier, ResourceSpec
from skcapstone.providers.local import (
    LocalProvider,
    _build_crush_config,
    _build_session_config,
    _find_crush_binary,
    _is_claude_binary,
    _pid_is_alive,
    _read_pid,
    _read_session_state,
    _resolve_skill_paths,
    _resolve_soul_blueprint_path,
    _session_state_to_agent_status,
    _stub_script,
    _write_session_state,
)
from skcapstone.team_engine import AgentStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    role: str = "worker",
    model: str = "fast",
    memory: str = "2g",
    cores: int = 1,
    skills: list | None = None,
    soul_blueprint: str | None = None,
) -> AgentSpec:
    return AgentSpec(
        role=AgentRole(role),
        model=ModelTier(model),
        resources=ResourceSpec(memory=memory, cores=cores),
        skills=skills or [],
        env={},
        soul_blueprint=soul_blueprint,
    )


# ---------------------------------------------------------------------------
# _find_crush_binary
# ---------------------------------------------------------------------------


class TestFindCrushBinary:
    def test_returns_path_when_found(self):
        with patch("shutil.which", return_value="/usr/bin/crush"):
            result = _find_crush_binary()
        assert result == "/usr/bin/crush"

    def test_returns_none_when_not_found(self):
        with patch("shutil.which", return_value=None):
            result = _find_crush_binary()
        assert result is None

    def test_falls_back_to_claude(self):
        def _which(name):
            return "/usr/local/bin/claude" if name == "claude" else None

        with patch("shutil.which", side_effect=_which):
            result = _find_crush_binary()
        assert result == "/usr/local/bin/claude"


# ---------------------------------------------------------------------------
# _is_claude_binary
# ---------------------------------------------------------------------------


class TestIsCaudeBinary:
    def test_returns_true_for_claude(self):
        assert _is_claude_binary("/usr/local/bin/claude") is True

    def test_returns_false_for_crush(self):
        assert _is_claude_binary("/usr/bin/crush") is False

    def test_returns_false_for_python(self):
        assert _is_claude_binary("/usr/bin/python3") is False


# ---------------------------------------------------------------------------
# _resolve_soul_blueprint_path
# ---------------------------------------------------------------------------


class TestResolveSoulBlueprintPath:
    def test_returns_none_for_empty(self, tmp_path):
        result = _resolve_soul_blueprint_path(None, tmp_path)
        assert result is None

    def test_returns_absolute_path_unchanged(self, tmp_path):
        bp = tmp_path / "LUMINA.md"
        bp.write_text("soul")
        result = _resolve_soul_blueprint_path(str(bp), tmp_path)
        assert result == str(bp)

    def test_resolves_via_repo_root_blueprints(self, tmp_path):
        bp_dir = tmp_path / "soul-blueprints" / "blueprints" / "lumina"
        bp_dir.mkdir(parents=True)
        result = _resolve_soul_blueprint_path("lumina", tmp_path, repo_root=tmp_path)
        assert result == str(bp_dir)

    def test_resolves_relative_to_work_dir(self, tmp_path):
        bp = tmp_path / "my_soul.md"
        bp.write_text("soul")
        result = _resolve_soul_blueprint_path("my_soul.md", tmp_path)
        assert result == str(bp)

    def test_returns_original_when_unresolvable(self, tmp_path):
        result = _resolve_soul_blueprint_path("ghost_blueprint", tmp_path)
        assert result == "ghost_blueprint"


# ---------------------------------------------------------------------------
# _resolve_skill_paths
# ---------------------------------------------------------------------------


class TestResolveSkillPaths:
    def test_keeps_non_existent_as_is(self):
        result = _resolve_skill_paths(["my-skill"])
        assert result == ["my-skill"]

    def test_resolves_absolute_existing_path(self, tmp_path):
        skill_file = tmp_path / "my_skill.yaml"
        skill_file.write_text("skill: true")
        result = _resolve_skill_paths([str(skill_file)])
        assert result == [str(skill_file)]

    def test_empty_list_returns_empty(self):
        assert _resolve_skill_paths([]) == []


# ---------------------------------------------------------------------------
# _build_session_config
# ---------------------------------------------------------------------------


class TestBuildSessionConfig:
    def test_returns_required_keys(self, tmp_path):
        spec = _make_spec()
        config = _build_session_config("agent-1", "team-a", spec, tmp_path)
        assert config["agent_name"] == "agent-1"
        assert config["team_name"] == "team-a"
        assert config["role"] == "worker"
        assert "model" in config
        assert "memory_dir" in config
        assert "scratch_dir" in config
        assert "state_file" in config

    def test_soul_blueprint_resolved(self, tmp_path):
        spec = _make_spec(soul_blueprint=None)
        config = _build_session_config("agent-1", "team", spec, tmp_path)
        assert config["soul_blueprint"] is None

    def test_model_tier_used_as_fallback(self, tmp_path):
        spec = _make_spec(model="fast")
        with patch(
            "skcapstone.providers.local._resolve_model_via_router",
            return_value="claude-haiku-4-5",
        ):
            config = _build_session_config("a", "t", spec, tmp_path)
        assert config["model"] == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# _build_crush_config
# ---------------------------------------------------------------------------


class TestBuildCrushConfig:
    def test_has_schema_key(self, tmp_path):
        session_cfg = {"agent_name": "a", "soul_blueprint": None, "model": "fast", "role": "worker", "skills": []}
        cfg = _build_crush_config("a", session_cfg, tmp_path)
        assert "$schema" in cfg

    def test_session_keys_present(self, tmp_path):
        session_cfg = {
            "agent_name": "bob",
            "soul_blueprint": "/blueprints/lumina",
            "model": "code",
            "role": "coder",
            "skills": ["sk1"],
            "memory_dir": str(tmp_path / "memory"),
            "state_file": str(tmp_path / "state.json"),
        }
        cfg = _build_crush_config("bob", session_cfg, tmp_path)
        assert cfg["session"]["agent_name"] == "bob"
        assert cfg["session"]["role"] == "coder"
        assert cfg["session"]["model"] == "code"

    def test_none_soul_removed_from_context_paths(self, tmp_path):
        session_cfg = {"agent_name": "a", "soul_blueprint": None, "model": "fast", "role": "worker", "skills": []}
        cfg = _build_crush_config("a", session_cfg, tmp_path)
        assert None not in cfg["options"]["context_paths"]


# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


class TestSessionStateHelpers:
    def test_write_and_read_roundtrip(self, tmp_path):
        state = {"status": "running", "pid": 1234}
        _write_session_state(tmp_path, state)
        result = _read_session_state(tmp_path)
        assert result == state

    def test_read_missing_returns_none(self, tmp_path):
        result = _read_session_state(tmp_path)
        assert result is None

    def test_read_corrupt_returns_none(self, tmp_path):
        (tmp_path / "session_state.json").write_text("not-json")
        assert _read_session_state(tmp_path) is None

    def test_read_pid_returns_int(self, tmp_path):
        (tmp_path / "agent.pid").write_text("5678\n")
        assert _read_pid(tmp_path) == 5678

    def test_read_pid_missing_returns_none(self, tmp_path):
        assert _read_pid(tmp_path) is None

    def test_read_pid_invalid_returns_none(self, tmp_path):
        (tmp_path / "agent.pid").write_text("notanumber")
        assert _read_pid(tmp_path) is None


# ---------------------------------------------------------------------------
# _pid_is_alive
# ---------------------------------------------------------------------------


class TestPidIsAlive:
    def test_alive_process(self):
        # Use os.getpid() — current process is always alive.
        assert _pid_is_alive(os.getpid()) is True

    def test_dead_process(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _pid_is_alive(99999) is False

    def test_permission_denied_counts_as_alive(self):
        with patch("os.kill", side_effect=OSError("permission denied")):
            assert _pid_is_alive(1) is True


# ---------------------------------------------------------------------------
# _session_state_to_agent_status
# ---------------------------------------------------------------------------


class TestSessionStateToAgentStatus:
    def test_running_status(self):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            status = _session_state_to_agent_status({"status": "running", "pid": 1}, 1)
        assert status == AgentStatus.RUNNING

    def test_idle_status(self):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            status = _session_state_to_agent_status({"status": "idle", "pid": 1}, 1)
        assert status == AgentStatus.RUNNING

    def test_running_but_dead_pid_is_degraded(self):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=False):
            status = _session_state_to_agent_status({"status": "running", "pid": 1}, 1)
        assert status == AgentStatus.DEGRADED

    def test_error_status_is_degraded(self):
        status = _session_state_to_agent_status({"status": "error"}, None)
        assert status == AgentStatus.DEGRADED

    def test_stopped_status(self):
        status = _session_state_to_agent_status({"status": "stopped"}, None)
        assert status == AgentStatus.STOPPED

    def test_unknown_status_is_degraded(self):
        status = _session_state_to_agent_status({"status": "whatever"}, None)
        assert status == AgentStatus.DEGRADED


# ---------------------------------------------------------------------------
# LocalProvider.provision
# ---------------------------------------------------------------------------


class TestLocalProviderProvision:
    @pytest.fixture()
    def provider(self, tmp_path):
        return LocalProvider(
            home=tmp_path / "home",
            work_dir=tmp_path / "agents",
        )

    def test_creates_agent_directories(self, provider, tmp_path):
        spec = _make_spec()
        result = provider.provision("agent-1", spec, "team-a")
        agent_dir = Path(result["work_dir"])
        assert (agent_dir / "memory").is_dir()
        assert (agent_dir / "scratch").is_dir()

    def test_writes_session_json(self, provider):
        spec = _make_spec()
        result = provider.provision("agent-1", spec, "team-a")
        session_file = Path(result["work_dir"]) / "session.json"
        assert session_file.exists()
        data = json.loads(session_file.read_text())
        assert data["agent_name"] == "agent-1"
        assert data["team_name"] == "team-a"

    def test_host_is_localhost(self, provider):
        result = provider.provision("a", _make_spec(), "t")
        assert result["host"] == "localhost"

    def test_returns_session_config(self, provider):
        result = provider.provision("a", _make_spec(), "t")
        assert "session_config" in result


# ---------------------------------------------------------------------------
# LocalProvider.configure
# ---------------------------------------------------------------------------


class TestLocalProviderConfigure:
    @pytest.fixture()
    def provider(self, tmp_path):
        return LocalProvider(
            home=tmp_path / "home",
            work_dir=tmp_path / "agents",
        )

    def test_writes_crush_json(self, provider):
        spec = _make_spec()
        prov = provider.provision("agent-cfg", spec, "team")
        ok = provider.configure("agent-cfg", spec, prov)
        assert ok is True
        crush_file = Path(prov["work_dir"]) / "crush.json"
        assert crush_file.exists()

    def test_configure_missing_work_dir_returns_false(self, provider):
        result = provider.configure("a", _make_spec(), {})
        assert result is False

    def test_configure_invalid_work_dir_returns_false(self, provider):
        result = provider.configure("a", _make_spec(), {"work_dir": ""})
        assert result is False


# ---------------------------------------------------------------------------
# LocalProvider.start — stub path (no crush binary)
# ---------------------------------------------------------------------------


class TestLocalProviderStart:
    @pytest.fixture()
    def provider(self, tmp_path):
        return LocalProvider(
            home=tmp_path / "home",
            work_dir=tmp_path / "agents",
            crush_binary=None,
        )

    def test_start_missing_work_dir_returns_false(self, provider):
        result = provider.start("a", {})
        assert result is False

    def test_start_with_stub_when_no_binary(self, provider):
        spec = _make_spec()
        prov = provider.provision("agent-stub", spec, "team")
        with patch("skcapstone.providers.local._find_crush_binary", return_value=None):
            ok = provider.start("agent-stub", prov)
        assert ok is True
        assert prov.get("pid") is not None
        # Clean up the spawned stub
        try:
            os.kill(prov["pid"], signal.SIGTERM)
        except OSError:
            pass

    def test_start_with_crush_binary(self, provider, tmp_path):
        spec = _make_spec()
        prov = provider.provision("agent-crush", spec, "team")
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            with patch(
                "skcapstone.providers.local._find_crush_binary",
                return_value="/usr/bin/crush",
            ):
                ok = provider.start("agent-crush", prov)
        assert ok is True
        assert prov["pid"] == 12345
        mock_popen.assert_called_once()

    def test_start_with_claude_binary(self, provider, tmp_path):
        spec = _make_spec()
        prov = provider.provision("agent-claude", spec, "team")
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        with patch("subprocess.Popen", return_value=mock_proc):
            with patch(
                "skcapstone.providers.local._find_crush_binary",
                return_value="/usr/bin/claude",
            ):
                ok = provider.start("agent-claude", prov)
        assert ok is True
        assert prov["pid"] == 99999

    def test_start_crush_oserror_returns_false(self, provider, tmp_path):
        spec = _make_spec()
        prov = provider.provision("agent-err", spec, "team")
        with patch("subprocess.Popen", side_effect=OSError("no such file")):
            with patch(
                "skcapstone.providers.local._find_crush_binary",
                return_value="/usr/bin/crush",
            ):
                ok = provider.start("agent-err", prov)
        assert ok is False


# ---------------------------------------------------------------------------
# LocalProvider.stop
# ---------------------------------------------------------------------------


class TestLocalProviderStop:
    @pytest.fixture()
    def provider(self, tmp_path):
        return LocalProvider(
            home=tmp_path / "home",
            work_dir=tmp_path / "agents",
        )

    def test_stop_no_pid_returns_true(self, provider, tmp_path):
        ok = provider.stop("a", {"work_dir": str(tmp_path)})
        assert ok is True

    def test_stop_already_dead_returns_true(self, provider, tmp_path):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=False):
            ok = provider.stop("a", {"pid": 12345, "work_dir": str(tmp_path)})
        assert ok is True

    def test_stop_sends_sigterm(self, provider, tmp_path):
        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))

        # First call: process is alive (don't early-exit), then dead (exit wait loop)
        alive_iter = iter([True, False])

        with patch("os.kill", side_effect=fake_kill):
            with patch(
                "skcapstone.providers.local._pid_is_alive",
                side_effect=lambda p: next(alive_iter, False),
            ):
                with patch("time.sleep"):
                    ok = provider.stop("a", {"pid": 12345, "work_dir": str(tmp_path)})
        assert any(sig == signal.SIGTERM for _, sig in kill_calls)
        assert ok is True

    def test_stop_process_lookup_error_returns_true(self, provider, tmp_path):
        with patch("os.kill", side_effect=ProcessLookupError):
            with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
                ok = provider.stop("a", {"pid": 12345, "work_dir": str(tmp_path)})
        assert ok is True

    def test_stop_sigterm_oserror_returns_false(self, provider, tmp_path):
        with patch("os.kill", side_effect=OSError("permission denied")):
            with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
                ok = provider.stop("a", {"pid": 12345, "work_dir": str(tmp_path)})
        assert ok is False


# ---------------------------------------------------------------------------
# LocalProvider.destroy
# ---------------------------------------------------------------------------


class TestLocalProviderDestroy:
    def test_destroy_removes_directory(self, tmp_path):
        provider = LocalProvider(
            home=tmp_path / "home",
            work_dir=tmp_path / "agents",
        )
        spec = _make_spec()
        prov = provider.provision("agent-del", spec, "team")
        agent_dir = Path(prov["work_dir"])
        assert agent_dir.exists()

        with patch.object(provider, "stop", return_value=True):
            ok = provider.destroy("agent-del", prov)
        assert ok is True
        assert not agent_dir.exists()

    def test_destroy_missing_work_dir_returns_true(self, tmp_path):
        provider = LocalProvider(
            home=tmp_path / "home",
            work_dir=tmp_path / "agents",
        )
        with patch.object(provider, "stop", return_value=True):
            ok = provider.destroy("a", {})
        assert ok is True


# ---------------------------------------------------------------------------
# LocalProvider.health_check
# ---------------------------------------------------------------------------


class TestLocalProviderHealthCheck:
    @pytest.fixture()
    def provider(self, tmp_path):
        return LocalProvider(
            home=tmp_path / "home",
            work_dir=tmp_path / "agents",
        )

    def test_health_running_from_state_file(self, provider, tmp_path):
        work_dir = tmp_path / "agent-hc"
        work_dir.mkdir()
        _write_session_state(work_dir, {"status": "running", "pid": 1})
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            status = provider.health_check("a", {"work_dir": str(work_dir), "pid": 1})
        assert status == AgentStatus.RUNNING

    def test_health_stopped_from_state_file(self, provider, tmp_path):
        work_dir = tmp_path / "agent-stopped"
        work_dir.mkdir()
        _write_session_state(work_dir, {"status": "stopped"})
        status = provider.health_check("a", {"work_dir": str(work_dir)})
        assert status == AgentStatus.STOPPED

    def test_health_no_work_dir_no_pid_returns_stopped(self, provider):
        status = provider.health_check("a", {})
        assert status == AgentStatus.STOPPED

    def test_health_pid_alive_no_state_file(self, provider, tmp_path):
        work_dir = tmp_path / "no-state"
        work_dir.mkdir()
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            status = provider.health_check("a", {"work_dir": str(work_dir), "pid": 1})
        assert status == AgentStatus.RUNNING

    def test_health_pid_dead_no_state_file(self, provider, tmp_path):
        work_dir = tmp_path / "dead-pid"
        work_dir.mkdir()
        with patch("skcapstone.providers.local._pid_is_alive", return_value=False):
            status = provider.health_check("a", {"work_dir": str(work_dir), "pid": 1})
        assert status == AgentStatus.STOPPED


# ---------------------------------------------------------------------------
# _stub_script
# ---------------------------------------------------------------------------


class TestStubScript:
    def test_contains_agent_name(self):
        script = _stub_script("my-agent", "/tmp/state.json")
        assert "my-agent" in script

    def test_contains_state_file(self):
        script = _stub_script("a", "/tmp/agent_state.json")
        assert "/tmp/agent_state.json" in script

    def test_contains_signal_handling(self):
        script = _stub_script("a", "/tmp/s.json")
        assert "SIGTERM" in script
