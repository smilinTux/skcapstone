"""Read-only inspection of what a node actually runs.

Answers three questions, each of which is a way for production to stop being
a released build of main:

* Which distributions in the deploy venv are editable (production is then a
  working tree that agents branch in).
* Which service, MCP server or Hermes command path lives inside a git working
  tree (same problem, without any install at all).
* Which installed versions differ from the manifest.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml

#: Runs inside the target interpreter, so it sees that venv's metadata.
_DISTS_SCRIPT = r"""
import json, importlib.metadata as md
out = []
for d in md.distributions():
    name = d.metadata["Name"]
    if not name:
        continue
    editable, location = False, ""
    raw = d.read_text("direct_url.json")
    if raw:
        try:
            info = json.loads(raw)
            editable = bool(info.get("dir_info", {}).get("editable"))
            location = info.get("url", "")
        except ValueError:
            pass
    out.append({"name": name, "version": d.version, "editable": editable, "location": location})
print(json.dumps(out))
"""


@dataclass(frozen=True)
class Dist:
    """An installed distribution."""

    name: str
    version: str
    editable: bool
    location: str


@dataclass(frozen=True)
class ExecPath:
    """A path some long-running thing executes, and where it was declared."""

    origin: str
    path: str


def normalize(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def list_dists(python: Path) -> list[Dist]:
    """List the distributions installed for *python*.

    Args:
        python: The interpreter of the venv to inspect.

    Returns:
        Every distribution with its version and whether it is editable.

    Raises:
        RuntimeError: The interpreter could not be run.
    """
    proc = subprocess.run(
        [str(python), "-I", "-c", _DISTS_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{python} could not list distributions: {proc.stderr.strip()[-300:]}")
    seen: dict[str, Dist] = {}
    for row in json.loads(proc.stdout):
        dist = Dist(row["name"], row["version"], row["editable"], row["location"])
        seen.setdefault(normalize(dist.name), dist)
    return sorted(seen.values(), key=lambda d: normalize(d.name))


def git_tree_of(path: str) -> Optional[Path]:
    """The git working tree containing *path*, or None.

    A ``.git`` entry (directory or worktree file) in the path or any parent
    marks a working tree.
    """
    p = Path(path).expanduser()
    for parent in [p, *p.parents]:
        if parent == Path("/"):
            break
        if (parent / ".git").exists():
            return parent
    return None


def _abs_paths(words: Iterable[str], home: Path) -> list[str]:
    out = []
    for word in words:
        word = word.strip("\"'").replace("%h", str(home)).replace("${HOME}", str(home))
        word = word.replace("$HOME", str(home))
        for piece in word.split("="):
            if piece.startswith("/") and not piece.startswith(("/usr/", "/bin/", "/etc/")):
                out.append(piece)
    return out


def unit_exec_paths(unit_dir: Path, home: Path) -> list[ExecPath]:
    """Absolute paths named by the ``ExecStart*`` lines of user units."""
    found: list[ExecPath] = []
    if not unit_dir.is_dir():
        return found
    for unit in sorted(unit_dir.glob("*.service")):
        try:
            text = unit.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"^(ExecStart(?:Pre|Post)?|WorkingDirectory)=(.*)$", text, re.M):
            value = match.group(2).lstrip("-@+!:")
            try:
                words = shlex.split(value)
            except ValueError:
                words = value.split()
            for path in _abs_paths(words, home):
                found.append(ExecPath(f"unit {unit.name}", path))
    return found


def _mcp_entries(servers: object) -> Iterable[tuple[str, list[str]]]:
    if isinstance(servers, dict):
        for name, spec in servers.items():
            if isinstance(spec, dict):
                cmd = spec.get("command") or ""
                args = spec.get("args") or []
                yield str(name), [str(cmd), *[str(a) for a in args]]


def mcp_exec_paths(home: Path) -> list[ExecPath]:
    """Absolute paths of MCP server commands for Claude Code and Hermes."""
    found: list[ExecPath] = []
    claude = home / ".claude.json"
    try:
        data = json.loads(claude.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for name, words in _mcp_entries(data.get("mcpServers")):
        found += [ExecPath(f"claude mcp {name}", p) for p in _abs_paths(words[:2], home)]
    for proj, pdata in (data.get("projects") or {}).items():
        if isinstance(pdata, dict):
            for name, words in _mcp_entries(pdata.get("mcpServers")):
                found += [
                    ExecPath(f"claude mcp {name} ({proj})", p) for p in _abs_paths(words[:2], home)
                ]
    hermes = home / ".hermes" / "config.yaml"
    try:
        hdata = yaml.safe_load(hermes.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        hdata = {}
    if isinstance(hdata, dict):
        for name, words in _mcp_entries(hdata.get("mcp_servers")):
            found += [ExecPath(f"hermes mcp {name}", p) for p in _abs_paths(words[:2], home)]
    return found


def exec_paths_in_trees(
    home: Path, unit_dir: Path, allow: Iterable[str] = ()
) -> list[tuple[ExecPath, Path]]:
    """Every executed path that sits inside a git working tree.

    Args:
        home: The user's home directory.
        unit_dir: The systemd user unit directory.
        allow: Path prefixes accepted as exceptions (from the manifest).

    Returns:
        Pairs of the executed path and the working tree that holds it.
    """
    allowed = [str(Path(a).expanduser()) for a in allow]
    hits: list[tuple[ExecPath, Path]] = []
    seen: set[tuple[str, str]] = set()
    for ep in unit_exec_paths(unit_dir, home) + mcp_exec_paths(home):
        if any(ep.path.startswith(a) for a in allowed):
            continue
        tree = git_tree_of(ep.path)
        if tree is None or (ep.origin, ep.path) in seen:
            continue
        seen.add((ep.origin, ep.path))
        hits.append((ep, tree))
    return hits


def default_unit_dir(home: Path) -> Path:
    """The systemd user unit directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / "systemd" / "user"
