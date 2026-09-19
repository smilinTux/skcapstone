"""Authenticated local broker for append-only CardStore mutations.

Workers use :class:`CardStoreBrokerClient`; only the broker process opens the
CardStore event files. Requests and responses are JSON serialized (never built
by string concatenation) and are framed one object per line.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
from pathlib import Path
from typing import Any, Callable

from .card_store import CardStore

_MAX_REQUEST = 1024 * 1024


def _json_line(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_json_line(conn: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while len(data) < _MAX_REQUEST:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    line = bytes(data).split(b"\n", 1)[0]
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


class CardStoreBroker:
    """Serve append requests on a private, same-user Unix socket."""

    def __init__(self, home: Path, socket_path: Path, *, worker_uid: int | None = None) -> None:
        self.home = Path(home)
        self.socket_path = Path(socket_path)
        self.worker_uid = os.getuid() if worker_uid is None else worker_uid
        self._server: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)
        server.listen(32)
        self._server = server
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                if self._stop.is_set():
                    break
                raise
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _serve(self, conn: socket.socket) -> None:
        try:
            peer_uid = os.getuid()
            if hasattr(socket, "SO_PEERCRED"):
                peer_uid = int.from_bytes(conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)[4:8], "little")
            if peer_uid != self.worker_uid:
                raise PermissionError("unauthorized broker peer")
            request = _read_json_line(conn)
            result = self._append(request, peer_uid)
            conn.sendall(_json_line({"ok": True, "event": result}))
        except Exception as exc:  # protocol boundary must return a machine response
            conn.sendall(_json_line({"ok": False, "error": type(exc).__name__, "message": str(exc)}))
        finally:
            conn.close()

    def _append(self, request: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        if request.get("op") != "append":
            raise ValueError("unsupported broker operation")
        card_id = request.get("card_id")
        action = request.get("action")
        agent = request.get("agent")
        if not all(isinstance(x, str) and x for x in (card_id, action, agent)):
            raise ValueError("card_id, action, and agent are required")
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        store = CardStore(self.home)
        events = store._read_events(card_id)
        expected = request.get("expected_revision")
        if expected is not None and expected != len(events):
            raise ValueError("revision conflict")
        if "sequence" in request and request["sequence"] != len(events):
            raise ValueError("sequence conflict")
        if "prev_hash" in request:
            actual = events[-1].get("event_hash", "") if events else ""
            if request["prev_hash"] != actual:
                raise ValueError("prev_hash conflict")
        # The broker adds authenticated peer identity to the audit payload.
        payload = dict(payload)
        payload.setdefault("broker_uid", peer_uid)
        payload.setdefault("worker_identity", agent)
        return store.append_event(card_id, action, agent, **payload)


class CardStoreBrokerClient:
    def __init__(self, socket_path: Path, worker_identity: str, timeout: float = 5.0) -> None:
        self.socket_path = str(socket_path)
        self.worker_identity = worker_identity
        self.timeout = timeout

    def append(self, card_id: str, action: str, *, expected_revision: int | None = None,
               sequence: int | None = None, prev_hash: str | None = None,
               payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"op": "append", "card_id": card_id, "action": action,
                                   "agent": self.worker_identity, "payload": payload or {}}
        if expected_revision is not None:
            request["expected_revision"] = expected_revision
        if sequence is not None:
            request["sequence"] = sequence
        if prev_hash is not None:
            request["prev_hash"] = prev_hash
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout)
            conn.connect(self.socket_path)
            conn.sendall(_json_line(request))
            response = _read_json_line(conn)
        if not response.get("ok"):
            raise RuntimeError(f"broker rejected append: {response.get('message', 'unknown error')}")
        return response["event"]
