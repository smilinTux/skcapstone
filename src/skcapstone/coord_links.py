"""Shared coordination link persistence for CLI and MCP adapters."""

import socket
from pathlib import Path

from skcoord.card import CardEvent, CardEventLog
from skcoord.card_store import CardStore


def append_coord_link(home: Path, card_id: str, key: str, value: str, writer: str = "") -> None:
    """Write profile receipts to the exact card; preserve other overlay links."""
    if key in {"ci_applicability", "ci_profile_enrollment"}:
        from .ci_applicability import _json

        _json(value)
        CardStore(home).append_event(
            card_id,
            "link",
            writer or socket.gethostname(),
            link_key=key,
            link_value=value,
        )
        return
    CardEventLog(home).append(
        CardEvent(card_id=card_id, action="link", link_key=key, link_value=value, writer=writer)
    )
