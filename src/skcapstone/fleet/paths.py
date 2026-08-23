"""Fleet tree layout and node identity.

The fleet tree is a Syncthing-shared directory of JSON files. This module
is the single source of truth for where every file lives; nothing else in
the package builds fleet paths by hand.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def valid_name(name: str) -> bool:
    """Return True when name is a safe kind/object/node name.

    Names must be lowercase alphanumeric with ._- separators and must not
    start with a separator or underscore, which blocks path traversal and
    reserves the underscore prefix (e.g. _freeze.json) for plane files.
    """
    return bool(_NAME_RE.match(name)) and "/" not in name and ".." not in name


@dataclass(frozen=True)
class FleetPaths:
    """All paths inside one fleet tree, derived from its root."""

    root: Path

    @property
    def objects(self) -> Path:
        return self.root / "objects"

    @property
    def placements(self) -> Path:
        return self.root / "placements"

    @property
    def status(self) -> Path:
        return self.root / "status"

    def spec_path(self, kind: str, name: str) -> Path:
        return self.objects / kind / f"{name}.json"

    def placement_path(self, kind: str, name: str) -> Path:
        return self.placements / kind / f"{name}.json"

    def node_status_dir(self, node: str) -> Path:
        return self.status / node

    def status_path(self, node: str, kind: str, name: str) -> Path:
        return self.node_status_dir(node) / kind / f"{name}.json"

    def heartbeat_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "heartbeat.json"

    def node_report_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "node.json"

    def join_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "join.json"

    def events_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "events.jsonl"

    def freeze_path(self) -> Path:
        return self.objects / "_freeze.json"


#: The sovereign home that holds the live fleet tree and its equally live
#: siblings (agents/, trust/, coordination/), all of them Syncthing-shared to
#: the rest of the fleet. It lives here rather than in the module that needs
#: it because this file already owns the question "where does fleet state
#: live", and the answer to "where may a throwaway tree NOT go" is the same
#: fact stated once. Consumed by fleet.drill to keep its refusal honest.
SOVEREIGN_HOME = "~/.skcapstone"


def default_paths() -> FleetPaths:
    """The live fleet tree (SKFLEET_ROOT override for tests)."""
    root = os.environ.get("SKFLEET_ROOT", "~/.skcapstone/fleet")
    return FleetPaths(root=Path(root).expanduser())


def self_node_name() -> str:
    """This machine's node name (SKFLEET_NODE override, else hostname)."""
    env = os.environ.get("SKFLEET_NODE")
    if env:
        return env
    host = socket.gethostname().split(".")[0].lower()
    host = re.sub(r"[^a-z0-9-]", "-", host).strip("-") or "unknown"
    return f"node-{host}"
