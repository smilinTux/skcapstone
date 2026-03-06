"""
SKCapstone — Sovereign Agent Framework.

Conscious AI through identity, trust, memory, and security.
Install once. Your agent awakens everywhere.

A smilinTux Open Source Project.
"""

import os
from pathlib import Path

__version__ = "0.2.0"
__author__ = "smilinTux"

# Root of the skcapstone tree (shared infra lives here)
AGENT_HOME = os.environ.get("SKCAPSTONE_HOME", "~/.skcapstone")

# Which agent this process is running as (set by daemon/connector)
SKCAPSTONE_AGENT = os.environ.get("SKCAPSTONE_AGENT", "lumina")

# Default daemon port
DEFAULT_PORT = int(os.environ.get("SKCAPSTONE_PORT", "9383"))

# Backwards-compatible aliases (used by CLI, peers, dashboard, etc.)
SHARED_ROOT = os.environ.get("SKCAPSTONE_SHARED_ROOT", AGENT_HOME)
SKCAPSTONE_ROOT = os.environ.get("SKCAPSTONE_ROOT", AGENT_HOME)
AGENT_PORTS: dict[str, int] = {
    "opus": 9383,
    "lumina": 9383,
    "jarvis": 9383,
}


def agent_home(agent_name: str | None = None) -> Path:
    """Resolve the home directory for a specific agent.

    Per-agent state lives at ~/.skcapstone/agents/<name>/.
    Shared infrastructure stays at ~/.skcapstone/.

    If no agent_name is given, falls back to SKCAPSTONE_AGENT env var,
    then to the root AGENT_HOME.

    Args:
        agent_name: Agent name (e.g. "lumina", "opus").

    Returns:
        Path to the agent-specific home directory.
    """
    name = agent_name or SKCAPSTONE_AGENT
    root = Path(AGENT_HOME).expanduser()
    if name:
        return root / "agents" / name
    return root


def shared_home() -> Path:
    """Return the shared root directory (~/.skcapstone/).

    Node-level resources live here: identity, comms config,
    coordination, peers, docs.

    Returns:
        Path to the shared skcapstone root.
    """
    return Path(AGENT_HOME).expanduser()
