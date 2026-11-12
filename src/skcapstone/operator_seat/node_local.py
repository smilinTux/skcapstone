"""Node-local state and the single-actuator-node rule for the operator seat.

Two things must NOT live in the synced fleet tree (``~/.skcapstone/fleet``, replicated by
Syncthing to every node that runs ``skoperator``):

* **Locks.** An ``flock`` only orders processes on one kernel. A lock file that happens to sit
  in a replicated directory looks like it protects something across nodes and does not. Change
  locks therefore live in a node-local state directory (``state_dir``), created private
  (0700, owned by this user, never a symlink).
* **The decision to actuate.** The ledger, the ITIL records and the decisions store ARE
  replicated, so two nodes can each see the same approved change and each spend it. Exactly one
  node may actuate. ``actuator_refusal`` refuses unless this node is the DESIGNATED actuator,
  read from node-local configuration, and it defaults to NONE: until an operator designates a
  node, nothing actuates anywhere.

Designation: the environment variable ``SKOPERATOR_ACTUATOR_NODE`` (for a systemd drop-in) or
the first line of ``<config dir>/skcapstone/atlas-actuator-node`` (``$XDG_CONFIG_HOME``, else
``~/.config``), both node-local. The value is compared with this node's name
(``fleet.paths.self_node_name``).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..fleet.paths import self_node_name

ENV_STATE_DIR = "SKCAPSTONE_LOCAL_STATE_DIR"
ENV_ACTUATOR = "SKOPERATOR_ACTUATOR_NODE"
ACTUATOR_FILE = "atlas-actuator-node"


class UnsafeDirectoryError(RuntimeError):
    """A node-local directory is not private to this user (or is a symlink)."""


def state_dir() -> Path:
    """The node-local state directory (never inside the synced fleet tree)."""
    explicit = os.environ.get(ENV_STATE_DIR)
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "skcapstone"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "skcapstone"


def private_dir(path: Path) -> Path:
    """Create ``path`` (0700) and verify it is a real directory, owned by this user, with no
    group or world access. Raises :class:`UnsafeDirectoryError` otherwise, never repairs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    for candidate in (path.parent, path):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeDirectoryError(f"lock directory {candidate} is a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDirectoryError(f"lock directory {candidate} is not a directory")
        if info.st_uid != os.geteuid():
            raise UnsafeDirectoryError(f"lock directory {candidate} is not owned by this user")
        if info.st_mode & 0o077:
            raise UnsafeDirectoryError(
                f"lock directory {candidate} is not private (mode {stat.S_IMODE(info.st_mode):o})"
            )
    return path


def designated_actuator() -> str | None:
    """The node designated to actuate, from node-local config, or None (the default)."""
    env = os.environ.get(ENV_ACTUATOR, "").strip()
    if env:
        return env
    try:
        for line in (config_dir() / ACTUATOR_FILE).read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except FileNotFoundError:
        return None
    return None


def check_actuator(*, designated: str | None, this_node: str) -> str | None:
    """Why ``this_node`` may not actuate, or None when it is the designated actuator."""
    if not designated:
        return (
            "no actuator node is designated (set SKOPERATOR_ACTUATOR_NODE or "
            f"~/.config/skcapstone/{ACTUATOR_FILE}); nothing actuates until one is"
        )
    if designated != this_node:
        return f"this node ({this_node}) is not the designated actuator ({designated})"
    return None


def actuator_refusal() -> str | None:
    """The single-actuator-node rule for THIS node. Fails closed on any error."""
    try:
        return check_actuator(designated=designated_actuator(), this_node=self_node_name())
    except Exception as exc:  # noqa: BLE001 - an unreadable designation is no designation
        return f"the actuator-node designation could not be read: {exc}"


__all__ = [
    "ACTUATOR_FILE",
    "ENV_ACTUATOR",
    "ENV_STATE_DIR",
    "UnsafeDirectoryError",
    "actuator_refusal",
    "check_actuator",
    "config_dir",
    "designated_actuator",
    "private_dir",
    "state_dir",
]
