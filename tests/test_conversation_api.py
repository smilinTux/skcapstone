"""Tests for the conversation API endpoints in the daemon HTTP server.

Covers:
  GET  /api/v1/conversations          — list all peers
  GET  /api/v1/conversations/{peer}   — full history for a peer
  POST /api/v1/conversations/{peer}/send — send message, write to outbox
  DELETE /api/v1/conversations/{peer} — clear history
  Path-traversal sanitization
"""

from __future__ import annotations

import json
import socket
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.daemon import DaemonConfig, DaemonService, _sanitize_peer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _api(port: int, path: str, *, method: str = "GET", body: bytes | None = None) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def conv_home(tmp_path):
    """Agent home with conversations directory pre-populated."""
    home = tmp_path / ".skcapstone"
    (home / "logs").mkdir(parents=True)
    conv_dir = home / "conversations"
    conv_dir.mkdir()

    # Two peers with conversation history
    alice = [
        {"role": "user", "content": "hello alice", "timestamp": "2026-01-01T10:00:00+00:00"},
        {"role": "assistant", "content": "hi there", "timestamp": "2026-01-01T10:00:01+00:00"},
    ]
    bob = [
        {"role": "user", "content": "hey bob", "timestamp": "2026-01-02T12:00:00+00:00"},
    ]
    (conv_dir / "alice.json").write_text(json.dumps(alice), encoding="utf-8")
    (conv_dir / "bob.json").write_text(json.dumps(bob), encoding="utf-8")
    return home


@pytest.fixture
def live_server(conv_home):
    """Start the daemon API server, yield (svc, port), then stop."""
    config = DaemonConfig(
        home=conv_home,
        shared_root=conv_home,   # keep test data isolated from real ~/.skcapstone
        port=_find_free_port(),
        poll_interval=60,
    )
    svc = DaemonService(config)
    svc.state.running = True

    with patch.object(svc, "_load_components"):
        svc._write_pid()
        svc._start_api_server()
        time.sleep(0.4)
        yield svc, config.port
        svc.stop()


# ---------------------------------------------------------------------------
# Unit tests: _sanitize_peer
# ---------------------------------------------------------------------------

class TestSanitizePeer:
    def test_normal_name(self):
        assert _sanitize_peer("alice") == "alice"

    def test_strips_slashes(self):
        assert "/" not in _sanitize_peer("../etc/passwd")
        assert "\\" not in _sanitize_peer("..\\windows")

    def test_path_traversal(self):
        result = _sanitize_peer("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result

    def test_empty_string(self):
        assert _sanitize_peer("") == ""

    def test_none_returns_empty(self):
        assert _sanitize_peer(None) == ""  # type: ignore[arg-type]

    def test_max_length(self):
        long = "a" * 100
        assert len(_sanitize_peer(long)) <= 64

    def test_allowed_chars(self):
        assert _sanitize_peer("user@domain.io") == "user@domain.io"
        assert _sanitize_peer("my-peer_01") == "my-peer_01"

    def test_null_bytes_stripped(self):
        assert "\x00" not in _sanitize_peer("evil\x00peer")


# ---------------------------------------------------------------------------
# Integration tests: GET /api/v1/conversations
# ---------------------------------------------------------------------------

class TestListConversations:
    def test_returns_list(self, live_server):
        svc, port = live_server
        status, data = _api(port, "/api/v1/conversations")
        assert status == 200
        assert "conversations" in data
        assert isinstance(data["conversations"], list)

    def test_includes_both_peers(self, live_server):
        svc, port = live_server
        _, data = _api(port, "/api/v1/conversations")
        peers = {c["peer"] for c in data["conversations"]}
        assert "alice" in peers
        assert "bob" in peers

    def test_message_count(self, live_server):
        svc, port = live_server
        _, data = _api(port, "/api/v1/conversations")
        alice = next(c for c in data["conversations"] if c["peer"] == "alice")
        assert alice["message_count"] == 2

    def test_last_message_time_present(self, live_server):
        svc, port = live_server
        _, data = _api(port, "/api/v1/conversations")
        alice = next(c for c in data["conversations"] if c["peer"] == "alice")
        assert "last_message_time" in alice
        assert alice["last_message_time"] is not None

    def test_last_message_preview_present(self, live_server):
        svc, port = live_server
        _, data = _api(port, "/api/v1/conversations")
        alice = next(c for c in data["conversations"] if c["peer"] == "alice")
        assert "last_message_preview" in alice
        assert isinstance(alice["last_message_preview"], str)

    def test_empty_conversations_dir(self, tmp_path):
        home = tmp_path / ".skcapstone"
        (home / "logs").mkdir(parents=True)
        (home / "conversations").mkdir()
        config = DaemonConfig(
            home=home,
            shared_root=home,
            port=_find_free_port(),
            poll_interval=60,
        )
        svc = DaemonService(config)
        svc.state.running = True
        with patch.object(svc, "_load_components"):
            svc._write_pid()
            svc._start_api_server()
            time.sleep(0.4)
            try:
                _, data = _api(config.port, "/api/v1/conversations")
                assert data["conversations"] == []
            finally:
                svc.stop()


# ---------------------------------------------------------------------------
# Integration tests: GET /api/v1/conversations/{peer}
# ---------------------------------------------------------------------------

class TestGetConversation:
    def test_existing_peer(self, live_server):
        svc, port = live_server
        status, data = _api(port, "/api/v1/conversations/alice")
        assert status == 200
        assert data["peer"] == "alice"
        assert len(data["messages"]) == 2

    def test_missing_peer_404(self, live_server):
        svc, port = live_server
        status, _ = _api(port, "/api/v1/conversations/nobody")
        assert status == 404

    def test_path_traversal_rejected(self, live_server):
        svc, port = live_server
        # After sanitization "../../etc/passwd" → "etcpasswd" which doesn't exist → 404 or 400
        status, _ = _api(port, "/api/v1/conversations/../../etc/passwd")
        assert status in (400, 404)

    def test_get_on_send_returns_405(self, live_server):
        svc, port = live_server
        status, data = _api(port, "/api/v1/conversations/alice/send")
        assert status == 405


# ---------------------------------------------------------------------------
# Integration tests: POST /api/v1/conversations/{peer}/send
# ---------------------------------------------------------------------------

class TestSendMessage:
    def test_send_returns_sent(self, live_server):
        svc, port = live_server
        body = json.dumps({"content": "hello world"}).encode()
        status, data = _api(port, "/api/v1/conversations/alice/send", method="POST", body=body)
        assert status == 200
        assert data["status"] == "sent"
        assert "message_id" in data

    def test_send_writes_outbox_file(self, live_server, conv_home):
        svc, port = live_server
        body = json.dumps({"content": "test outbox write"}).encode()
        status, resp = _api(port, "/api/v1/conversations/alice/send", method="POST", body=body)
        assert status == 200, resp
        msg_id = resp["message_id"]
        # shared_root == conv_home in tests
        outbox = svc.config.shared_root / "sync" / "comms" / "outbox" / f"{msg_id}.skc.json"
        assert outbox.exists(), f"Outbox file not created: {outbox}"
        envelope = json.loads(outbox.read_text())
        assert envelope["recipient"] == "alice"
        assert envelope["payload"]["content"] == "test outbox write"

    def test_send_missing_content_400(self, live_server):
        svc, port = live_server
        body = json.dumps({"content": ""}).encode()
        status, data = _api(port, "/api/v1/conversations/alice/send", method="POST", body=body)
        assert status == 400
        assert "content" in data["error"].lower()

    def test_send_invalid_json_400(self, live_server):
        svc, port = live_server
        status, data = _api(
            port, "/api/v1/conversations/alice/send", method="POST", body=b"not-json"
        )
        assert status == 400

    def test_send_path_traversal_rejected(self, live_server):
        svc, port = live_server
        body = json.dumps({"content": "hi"}).encode()
        # URL path traversal: peer sanitizes to empty or benign string
        status, _ = _api(
            port, "/api/v1/conversations/../../evil/send", method="POST", body=body
        )
        # Either 400 (invalid peer) or 200 (sanitized to "evil" which is fine) — not a server error
        assert status in (200, 400)

    def test_send_unique_message_ids(self, live_server):
        svc, port = live_server
        body = json.dumps({"content": "msg"}).encode()
        ids = set()
        for _ in range(5):
            _, resp = _api(port, "/api/v1/conversations/alice/send", method="POST", body=body)
            ids.add(resp["message_id"])
        assert len(ids) == 5, "message_id should be unique per send"


# ---------------------------------------------------------------------------
# Integration tests: DELETE /api/v1/conversations/{peer}
# ---------------------------------------------------------------------------

class TestDeleteConversation:
    def test_delete_existing_peer(self, live_server, conv_home):
        svc, port = live_server
        assert (conv_home / "conversations" / "bob.json").exists()
        status, data = _api(port, "/api/v1/conversations/bob", method="DELETE")
        assert status == 200
        assert data["status"] == "deleted"
        assert not (conv_home / "conversations" / "bob.json").exists()

    def test_delete_missing_peer_404(self, live_server):
        svc, port = live_server
        status, _ = _api(port, "/api/v1/conversations/nobody", method="DELETE")
        assert status == 404

    def test_delete_invalid_peer_400(self, live_server):
        svc, port = live_server
        status, _ = _api(port, "/api/v1/conversations/", method="DELETE")
        assert status in (400, 404)

    def test_delete_does_not_affect_other_peers(self, live_server, conv_home):
        svc, port = live_server
        _api(port, "/api/v1/conversations/bob", method="DELETE")
        # alice should still exist
        assert (conv_home / "conversations" / "alice.json").exists()
        status, _ = _api(port, "/api/v1/conversations/alice")
        assert status == 200
