"""
VSCodeConnector — sovereign agent viewport into Visual Studio Code.

Communicates with the SKCapstone VS Code extension via a Unix domain socket
at ``~/.skcapstone/connectors/vscode.sock``.

Protocol
--------
The agent is the **server**.  On ``connect()`` it creates and binds the socket
so the VS Code extension (client) can connect at any time.

Wire format: 4-byte big-endian uint32 length prefix + UTF-8 JSON-RPC 2.0 body.
See :class:`~skcapstone.connectors.base.UnixSocketConnector` for details.

Extension contract:
  - Extension connects to ``~/.skcapstone/connectors/vscode.sock``.
  - Both sides exchange length-framed JSON-RPC 2.0 messages.
  - Notifications (no ``"id"`` field) are fire-and-forget.
  - Requests carry an ``"id"`` and expect a matching ``"result"`` response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import ConnectorStatus, ConnectorType, UnixSocketConnector

_DEFAULT_SOCKET_PATH = Path("~/.skcapstone/connectors/vscode.sock")


class VSCodeConnector(UnixSocketConnector):
    """Connect the sovereign agent to Visual Studio Code.

    Binds a Unix domain socket at ``~/.skcapstone/connectors/vscode.sock``
    and waits for the SKCapstone VS Code extension to connect.

    Args:
        socket_path: Override the default socket file location.
    """

    connector_type = ConnectorType.VSCODE

    def __init__(self, socket_path: Optional[Path] = None) -> None:
        super().__init__(socket_path or _DEFAULT_SOCKET_PATH)

    # health_check is inherited — returns UNAVAILABLE only if _connected is False
    # and no error; override to surface UNAVAILABLE while socket dir is absent
    # so that registry.probe() can distinguish "never started" from "disconnected".

    def health_check(self) -> ConnectorStatus:
        """Return the current connector status.

        Returns UNAVAILABLE when neither connected nor the socket dir exists,
        so callers can tell whether the extension has ever been set up.
        """
        status = super().health_check()
        if status == ConnectorStatus.DISCONNECTED and not self._socket_path.parent.exists():
            return ConnectorStatus.UNAVAILABLE
        return status
