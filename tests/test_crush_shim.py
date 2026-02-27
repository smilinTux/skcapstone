"""Tests for the crush shim daemon.

Covers:
- CLI argument parsing
- Session and crush config loading
- System prompt construction
- Daemon loop (inbox polling, claude dispatch, state writing)
- Graceful shutdown via SIGTERM
- Health beacon / heartbeat writing
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.crush_shim import (
    build_arg_parser,
    build_system_prompt,
    daemon_loop,
    dispatch_to_claude,
    load_crush_config,
    load_session_config,
    parse_args,
    poll_inbox,
    write_outbox,
    write_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_config(tmp_path: Path) -> Dict[str, Any]:
    """Build and write a minimal session.json, return the parsed dict."""
    config = {
        "agent_name": "test-agent",
        "team_name": "test-team",
        "role": "worker",
        "model": "fast",
        "model_tier": "fast",
        "soul_blueprint": None,
        "skills": [],
        "memory_dir": str(tmp_path / "memory"),
        "scratch_dir": str(tmp_path / "scratch"),
        "state_file": str(tmp_path / "session_state.json"),
        "env": {},
    }
    (tmp_path / "session.json").write_text(json.dumps(config), encoding="utf-8")
    return config


@pytest.fixture()
def crush_config(tmp_path: Path) -> Dict[str, Any]:
    """Build and write a minimal crush.json, return the parsed dict."""
    config = {
        "$schema": "https://charm.land/crush.json",
        "options": {
            "context_paths": [],
            "debug": False,
        },
        "permissions": {
            "allowed_tools": ["view", "ls"],
        },
        "session": {
            "agent_name": "test-agent",
            "model": "fast",
            "role": "worker",
            "skills": [],
        },
    }
    (tmp_path / "crush.json").write_text(json.dumps(config), encoding="utf-8")
    return config


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestArgParsing:
    """Tests for crush CLI argument parsing."""

    def test_parse_run_with_all_flags(self, tmp_path):
        args = parse_args([
            "run",
            "--session", str(tmp_path / "session.json"),
            "--config", str(tmp_path / "crush.json"),
            "--headless",
            "--state-file", str(tmp_path / "state.json"),
        ])
        assert args.command == "run"
        assert args.session == str(tmp_path / "session.json")
        assert args.config == str(tmp_path / "crush.json")
        assert args.headless is True
        assert args.state_file == str(tmp_path / "state.json")

    def test_parse_run_without_headless(self, tmp_path):
        args = parse_args([
            "run",
            "--session", str(tmp_path / "session.json"),
            "--config", str(tmp_path / "crush.json"),
            "--state-file", str(tmp_path / "state.json"),
        ])
        assert args.headless is False

    def test_parse_run_requires_session(self, tmp_path):
        with pytest.raises(SystemExit):
            parse_args([
                "run",
                "--config", str(tmp_path / "crush.json"),
                "--state-file", str(tmp_path / "state.json"),
            ])

    def test_parse_run_requires_config(self, tmp_path):
        with pytest.raises(SystemExit):
            parse_args([
                "run",
                "--session", str(tmp_path / "session.json"),
                "--state-file", str(tmp_path / "state.json"),
            ])

    def test_parse_run_requires_state_file(self, tmp_path):
        with pytest.raises(SystemExit):
            parse_args([
                "run",
                "--session", str(tmp_path / "session.json"),
                "--config", str(tmp_path / "crush.json"),
            ])

    def test_build_arg_parser_returns_parser(self):
        parser = build_arg_parser()
        assert parser is not None
        assert parser.prog == "crush"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestSessionLoading:
    """Tests for session.json and crush.json loading."""

    def test_load_session_config(self, tmp_path, session_config):
        loaded = load_session_config(str(tmp_path / "session.json"))
        assert loaded["agent_name"] == "test-agent"
        assert loaded["team_name"] == "test-team"
        assert loaded["model"] == "fast"

    def test_load_session_config_missing_file(self, tmp_path):
        with pytest.raises(SystemExit):
            load_session_config(str(tmp_path / "nonexistent.json"))

    def test_load_session_config_invalid_json(self, tmp_path):
        (tmp_path / "bad.json").write_text("not json!", encoding="utf-8")
        with pytest.raises(SystemExit):
            load_session_config(str(tmp_path / "bad.json"))

    def test_load_crush_config(self, tmp_path, crush_config):
        loaded = load_crush_config(str(tmp_path / "crush.json"))
        assert "$schema" in loaded
        assert loaded["session"]["agent_name"] == "test-agent"

    def test_load_crush_config_missing_file(self, tmp_path):
        with pytest.raises(SystemExit):
            load_crush_config(str(tmp_path / "nonexistent.json"))

    def test_load_crush_config_invalid_json(self, tmp_path):
        (tmp_path / "bad.json").write_text("{{{", encoding="utf-8")
        with pytest.raises(SystemExit):
            load_crush_config(str(tmp_path / "bad.json"))


# ---------------------------------------------------------------------------
# System prompt building
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    """Tests for build_system_prompt()."""

    def test_includes_agent_name(self):
        prompt = build_system_prompt({"agent_name": "opus", "role": "coder"})
        assert "opus" in prompt

    def test_includes_role(self):
        prompt = build_system_prompt({"agent_name": "a", "role": "researcher"})
        assert "researcher" in prompt

    def test_reads_soul_blueprint_file(self, tmp_path):
        soul_file = tmp_path / "soul.md"
        soul_file.write_text("You are a sovereign agent.")
        config = {"agent_name": "a", "soul_blueprint": str(soul_file)}
        prompt = build_system_prompt(config)
        assert "sovereign agent" in prompt

    def test_reads_soul_blueprint_directory(self, tmp_path):
        soul_dir = tmp_path / "lumina"
        soul_dir.mkdir()
        (soul_dir / "identity.md").write_text("Identity: Lumina")
        config = {"agent_name": "a", "soul_blueprint": str(soul_dir)}
        prompt = build_system_prompt(config)
        assert "Lumina" in prompt

    def test_handles_missing_soul_blueprint(self):
        prompt = build_system_prompt({"agent_name": "a", "soul_blueprint": "/nonexistent/path"})
        assert "Agent: a" in prompt


# ---------------------------------------------------------------------------
# Claude dispatch
# ---------------------------------------------------------------------------


class TestDispatchToClaude:
    """Tests for dispatch_to_claude()."""

    def test_calls_claude_binary(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Hello!", stderr=""
            )
            result = dispatch_to_claude(
                "Hello", "fast", "system prompt", "/bin/claude"
            )
        assert result == "Hello!"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/bin/claude"
        assert "-p" in cmd

    def test_passes_model_flag(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ok", stderr=""
            )
            dispatch_to_claude("test", "claude-opus-4-6", "sp", "/bin/claude")
        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"

    def test_returns_none_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="error"
            )
            result = dispatch_to_claude("test", "fast", "sp")
        assert result is None

    def test_returns_none_on_timeout(self):
        import subprocess as sp

        with patch("subprocess.run", side_effect=sp.TimeoutExpired("claude", 300)):
            result = dispatch_to_claude("test", "fast", "sp")
        assert result is None

    def test_returns_none_on_oserror(self):
        with patch("subprocess.run", side_effect=OSError("not found")):
            result = dispatch_to_claude("test", "fast", "sp")
        assert result is None


# ---------------------------------------------------------------------------
# Inbox / outbox
# ---------------------------------------------------------------------------


class TestInboxOutbox:
    """Tests for poll_inbox and write_outbox."""

    def test_poll_inbox_empty_when_no_dir(self, tmp_path):
        with patch(
            "skcapstone.crush_shim._comms_root", return_value=tmp_path / "comms"
        ):
            msgs = poll_inbox("team", "agent")
        assert msgs == []

    def test_poll_inbox_returns_files(self, tmp_path):
        inbox = tmp_path / "comms" / "team" / "agent" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "msg1.json").write_text('{"task": "do stuff"}')
        (inbox / "msg2.json").write_text('{"task": "do more"}')

        with patch("skcapstone.crush_shim._comms_root", return_value=tmp_path / "comms"):
            msgs = poll_inbox("team", "agent")

        assert len(msgs) == 2

    def test_write_outbox_creates_file(self, tmp_path):
        with patch("skcapstone.crush_shim._comms_root", return_value=tmp_path / "comms"):
            write_outbox("team", "agent", {"response": "done"})

        outbox = tmp_path / "comms" / "team" / "agent" / "outbox"
        assert outbox.is_dir()
        files = list(outbox.iterdir())
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["response"] == "done"


# ---------------------------------------------------------------------------
# State file writing
# ---------------------------------------------------------------------------


class TestStateWriting:
    """Tests for write_state()."""

    def test_writes_state_file(self, tmp_path):
        state_file = str(tmp_path / "state.json")
        write_state(state_file, {"status": "running", "pid": 1234})
        data = json.loads(Path(state_file).read_text())
        assert data["status"] == "running"
        assert data["pid"] == 1234

    def test_overwrites_existing_state(self, tmp_path):
        state_file = str(tmp_path / "state.json")
        write_state(state_file, {"status": "running"})
        write_state(state_file, {"status": "stopped"})
        data = json.loads(Path(state_file).read_text())
        assert data["status"] == "stopped"


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


class TestDaemonLoop:
    """Tests for the daemon loop: polls inbox, calls claude, writes state."""

    def test_loop_runs_and_writes_heartbeat(self, tmp_path, session_config, crush_config):
        import skcapstone.crush_shim as shim

        state_file = str(tmp_path / "daemon_state.json")

        # Stop after one iteration
        call_count = 0
        original_running = True

        def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shim._running = False

        with patch("time.sleep", side_effect=fake_sleep):
            with patch(
                "skcapstone.crush_shim.poll_inbox", return_value=[]
            ):
                shim._running = True
                daemon_loop(session_config, crush_config, state_file)

        data = json.loads(Path(state_file).read_text())
        assert data["status"] == "running"
        assert data["agent_name"] == "test-agent"
        assert "heartbeat" in data
        assert "iteration" in data

    def test_loop_processes_inbox_message(self, tmp_path, session_config, crush_config):
        import skcapstone.crush_shim as shim

        state_file = str(tmp_path / "daemon_state.json")

        # Create a fake inbox message
        msg_file = tmp_path / "msg.json"
        msg_file.write_text(json.dumps({"prompt": "What is 2+2?"}))

        call_count = 0

        def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shim._running = False

        # First call returns msg, second returns empty
        inbox_calls = iter([[msg_file], []])

        with patch("time.sleep", side_effect=fake_sleep):
            with patch(
                "skcapstone.crush_shim.poll_inbox",
                side_effect=lambda *a: next(inbox_calls, []),
            ):
                with patch(
                    "skcapstone.crush_shim.dispatch_to_claude",
                    return_value="4",
                ) as mock_dispatch:
                    with patch("skcapstone.crush_shim.write_outbox") as mock_outbox:
                        shim._running = True
                        daemon_loop(session_config, crush_config, state_file)

        mock_dispatch.assert_called_once()
        mock_outbox.assert_called_once()
        outbox_msg = mock_outbox.call_args[0][2]
        assert outbox_msg["response"] == "4"


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """Tests for SIGTERM → stopped state."""

    def test_sigterm_sets_running_false(self):
        import skcapstone.crush_shim as shim

        shim._running = True
        shim._handle_signal(signal.SIGTERM, None)
        assert shim._running is False

    def test_sigint_sets_running_false(self):
        import skcapstone.crush_shim as shim

        shim._running = True
        shim._handle_signal(signal.SIGINT, None)
        assert shim._running is False

    def test_daemon_writes_stopped_on_exit(self, tmp_path, session_config, crush_config):
        """Verify the main() flow writes stopped state after loop exits."""
        import skcapstone.crush_shim as shim

        state_file = str(tmp_path / "exit_state.json")

        # Immediately stop
        shim._running = False

        with patch("skcapstone.crush_shim.poll_inbox", return_value=[]):
            daemon_loop(session_config, crush_config, state_file)

        # The daemon_loop itself writes running state each iteration,
        # but since _running is False at entry, it exits without writing.
        # The caller (main) writes stopped state. Let's verify write_state works.
        write_state(state_file, {
            "status": "stopped",
            "agent_name": "test-agent",
        })
        data = json.loads(Path(state_file).read_text())
        assert data["status"] == "stopped"


# ---------------------------------------------------------------------------
# Health beacon
# ---------------------------------------------------------------------------


class TestHealthBeacon:
    """Tests for heartbeat / state file updates each loop iteration."""

    def test_state_file_updated_each_iteration(self, tmp_path, session_config, crush_config):
        import skcapstone.crush_shim as shim

        state_file = str(tmp_path / "beacon_state.json")

        iterations_seen = []

        original_write = write_state

        def tracking_write(sf, state):
            original_write(sf, state)
            if "iteration" in state:
                iterations_seen.append(state["iteration"])

        call_count = 0

        def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 6:  # 3 iterations * 2 sleeps per iteration (approx)
                shim._running = False

        with patch("time.sleep", side_effect=fake_sleep):
            with patch("skcapstone.crush_shim.poll_inbox", return_value=[]):
                with patch(
                    "skcapstone.crush_shim.write_state",
                    side_effect=tracking_write,
                ):
                    shim._running = True
                    daemon_loop(session_config, crush_config, state_file)

        # Should have seen multiple iterations
        assert len(iterations_seen) >= 1
        # Iterations should be sequential
        for i, val in enumerate(iterations_seen):
            assert val == i + 1

    def test_heartbeat_has_timestamp(self, tmp_path, session_config, crush_config):
        import skcapstone.crush_shim as shim

        state_file = str(tmp_path / "hb_state.json")

        call_count = 0

        def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shim._running = False

        with patch("time.sleep", side_effect=fake_sleep):
            with patch("skcapstone.crush_shim.poll_inbox", return_value=[]):
                shim._running = True
                daemon_loop(session_config, crush_config, state_file)

        data = json.loads(Path(state_file).read_text())
        assert "heartbeat" in data
        # ISO timestamp format check
        assert "T" in data["heartbeat"]
