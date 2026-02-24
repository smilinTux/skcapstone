"""Tests for the skcapstone agent-to-agent chat module.

Covers:
- AgentChat.send() with and without transport
- AgentChat.receive() and inbox retrieval
- Payload serialization round-trip
- CLI commands (send, inbox) via CliRunner
- Graceful degradation when dependencies are missing
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skcapstone.chat import (
    AgentChat,
    _pack_chat_payload,
    _short_timestamp,
    _unpack_chat_payload,
)


@pytest.fixture
def tmp_home(tmp_path):
    """Create a minimal agent home directory."""
    home = tmp_path / ".skcapstone"
    (home / "config").mkdir(parents=True)
    (home / "identity").mkdir(parents=True)
    identity_data = {"name": "TestAgent", "fingerprint": "AABB1234", "capauth_managed": False}
    (home / "identity" / "identity.json").write_text(json.dumps(identity_data))
    (home / "manifest.json").write_text(json.dumps({"name": "TestAgent", "version": "0.1.0"}))
    import yaml
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": "TestAgent"}))
    return home


class TestPayloadRoundTrip:
    """Verify pack/unpack preserves message data."""

    def test_pack_unpack_basic(self):
        """Basic message survives serialize/deserialize."""
        mock_msg = MagicMock()
        mock_msg.id = "msg-123"
        mock_msg.sender = "opus"
        mock_msg.recipient = "lumina"
        mock_msg.content = "Hello sovereign world"
        mock_msg.thread_id = None
        mock_msg.timestamp.isoformat.return_value = "2026-02-24T05:00:00Z"

        packed = _pack_chat_payload(mock_msg)
        unpacked = _unpack_chat_payload(packed, sender="fallback", recipient="fallback")

        assert unpacked["sender"] == "opus"
        assert unpacked["recipient"] == "lumina"
        assert unpacked["content"] == "Hello sovereign world"

    def test_pack_unpack_with_thread(self):
        """Thread ID survives round-trip."""
        mock_msg = MagicMock()
        mock_msg.id = "msg-456"
        mock_msg.sender = "jarvis"
        mock_msg.recipient = "opus"
        mock_msg.content = "Build update"
        mock_msg.thread_id = "deploy-01"
        mock_msg.timestamp.isoformat.return_value = "2026-02-24T05:00:00Z"

        packed = _pack_chat_payload(mock_msg)
        unpacked = _unpack_chat_payload(packed, sender="x", recipient="y")

        assert unpacked["thread_id"] == "deploy-01"

    def test_unpack_plain_text_fallback(self):
        """Non-JSON payloads treated as plain text."""
        result = _unpack_chat_payload("just plain text", sender="opus", recipient="lumina")

        assert result["content"] == "just plain text"
        assert result["sender"] == "opus"
        assert result["recipient"] == "lumina"

    def test_unpack_non_skchat_json(self):
        """JSON without skchat_version falls back to plain text."""
        payload = json.dumps({"type": "other", "data": "test"})
        result = _unpack_chat_payload(payload, sender="a", recipient="b")

        assert result["sender"] == "a"
        assert "other" in result["content"] or result["content"] == payload


class TestShortTimestamp:
    """Verify timestamp formatting."""

    def test_format(self):
        """Timestamp is HH:MM:SS format."""
        ts = _short_timestamp()
        assert len(ts) == 8
        assert ts[2] == ":"
        assert ts[5] == ":"


class TestAgentChatSend:
    """Tests for AgentChat.send() without real transport."""

    def test_send_stores_locally_without_skcomm(self, tmp_home):
        """Message is stored in history even without SKComm."""
        agent = AgentChat(home=tmp_home, identity="opus")

        mock_history = MagicMock()
        mock_history.store_message.return_value = "mem-abc"
        agent._history = mock_history

        result = agent.send("lumina", "Hello!")

        assert result["stored"] is True
        assert result["delivered"] is False
        mock_history.store_message.assert_called_once()

    def test_send_delivers_with_skcomm(self, tmp_home):
        """Message is delivered when SKComm has transports."""
        agent = AgentChat(home=tmp_home, identity="opus")

        mock_comm = MagicMock()
        mock_report = MagicMock()
        mock_report.delivered = True
        mock_report.successful_transport = "syncthing"
        mock_comm.send.return_value = mock_report
        mock_comm.router.transports = [MagicMock()]
        agent._comm = mock_comm

        mock_history = MagicMock()
        mock_history.store_message.return_value = "mem-xyz"
        agent._history = mock_history

        result = agent.send("lumina", "Delivered message")

        assert result["stored"] is True
        assert result["delivered"] is True
        assert result["transport"] == "syncthing"


class TestAgentChatReceive:
    """Tests for AgentChat.receive()."""

    def test_receive_empty_inbox(self, tmp_home):
        """Empty inbox returns empty list."""
        agent = AgentChat(home=tmp_home, identity="opus")

        mock_comm = MagicMock()
        mock_comm.receive.return_value = []
        mock_comm.router.transports = [MagicMock()]
        agent._comm = mock_comm

        messages = agent.receive()
        assert messages == []

    def test_receive_with_messages(self, tmp_home):
        """Incoming envelopes are parsed into message dicts."""
        agent = AgentChat(home=tmp_home, identity="opus")

        payload = json.dumps({
            "skchat_version": "1.0.0",
            "message_id": "msg-1",
            "sender": "lumina",
            "recipient": "opus",
            "content": "Hello from Lumina",
            "thread_id": None,
            "timestamp": "2026-02-24T05:00:00Z",
        })

        envelope = MagicMock()
        envelope.sender = "lumina"
        envelope.recipient = "opus"
        envelope.payload.content = payload

        mock_comm = MagicMock()
        mock_comm.receive.return_value = [envelope]
        mock_comm.router.transports = [MagicMock()]
        agent._comm = mock_comm

        mock_history = MagicMock()
        agent._history = mock_history

        messages = agent.receive()

        assert len(messages) == 1
        assert messages[0]["sender"] == "lumina"
        assert messages[0]["content"] == "Hello from Lumina"


class TestCLIChatCommands:
    """Integration tests for the CLI chat commands."""

    @patch("skcapstone.cli.get_runtime")
    def test_chat_send_help(self, mock_runtime):
        """chat send --help works."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["chat", "send", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output
        assert "MESSAGE" in result.output

    @patch("skcapstone.cli.get_runtime")
    def test_chat_inbox_help(self, mock_runtime):
        """chat inbox --help works."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["chat", "inbox", "--help"])
        assert result.exit_code == 0
        assert "--poll" in result.output
        assert "--limit" in result.output

    @patch("skcapstone.cli.get_runtime")
    def test_chat_live_help(self, mock_runtime):
        """chat live --help works."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["chat", "live", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output
        assert "--poll-interval" in result.output
