"""Default agent profiles shipped with SKCapstone.

The ``lumina`` profile provides a ready-to-use sovereign agent with
pre-loaded memories about the SKWorld ecosystem, default seeds for
emotional calibration, and a welcome FEB file.

Usage::

    from skcapstone.defaults import install_default_agent
    install_default_agent("lumina", target=Path("~/.skcapstone/agents/lumina"))
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from importlib import resources


def get_defaults_path(agent_name: str = "lumina") -> Path:
    """Return the path to the bundled default profile for *agent_name*."""
    ref = resources.files("skcapstone") / "defaults" / agent_name
    return Path(str(ref))


def install_default_agent(
    agent_name: str = "lumina",
    target: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Copy the bundled default agent profile to the target directory.

    Args:
        agent_name: Name of the default profile (currently only "lumina").
        target: Destination directory. Defaults to
                ``~/.skcapstone/agents/{agent_name}``.
        overwrite: If True, overwrite existing files.

    Returns:
        Path to the installed agent directory.
    """
    src = get_defaults_path(agent_name)
    if target is None:
        target = Path.home() / ".skcapstone" / "agents" / agent_name

    target = target.expanduser()

    if target.exists() and not overwrite:
        return target

    if src.is_dir():
        shutil.copytree(src, target, dirs_exist_ok=True)

    return target
