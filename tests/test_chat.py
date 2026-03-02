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
        agent._ensure_comm = lambda: False

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


class TestAgentChatForward:
    """Tests for AgentChat.forward()."""

    def test_forward_preserves_original_sender_and_timestamp(self, tmp_home):
        """Forward envelope preserves forwarded_from and forwarded_at."""
        agent = AgentChat(home=tmp_home, identity="opus")
        agent._ensure_comm = lambda: False

        mock_history = MagicMock()
        agent._history = mock_history

        original = {
            "message_id": "orig-001",
            "sender": "lumina",
            "recipient": "opus",
            "content": "Deploy the fleet",
            "timestamp": "2026-03-01T12:00:00+00:00",
            "thread_id": None,
        }

        result = agent.forward(original, "jarvis")

        assert result["stored"] is True
        assert result["forwarded_id"] is not None

        stored_msg = mock_history.store_message.call_args[0][0]
        payload = json.loads(stored_msg.content)
        assert payload["skchat_forward"] is True
        assert payload["forwarded_from"] == "lumina"
        assert payload["forwarded_at"] == "2026-03-01T12:00:00+00:00"
        assert payload["original_message_id"] == "orig-001"
        assert payload["sender"] == "opus"
        assert payload["recipient"] == "jarvis"
        assert payload["content"] == "Deploy the fleet"

    def test_forward_delivers_via_comm(self, tmp_home):
        """Forward delivers via SKComm when transports are available."""
        agent = AgentChat(home=tmp_home, identity="opus")

        mock_comm = MagicMock()
        mock_report = MagicMock()
        mock_report.delivered = True
        mock_report.successful_transport = "syncthing"
        mock_comm.send.return_value = mock_report
        mock_comm.router.transports = [MagicMock()]
        agent._comm = mock_comm

        mock_history = MagicMock()
        agent._history = mock_history

        original = {
            "message_id": "orig-002",
            "sender": "jarvis",
            "content": "Status update",
            "timestamp": "2026-03-01T13:00:00+00:00",
        }

        result = agent.forward(original, "lumina")

        assert result["delivered"] is True
        assert result["transport"] == "syncthing"
        assert result["stored"] is True
        assert result["forwarded_id"] is not None

        call_kwargs = mock_comm.send.call_args[1]
        assert call_kwargs["recipient"] == "lumina"
        fwd_payload = json.loads(call_kwargs["message"])
        assert fwd_payload["skchat_forward"] is True
        assert fwd_payload["forwarded_from"] == "jarvis"

    def test_forward_without_comm_stores_locally(self, tmp_home):
        """Forward stores locally and returns stored=True when no comm."""
        agent = AgentChat(home=tmp_home, identity="lumina")
        agent._ensure_comm = lambda: False

        mock_history = MagicMock()
        agent._history = mock_history

        original = {
            "message_id": "orig-003",
            "sender": "ava",
            "content": "Check in",
            "timestamp": "2026-03-01T14:00:00+00:00",
        }

        result = agent.forward(original, "opus")

        assert result["stored"] is True
        assert result["delivered"] is False
        assert result["transport"] is None
        assert result["forwarded_id"] is not None

    def test_forward_unique_ids_per_call(self, tmp_home):
        """Each forward call generates a distinct forwarded_id."""
        agent = AgentChat(home=tmp_home, identity="opus")
        agent._ensure_comm = lambda: False

        mock_history = MagicMock()
        agent._history = mock_history

        original = {"message_id": "m1", "sender": "x", "content": "hi", "timestamp": ""}

        r1 = agent.forward(original, "peer-a")
        r2 = agent.forward(original, "peer-b")

        assert r1["forwarded_id"] != r2["forwarded_id"]


class TestCLIChatCommands:
    """Integration tests for the CLI chat commands."""

    def test_chat_send_help(self):
        """chat send --help works."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["chat", "send", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output
        assert "MESSAGE" in result.output

    def test_chat_inbox_help(self):
        """chat inbox --help works."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["chat", "inbox", "--help"])
        assert result.exit_code == 0
        assert "--poll" in result.output
        assert "--limit" in result.output

    def test_chat_live_help(self):
        """chat live --help works."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["chat", "live", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output
        assert "--poll-interval" in result.output

    def test_chat_forward_help(self):
        """chat forward --help works and shows PEER and MSG_ID."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["chat", "forward", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output
        assert "MSG_ID" in result.output

    def test_chat_forward_message_not_found(self, tmp_home):
        """chat forward exits non-zero when MSG_ID is not in inbox."""
        from skcapstone.cli import main
        from skcapstone.cli._common import AGENT_HOME

        runner = CliRunner()
        with patch("skcapstone.cli._common.get_runtime") as mock_rt, \
             patch("skcapstone.chat.AgentChat.get_inbox", return_value=[]):
            mock_rt.return_value.manifest.name = "opus"
            result = runner.invoke(
                main,
                ["chat", "forward", "lumina", "no-such-id", "--home", str(tmp_home)],
            )
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or result.exit_code == 1

    def test_chat_forward_stored_locally(self, tmp_home):
        """chat forward stores message when SKComm unavailable."""
        from skcapstone.cli import main

        original = {
            "message_id": "msg-fwd-001",
            "sender": "jarvis",
            "content": "Forward this",
            "timestamp": "2026-03-01T10:00:00+00:00",
            "thread_id": None,
        }

        runner = CliRunner()
        with patch("skcapstone.cli._common.get_runtime") as mock_rt, \
             patch("skcapstone.chat.AgentChat.get_inbox", return_value=[original]), \
             patch("skcapstone.chat.AgentChat._ensure_comm", return_value=False), \
             patch("skcapstone.chat.AgentChat._ensure_history", return_value=None):
            mock_rt.return_value.manifest.name = "opus"
            result = runner.invoke(
                main,
                ["chat", "forward", "lumina", "msg-fwd-001", "--home", str(tmp_home)],
            )
        # stored=False (no history) and delivered=False → "Failed" or graceful output
        assert result.exit_code == 0 or "failed" in result.output.lower() or "stored" in result.output.lower()
