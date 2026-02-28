"""
Platform connectors — windows into the sovereign agent.

Every connector talks to the same AgentRuntime.
The platform is just a viewport. The agent is the truth.
"""

from .base import ConnectorBackend, ConnectorInfo, ConnectorStatus, ConnectorType
from .cursor import CursorConnector
from .registry import ConnectorRegistry
from .terminal import TerminalConnector
from .vscode import VSCodeConnector

__all__ = [
    "ConnectorBackend",
    "ConnectorInfo",
    "ConnectorRegistry",
    "ConnectorStatus",
    "ConnectorType",
    "CursorConnector",
    "TerminalConnector",
    "VSCodeConnector",
]
