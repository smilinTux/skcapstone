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
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, Optional


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
