"""
SKCapstone — Sovereign Agent Framework.

Conscious AI through identity, trust, memory, and security.
Install once. Your agent awakens everywhere.

A smilinTux Open Source Project.
"""

import os
import platform
from pathlib import Path

__version__ = "0.6.7"
__author__ = "smilinTux"


def _default_home() -> str:
    """Platform-aware default home for skcapstone."""
    if platform.system() == "Windows":
        # Use %LOCALAPPDATA%\skcapstone on Windows
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return os.path.join(local, "skcapstone")
    return os.path.expanduser("~/.skcapstone")


def _detect_active_agent(root: str | None = None) -> str | None:
    """Best-effort active agent discovery.

    Resolution order:
    1. Explicit SKCAPSTONE_AGENT environment variable
    2. First non-template directory under ~/.skcapstone/agents

    Returns:
        The active agent name if one can be resolved, else None.
    """
    env_agent = (os.environ.get("SKAGENT") or os.environ.get("SKCAPSTONE_AGENT", "")).strip()
    if env_agent:
        return env_agent

    base = Path(root or os.environ.get("SKCAPSTONE_HOME", _default_home())).expanduser()
    agents_dir = base / "agents"
    if not agents_dir.exists():
        return None

    candidates = sorted(
        entry.name
        for entry in agents_dir.iterdir()
        if entry.is_dir() and not entry.name.endswith("-template")
    )
    return candidates[0] if candidates else None


# Root of the skcapstone tree (shared infra lives here)
AGENT_HOME = os.environ.get("SKCAPSTONE_HOME", _default_home())

# Which agent this process is running as (set by daemon/connector)
SKCAPSTONE_AGENT = _detect_active_agent() or ""

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
    name = agent_name or SKCAPSTONE_AGENT or _detect_active_agent()
    root = Path(AGENT_HOME).expanduser()
    if name:
        return root / "agents" / name
    return root


def active_agent_name() -> str | None:
    """Return the currently active agent name, if one can be resolved."""
    return SKCAPSTONE_AGENT or _detect_active_agent()


def shared_home() -> Path:
    """Return the shared root directory (~/.skcapstone/).

    Node-level resources live here: identity, comms config,
    coordination, peers, docs.

    Returns:
        Path to the shared skcapstone root.
    """
    return Path(AGENT_HOME).expanduser()


def ensure_skeleton(agent_name: str | None = None) -> None:
    """Create all expected directories for the shared root and agent home.

    Idempotent — safe to call multiple times. Creates any missing
    directories so that all CLI commands and services find the paths
    they expect.

    Args:
        agent_name: Agent name (defaults to SKCAPSTONE_AGENT).
    """
    root = shared_home()
    name = agent_name or SKCAPSTONE_AGENT
    agent_dir = root / "agents" / name

    # Shared root directories
    for d in (
        root / "config",
        root / "identity",
        root / "security",
        root / "skills",
        root / "heartbeats",
        root / "peers",
        root / "coordination" / "tasks",
        root / "coordination" / "agents",
        root / "logs",
        root / "comms" / "inbox",
        root / "comms" / "outbox",
        root / "comms" / "archive",
        root / "archive",
        root / "deployments",
        root / "docs",
        root / "metrics",
        root / "memory",
        root / "sync" / "outbox",
        root / "sync" / "inbox",
        root / "sync" / "archive",
        root / "trust" / "febs",
    ):
        d.mkdir(parents=True, exist_ok=True)

    # Per-agent directories
    for d in (
        agent_dir / "memory" / "short-term",
        agent_dir / "memory" / "mid-term",
        agent_dir / "memory" / "long-term",
        agent_dir / "soul" / "installed",
        agent_dir / "wallet",
        agent_dir / "seeds",
        agent_dir / "identity",
        agent_dir / "config",
        agent_dir / "logs",
        agent_dir / "security",
        agent_dir / "cloud9",
        agent_dir / "trust" / "febs",
        agent_dir / "sync" / "outbox",
        agent_dir / "sync" / "inbox",
        agent_dir / "sync" / "archive",
        agent_dir / "reflections",
        agent_dir / "improvements",
        agent_dir / "scripts",
        agent_dir / "cron",
        agent_dir / "archive",
        agent_dir / "comms" / "inbox",
        agent_dir / "comms" / "outbox",
        agent_dir / "comms" / "archive",
    ):
        d.mkdir(parents=True, exist_ok=True)
