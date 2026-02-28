"""
TerminalConnector — sovereign agent viewport over stdin/stdout.

The terminal is the primordial interface: no extension required, no
network needed.  Messages are written to stdout and read from a queue
fed by a background reader thread on stdin.

Thread safety:
- A daemon thread reads stdin lines and enqueues them.
- send() writes directly to the output stream (default: sys.stdout).
- receive() pops from the queue (non-blocking).
"""

from __future__ import annotations

import queue
import sys
import threading
from io import TextIOBase
from typing import Optional, TextIO

from .base import ConnectorBackend, ConnectorStatus, ConnectorType


class TerminalConnector(ConnectorBackend):
    """Read from stdin, write to stdout — the always-available channel.

    Args:
        input_stream: Readable text stream (default: sys.stdin).
        output_stream: Writable text stream (default: sys.stdout).
        prompt: Optional prompt string written before each received line.
    """

    connector_type = ConnectorType.TERMINAL

    def __init__(
        self,
        input_stream: Optional[TextIO] = None,
        output_stream: Optional[TextIO] = None,
        prompt: str = "",
    ) -> None:
        self._input: TextIO = input_stream or sys.stdin
        self._output: TextIO = output_stream or sys.stdout
        self._prompt = prompt
        self._queue: queue.Queue[str] = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._connected = False
        self._error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Start the background stdin reader thread.

        Returns:
            True if connected (idempotent — safe to call multiple times).
        """
        if self._connected:
            return True

        self._error = None
        self._reader = threading.Thread(
            target=self._read_loop,
            name="terminal-connector-reader",
            daemon=True,
        )
        self._reader.start()
        self._connected = True
        return True

    def disconnect(self) -> bool:
        """Mark the connector as disconnected.

        The background reader thread will exit naturally when stdin closes.

        Returns:
            True always.
        """
        self._connected = False
        return True

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send(self, message: str) -> bool:
        """Write a message to the output stream.

        Args:
            message: Text to write.  A trailing newline is appended if
                     not already present.

        Returns:
            True if written successfully, False on I/O error.
        """
        if not self._connected:
            return False
        try:
            if self._prompt:
                self._output.write(self._prompt)
            if not message.endswith("\n"):
                message = message + "\n"
            self._output.write(message)
            self._output.flush()
            return True
        except OSError as exc:
            self._error = str(exc)
            return False

    def receive(self) -> Optional[str]:
        """Return the next queued line from stdin (non-blocking).

        Returns:
            Line string (without trailing newline) or None if the queue
            is empty.
        """
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> ConnectorStatus:
        """Return connector status based on connection state and errors.

        Returns:
            CONNECTED, DISCONNECTED, or ERROR.
        """
        if self._error:
            return ConnectorStatus.ERROR
        if not self._connected:
            return ConnectorStatus.DISCONNECTED
        return ConnectorStatus.CONNECTED

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Background thread: read lines from stdin and enqueue them."""
        try:
            for line in self._input:
                if not self._connected:
                    break
                self._queue.put(line.rstrip("\n"))
        except OSError as exc:
            self._error = str(exc)
