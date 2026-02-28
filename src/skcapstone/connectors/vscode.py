"""
VSCodeConnector — sovereign agent viewport into Visual Studio Code.

Communicates with the SKCapstone VS Code extension via a local Unix
socket or HTTP endpoint exposed by the extension's language server.

Status: STUB — the extension protocol is not yet defined.
        connect() returns False (UNAVAILABLE) until the extension
        publishes its socket path in ~/.skcapstone/connectors/vscode.sock.

Extension contract (planned):
  - Extension writes socket path to ~/.skcapstone/connectors/vscode.sock
  - Agent connects, exchanges newline-delimited JSON messages
  - Each message: {"type": "...", "payload": {...}}
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .base import ConnectorBackend, ConnectorStatus, ConnectorType

logger = logging.getLogger(__name__)

_DEFAULT_SOCKET_PATH = Path("~/.skcapstone/connectors/vscode.sock")


class VSCodeConnector(ConnectorBackend):
    """Connect the sovereign agent to Visual Studio Code.

    Reads the extension socket path from ``~/.skcapstone/connectors/vscode.sock``.
    Returns UNAVAILABLE until the VS Code extension is installed and running.

    Args:
        socket_path: Override the default socket file location.
    """

    connector_type = ConnectorType.VSCODE

    def __init__(self, socket_path: Optional[Path] = None) -> None:
        self._socket_path = (socket_path or _DEFAULT_SOCKET_PATH).expanduser()
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Attempt to connect to the VS Code extension socket.

        Returns:
            False — stub not yet implemented.  Logs a notice with setup
            instructions.
        """
        if not self._socket_path.exists():
            logger.info(
                "VSCodeConnector: socket not found at %s. "
                "Install the SKCapstone VS Code extension to enable this connector.",
                self._socket_path,
            )
            return False

        # TODO: implement unix socket / HTTP connection to extension
        logger.warning(
            "VSCodeConnector.connect() is a stub — socket exists at %s "
            "but protocol is not yet implemented.",
            self._socket_path,
        )
        return False

    def disconnect(self) -> bool:
        """No-op disconnect for the stub.

        Returns:
            True always.
        """
        self._connected = False
        return True

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send(self, message: str) -> bool:
        """Not implemented — VS Code connector is a stub.

        Args:
            message: Ignored.

        Returns:
            False always.
        """
        logger.debug("VSCodeConnector.send() called but connector is a stub.")
        return False

    def receive(self) -> Optional[str]:
        """Not implemented — VS Code connector is a stub.

        Returns:
            None always.
        """
        return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> ConnectorStatus:
        """Return UNAVAILABLE until the extension socket is present.

        Returns:
            CONNECTED if connected, UNAVAILABLE if socket missing,
            DISCONNECTED otherwise.
        """
        if self._connected:
            return ConnectorStatus.CONNECTED
        if not self._socket_path.exists():
            return ConnectorStatus.UNAVAILABLE
        return ConnectorStatus.DISCONNECTED
