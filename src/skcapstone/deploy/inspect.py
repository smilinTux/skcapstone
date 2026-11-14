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


_EXEC_KEYS = ("ExecStart", "ExecStartPre", "ExecStartPost", "WorkingDirectory")
_KEY_RE = re.compile(r"^(ExecStart(?:Pre|Post)?|WorkingDirectory|Environment)=(.*)$", re.M)


def _unit_settings(unit: Path, skip: Iterable[str] = ()) -> dict[str, list[str]]:
    """The effective exec settings of *unit*, with its drop-ins applied.

    Drop-ins in ``<unit>.d/*.conf`` are read after the unit in name order, as
    systemd does: an empty ``ExecStart=`` clears the list so far, and a later
    ``WorkingDirectory=`` replaces an earlier one. ``Environment`` keeps every
    assignment line in order (a later one wins per variable).

    Args:
        unit: The unit file.
        skip: Drop-in file names to leave out (a managed drop-in that is
            about to be regenerated from the rest).
    """
    files = [unit]
    dropin = unit.with_name(unit.name + ".d")
    if dropin.is_dir():
        files += sorted(p for p in dropin.glob("*.conf") if p.name not in set(skip))
    settings: dict[str, list[str]] = {k: [] for k in (*_EXEC_KEYS, "Environment")}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _KEY_RE.finditer(text):
            key, value = match.group(1), match.group(2).strip()
            if key == "Environment":
                if value:
                    settings[key].append(value)
                continue
            if not value:
                settings[key] = []
            elif key == "WorkingDirectory":
                settings[key] = [value]
            else:
                settings[key].append(value)
    return settings


def unit_exec_paths(unit_dir: Path, home: Path) -> list[ExecPath]:
    """Absolute paths named by the effective ``ExecStart*`` lines of user units."""
    found: list[ExecPath] = []
    if not unit_dir.is_dir():
        return found
    for unit in sorted(unit_dir.glob("*.service")):
        settings = _unit_settings(unit)
        for key in _EXEC_KEYS:
            for raw in settings[key]:
                value = raw.lstrip("-@+!:")
                try:
                    words = shlex.split(value)
                except ValueError:
                    words = value.split()
                for path in _abs_paths(words, home):
                    found.append(ExecPath(f"unit {unit.name}", path))
    return found


_INTERPRETERS = ("python", "python3", "bash", "sh", "node", "perl")


def unit_missing_paths(unit_dir: Path, home: Path) -> list[ExecPath]:
    """Program, script and working directory paths of units that do not exist.

    Only the program, the script an interpreter runs (its first non-option
    argument) and ``WorkingDirectory`` are checked: other arguments are often
    remote paths or files a unit creates, which are not breakage.
    """
    missing: list[ExecPath] = []
    for unit in sorted(unit_dir.glob("*.service")):
        settings = _unit_settings(unit)
        candidates: list[str] = []
        for key in ("ExecStart", "ExecStartPre", "ExecStartPost"):
            for raw in settings[key]:
                try:
                    words = shlex.split(raw.lstrip("-@+!:"))
                except ValueError:
                    words = raw.split()
                if not words:
                    continue
                candidates.append(words[0])
                if Path(words[0]).name.rstrip("0123456789.") in _INTERPRETERS:
                    rest = [w for w in words[1:] if not w.startswith("-")]
                    if rest and "-m" not in words[1:2] and "-c" not in words[1:2]:
                        candidates.append(rest[0])
        candidates += settings["WorkingDirectory"][-1:]
        for path in _abs_paths(candidates, home):
            if "%i" in path or "%" in path or "$" in path:
                continue
            if not Path(path).exists():
                missing.append(ExecPath(f"unit {unit.name}", path))
    return missing


def live_units(unit_dir: Path, runner=subprocess.run) -> Optional[set[str]]:
    """User units that can run: enabled, active, or started by a live trigger.

    A template counts as live when any instance is enabled or active. Returns
    None when systemd cannot be asked, so callers treat every unit as live.
    """
    names = [u.name for u in sorted(unit_dir.glob("*.service"))]
    if not names:
        return set()
    try:
        proc = runner(
            ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        files = runner(
            ["systemctl", "--user", "list-unit-files", "--plain", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or files.returncode != 0:
        return None
    active: set[str] = set()
    for line in proc.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[2] in ("active", "activating", "reloading"):
            active.add(cols[0])
    enabled = {
        cols[0]
        for cols in (line.split() for line in files.stdout.splitlines())
        if len(cols) >= 2 and cols[1] in ("enabled", "enabled-runtime", "linked", "alias")
    }
    running = active | enabled
    live: set[str] = set()
    for name in names:
        stem = name.removesuffix(".service")
        triggers = [f"{stem}.timer", f"{stem}.path", f"{stem}.socket"]
        if stem.endswith("@"):
            if any(u.startswith(stem) and u != name for u in running):
                live.add(name)
        elif name in running or any(t in running for t in triggers):
            live.add(name)
    return live


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
    home: Path,
    unit_dir: Path,
    allow: Iterable[str] = (),
    live: Optional[set[str]] = None,
) -> list[tuple[ExecPath, Path]]:
    """Every executed path that sits inside a git working tree.

    Args:
        home: The user's home directory.
        unit_dir: The systemd user unit directory.
        allow: Path prefixes accepted as exceptions (from the manifest).
        live: When given, only these units are reported (a unit nothing can
            start is not running anything from a tree).

    Returns:
        Pairs of the executed path and the working tree that holds it.
    """
    allowed = [str(Path(a).expanduser()) for a in allow]
    hits: list[tuple[ExecPath, Path]] = []
    seen: set[tuple[str, str]] = set()
    for ep in unit_exec_paths(unit_dir, home) + mcp_exec_paths(home):
        if any(ep.path.startswith(a) for a in allowed):
            continue
        if live is not None and ep.origin.startswith("unit ") and ep.origin[5:] not in live:
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
