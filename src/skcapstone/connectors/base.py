"""
ConnectorBackend — abstract interface for platform connectors.

A connector is a viewport into the sovereign agent from an external tool
(terminal, VS Code, Cursor, Windsurf, etc.).  The agent is the truth;
the platform is just a window.

Every connector must implement five lifecycle methods:
  connect()      — establish the channel
  disconnect()   — clean teardown
  send()         — push a message to the platform
  receive()      — pull the next incoming message (non-blocking)
  health_check() — return current ConnectorStatus

Wire format (Unix domain socket connectors)
-------------------------------------------
All Unix socket connectors use the same framed JSON-RPC 2.0 protocol:

  ┌─────────────────────────────────────────┐
  │  4-byte big-endian uint32 length (N)    │
  │  N bytes of UTF-8 JSON-RPC 2.0 payload  │
  └─────────────────────────────────────────┘

JSON-RPC 2.0 request example::

    {"jsonrpc": "2.0", "id": 1, "method": "agent/notify",
     "params": {"type": "status", "payload": {"state": "ready"}}}

JSON-RPC 2.0 notification (no "id")::

    {"jsonrpc": "2.0", "method": "agent/event",
     "params": {"type": "memory_stored", "payload": {...}}}

The agent acts as the server: it creates, binds, and listens on the socket.
The IDE extension connects as a client.  Only one client at a time is
supported; a new connection replaces the previous one.

Constants exported for use by subclasses:
  FRAME_FORMAT      — struct format string (">I")
  FRAME_HEADER_SIZE — byte size of the length prefix (4)
"""

from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Wire-format constants
# ---------------------------------------------------------------------------

FRAME_FORMAT = ">I"
FRAME_HEADER_SIZE: int = struct.calcsize(FRAME_FORMAT)
_MAX_FRAME_BYTES = 10 * 1024 * 1024  # 10 MB guard

_log = logging.getLogger(__name__)


class ConnectorType(str, Enum):
    """Identifies the platform this connector talks to."""

    TERMINAL = "terminal"
    VSCODE = "vscode"
    CURSOR = "cursor"
    WINDSURF = "windsurf"
    UNKNOWN = "unknown"


class ConnectorStatus(str, Enum):
    """Operational state of a connector."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


class ConnectorInfo:
    """Snapshot of a connector's current state.

    Args:
        name: Human-readable connector name.
        connector_type: Which platform this connector targets.
        status: Current operational status.
        metadata: Optional extra platform-specific data.
    """

    __slots__ = ("name", "connector_type", "status", "metadata")

    def __init__(
        self,
        name: str,
        connector_type: ConnectorType,
        status: ConnectorStatus,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.connector_type = connector_type
        self.status = status
        self.metadata: Dict[str, Any] = metadata or {}

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ConnectorInfo(name={self.name!r}, type={self.connector_type.value}, "
            f"status={self.status.value})"
        )


class ConnectorBackend(ABC):
    """Abstract base for sovereign agent platform connectors.

    Subclasses implement this interface for each target platform
    (terminal, VS Code, Cursor, …).  The agent runtime uses connectors
    to send/receive messages without caring about the underlying channel.

    Class attributes:
        connector_type: Set by each subclass to identify its platform.
    """

    connector_type: ConnectorType = ConnectorType.UNKNOWN

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> bool:
        """Establish the platform channel.

        Returns:
            True if the connection was established successfully.
        """

    @abstractmethod
    def disconnect(self) -> bool:
        """Tear down the platform channel cleanly.

        Returns:
            True if disconnection succeeded (or was already disconnected).
        """

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    @abstractmethod
    def send(self, message: str) -> bool:
        """Push a message to the connected platform.

        Args:
            message: UTF-8 text payload to send.

        Returns:
            True if the message was delivered (or queued) successfully.
        """

    @abstractmethod
    def receive(self) -> Optional[str]:
        """Pull the next pending message from the platform (non-blocking).

        Returns:
            Message string if one is available, None otherwise.
        """

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @abstractmethod
    def health_check(self) -> ConnectorStatus:
        """Return the current operational status of this connector.

        Returns:
            ConnectorStatus reflecting the channel health.
        """

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def info(self) -> ConnectorInfo:
        """Return a ConnectorInfo snapshot for this connector.

        Returns:
            ConnectorInfo with current status and metadata.
        """
        return ConnectorInfo(
            name=self.__class__.__name__,
            connector_type=self.connector_type,
            status=self.health_check(),
        )


# ---------------------------------------------------------------------------
# Shared Unix domain socket server connector
# ---------------------------------------------------------------------------


class UnixSocketConnector(ConnectorBackend):
    """Server-side Unix domain socket connector with framed JSON-RPC transport.

    The agent is the **server**: it creates and binds the socket file so the
    IDE extension can connect as a client.  The socket directory is created on
    ``connect()``; the socket file is removed on ``disconnect()``.

    Thread model:
    - An accept-loop daemon thread blocks on ``socket.accept()``.
    - Each accepted client gets a reader daemon thread that enqueues frames.
    - ``send()`` acquires ``_client_lock`` before writing to the live client.

    Subclasses must set ``connector_type`` and pass the desired ``socket_path``
    to ``super().__init__()``.

    Args:
        socket_path: Absolute (or ``~``-prefixed) path for the socket file.
    """

    connector_type: ConnectorType = ConnectorType.UNKNOWN

    def __init__(self, socket_path: Path) -> None:
        self._socket_path: Path = socket_path.expanduser()
        self._server_sock: Optional[socket.socket] = None
        self._client_conn: Optional[socket.socket] = None
        self._client_lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._accept_thread: Optional[threading.Thread] = None
        self._connected = False
        self._error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Create the socket directory, bind, listen, and start accept loop.

        Returns:
            True if the server socket was created successfully.
        """
        if self._connected:
            return True

        try:
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)
            if self._socket_path.exists():
                self._socket_path.unlink()

            self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server_sock.bind(str(self._socket_path))
            self._server_sock.listen(1)
            # Use a timeout so the accept loop can exit cleanly on disconnect.
            self._server_sock.settimeout(1.0)

            self._connected = True
            self._error = None

            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                name=f"{self.__class__.__name__}-accept",
                daemon=True,
            )
            self._accept_thread.start()
            _log.info("%s: listening on %s", self.__class__.__name__, self._socket_path)
            return True

        except OSError as exc:
            self._error = str(exc)
            _log.error("%s: connect() failed: %s", self.__class__.__name__, exc)
            return False

    def disconnect(self) -> bool:
        """Close the server socket, drop the client connection, and remove the socket file.

        Returns:
            True always (errors during teardown are logged and swallowed).
        """
        self._connected = False

        with self._client_lock:
            if self._client_conn is not None:
                try:
                    self._client_conn.close()
                except OSError:
                    pass
                self._client_conn = None

        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass

        return True

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send(self, message: str) -> bool:
        """Write a framed JSON-RPC message to the connected client.

        Frame layout: 4-byte big-endian uint32 length + UTF-8 payload.

        Args:
            message: UTF-8 JSON string to send.

        Returns:
            True if delivered; False if no client is connected or I/O failed.
        """
        with self._client_lock:
            if self._client_conn is None:
                return False
            try:
                payload = message.encode("utf-8")
                header = struct.pack(FRAME_FORMAT, len(payload))
                self._client_conn.sendall(header + payload)
                return True
            except OSError as exc:
                _log.warning("%s: send failed: %s", self.__class__.__name__, exc)
                self._error = str(exc)
                self._client_conn = None
                return False

    def receive(self) -> Optional[str]:
        """Return the next queued message from the client (non-blocking).

        Returns:
            JSON string if one is available, None if the queue is empty.
        """
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> ConnectorStatus:
        """Return the current connector status.

        Returns:
            CONNECTED when a client is live, DISCONNECTED when the server is
            bound but idle, ERROR on any socket failure, or UNAVAILABLE if
            ``connect()`` was never called.
        """
        if self._error:
            return ConnectorStatus.ERROR
        if not self._connected:
            return ConnectorStatus.DISCONNECTED
        with self._client_lock:
            if self._client_conn is not None:
                return ConnectorStatus.CONNECTED
        return ConnectorStatus.DISCONNECTED

    # ------------------------------------------------------------------
    # Internal threads
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        """Daemon thread: accept incoming client connections."""
        while self._connected and self._server_sock is not None:
            try:
                conn, _ = self._server_sock.accept()
                _log.info("%s: client connected", self.__class__.__name__)
                with self._client_lock:
                    if self._client_conn is not None:
                        try:
                            self._client_conn.close()
                        except OSError:
                            pass
                    self._client_conn = conn
                threading.Thread(
                    target=self._read_loop,
                    args=(conn,),
                    name=f"{self.__class__.__name__}-reader",
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._connected:
                    self._error = str(exc)
                    _log.error("%s: accept error: %s", self.__class__.__name__, exc)
                break

    def _read_loop(self, conn: socket.socket) -> None:
        """Daemon thread: read framed messages from a client connection."""
        try:
            while self._connected:
                header_data = _recv_exactly(conn, FRAME_HEADER_SIZE)
                if header_data is None:
                    break
                (length,) = struct.unpack(FRAME_FORMAT, header_data)
                if length == 0 or length > _MAX_FRAME_BYTES:
                    _log.warning(
                        "%s: invalid frame length %d — dropping connection",
                        self.__class__.__name__,
                        length,
                    )
                    break
                payload_data = _recv_exactly(conn, length)
                if payload_data is None:
                    break
                self._queue.put(payload_data.decode("utf-8"))
        except OSError as exc:
            _log.debug("%s: read loop ended: %s", self.__class__.__name__, exc)
        finally:
            with self._client_lock:
                if self._client_conn is conn:
                    self._client_conn = None
            try:
                conn.close()
            except OSError:
                pass
            _log.info("%s: client disconnected", self.__class__.__name__)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _recv_exactly(conn: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly *n* bytes from *conn*, returning None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
