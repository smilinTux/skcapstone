"""Tests for skcapstone test-connection command.

Covers:
- Happy path: peer responds with pong, latency reported
- Timeout: peer never replies, command exits with code 1
- No-transport: SKComm has no live transport, error reported
- Payload helpers: _make_ping_payload / _is_pong_for / _is_ping / _make_pong_payload
- --count > 1: multi-ping summary statistics
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skcapstone.cli.test_connection import (
    _is_ping,
    _is_pong_for,
    _make_ping_payload,
    _make_pong_payload,
    ping_peer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path):
    """Minimal agent home directory for CLI tests."""
    home = tmp_path / ".skcapstone"
    (home / "identity").mkdir(parents=True)
    (home / "config").mkdir(parents=True)
    identity = {
        "name": "TestAgent",
        "fingerprint": "AABB1234",
        "capauth_managed": False,
    }
    (home / "identity" / "identity.json").write_text(json.dumps(identity))
    (home / "manifest.json").write_text(
        json.dumps({"name": "TestAgent", "version": "0.1.0"})
    )
    import yaml

    (home / "config" / "config.yaml").write_text(
        yaml.dump({"agent_name": "TestAgent"})
    )
    return home


def _make_mock_chat(delivered: bool, pong_nonce: str | None = None, peer: str = "lumina"):
    """Build a mocked AgentChat instance.

    Args:
        delivered: Whether AgentChat.send() reports successful delivery.
        pong_nonce: When set, AgentChat.receive() will return a pong message
            matching this nonce on the first call.
        peer: Peer name used as sender in the pong message.

    Returns:
        MagicMock configured as AgentChat.
    """
    mock_chat = MagicMock()
    mock_chat.send.return_value = {
        "stored": True,
        "delivered": delivered,
        "transport": "tailscale" if delivered else None,
        "error": None,
    }

    if pong_nonce is not None:
        pong_payload = _make_pong_payload(pong_nonce, peer)
        mock_chat.receive.return_value = [
            {"sender": peer, "content": pong_payload}
        ]
    else:
        mock_chat.receive.return_value = []

    return mock_chat


# ---------------------------------------------------------------------------
# Unit tests — payload helpers
# ---------------------------------------------------------------------------


class TestPayloadHelpers:
    """Verify ping/pong serialization round-trips."""

    def test_make_ping_payload_is_json(self):
        """Ping payload is valid JSON with required keys."""
        payload = _make_ping_payload("abc123", "opus")
        data = json.loads(payload)
        assert data["skchat_ping"] is True
        assert data["nonce"] == "abc123"
        assert data["sender"] == "opus"

    def test_is_pong_for_matching_nonce(self):
        """_is_pong_for returns True for a matching pong."""
        pong = _make_pong_payload("xyz-789", "lumina")
        assert _is_pong_for(pong, "xyz-789") is True

    def test_is_pong_for_wrong_nonce(self):
        """_is_pong_for returns False when nonce differs."""
        pong = _make_pong_payload("xyz-789", "lumina")
        assert _is_pong_for(pong, "different-nonce") is False

    def test_is_pong_for_plain_text(self):
        """_is_pong_for is False for arbitrary text."""
        assert _is_pong_for("hello world", "nonce") is False

    def test_is_ping_detects_ping(self):
        """_is_ping correctly identifies a ping payload."""
        ping = _make_ping_payload("nonce-42", "opus")
        ok, nonce, sender = _is_ping(ping)
        assert ok is True
        assert nonce == "nonce-42"
        assert sender == "opus"

    def test_is_ping_plain_text(self):
        """_is_ping returns False for non-ping content."""
        ok, nonce, sender = _is_ping("just a regular message")
        assert ok is False
        assert nonce == ""

    def test_make_pong_payload_round_trip(self):
        """Pong payload can be verified by _is_pong_for."""
        pong = _make_pong_payload("round-trip-nonce", "jarvis")
        assert _is_pong_for(pong, "round-trip-nonce") is True


# ---------------------------------------------------------------------------
# Unit tests — ping_peer()
# ---------------------------------------------------------------------------


class TestPingPeer:
    """Tests for the ping_peer() core function."""

    def test_happy_path_reachable(self, agent_home):
        """ping_peer returns reachable=True when pong arrives."""
        nonce_holder: list[str] = []

        def fake_send(peer, message, **kwargs):
            # Capture the nonce from the ping payload
            data = json.loads(message)
            nonce_holder.append(data["nonce"])
            return {"stored": True, "delivered": True, "transport": "tailscale", "error": None}

        def fake_receive(limit=20):
            if not nonce_holder:
                return []
            return [
                {
                    "sender": "lumina",
                    "content": _make_pong_payload(nonce_holder[0], "lumina"),
                }
            ]

        mock_chat = MagicMock()
        mock_chat.send.side_effect = fake_send
        mock_chat.receive.side_effect = fake_receive

        with patch(
            "skcapstone.cli.test_connection.AgentChat", return_value=mock_chat
        ):
            result = ping_peer("lumina", agent_home, "TestAgent", timeout=2.0)

        assert result["reachable"] is True
        assert result["latency_ms"] is not None
        assert result["latency_ms"] >= 0.0

    def test_timeout_unreachable(self, agent_home):
        """ping_peer returns reachable=False when pong never arrives."""
        mock_chat = _make_mock_chat(delivered=True, pong_nonce=None)

        with patch(
            "skcapstone.cli.test_connection.AgentChat", return_value=mock_chat
        ):
            # Use a very short timeout so the test runs quickly
            result = ping_peer("lumina", agent_home, "TestAgent", timeout=0.3)

        assert result["reachable"] is False
        assert result["latency_ms"] is None
        assert "timeout" in (result["error"] or "").lower()

    def test_no_live_transport_returns_error(self, agent_home):
        """ping_peer reports error when send has no live transport."""
        mock_chat = MagicMock()
        mock_chat.send.return_value = {
            "stored": True,
            "delivered": False,
            "transport": None,
            "error": None,
        }

        with patch(
            "skcapstone.cli.test_connection.AgentChat", return_value=mock_chat
        ):
            result = ping_peer("lumina", agent_home, "TestAgent", timeout=2.0)

        assert result["reachable"] is False
        assert "transport" in (result["error"] or "").lower()

    def test_send_failure_returns_error(self, agent_home):
        """ping_peer returns error when send itself fails entirely."""
        mock_chat = MagicMock()
        mock_chat.send.return_value = {
            "stored": False,
            "delivered": False,
            "transport": None,
            "error": "connection refused",
        }

        with patch(
            "skcapstone.cli.test_connection.AgentChat", return_value=mock_chat
        ):
            result = ping_peer("lumina", agent_home, "TestAgent", timeout=2.0)

        assert result["reachable"] is False
        assert result["error"] is not None


# ---------------------------------------------------------------------------
# CLI integration tests — skcapstone test-connection
# ---------------------------------------------------------------------------


class TestTestConnectionCLI:
    """CLI-level tests via CliRunner."""

    def _run(self, args: list[str]):
        from skcapstone.cli import main

        runner = CliRunner()
        return runner.invoke(main, args, catch_exceptions=False)

    def test_cli_reachable_exit_zero(self, agent_home):
        """CLI exits 0 and prints REACHABLE when pong arrives."""
        nonce_holder: list[str] = []

        def fake_send(peer, message, **kwargs):
            try:
                data = json.loads(message)
                nonce_holder.append(data.get("nonce", ""))
            except Exception:
                pass
            return {
                "stored": True,
                "delivered": True,
                "transport": "tailscale",
                "error": None,
            }

        def fake_receive(limit=20):
            if not nonce_holder:
                return []
            return [
                {
                    "sender": "lumina",
                    "content": _make_pong_payload(nonce_holder[0], "lumina"),
                }
            ]

        mock_chat = MagicMock()
        mock_chat.send.side_effect = fake_send
        mock_chat.receive.side_effect = fake_receive

        with patch("skcapstone.cli.test_connection.AgentChat", return_value=mock_chat):
            result = self._run(
                ["test-connection", "lumina", "--home", str(agent_home)]
            )

        assert result.exit_code == 0, result.output
        assert "REACHABLE" in result.output

    def test_cli_timeout_exit_one(self, agent_home):
        """CLI exits 1 and prints UNREACHABLE on timeout."""
        mock_chat = _make_mock_chat(delivered=True, pong_nonce=None)

        with patch("skcapstone.cli.test_connection.AgentChat", return_value=mock_chat):
            # Very short timeout so the test completes fast
            result = self._run(
                [
                    "test-connection", "lumina",
                    "--home", str(agent_home),
                    "--timeout", "0.3",
                ]
            )

        assert result.exit_code == 1, result.output
        assert "UNREACHABLE" in result.output

    def test_cli_no_transport_exit_one(self, agent_home):
        """CLI exits 1 when no live transport is available."""
        mock_chat = MagicMock()
        mock_chat.send.return_value = {
            "stored": True,
            "delivered": False,
            "transport": None,
            "error": None,
        }

        with patch("skcapstone.cli.test_connection.AgentChat", return_value=mock_chat):
            result = self._run(
                ["test-connection", "lumina", "--home", str(agent_home)]
            )

        assert result.exit_code == 1, result.output
        assert "UNREACHABLE" in result.output

    def test_cli_count_shows_statistics(self, agent_home):
        """--count 3 produces min/avg/max latency summary."""
        nonce_holder: list[str] = []

        def fake_send(peer, message, **kwargs):
            try:
                data = json.loads(message)
                nonce_holder.append(data.get("nonce", ""))
            except Exception:
                pass
            return {
                "stored": True,
                "delivered": True,
                "transport": "tailscale",
                "error": None,
            }

        def fake_receive(limit=20):
            if not nonce_holder:
                return []
            latest = nonce_holder[-1]
            return [
                {
                    "sender": "lumina",
                    "content": _make_pong_payload(latest, "lumina"),
                }
            ]

        mock_chat = MagicMock()
        mock_chat.send.side_effect = fake_send
        mock_chat.receive.side_effect = fake_receive

        with patch("skcapstone.cli.test_connection.AgentChat", return_value=mock_chat):
            result = self._run(
                [
                    "test-connection", "lumina",
                    "--home", str(agent_home),
                    "--count", "3",
                ]
            )

        assert result.exit_code == 0, result.output
        # Summary line for multiple pings contains avg=
        assert "avg=" in result.output
        assert "min=" in result.output
        assert "max=" in result.output
