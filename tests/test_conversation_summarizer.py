"""Tests for ConversationSummarizer and the `skcapstone chat summary` CLI.

Test coverage:
- Happy path: summarize() calls LLM and returns a ConversationSummary
- Empty conversation raises ValueError
- Summary is persisted to {home}/summaries/{peer}.json
- load_summary() retrieves stored summary
- Peer name sanitization (path traversal attempt)
- CLI: chat summary renders summary text
- CLI: chat summary --show-stored shows stored summary
- CLI: chat summary --show-stored with no stored summary shows helpful message
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
    """Minimal agent home with identity manifest."""
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
def agent_home_with_conv(agent_home):
    """Agent home with a lumina conversation file."""
    convs = agent_home / "conversations"
    convs.mkdir(parents=True)
    messages = [
        {"role": "user", "content": "Hello Lumina!", "timestamp": "2026-03-01T10:00:00Z"},
        {"role": "assistant", "content": "Hi! I'm here.", "timestamp": "2026-03-01T10:00:01Z"},
        {"role": "user", "content": "Can you deploy the update?", "timestamp": "2026-03-01T10:01:00Z"},
        {"role": "assistant", "content": "Sure, initiating deployment now.", "timestamp": "2026-03-01T10:01:05Z"},
    ]
    (convs / "lumina.json").write_text(json.dumps(messages))
    return agent_home


def _make_bridge(response: str = "Two agents discussed deployment of the update. The task was agreed upon and initiated."):
    """Return a mock LLMBridge with a canned generate() return value."""
    bridge = MagicMock()
    bridge.generate.return_value = response
    return bridge


# ---------------------------------------------------------------------------
# ConversationSummarizer unit tests
# ---------------------------------------------------------------------------


class TestConversationSummarizer:
    """Unit tests for ConversationSummarizer."""

    def test_summarize_happy_path(self, agent_home_with_conv):
        """summarize() calls LLMBridge and returns a ConversationSummary."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        bridge = _make_bridge("Lumina and agent discussed deployment. The update was initiated.")
        summarizer = ConversationSummarizer(home=agent_home_with_conv)
        result = summarizer.summarize("lumina", n=20, bridge=bridge)

        assert result.peer == "lumina"
        assert "deployment" in result.text.lower() or result.text  # LLM returned something
        assert result.message_count == 4
        assert result.generated_at  # non-empty timestamp
        bridge.generate.assert_called_once()

    def test_summarize_empty_conversation_raises(self, agent_home):
        """summarize() raises ValueError when there is no conversation history."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        summarizer = ConversationSummarizer(home=agent_home)
        with pytest.raises(ValueError, match="No conversation history"):
            summarizer.summarize("nobody", bridge=_make_bridge())

    def test_summarize_persists_to_disk(self, agent_home_with_conv):
        """summarize() writes the summary JSON to {home}/summaries/{peer}.json."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        summarizer = ConversationSummarizer(home=agent_home_with_conv)
        result = summarizer.summarize("lumina", bridge=_make_bridge("Summary text here."))

        summary_file = agent_home_with_conv / "summaries" / "lumina.json"
        assert summary_file.exists(), "summaries/lumina.json should be created"

        data = json.loads(summary_file.read_text())
        assert data["peer"] == "lumina"
        assert data["text"] == "Summary text here."
        assert data["message_count"] == 4

    def test_load_summary_returns_stored(self, agent_home_with_conv):
        """load_summary() retrieves the previously stored summary."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        summarizer = ConversationSummarizer(home=agent_home_with_conv)
        summarizer.summarize("lumina", bridge=_make_bridge("Stored summary content."))

        loaded = summarizer.load_summary("lumina")
        assert loaded is not None
        assert loaded.peer == "lumina"
        assert loaded.text == "Stored summary content."

    def test_load_summary_returns_none_when_missing(self, agent_home):
        """load_summary() returns None when no summary has been stored yet."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        summarizer = ConversationSummarizer(home=agent_home)
        assert summarizer.load_summary("nobody") is None

    def test_summarize_respects_n_limit(self, agent_home):
        """summarize() only includes the last n messages."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        convs = agent_home / "conversations"
        convs.mkdir(parents=True)
        messages = [
            {"role": "user", "content": f"Message {i}", "timestamp": "2026-03-01T10:00:00Z"}
            for i in range(30)
        ]
        (convs / "peer.json").write_text(json.dumps(messages))

        bridge = _make_bridge("Summary of last 5.")
        summarizer = ConversationSummarizer(home=agent_home)
        result = summarizer.summarize("peer", n=5, bridge=bridge)

        assert result.message_count == 5

    def test_summarize_sanitizes_peer_name(self, agent_home):
        """Path traversal in peer name is stripped, not stored as-is."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        convs = agent_home / "conversations"
        convs.mkdir(parents=True)
        messages = [{"role": "user", "content": "hi", "timestamp": "2026-03-01T10:00:00Z"}]
        # The sanitizer will strip path separators; "etcpasswd" will be the key
        (convs / "etcpasswd.json").write_text(json.dumps(messages))

        summarizer = ConversationSummarizer(home=agent_home)
        result = summarizer.summarize("../../../etc/passwd", bridge=_make_bridge("Safe."))

        assert result.peer == "etcpasswd"
        summary_file = agent_home / "summaries" / "etcpasswd.json"
        assert summary_file.exists()

    def test_summarize_llm_error_returns_error_text(self, agent_home_with_conv):
        """If the LLM fails, summarize() stores an error placeholder instead of raising."""
        from skcapstone.conversation_summarizer import ConversationSummarizer

        bridge = MagicMock()
        bridge.generate.side_effect = RuntimeError("LLM offline")

        summarizer = ConversationSummarizer(home=agent_home_with_conv)
        result = summarizer.summarize("lumina", bridge=bridge)

        assert "[Summary unavailable" in result.text
        assert result.message_count == 4

    def test_summary_to_dict_roundtrip(self):
        """ConversationSummary serializes and deserializes correctly."""
        from skcapstone.conversation_summarizer import ConversationSummary

        original = ConversationSummary(
            peer="opus",
            text="A concise summary.",
            message_count=10,
            generated_at="2026-03-01T12:00:00+00:00",
        )
        data = original.to_dict()
        restored = ConversationSummary.from_dict(data)

        assert restored.peer == original.peer
        assert restored.text == original.text
        assert restored.message_count == original.message_count
        assert restored.generated_at == original.generated_at


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home_cli(agent_home_with_conv):
    """Agent home suitable for CLI tests (has runtime files)."""
    return agent_home_with_conv


class TestChatSummaryCLI:
    """Tests for `skcapstone chat summary`."""

    def _make_runtime(self, name="TestAgent"):
        rt = MagicMock()
        rt.manifest.name = name
        return rt

    @patch("skcapstone.cli.chat.get_runtime")
    def test_chat_summary_help(self, _mock_rt):
        """chat summary --help exits cleanly and mentions PEER."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "summary", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    @patch("skcapstone.conversation_summarizer.ConversationSummarizer.summarize")
    def test_chat_summary_prints_result(self, mock_summarize, mock_rt, agent_home_cli):
        """chat summary prints the generated summary text."""
        from skcapstone.conversation_summarizer import ConversationSummary
        from skcapstone.cli import main

        mock_rt.return_value = self._make_runtime()
        mock_summarize.return_value = ConversationSummary(
            peer="lumina",
            text="Two agents talked about deployment. The update was shipped.",
            message_count=4,
            generated_at="2026-03-01T10:00:00+00:00",
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "summary", "lumina", "--home", str(agent_home_cli)]
        )

        assert result.exit_code == 0
        assert "Two agents talked about deployment" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    @patch("skcapstone.conversation_summarizer.ConversationSummarizer.load_summary")
    def test_chat_summary_show_stored(self, mock_load, mock_rt, agent_home_cli):
        """chat summary --show-stored displays previously stored summary."""
        from skcapstone.conversation_summarizer import ConversationSummary
        from skcapstone.cli import main

        mock_rt.return_value = self._make_runtime()
        mock_load.return_value = ConversationSummary(
            peer="lumina",
            text="Stored summary about prior work.",
            message_count=8,
            generated_at="2026-03-01T09:00:00+00:00",
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "summary", "lumina", "--home", str(agent_home_cli), "--show-stored"]
        )

        assert result.exit_code == 0
        assert "Stored summary about prior work" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    @patch("skcapstone.conversation_summarizer.ConversationSummarizer.load_summary")
    def test_chat_summary_show_stored_missing(self, mock_load, mock_rt, agent_home_cli):
        """chat summary --show-stored with no stored summary shows helpful message."""
        from skcapstone.cli import main

        mock_rt.return_value = self._make_runtime()
        mock_load.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "summary", "lumina", "--home", str(agent_home_cli), "--show-stored"]
        )

        assert result.exit_code == 0
        assert "No stored summary" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    @patch("skcapstone.conversation_summarizer.ConversationSummarizer.summarize")
    def test_chat_summary_no_history(self, mock_summarize, mock_rt, agent_home):
        """chat summary prints an error message when the peer has no conversation."""
        from skcapstone.cli import main

        mock_rt.return_value = self._make_runtime()
        mock_summarize.side_effect = ValueError("No conversation history found for peer 'nobody'.")

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "summary", "nobody", "--home", str(agent_home)]
        )

        assert result.exit_code == 0  # CLI handles the ValueError gracefully
        assert "Error" in result.output or "No conversation" in result.output
