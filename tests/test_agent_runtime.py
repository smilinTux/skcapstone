"""Tests for LocalProvider agent runtime lifecycle.

Covers:
- Path resolution helpers (soul blueprints, skills)
- Session and crush config builders
- provision(), configure(), start(), stop(), health_check(), destroy()
- Crush binary launch path vs. stub fallback
- Session state → AgentStatus mapping
- Edge cases and failure scenarios

All subprocess calls are mocked so no real processes or binaries are required.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, call, patch

import pytest

from skcapstone.blueprints.schema import (
    AgentRole,
    AgentSpec,
    ModelTier,
    ProviderType,
    ResourceSpec,
)
from skcapstone.providers.local import (
    LocalProvider,
    _SESSION_STATE_FILE,
    _PID_FILE,
    _SESSION_CONFIG_FILE,
    _CRUSH_CONFIG_FILE,
    _STATE_RUNNING,
    _STATE_STOPPED,
    _STATE_ERROR,
    _STATE_IDLE,
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
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_spec(
    role: str = "worker",
    model: str = "fast",
    soul_blueprint: str | None = None,
    skills: list | None = None,
    env: dict | None = None,
    model_name: str | None = None,
) -> AgentSpec:
    """Build a minimal AgentSpec for testing."""
    return AgentSpec(
        role=AgentRole(role),
        model=ModelTier(model),
        model_name=model_name,
        resources=ResourceSpec(),
        soul_blueprint=soul_blueprint,
        skills=skills or [],
        env=env or {},
    )


def _provision_result(
    work_dir: str,
    pid: int | None = None,
    session_config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a typical provision_result dict."""
    result: Dict[str, Any] = {"host": "localhost", "work_dir": work_dir}
    if pid is not None:
        result["pid"] = pid
    if session_config is not None:
        result["session_config"] = session_config
    return result


@pytest.fixture()
def provider(tmp_path: Path) -> LocalProvider:
    """LocalProvider with tmp_path as both home and work_dir."""
    return LocalProvider(
        home=tmp_path / "home",
        work_dir=tmp_path / "agents",
        repo_root=tmp_path / "repo",
    )


@pytest.fixture()
def agent_dir(tmp_path: Path) -> Path:
    """Create and return a fake agent working directory."""
    d = tmp_path / "agents" / "test-agent"
    d.mkdir(parents=True)
    (d / "memory").mkdir()
    (d / "scratch").mkdir()
    return d


# ---------------------------------------------------------------------------
# _find_crush_binary
# ---------------------------------------------------------------------------


class TestFindCrushBinary:
    """Tests for _find_crush_binary helper."""

    def test_returns_none_when_not_on_path(self):
        with patch("shutil.which", return_value=None):
            assert _find_crush_binary() is None

    def test_returns_crush_path_when_found(self):
        with patch("shutil.which", side_effect=lambda x: "/usr/bin/crush" if x == "crush" else None):
            result = _find_crush_binary()
        assert result == "/usr/bin/crush"

    def test_returns_openclaw_path_as_fallback(self):
        with patch(
            "shutil.which",
            side_effect=lambda x: None if x == "crush" else "/usr/local/bin/openclaw",
        ):
            result = _find_crush_binary()
        assert result == "/usr/local/bin/openclaw"

    def test_returns_claude_path_as_last_fallback(self):
        def _which(name):
            if name == "claude":
                return "/bin/claude"
            return None

        with patch("shutil.which", side_effect=_which):
            result = _find_crush_binary()
        assert result == "/bin/claude"


# ---------------------------------------------------------------------------
# _resolve_soul_blueprint_path
# ---------------------------------------------------------------------------


class TestResolveSoulBlueprintPath:
    """Tests for _resolve_soul_blueprint_path helper."""

    def test_returns_none_for_none_input(self, tmp_path):
        assert _resolve_soul_blueprint_path(None, tmp_path) is None

    def test_returns_absolute_path_unchanged_when_exists(self, tmp_path):
        soul = tmp_path / "lumina.yaml"
        soul.write_text("soul: lumina")
        result = _resolve_soul_blueprint_path(str(soul), tmp_path)
        assert result == str(soul)

    def test_resolves_via_soul_blueprints_dir(self, tmp_path):
        blueprint_dir = tmp_path / "soul-blueprints" / "blueprints" / "lumina"
        blueprint_dir.mkdir(parents=True)
        result = _resolve_soul_blueprint_path("lumina", tmp_path, repo_root=tmp_path)
        assert result == str(blueprint_dir)

    def test_resolves_direct_under_soul_blueprints(self, tmp_path):
        soul_file = tmp_path / "soul-blueprints" / "sentinel.yaml"
        soul_file.parent.mkdir(parents=True)
        soul_file.write_text("soul: sentinel")
        result = _resolve_soul_blueprint_path(
            "sentinel.yaml", tmp_path, repo_root=tmp_path
        )
        assert result == str(soul_file)

    def test_returns_original_value_when_unresolvable(self, tmp_path):
        result = _resolve_soul_blueprint_path("nonexistent", tmp_path)
        assert result == "nonexistent"

    def test_resolves_relative_to_work_dir(self, tmp_path):
        soul_file = tmp_path / "soul.yaml"
        soul_file.write_text("soul: local")
        result = _resolve_soul_blueprint_path("soul.yaml", tmp_path)
        assert result == str(soul_file)


# ---------------------------------------------------------------------------
# _resolve_skill_paths
# ---------------------------------------------------------------------------


class TestResolveSkillPaths:
    """Tests for _resolve_skill_paths helper."""

    def test_empty_list_returns_empty(self):
        assert _resolve_skill_paths([]) == []

    def test_absolute_existing_path_kept(self, tmp_path):
        skill = tmp_path / "my.skill"
        skill.write_text("skill")
        result = _resolve_skill_paths([str(skill)])
        assert result == [str(skill)]

    def test_resolves_via_openclaw_skills_file(self, tmp_path):
        skill_file = tmp_path / "openclaw-skills" / "discord-bot-manager.skill"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("skill data")
        result = _resolve_skill_paths(["discord-bot-manager"], repo_root=tmp_path)
        assert result == [str(skill_file)]

    def test_resolves_via_openclaw_skills_dir(self, tmp_path):
        skill_dir = tmp_path / "openclaw-skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        result = _resolve_skill_paths(["my-skill"], repo_root=tmp_path)
        assert result == [str(skill_dir)]

    def test_unknown_skill_kept_as_is(self, tmp_path):
        result = _resolve_skill_paths(["unknown-skill"], repo_root=tmp_path)
        assert result == ["unknown-skill"]

    def test_mixed_resolved_and_unresolved(self, tmp_path):
        skill_file = tmp_path / "openclaw-skills" / "known.skill"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("")
        result = _resolve_skill_paths(["known", "unknown"], repo_root=tmp_path)
        assert result[0] == str(skill_file)
        assert result[1] == "unknown"


# ---------------------------------------------------------------------------
# _build_session_config
# ---------------------------------------------------------------------------


class TestBuildSessionConfig:
    """Tests for _build_session_config builder."""

    def test_required_keys_present(self, tmp_path):
        spec = _make_spec(role="coder", model="reason")
        config = _build_session_config("agent-1", "team-1", spec, tmp_path)
        for key in ("agent_name", "team_name", "role", "model", "model_tier",
                    "soul_blueprint", "skills", "memory_dir", "scratch_dir",
                    "state_file", "env"):
            assert key in config

    def test_agent_and_team_name_set(self, tmp_path):
        spec = _make_spec()
        config = _build_session_config("my-agent", "my-team", spec, tmp_path)
        assert config["agent_name"] == "my-agent"
        assert config["team_name"] == "my-team"

    def test_model_name_override_used(self, tmp_path):
        spec = _make_spec(model_name="kimi-k2.5")
        config = _build_session_config("a", "t", spec, tmp_path)
        assert config["model"] == "kimi-k2.5"

    def test_model_tier_always_set(self, tmp_path):
        spec = _make_spec(model="code")
        config = _build_session_config("a", "t", spec, tmp_path)
        assert config["model_tier"] == "code"

    def test_soul_blueprint_resolved(self, tmp_path):
        soul_dir = tmp_path / "soul-blueprints" / "blueprints" / "lumina"
        soul_dir.mkdir(parents=True)
        spec = _make_spec(soul_blueprint="lumina")
        config = _build_session_config("a", "t", spec, tmp_path, repo_root=tmp_path)
        assert config["soul_blueprint"] == str(soul_dir)

    def test_skills_resolved(self, tmp_path):
        skill_file = tmp_path / "openclaw-skills" / "test.skill"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("")
        spec = _make_spec(skills=["test"])
        config = _build_session_config("a", "t", spec, tmp_path, repo_root=tmp_path)
        assert config["skills"] == [str(skill_file)]

    def test_env_vars_included(self, tmp_path):
        spec = _make_spec(env={"MY_KEY": "my_value"})
        config = _build_session_config("a", "t", spec, tmp_path)
        assert config["env"]["MY_KEY"] == "my_value"


# ---------------------------------------------------------------------------
# _build_crush_config
# ---------------------------------------------------------------------------


class TestBuildCrushConfig:
    """Tests for _build_crush_config builder."""

    def test_has_schema_key(self, tmp_path):
        config = _build_crush_config("agent", {}, tmp_path)
        assert "$schema" in config

    def test_session_block_present(self, tmp_path):
        session = {"agent_name": "a", "model": "fast", "role": "worker", "skills": []}
        config = _build_crush_config("a", session, tmp_path)
        assert "session" in config
        assert config["session"]["agent_name"] == "a"

    def test_no_none_in_context_paths(self, tmp_path):
        config = _build_crush_config("a", {"soul_blueprint": None}, tmp_path)
        assert None not in config["options"]["context_paths"]

    def test_soul_blueprint_in_context_paths_when_set(self, tmp_path):
        session = {"soul_blueprint": "/path/to/soul.yaml"}
        config = _build_crush_config("a", session, tmp_path)
        assert "/path/to/soul.yaml" in config["options"]["context_paths"]


# ---------------------------------------------------------------------------
# LocalProvider.provision
# ---------------------------------------------------------------------------


class TestProvision:
    """Tests for LocalProvider.provision()."""

    def test_creates_work_dir(self, provider, tmp_path):
        spec = _make_spec()
        result = provider.provision("my-agent", spec, "my-team")
        assert Path(result["work_dir"]).exists()

    def test_creates_memory_and_scratch_dirs(self, provider):
        spec = _make_spec()
        result = provider.provision("agent-x", spec, "team-y")
        wd = Path(result["work_dir"])
        assert (wd / "memory").is_dir()
        assert (wd / "scratch").is_dir()

    def test_writes_config_json(self, provider):
        spec = _make_spec()
        result = provider.provision("agent-x", spec, "team-y")
        config_file = Path(result["work_dir"]) / "config.json"
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["agent_name"] == "agent-x"

    def test_writes_session_json(self, provider):
        spec = _make_spec()
        result = provider.provision("agent-x", spec, "team-y")
        session_file = Path(result["work_dir"]) / _SESSION_CONFIG_FILE
        assert session_file.exists()

    def test_returns_host_localhost(self, provider):
        spec = _make_spec()
        result = provider.provision("agent-x", spec, "team-y")
        assert result["host"] == "localhost"

    def test_session_config_in_result(self, provider):
        spec = _make_spec(soul_blueprint="lumina", skills=["code"])
        result = provider.provision("agent-x", spec, "team-y")
        assert "session_config" in result
        sc = result["session_config"]
        assert sc["agent_name"] == "agent-x"
        assert sc["model_tier"] == "fast"

    def test_edge_agent_name_with_hyphens(self, provider):
        spec = _make_spec()
        result = provider.provision("team-abc-worker-1", spec, "team-abc")
        assert Path(result["work_dir"]).exists()


# ---------------------------------------------------------------------------
# LocalProvider.configure
# ---------------------------------------------------------------------------


class TestConfigure:
    """Tests for LocalProvider.configure()."""

    def test_returns_true_on_success(self, provider, tmp_path):
        spec = _make_spec()
        pr = provider.provision("agent-c", spec, "team-c")
        assert provider.configure("agent-c", spec, pr) is True

    def test_writes_crush_json(self, provider, tmp_path):
        spec = _make_spec()
        pr = provider.provision("agent-c", spec, "team-c")
        provider.configure("agent-c", spec, pr)
        crush_file = Path(pr["work_dir"]) / _CRUSH_CONFIG_FILE
        assert crush_file.exists()
        data = json.loads(crush_file.read_text())
        assert "$schema" in data

    def test_returns_false_when_work_dir_missing(self, provider):
        spec = _make_spec()
        assert provider.configure("ghost", spec, {}) is False

    def test_crush_json_contains_session_block(self, provider):
        spec = _make_spec(model="code", role="coder")
        pr = provider.provision("agent-d", spec, "team-d")
        provider.configure("agent-d", spec, pr)
        crush_file = Path(pr["work_dir"]) / _CRUSH_CONFIG_FILE
        data = json.loads(crush_file.read_text())
        assert data["session"]["role"] == "coder"


# ---------------------------------------------------------------------------
# LocalProvider.start — crush binary path
# ---------------------------------------------------------------------------


class TestStartWithCrushBinary:
    """Tests for LocalProvider.start() when crush binary is available."""

    @pytest.fixture()
    def _patched_popen(self):
        """Patch subprocess.Popen to return a fake process."""
        mock_proc = MagicMock()
        mock_proc.pid = 42000
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            yield mock_popen, mock_proc

    def test_spawns_crush_subprocess(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("agent-s", spec, "team-s")
        provider.configure("agent-s", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            result = provider.start("agent-s", pr)

        assert result is True
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "/usr/bin/crush"
        assert "run" in cmd

    def test_sets_pid_in_provision_result(self, provider, tmp_path, _patched_popen):
        _, mock_proc = _patched_popen
        mock_proc.pid = 55555
        spec = _make_spec()
        pr = provider.provision("agent-pid", spec, "team-s")
        provider.configure("agent-pid", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            provider.start("agent-pid", pr)

        assert pr["pid"] == 55555

    def test_writes_pid_file(self, provider, tmp_path, _patched_popen):
        _, mock_proc = _patched_popen
        mock_proc.pid = 11111
        spec = _make_spec()
        pr = provider.provision("agent-pf", spec, "team-s")
        provider.configure("agent-pf", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            provider.start("agent-pf", pr)

        pid_file = Path(pr["work_dir"]) / _PID_FILE
        assert pid_file.read_text().strip() == "11111"

    def test_writes_session_state_file(self, provider, tmp_path, _patched_popen):
        _, mock_proc = _patched_popen
        mock_proc.pid = 22222
        spec = _make_spec()
        pr = provider.provision("agent-ss", spec, "team-s")
        provider.configure("agent-ss", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            provider.start("agent-ss", pr)

        state = json.loads((Path(pr["work_dir"]) / _SESSION_STATE_FILE).read_text())
        assert state["status"] == _STATE_RUNNING
        assert state["pid"] == 22222

    def test_passes_soul_blueprint_in_env(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(soul_blueprint="lumina")
        pr = provider.provision("agent-soul", spec, "team-s")
        provider.configure("agent-soul", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            provider.start("agent-soul", pr)

        env = mock_popen.call_args[1]["env"]
        assert "SOUL_BLUEPRINT" in env
        # Value may be resolved path or original slug
        assert "lumina" in env["SOUL_BLUEPRINT"].lower() or env["SOUL_BLUEPRINT"] == "lumina"

    def test_passes_skills_as_json_in_env(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(skills=["code-review", "docs"])
        pr = provider.provision("agent-skills", spec, "team-s")
        provider.configure("agent-skills", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            provider.start("agent-skills", pr)

        env = mock_popen.call_args[1]["env"]
        parsed_skills = json.loads(env["AGENT_SKILLS"])
        assert "code-review" in parsed_skills
        assert "docs" in parsed_skills

    def test_passes_model_tier_in_env(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(model="reason")
        pr = provider.provision("agent-model", spec, "team-s")
        provider.configure("agent-model", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            provider.start("agent-model", pr)

        env = mock_popen.call_args[1]["env"]
        assert env["AGENT_MODEL_TIER"] == "reason"

    def test_returns_false_on_popen_error(self, provider, tmp_path):
        spec = _make_spec()
        pr = provider.provision("agent-err", spec, "team-s")
        provider.configure("agent-err", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            with patch("subprocess.Popen", side_effect=OSError("permission denied")):
                result = provider.start("agent-err", pr)

        assert result is False

    def test_returns_false_when_work_dir_missing(self, provider):
        result = provider.start("ghost", {})
        assert result is False


# ---------------------------------------------------------------------------
# LocalProvider.start — stub fallback
# ---------------------------------------------------------------------------


class TestStartStubFallback:
    """Tests for LocalProvider.start() stub when crush is not available."""

    @pytest.fixture()
    def _patched_popen(self):
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            yield mock_popen, mock_proc

    def test_falls_back_to_stub(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("agent-stub", spec, "team-s")

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value=None,
        ):
            result = provider.start("agent-stub", pr)

        assert result is True
        mock_popen.assert_called_once()
        # Stub uses python -c ...
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == os.sys.executable
        assert cmd[1] == "-c"

    def test_stub_writes_running_state(self, provider, _patched_popen):
        _, mock_proc = _patched_popen
        mock_proc.pid = 7777
        spec = _make_spec()
        pr = provider.provision("agent-stub2", spec, "team-s")

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value=None,
        ):
            provider.start("agent-stub2", pr)

        state = json.loads(
            (Path(pr["work_dir"]) / _SESSION_STATE_FILE).read_text()
        )
        assert state["status"] == _STATE_RUNNING
        assert state["pid"] == 7777


# ---------------------------------------------------------------------------
# LocalProvider.stop
# ---------------------------------------------------------------------------


class TestStop:
    """Tests for LocalProvider.stop()."""

    def test_returns_true_when_no_pid(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir))
        assert provider.stop("no-pid-agent", pr) is True

    def test_returns_true_when_pid_already_dead(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir), pid=99999999)
        with patch("skcapstone.providers.local._pid_is_alive", return_value=False):
            result = provider.stop("dead-agent", pr)
        assert result is True

    def test_sends_sigterm(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir), pid=12345)
        # Return True once (pre-SIGTERM check), then False (loop exit)
        alive_seq = iter([True] + [False] * 60)
        with patch("os.kill") as mock_kill:
            with patch(
                "skcapstone.providers.local._pid_is_alive",
                side_effect=alive_seq,
            ):
                provider.stop("agent-term", pr)

        mock_kill.assert_any_call(12345, signal.SIGTERM)

    def test_writes_stopped_state_after_stop(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir), pid=12345)
        alive_seq = iter([True] + [False] * 60)
        with patch("os.kill"):
            with patch(
                "skcapstone.providers.local._pid_is_alive",
                side_effect=alive_seq,
            ):
                provider.stop("agent-state", pr)

        state = _read_session_state(agent_dir)
        assert state is not None
        assert state["status"] == _STATE_STOPPED

    def test_sends_sigkill_after_timeout(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir), pid=12345)
        # Always alive so that SIGKILL path is triggered
        with patch("os.kill") as mock_kill:
            with patch(
                "skcapstone.providers.local._pid_is_alive",
                return_value=True,
            ):
                with patch("skcapstone.providers.local._STOP_TIMEOUT_SECONDS", 0):
                    with patch(
                        "skcapstone.providers.local._STOP_KILL_TIMEOUT_SECONDS", 0
                    ):
                        provider.stop("slow-agent", pr)

        mock_kill.assert_any_call(12345, signal.SIGTERM)
        mock_kill.assert_any_call(12345, signal.SIGKILL)

    def test_reads_pid_from_pid_file_when_not_in_result(self, provider, agent_dir):
        (agent_dir / _PID_FILE).write_text("54321")
        pr = _provision_result(str(agent_dir))  # no pid key

        alive_seq = iter([True] + [False] * 60)
        with patch("os.kill"):
            with patch(
                "skcapstone.providers.local._pid_is_alive",
                side_effect=alive_seq,
            ):
                result = provider.stop("file-pid-agent", pr)

        assert result is True

    def test_returns_false_on_sigterm_oserror(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir), pid=12345)
        with patch(
            "skcapstone.providers.local._pid_is_alive", return_value=True
        ):
            with patch("os.kill", side_effect=OSError("eperm")):
                result = provider.stop("perm-agent", pr)

        assert result is False


# ---------------------------------------------------------------------------
# LocalProvider.health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Tests for LocalProvider.health_check()."""

    def test_returns_stopped_when_no_pid_no_state(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir))
        assert provider.health_check("agent", pr) == AgentStatus.STOPPED

    def test_running_state_file_returns_running(self, provider, agent_dir):
        _write_session_state(agent_dir, {"status": "running", "pid": 9999})
        pr = _provision_result(str(agent_dir), pid=9999)
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = provider.health_check("agent", pr)
        assert result == AgentStatus.RUNNING

    def test_idle_state_file_returns_running(self, provider, agent_dir):
        _write_session_state(agent_dir, {"status": "idle", "pid": 9999})
        pr = _provision_result(str(agent_dir), pid=9999)
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = provider.health_check("agent", pr)
        assert result == AgentStatus.RUNNING

    def test_stopped_state_file_returns_stopped(self, provider, agent_dir):
        _write_session_state(agent_dir, {"status": "stopped"})
        pr = _provision_result(str(agent_dir))
        result = provider.health_check("agent", pr)
        assert result == AgentStatus.STOPPED

    def test_error_state_file_returns_degraded(self, provider, agent_dir):
        _write_session_state(agent_dir, {"status": "error", "pid": 9999})
        pr = _provision_result(str(agent_dir), pid=9999)
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = provider.health_check("agent", pr)
        assert result == AgentStatus.DEGRADED

    def test_running_state_but_dead_pid_returns_degraded(self, provider, agent_dir):
        _write_session_state(agent_dir, {"status": "running", "pid": 9999})
        pr = _provision_result(str(agent_dir), pid=9999)
        with patch("skcapstone.providers.local._pid_is_alive", return_value=False):
            result = provider.health_check("agent", pr)
        assert result == AgentStatus.DEGRADED

    def test_no_state_file_alive_pid_returns_running(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir), pid=9999)
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = provider.health_check("agent", pr)
        assert result == AgentStatus.RUNNING

    def test_no_state_file_dead_pid_returns_stopped(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir), pid=9999)
        with patch("skcapstone.providers.local._pid_is_alive", return_value=False):
            result = provider.health_check("agent", pr)
        assert result == AgentStatus.STOPPED

    def test_reads_pid_from_pid_file(self, provider, agent_dir):
        (agent_dir / _PID_FILE).write_text("12345")
        pr = _provision_result(str(agent_dir))
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = provider.health_check("agent", pr)
        assert result == AgentStatus.RUNNING


# ---------------------------------------------------------------------------
# LocalProvider.destroy
# ---------------------------------------------------------------------------


class TestDestroy:
    """Tests for LocalProvider.destroy()."""

    def test_removes_work_dir(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir))
        with patch.object(provider, "stop", return_value=True):
            result = provider.destroy("agent", pr)
        assert result is True
        assert not agent_dir.exists()

    def test_calls_stop_first(self, provider, agent_dir):
        pr = _provision_result(str(agent_dir))
        with patch.object(provider, "stop", return_value=True) as mock_stop:
            provider.destroy("agent", pr)
        mock_stop.assert_called_once_with("agent", pr)

    def test_returns_true_even_when_dir_missing(self, provider, tmp_path):
        pr = _provision_result(str(tmp_path / "ghost"))
        with patch.object(provider, "stop", return_value=True):
            result = provider.destroy("ghost", pr)
        assert result is True

    def test_empty_provision_result_returns_true(self, provider):
        result = provider.destroy("nobody", {})
        assert result is True


# ---------------------------------------------------------------------------
# _session_state_to_agent_status
# ---------------------------------------------------------------------------


class TestSessionStateToAgentStatus:
    """Tests for the state → AgentStatus mapper."""

    def test_running_with_live_pid(self):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = _session_state_to_agent_status(
                {"status": "running", "pid": 1234}, 1234
            )
        assert result == AgentStatus.RUNNING

    def test_running_with_dead_pid_returns_degraded(self):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=False):
            result = _session_state_to_agent_status(
                {"status": "running", "pid": 1234}, 1234
            )
        assert result == AgentStatus.DEGRADED

    def test_idle_returns_running(self):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = _session_state_to_agent_status({"status": "idle", "pid": 1}, 1)
        assert result == AgentStatus.RUNNING

    def test_stopped_returns_stopped(self):
        result = _session_state_to_agent_status({"status": "stopped"}, None)
        assert result == AgentStatus.STOPPED

    def test_error_returns_degraded(self):
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            result = _session_state_to_agent_status({"status": "error", "pid": 1}, 1)
        assert result == AgentStatus.DEGRADED

    def test_unknown_status_returns_degraded(self):
        result = _session_state_to_agent_status({"status": "banana"}, None)
        assert result == AgentStatus.DEGRADED


# ---------------------------------------------------------------------------
# _stub_script
# ---------------------------------------------------------------------------


class TestStubScript:
    """Tests for the stub process script generator."""

    def test_returns_string(self, tmp_path):
        script = _stub_script("my-agent", str(tmp_path / "state.json"))
        assert isinstance(script, str)
        assert len(script) > 0

    def test_script_contains_agent_name(self, tmp_path):
        script = _stub_script("my-agent", str(tmp_path / "state.json"))
        assert "my-agent" in script

    def test_script_contains_state_file_path(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        script = _stub_script("agent", state_path)
        assert state_path in script

    def test_script_is_valid_python(self, tmp_path):
        script = _stub_script("test-agent", str(tmp_path / "state.json"))
        compile(script, "<stub>", "exec")


# ---------------------------------------------------------------------------
# Integration: full provision → configure → start → health_check → stop
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """End-to-end lifecycle tests with mocked subprocess."""

    def test_lifecycle_with_crush(self, provider, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 88888

        spec = _make_spec(
            role="coder",
            model="code",
            soul_blueprint="lumina",
            skills=["code-review"],
            env={"EXTRA": "val"},
        )

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch(
                "skcapstone.providers.local._find_crush_binary",
                return_value="/usr/bin/crush",
            ):
                pr = provider.provision("lifecycle-agent", spec, "team-lc")
                provider.configure("lifecycle-agent", spec, pr)
                started = provider.start("lifecycle-agent", pr)

        assert started is True
        assert pr["pid"] == 88888

        # Health check: session state says running, pid alive
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            status = provider.health_check("lifecycle-agent", pr)
        assert status == AgentStatus.RUNNING

        # Stop
        alive_seq = iter([True] + [False] * 60)
        with patch("os.kill"):
            with patch(
                "skcapstone.providers.local._pid_is_alive",
                side_effect=alive_seq,
            ):
                stopped = provider.stop("lifecycle-agent", pr)

        assert stopped is True

        # Post-stop health check
        status_after = provider.health_check("lifecycle-agent", pr)
        assert status_after == AgentStatus.STOPPED

    def test_lifecycle_with_stub_fallback(self, provider):
        mock_proc = MagicMock()
        mock_proc.pid = 77777
        spec = _make_spec()

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch(
                "skcapstone.providers.local._find_crush_binary",
                return_value=None,
            ):
                pr = provider.provision("stub-agent", spec, "team-stub")
                provider.configure("stub-agent", spec, pr)
                started = provider.start("stub-agent", pr)

        assert started is True

        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            status = provider.health_check("stub-agent", pr)
        assert status == AgentStatus.RUNNING


# ---------------------------------------------------------------------------
# _is_claude_binary
# ---------------------------------------------------------------------------


class TestIsClaudeBinary:
    """Tests for _is_claude_binary helper."""

    def test_returns_true_for_claude(self):
        assert _is_claude_binary("/bin/claude") is True

    def test_returns_true_for_claude_in_usr(self):
        assert _is_claude_binary("/usr/local/bin/claude") is True

    def test_returns_false_for_crush(self):
        assert _is_claude_binary("/usr/bin/crush") is False

    def test_returns_false_for_openclaw(self):
        assert _is_claude_binary("/usr/local/bin/openclaw") is False


# ---------------------------------------------------------------------------
# LocalProvider.start — claude binary path
# ---------------------------------------------------------------------------


class TestStartWithClaudeBinary:
    """Tests for LocalProvider.start() when claude binary is found."""

    @pytest.fixture()
    def _patched_popen(self):
        """Patch subprocess.Popen to return a fake process."""
        mock_proc = MagicMock()
        mock_proc.pid = 33000
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            yield mock_popen, mock_proc

    def test_spawns_claude_subprocess(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("agent-claude", spec, "team-c")
        provider.configure("agent-claude", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            result = provider.start("agent-claude", pr)

        assert result is True
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "/bin/claude"
        assert "-p" in cmd

    def test_claude_cmd_includes_model(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(model_name="claude-opus-4-6")
        pr = provider.provision("agent-cm", spec, "team-c")
        provider.configure("agent-cm", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("agent-cm", pr)

        cmd = mock_popen.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"

    def test_claude_cmd_includes_system_prompt(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(soul_blueprint="lumina")
        pr = provider.provision("agent-sp", spec, "team-c")
        provider.configure("agent-sp", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("agent-sp", pr)

        cmd = mock_popen.call_args[0][0]
        assert "--system-prompt" in cmd

    def test_claude_cmd_includes_output_format(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("agent-of", spec, "team-c")
        provider.configure("agent-of", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("agent-of", pr)

        cmd = mock_popen.call_args[0][0]
        of_idx = cmd.index("--output-format")
        assert cmd[of_idx + 1] == "stream-json"

    def test_claude_cmd_includes_session_id(self, provider, tmp_path, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("agent-si", spec, "team-c")
        provider.configure("agent-si", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("agent-si", pr)

        cmd = mock_popen.call_args[0][0]
        assert "--session-id" in cmd

    def test_claude_cmd_includes_dangerously_skip_permissions(
        self, provider, tmp_path, _patched_popen
    ):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("agent-dsp", spec, "team-c")
        provider.configure("agent-dsp", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("agent-dsp", pr)

        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    def test_sets_pid_in_provision_result(self, provider, tmp_path, _patched_popen):
        _, mock_proc = _patched_popen
        mock_proc.pid = 44444
        spec = _make_spec()
        pr = provider.provision("agent-cpid", spec, "team-c")
        provider.configure("agent-cpid", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("agent-cpid", pr)

        assert pr["pid"] == 44444

    def test_writes_session_state_with_claude_backend(
        self, provider, tmp_path, _patched_popen
    ):
        _, mock_proc = _patched_popen
        mock_proc.pid = 55000
        spec = _make_spec()
        pr = provider.provision("agent-csb", spec, "team-c")
        provider.configure("agent-csb", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("agent-csb", pr)

        state = json.loads(
            (Path(pr["work_dir"]) / _SESSION_STATE_FILE).read_text()
        )
        assert state["status"] == _STATE_RUNNING
        assert state["backend"] == "claude"
        assert state["pid"] == 55000

    def test_returns_false_on_popen_error(self, provider, tmp_path):
        spec = _make_spec()
        pr = provider.provision("agent-cerr", spec, "team-c")
        provider.configure("agent-cerr", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            with patch(
                "subprocess.Popen", side_effect=OSError("not found")
            ):
                result = provider.start("agent-cerr", pr)

        assert result is False


# ---------------------------------------------------------------------------
# Claude session environment
# ---------------------------------------------------------------------------


class TestClaudeSessionEnv:
    """Tests for claude session environment variable passing."""

    @pytest.fixture()
    def _patched_popen(self):
        mock_proc = MagicMock()
        mock_proc.pid = 66000
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            yield mock_popen, mock_proc

    def test_passes_agent_name_in_env(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("env-agent", spec, "team-env")
        provider.configure("env-agent", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("env-agent", pr)

        env = mock_popen.call_args[1]["env"]
        assert env["AGENT_NAME"] == "env-agent"

    def test_passes_model_in_env(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(model="reason")
        pr = provider.provision("env-model", spec, "team-env")
        provider.configure("env-model", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("env-model", pr)

        env = mock_popen.call_args[1]["env"]
        assert env["AGENT_MODEL_TIER"] == "reason"

    def test_passes_soul_in_env(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(soul_blueprint="lumina")
        pr = provider.provision("env-soul", spec, "team-env")
        provider.configure("env-soul", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("env-soul", pr)

        env = mock_popen.call_args[1]["env"]
        assert "SOUL_BLUEPRINT" in env
        assert "lumina" in env["SOUL_BLUEPRINT"].lower() or env["SOUL_BLUEPRINT"] == "lumina"

    def test_passes_skills_as_json_in_env(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec(skills=["code-review", "docs"])
        pr = provider.provision("env-skills", spec, "team-env")
        provider.configure("env-skills", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("env-skills", pr)

        env = mock_popen.call_args[1]["env"]
        parsed = json.loads(env["AGENT_SKILLS"])
        assert "code-review" in parsed
        assert "docs" in parsed


# ---------------------------------------------------------------------------
# Fallback chain priority: crush → claude → stub
# ---------------------------------------------------------------------------


class TestStartFallbackChain:
    """Tests for the three-tier fallback: crush → claude → stub."""

    @pytest.fixture()
    def _patched_popen(self):
        mock_proc = MagicMock()
        mock_proc.pid = 99000
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            yield mock_popen, mock_proc

    def test_crush_binary_uses_crush_session(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("fb-crush", spec, "team-fb")
        provider.configure("fb-crush", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/usr/bin/crush",
        ):
            provider.start("fb-crush", pr)

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "/usr/bin/crush"
        assert "run" in cmd
        # Should NOT have -p flag (that's the claude path)
        assert "-p" not in cmd

    def test_claude_binary_uses_claude_session(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("fb-claude", spec, "team-fb")
        provider.configure("fb-claude", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value="/bin/claude",
        ):
            provider.start("fb-claude", pr)

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "/bin/claude"
        assert "-p" in cmd

    def test_no_binary_uses_stub(self, provider, _patched_popen):
        mock_popen, _ = _patched_popen
        spec = _make_spec()
        pr = provider.provision("fb-stub", spec, "team-fb")
        provider.configure("fb-stub", spec, pr)

        with patch(
            "skcapstone.providers.local._find_crush_binary",
            return_value=None,
        ):
            provider.start("fb-stub", pr)

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == os.sys.executable
        assert cmd[1] == "-c"

    def test_lifecycle_with_claude(self, provider):
        """End-to-end lifecycle with claude binary."""
        mock_proc = MagicMock()
        mock_proc.pid = 98765

        spec = _make_spec(
            role="coder",
            model="code",
            soul_blueprint="lumina",
            skills=["code-review"],
        )

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch(
                "skcapstone.providers.local._find_crush_binary",
                return_value="/bin/claude",
            ):
                pr = provider.provision("claude-lc", spec, "team-lc")
                provider.configure("claude-lc", spec, pr)
                started = provider.start("claude-lc", pr)

        assert started is True
        assert pr["pid"] == 98765

        # Health check
        with patch("skcapstone.providers.local._pid_is_alive", return_value=True):
            status = provider.health_check("claude-lc", pr)
        assert status == AgentStatus.RUNNING

        # Stop
        alive_seq = iter([True] + [False] * 60)
        with patch("os.kill"):
            with patch(
                "skcapstone.providers.local._pid_is_alive",
                side_effect=alive_seq,
            ):
                stopped = provider.stop("claude-lc", pr)
        assert stopped is True
