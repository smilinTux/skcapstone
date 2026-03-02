"""Tests for the LLM-powered skcapstone chat CLI commands.

Covers:
- chat list / chat --list (empty and non-empty)
- chat open LLM loop (mocked LLMBridge + console.input)
- _run_llm_chat saves exchanges to conversations/{peer}.json
- --list flag routing via _ChatGroup
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path):
    """Minimal agent home with identity and manifest."""
    home = tmp_path / ".skcapstone"
    (home / "identity").mkdir(parents=True)
    (home / "config").mkdir(parents=True)
    identity = {"name": "TestAgent", "fingerprint": "AABB1234", "capauth_managed": False}
    (home / "identity" / "identity.json").write_text(json.dumps(identity))
    (home / "manifest.json").write_text(json.dumps({"name": "TestAgent", "version": "0.1.0"}))
    import yaml
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": "TestAgent"}))
    return home


@pytest.fixture
def agent_home_with_convs(agent_home):
    """Agent home with pre-existing conversation files."""
    convs = agent_home / "conversations"
    convs.mkdir(parents=True)
    lumina_history = [
        {"role": "user", "content": "Hello Lumina!", "timestamp": "2026-03-01T10:00:00Z"},
        {"role": "assistant", "content": "Hello! How can I help?", "timestamp": "2026-03-01T10:00:01Z"},
    ]
    (convs / "lumina.json").write_text(json.dumps(lumina_history))
    jarvis_history = [
        {"role": "user", "content": "Status?", "timestamp": "2026-03-01T11:00:00Z"},
    ]
    (convs / "jarvis.json").write_text(json.dumps(jarvis_history))
    return agent_home


# ---------------------------------------------------------------------------
# chat list
# ---------------------------------------------------------------------------


class TestChatList:
    """Tests for `skcapstone chat list`."""

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_list_help(self, _mock_rt):
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "list", "--help"])
        assert result.exit_code == 0
        assert "conversation" in result.output.lower()

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_list_empty_no_dir(self, _mock_rt, agent_home):
        """No conversations dir → 'No conversations yet' message."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "list", "--home", str(agent_home)])
        assert result.exit_code == 0
        assert "No conversations" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_list_empty_dir(self, _mock_rt, agent_home):
        """Empty conversations dir → 'No conversations yet' message."""
        (agent_home / "conversations").mkdir(parents=True)
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "list", "--home", str(agent_home)])
        assert result.exit_code == 0
        assert "No conversations" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_list_shows_peers(self, _mock_rt, agent_home_with_convs):
        """Peers with conversations are listed with message counts."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "list", "--home", str(agent_home_with_convs)])
        assert result.exit_code == 0
        assert "lumina" in result.output
        assert "jarvis" in result.output
        assert "2" in result.output
        assert "1" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_list_shows_last_message_preview(self, _mock_rt, agent_home_with_convs):
        """Last message content is shown in the list."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "list", "--home", str(agent_home_with_convs)])
        assert result.exit_code == 0
        assert "Hello! How can I help?" in result.output or "How can I help" in result.output


class TestChatListFlag:
    """Tests for `skcapstone chat --list` routing."""

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_double_dash_list(self, _mock_rt, agent_home):
        """chat --list routes to the list subcommand."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "--list", "--home", str(agent_home)])
        assert result.exit_code == 0
        assert "No conversations" in result.output


# ---------------------------------------------------------------------------
# chat open (LLM loop)
# ---------------------------------------------------------------------------


class TestChatOpenLLM:
    """Tests for `skcapstone chat open` using LLMBridge."""

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_open_help(self, _mock_rt):
        """chat open --help works and shows PEER argument."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "open", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_open_missing_home(self, _mock_rt, tmp_path):
        """chat open exits with error if home dir doesn't exist."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "open", "lumina", "--home", str(tmp_path / "nonexistent")]
        )
        assert result.exit_code != 0 or "No agent found" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    @patch("skcapstone.cli.chat._run_llm_chat")
    def test_chat_open_calls_llm_chat(self, mock_llm_chat, mock_rt, agent_home):
        """chat open delegates to _run_llm_chat."""
        mock_runtime = MagicMock()
        mock_runtime.manifest.name = "TestAgent"
        mock_rt.return_value = mock_runtime

        from skcapstone.cli import main
        runner = CliRunner()
        runner.invoke(main, ["chat", "open", "lumina", "--home", str(agent_home)])

        assert mock_llm_chat.called
        assert mock_llm_chat.call_args[0][0] == "lumina"

    @patch("skcapstone.cli.chat.get_runtime")
    @patch("skcapstone.cli.chat._run_llm_chat")
    def test_chat_shortcut_calls_llm_chat(self, mock_llm_chat, mock_rt, agent_home):
        """skcapstone chat <peer> (shortcut) also calls _run_llm_chat."""
        mock_runtime = MagicMock()
        mock_runtime.manifest.name = "TestAgent"
        mock_rt.return_value = mock_runtime

        from skcapstone.cli import main
        runner = CliRunner()
        runner.invoke(main, ["chat", "lumina", "--home", str(agent_home)])

        assert mock_llm_chat.called


# ---------------------------------------------------------------------------
# _run_llm_chat unit tests
# ---------------------------------------------------------------------------

# Patch targets — lazy imports inside _run_llm_chat are fetched from
# consciousness_loop at call time, so patch at the source module.
_CL = "skcapstone.consciousness_loop"


class TestRunLLMChat:
    """Unit tests for _run_llm_chat helper."""

    def _make_mock_bridge(self, response="Mock LLM response"):
        mock_bridge = MagicMock()
        mock_bridge.generate.return_value = response
        return mock_bridge

    def _make_mock_builder(self):
        mock_builder = MagicMock()
        mock_builder.build.return_value = "System prompt text"
        return mock_builder

    def _status_ctx(self, mock_console):
        """Wire console.status() as a no-op context manager."""
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_console.status.return_value = ctx

    def test_run_llm_chat_single_exchange(self, agent_home):
        """A single user message triggers LLMBridge.generate and saves history."""
        from skcapstone.cli.chat import _run_llm_chat

        mock_bridge = self._make_mock_bridge("Hello from LLM!")
        mock_builder = self._make_mock_builder()

        with patch(f"{_CL}.LLMBridge", return_value=mock_bridge), \
             patch(f"{_CL}.SystemPromptBuilder", return_value=mock_builder), \
             patch(f"{_CL}.ConsciousnessConfig"), \
             patch(f"{_CL}._classify_message", return_value=MagicMock()), \
             patch("skcapstone.cli.chat.console") as mock_console:
            self._status_ctx(mock_console)
            mock_console.input.side_effect = ["hi", "/quit"]

            _run_llm_chat("lumina", agent_home, "TestAgent")

        mock_bridge.generate.assert_called_once()
        assert mock_builder.add_to_history.call_count == 2
        calls = mock_builder.add_to_history.call_args_list
        assert calls[0][0] == ("lumina", "user", "hi")
        assert calls[1][0] == ("lumina", "assistant", "Hello from LLM!")

    def test_run_llm_chat_empty_message_skipped(self, agent_home):
        """Empty input is skipped without calling LLMBridge."""
        from skcapstone.cli.chat import _run_llm_chat

        mock_bridge = self._make_mock_bridge()
        mock_builder = self._make_mock_builder()

        with patch(f"{_CL}.LLMBridge", return_value=mock_bridge), \
             patch(f"{_CL}.SystemPromptBuilder", return_value=mock_builder), \
             patch(f"{_CL}.ConsciousnessConfig"), \
             patch(f"{_CL}._classify_message", return_value=MagicMock()), \
             patch("skcapstone.cli.chat.console") as mock_console:
            self._status_ctx(mock_console)
            mock_console.input.side_effect = ["", "  ", "/quit"]

            _run_llm_chat("lumina", agent_home, "TestAgent")

        mock_bridge.generate.assert_not_called()

    def test_run_llm_chat_ctrl_c_exits_gracefully(self, agent_home):
        """KeyboardInterrupt exits the loop without raising."""
        from skcapstone.cli.chat import _run_llm_chat

        with patch(f"{_CL}.LLMBridge", return_value=self._make_mock_bridge()), \
             patch(f"{_CL}.SystemPromptBuilder", return_value=self._make_mock_builder()), \
             patch(f"{_CL}.ConsciousnessConfig"), \
             patch(f"{_CL}._classify_message", return_value=MagicMock()), \
             patch("skcapstone.cli.chat.console") as mock_console:
            mock_console.input.side_effect = KeyboardInterrupt

            _run_llm_chat("lumina", agent_home, "TestAgent")

        mock_console.print.assert_called()

    def test_run_llm_chat_shows_existing_history(self, agent_home_with_convs):
        """Existing conversation history is shown at startup."""
        from skcapstone.cli.chat import _run_llm_chat

        with patch(f"{_CL}.LLMBridge", return_value=self._make_mock_bridge()), \
             patch(f"{_CL}.SystemPromptBuilder", return_value=self._make_mock_builder()), \
             patch(f"{_CL}.ConsciousnessConfig"), \
             patch(f"{_CL}._classify_message", return_value=MagicMock()), \
             patch("skcapstone.cli.chat.console") as mock_console:
            self._status_ctx(mock_console)
            mock_console.input.side_effect = ["/quit"]

            _run_llm_chat("lumina", agent_home_with_convs, "TestAgent")

        all_printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "previous message" in all_printed

    def test_run_llm_chat_llmbridge_error_handled(self, agent_home):
        """LLMBridge exceptions are caught; error text saved to history."""
        from skcapstone.cli.chat import _run_llm_chat

        mock_bridge = MagicMock()
        mock_bridge.generate.side_effect = RuntimeError("LLM offline")
        mock_builder = self._make_mock_builder()

        with patch(f"{_CL}.LLMBridge", return_value=mock_bridge), \
             patch(f"{_CL}.SystemPromptBuilder", return_value=mock_builder), \
             patch(f"{_CL}.ConsciousnessConfig"), \
             patch(f"{_CL}._classify_message", return_value=MagicMock()), \
             patch("skcapstone.cli.chat.console") as mock_console:
            self._status_ctx(mock_console)
            mock_console.input.side_effect = ["hello", "/quit"]

            _run_llm_chat("lumina", agent_home, "TestAgent")

        saved_calls = [c for c in mock_builder.add_to_history.call_args_list
                       if c[0][1] == "assistant"]
        assert saved_calls
        assert "Error" in saved_calls[0][0][2]

    def test_run_llm_chat_saves_to_json(self, agent_home):
        """Conversation exchanges are persisted to conversations/{peer}.json."""
        from skcapstone.cli.chat import _run_llm_chat
        from skcapstone.consciousness_loop import SystemPromptBuilder

        mock_bridge = MagicMock()
        mock_bridge.generate.return_value = "Saved response"
        # Use a real SystemPromptBuilder so the JSON persistence path is exercised.
        real_builder = SystemPromptBuilder(home=agent_home)

        with patch(f"{_CL}.LLMBridge", return_value=mock_bridge), \
             patch(f"{_CL}.SystemPromptBuilder", return_value=real_builder), \
             patch(f"{_CL}.ConsciousnessConfig"), \
             patch("skcapstone.cli.chat.console") as mock_console:
            self._status_ctx(mock_console)
            mock_console.input.side_effect = ["persist this", "/quit"]

            _run_llm_chat("testpeer", agent_home, "TestAgent")

        conv_file = agent_home / "conversations" / "testpeer.json"
        assert conv_file.exists(), "conversations/testpeer.json should be created"
        data = json.loads(conv_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["role"] == "user"
        assert data[0]["content"] == "persist this"
        assert data[1]["role"] == "assistant"
        assert data[1]["content"] == "Saved response"
