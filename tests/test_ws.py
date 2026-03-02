"""Tests for the WebSocket streaming endpoint added to the daemon."""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
from unittest.mock import patch

import pytest

from skcapstone.daemon import (
    DaemonConfig,
    DaemonService,
    _ws_accept_key,
    _ws_encode_close,
    _ws_encode_frame,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _WsConn:
    """Minimal WebSocket client connection with a leftover-data buffer.

    Handles the case where the OS coalesces the 101 response and the first
    WebSocket frame into a single TCP segment (Nagle's algorithm).
    """

    def __init__(self, sock: socket.socket, leftover: bytes = b"") -> None:
        self._sock = sock
        self._buf = leftover

    def recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._sock.settimeout(3)
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("EOF reading from WebSocket")
            self._buf += chunk
        data = self._buf[:n]
        self._buf = self._buf[n:]
        return data

    def recv_frame(self) -> tuple[int, bytes]:
        """Read one unmasked server→client WebSocket frame."""
        header = self.recv_exact(2)
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.recv_exact(8))[0]
        payload = self.recv_exact(length) if length else b""
        return opcode, payload

    def close(self) -> None:
        self._sock.close()


def _ws_connect(port: int) -> _WsConn:
    """Open a TCP socket, perform the WebSocket handshake, and return a _WsConn.

    Any bytes received after the HTTP headers (e.g. an initial WS frame that
    arrived in the same TCP segment as the 101 response) are preserved in the
    _WsConn buffer so they are not lost.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    key_b64 = base64.b64encode(b"skcapstonetest!!").decode()
    request = (
        f"GET /ws HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key_b64}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    sock.sendall(request)
    # Read until the end of the HTTP response headers
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed before 101")
        response += chunk
    header_end = response.index(b"\r\n\r\n") + 4
    assert b"101" in response[:header_end], f"Expected 101, got: {response[:200]}"
    leftover = response[header_end:]
    return _WsConn(sock, leftover)


def _start_server(tmp_path):
    """Start a DaemonService HTTP/WS server on a free port; return the service."""
    config = DaemonConfig(
        home=tmp_path,
        shared_root=tmp_path,
        port=0,
        poll_interval=60,
        consciousness_enabled=False,
    )
    svc = DaemonService(config)
    svc.state.running = True
    with patch.object(svc, "_load_components"):
        svc.config.port = _find_free_port()
        svc._write_pid()
        svc._start_api_server()
    time.sleep(0.3)
    return svc


# ── Unit tests for module-level helpers ───────────────────────────────────────

class TestWsHelpers:
    """Tests for the RFC 6455 frame-encoding and handshake helpers."""

    def test_accept_key_rfc6455_example(self):
        """The Sec-WebSocket-Accept value matches the RFC 6455 §1.3 example."""
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        assert _ws_accept_key(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    def test_accept_key_deterministic(self):
        key = "abc123=="
        assert _ws_accept_key(key) == _ws_accept_key(key)

    def test_encode_frame_small_payload(self):
        """Single-byte length encoding (payload < 126 bytes)."""
        payload = b"hello"
        frame = _ws_encode_frame(payload)
        assert frame[0] == 0x81, "FIN=1 + opcode=1 (text)"
        assert frame[1] == 5, "length field"
        assert frame[2:] == b"hello"

    def test_encode_frame_medium_payload(self):
        """Two-byte extended length encoding (126 ≤ payload < 65536)."""
        payload = b"x" * 200
        frame = _ws_encode_frame(payload)
        assert frame[0] == 0x81
        assert frame[1] == 126
        length = struct.unpack("!H", frame[2:4])[0]
        assert length == 200
        assert frame[4:] == payload

    def test_encode_frame_roundtrip(self):
        """Encoded frame carries the original payload unchanged."""
        payload = json.dumps({"type": "test", "value": 42}).encode()
        frame = _ws_encode_frame(payload)
        # header is 2 bytes for small payloads
        assert frame[2:] == payload

    def test_encode_close_opcode(self):
        frame = _ws_encode_close()
        assert frame[0] == 0x88, "FIN=1 + opcode=8 (close)"
        assert frame[1] == 0, "no payload"


# ── Unit tests for _ws_broadcast ─────────────────────────────────────────────

class TestWsBroadcast:
    """Tests for DaemonService._ws_broadcast."""

    def test_broadcast_with_no_clients_is_noop(self, tmp_path):
        """Calling _ws_broadcast when no clients are connected must not raise."""
        svc = DaemonService(DaemonConfig(home=tmp_path, port=0))
        svc._ws_broadcast({"type": "test"})  # must not raise

    def test_broadcast_removes_dead_client(self, tmp_path):
        """A closed socket is removed from _ws_clients after a failed send."""
        svc = DaemonService(DaemonConfig(home=tmp_path, port=0))
        a, b = socket.socketpair()
        b.close()  # close the "client" side → sends to 'a' will fail
        with svc._ws_lock:
            svc._ws_clients.add(a)
        svc._ws_broadcast({"type": "test"})
        assert a not in svc._ws_clients
        a.close()

    def test_broadcast_sends_to_all_clients(self, tmp_path):
        """Message is delivered to every connected client socket."""
        svc = DaemonService(DaemonConfig(home=tmp_path, port=0))
        pairs = [socket.socketpair() for _ in range(3)]
        server_socks = [p[0] for p in pairs]
        client_socks = [p[1] for p in pairs]
        with svc._ws_lock:
            svc._ws_clients.update(server_socks)
        svc._ws_broadcast({"type": "hello"})
        for cs in client_socks:
            cs.settimeout(1)
            data = cs.recv(64)
            assert len(data) > 0
        for a, b in pairs:
            a.close()
            b.close()


# ── Integration tests for the /ws HTTP endpoint ───────────────────────────────

class TestWsEndpoint:
    """End-to-end tests against a live DaemonService API server."""

    def test_ws_handshake_returns_101_and_init_message(self, tmp_path):
        """Connecting to /ws completes the handshake and receives a 'connected' frame."""
        svc = _start_server(tmp_path)
        try:
            conn = _ws_connect(svc.config.port)
            try:
                opcode, data = conn.recv_frame()
                assert opcode == 0x1, "Expected text frame"
                msg = json.loads(data)
                assert msg["type"] == "connected"
                assert "state" in msg
            finally:
                conn.close()
        finally:
            svc.stop()

    def test_ws_broadcast_reaches_connected_client(self, tmp_path):
        """A message broadcast via _ws_broadcast is received by the WS client."""
        svc = _start_server(tmp_path)
        try:
            conn = _ws_connect(svc.config.port)
            try:
                conn.recv_frame()  # drain the 'connected' init frame
                # Broadcast from the server side
                svc._ws_broadcast({"type": "message", "content": "ping from daemon"})
                opcode, data = conn.recv_frame()
                assert opcode == 0x1
                msg = json.loads(data)
                assert msg["type"] == "message"
                assert msg["content"] == "ping from daemon"
            finally:
                conn.close()
        finally:
            svc.stop()

    def test_ws_client_removed_after_disconnect(self, tmp_path):
        """Closing the client socket causes it to be removed from _ws_clients."""
        svc = _start_server(tmp_path)
        try:
            conn = _ws_connect(svc.config.port)
            conn.recv_frame()  # drain init frame
            # Verify the client is registered
            time.sleep(0.1)
            with svc._ws_lock:
                assert len(svc._ws_clients) >= 1
            # Abruptly close the client; the server read-loop should detect it
            conn.close()
            time.sleep(0.8)
            with svc._ws_lock:
                count = len(svc._ws_clients)
            assert count == 0, f"Expected 0 clients after disconnect, got {count}"
        finally:
            svc.stop()

    def test_ws_rejected_without_upgrade_header(self, tmp_path):
        """Plain HTTP GET to /ws (no Upgrade header) returns a 4xx error."""
        import urllib.error
        import urllib.request

        svc = _start_server(tmp_path)
        try:
            url = f"http://127.0.0.1:{svc.config.port}/ws"
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    body = json.loads(resp.read())
                    # Some implementations return 200 with error body
                    assert "error" in body
            except urllib.error.HTTPError as exc:
                assert exc.code >= 400
        finally:
            svc.stop()

    def test_ws_process_messages_broadcasts(self, tmp_path):
        """_process_messages delivers the envelope to connected WS clients."""
        from types import SimpleNamespace

        svc = _start_server(tmp_path)
        try:
            conn = _ws_connect(svc.config.port)
            try:
                conn.recv_frame()  # drain init
                # Simulate an incoming envelope
                fake_payload = SimpleNamespace(
                    content="hello world",
                    content_type=SimpleNamespace(value="text"),
                )
                fake_env = SimpleNamespace(
                    message_id="test-id-1",
                    sender="test-peer",
                    payload=fake_payload,
                )
                svc._process_messages([fake_env])
                opcode, data = conn.recv_frame()
                assert opcode == 0x1
                msg = json.loads(data)
                assert msg["type"] == "message"
                assert msg["sender"] == "test-peer"
                assert msg["content"] == "hello world"
            finally:
                conn.close()
        finally:
            svc.stop()
