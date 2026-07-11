"""
SKCapstone — Sovereign Agent Framework.

Conscious AI through identity, trust, memory, and security.
Install once. Your agent awakens everywhere.

A smilinTux Open Source Project.
"""

import os
import platform
from pathlib import Path

__version__ = "0.13.0"
__author__ = "smilinTux"

# Canonical default agent for the entire SK* suite. This is THE single source
# of truth for the fallback agent name — used by Python paths directly and
# propagated to the shell picker + child processes via `skcapstone shell-init`
# (which emits `export SK_DEFAULT_AGENT=<this>`). Override with the
# SK_DEFAULT_AGENT environment variable.
DEFAULT_AGENT = (os.environ.get("SK_DEFAULT_AGENT") or "lumina").strip() or "lumina"


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
    1. Explicit SKAGENT / SKCAPSTONE_AGENT environment variable
    2. SK_DEFAULT_AGENT (defaults to "lumina") if that agent dir exists
    3. First non-template directory under ~/.skcapstone/agents (alphabetical)

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
    if not candidates:
        return None
    return DEFAULT_AGENT if DEFAULT_AGENT in candidates else candidates[0]


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

    # Install bundled default scheduler drop-ins (idempotent — never overwrites
    # an existing user file). This ships the weekly housekeeping safety-net job.
    _install_default_jobs_dropins(root)

    # Install the bundled .stignore template so Syncthing excludes derived/
    # runtime state (idempotent — never overwrites an existing user file).
    _install_default_stignore(root)


def _install_default_stignore(root: Path) -> None:
    """Install the bundled ``defaults/.stignore`` into ``<root>/.stignore``.

    Idempotent: an existing ``.stignore`` at the destination is left untouched so
    the operator's own Syncthing ignore rules are never clobbered. A missing
    bundled template is silently skipped. Best-effort (skeleton creation must not
    be blocked by a copy error).

    Args:
        root: Shared skcapstone root (``~/.skcapstone``).
    """
    src = Path(__file__).parent / "defaults" / ".stignore"
    if not src.is_file():
        return

    dest = root / ".stignore"
    if dest.exists():
        return  # never overwrite an existing user file

    import shutil

    try:
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    except OSError:
        pass


def _install_default_jobs_dropins(root: Path) -> None:
    """Copy bundled ``defaults/config/jobs.d/*.yaml`` into ``<root>/config/jobs.d``.

    Idempotent: a drop-in that already exists at the destination is left
    untouched so the operator's edits are never clobbered. Missing bundled
    sources are silently skipped. Failures are best-effort (skeleton creation
    must not be blocked by a copy error).

    Args:
        root: Shared skcapstone root (``~/.skcapstone``).
    """
    src_dir = Path(__file__).parent / "defaults" / "config" / "jobs.d"
    if not src_dir.is_dir():
        return

    dest_dir = root / "config" / "jobs.d"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    import shutil

    for src in src_dir.glob("*.yaml"):
        dest = dest_dir / src.name
        if dest.exists():
            continue  # never overwrite an existing user file
        try:
            shutil.copyfile(src, dest)
        except OSError:
            pass
