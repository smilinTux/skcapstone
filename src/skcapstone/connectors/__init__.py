"""
Platform connectors — windows into the sovereign agent.

Every connector talks to the same AgentRuntime.
The platform is just a viewport. The agent is the truth.
"""

from .base import (
    FRAME_FORMAT,
    FRAME_HEADER_SIZE,
    ConnectorBackend,
    ConnectorInfo,
    ConnectorStatus,
    ConnectorType,
    UnixSocketConnector,
)
from .cursor import CursorConnector
from .registry import ConnectorRegistry
from .terminal import TerminalConnector
from .vscode import VSCodeConnector

__all__ = [
    "FRAME_FORMAT",
    "FRAME_HEADER_SIZE",
    "ConnectorBackend",
    "ConnectorInfo",
    "ConnectorRegistry",
    "ConnectorStatus",
    "ConnectorType",
    "CursorConnector",
    "TerminalConnector",
    "UnixSocketConnector",
    "VSCodeConnector",
]
