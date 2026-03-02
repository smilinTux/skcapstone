"""Shared helpers for MCP tool modules."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from .. import AGENT_HOME, SHARED_ROOT

logger = logging.getLogger("skcapstone.mcp")


def _home() -> Path:
    """Resolve the per-agent home directory."""
    return Path(AGENT_HOME).expanduser()


def _shared_root() -> Path:
    """Resolve the shared agent root (coordination, heartbeats, peers)."""
    return Path(SHARED_ROOT).expanduser()


def _json_response(data: Any) -> list[TextContent]:
    """Wrap data as a JSON text content response."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _text_response(text: str) -> list[TextContent]:
    """Wrap a plain string as a text content response."""
    return [TextContent(type="text", text=text)]


def _error_response(message: str) -> list[TextContent]:
    """Return an error message as text content."""
    return [TextContent(type="text", text=json.dumps({"error": message}))]


def _get_agent_name(home: Path) -> str:
    """Read the agent name from identity file."""
    identity_path = home / "identity" / "identity.json"
    if identity_path.exists():
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
            return data.get("name", "anonymous")
        except Exception:
            pass
    return "anonymous"
