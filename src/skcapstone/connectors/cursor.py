"""
CursorConnector — sovereign agent viewport into the Cursor editor.

Cursor is an Electron-based VS Code fork.  The connector follows the same
Unix domain socket protocol as :class:`VSCodeConnector`: the agent binds
``~/.skcapstone/connectors/cursor.sock`` and the Cursor extension connects.

Protocol
--------
Wire format: 4-byte big-endian uint32 length prefix + UTF-8 JSON-RPC 2.0 body.
See :class:`~skcapstone.connectors.base.UnixSocketConnector` for details.

Extension contract:
  - Extension connects to ``~/.skcapstone/connectors/cursor.sock``.
  - Both sides exchange length-framed JSON-RPC 2.0 messages.
  - Notifications (no ``"id"`` field) are fire-and-forget.
  - Requests carry an ``"id"`` and expect a matching ``"result"`` response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import ConnectorStatus, ConnectorType, UnixSocketConnector

_DEFAULT_SOCKET_PATH = Path("~/.skcapstone/connectors/cursor.sock")


class CursorConnector(UnixSocketConnector):
    """Connect the sovereign agent to the Cursor editor.

    Binds a Unix domain socket at ``~/.skcapstone/connectors/cursor.sock``
    and waits for the SKCapstone Cursor extension to connect.

    Args:
        socket_path: Override the default socket file location.
    """

    connector_type = ConnectorType.CURSOR

    def __init__(self, socket_path: Optional[Path] = None) -> None:
        super().__init__(socket_path or _DEFAULT_SOCKET_PATH)

    def health_check(self) -> ConnectorStatus:
        """Return the current connector status.

        Returns UNAVAILABLE when neither connected nor the socket dir exists,
        so callers can tell whether the extension has ever been set up.
        """
        status = super().health_check()
        if status == ConnectorStatus.DISCONNECTED and not self._socket_path.parent.exists():
            return ConnectorStatus.UNAVAILABLE
        return status
