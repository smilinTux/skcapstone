"""Which agent folders on this node are real agents, and which are seat debris.

``~/.skcapstone/agents/`` on a chi fleet host holds thousands of folders, and
almost all of them are ephemeral coding-fleet seats (``pi-codex-chiap01-...``,
``codex-...-review``, ``cursor-w128-...``) plus a long tail of folders created
by shell-quoting accidents. Only a handful are agents with a soul, an identity
and a registered memory profile.

Anything that loops over every folder therefore has to decide which ones it
means. ``skcapstone doctor`` did not, and on chiap04 it ran one ``skmemory
health`` subprocess per memory-bearing folder (1,272 of them, 2 to 7 s each),
so it never finished. Worse, the answers were not about those folders at all:
skmemory's resolver is fail-closed, so for an unregistered seat it refuses the
profile and reports on the ``template`` agent instead. Every one of those
1,272 "checks" measured the same template corpus.

This module holds the shared classification:

* :func:`registered_memory_profiles` reads the profile registry, the same
  ``config/profile-registry.json`` skmemory's resolver trusts.
* :func:`is_ephemeral_seat` says whether a folder is a worker seat (no soul,
  no identity, not registered, not the active agent).
* :func:`stale_seats` / :func:`archive_seats` back ``skcapstone agents stale``.

Everything here except :func:`archive_seats` is read-only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

#: Same relative path skcapstone.profile_registry and skmemory use.
REGISTRY_RELPATH = Path("config") / "profile-registry.json"

#: Agent selectors, in the precedence order every SK resolver uses.
AGENT_SELECTOR_VARS = ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT")

#: Folder names that are structural, never seats, whatever they contain.
#: ``template`` is the agent skmemory falls back to; ``archive`` may be an
#: operator's own archive dir.
STRUCTURAL_NAMES = frozenset({"template", "archive"})

#: Fleet seat naming shapes. A hint only: the structural test (no soul, no
#: identity) is what decides, this just labels the reason.
_SEAT_NAME_RE = re.compile(
    r"^(codex|pi|kimi|glm|cursor|claude|deepseek|ds|qwen)-"
    r"|(^|-)(review|reviewer|rereview|repair|repairer|independent|worker|builder|canary)(-|$)"
    r"|-[0-9a-f]{8}(-|$)"
)

#: Roles that the lightweight provisioner writes for fleet seats.
_SEAT_ROLES = frozenset({"worker", "reviewer"})


def active_agent_from_env(env: Optional[dict] = None) -> str:
    """Return the agent named by the selectors, or ``""`` when none is set.

    Deliberately env-only: no sole-agent or default-agent guessing. A doctor
    run that picked an agent by guessing would be checking someone else.
    """
    env = os.environ if env is None else env
    for var in AGENT_SELECTOR_VARS:
        value = (env.get(var) or "").strip()
        if value:
            return value
    return ""


def shared_root_for(home: Path) -> Path:
    """Map either the shared root or one agent's home to the shared root."""
    home = Path(home).expanduser()
    if home.parent.name == "agents":
        return home.parent.parent
    return home


def registered_memory_profiles(root: Path) -> Optional[set[str]]:
    """Profile ids in the registry that carry a memory principal.

    A lenient read: this only decides which folders doctor looks at, it does
    not grant anything. Hash validation stays with the real resolvers, and a
    registry that fails it still shows up loudly in ``skmemory profile status``.

    Returns:
        The set of ids, or ``None`` when the registry is missing or unreadable
        (callers must tell "no registry" apart from "empty registry").
    """
    path = Path(root).expanduser() / REGISTRY_RELPATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = data.get("profiles") or []
        return {
            str(p["profile_id"])
            for p in profiles
            if isinstance(p, dict)
            and p.get("profile_id")
            and p.get("memory_principal_id")
            and p.get("selectable", True)
        }
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return None


def resolvable_memory_profiles(root: Path) -> tuple[set[str], dict[str, str]]:
    """Split registered memory profiles into resolvable and broken ones.

    Uses the strict resolver (:func:`skcapstone.profile_registry.resolve_profile`),
    the same contract skmemory enforces. A registry entry that does not
    resolve (no ``profile.json``, stale hash, ...) is one skmemory will refuse
    and silently replace with ``template``, so measuring it would report the
    wrong agent's memory. Seen on noroc2027 2026-10-27: 7 of 13 registered
    swarm agents were ``missing_profile``.

    Returns:
        ``(healthy_ids, {broken_id: state})``. Both empty when the registry
        is missing or unreadable.
    """
    from .profile_registry import resolve_profile

    healthy: set[str] = set()
    broken: dict[str, str] = {}
    for profile_id in sorted(registered_memory_profiles(root) or ()):
        try:
            resolved = resolve_profile(root, profile_id)
        except Exception as exc:  # a resolver crash must not take doctor down
            broken[profile_id] = f"error: {exc.__class__.__name__}"
            continue
        if resolved.healthy and resolved.memory_principal_id:
            healthy.add(profile_id)
        else:
            broken[profile_id] = resolved.state
    return healthy, broken


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def seat_reason(agent_dir: Path) -> str:
    """Why a folder looks like a seat: ``lightweight``, ``name`` or ``no-identity``."""
    ident = _read_json(agent_dir / "identity.json")
    if ident.get("profile") == "lightweight" or ident.get("role") in _SEAT_ROLES:
        return "lightweight"
    if _SEAT_NAME_RE.search(agent_dir.name):
        return "name"
    return "no-identity"


def is_ephemeral_seat(
    agent_dir: Path,
    registered: Optional[set[str]] = None,
    active: str = "",
) -> bool:
    """True when ``agent_dir`` is a worker seat rather than a real agent.

    A real agent is anything registered as a memory profile, the active
    agent, a structural folder, or a folder with a ``soul/`` or ``identity/``
    directory. Everything else is a seat. This is structural on purpose:
    chi seat names follow no single pattern (half of them came from quoting
    accidents), but none of them has a soul.
    """
    name = agent_dir.name
    if name == active or (registered and name in registered):
        return False
    if name in STRUCTURAL_NAMES or name.endswith("-template"):
        return False
    if (agent_dir / "soul").is_dir() or (agent_dir / "identity").is_dir():
        return False
    return True


def iter_agent_dirs(root: Path) -> Iterable[Path]:
    """Every directory under ``<root>/agents``, sorted, symlinks not followed.

    Dot-directories are never agents: noroc2027 keeps a 227 MiB ``.git`` in
    ``agents/``, and ``.stfolder``/``.stversions`` belong to Syncthing.
    """
    agents_dir = Path(root).expanduser() / "agents"
    try:
        entries = sorted(os.scandir(agents_dir), key=lambda e: e.name)
    except OSError:
        return []
    return [
        Path(e.path)
        for e in entries
        if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)
    ]


# ---------------------------------------------------------------------------
# Stale seat report
# ---------------------------------------------------------------------------


@dataclass
class StaleSeat:
    """One seat folder with no activity for longer than the threshold."""

    name: str
    path: str
    last_activity: float
    size_bytes: int
    files: int
    reason: str


@dataclass
class StaleReport:
    """Result of :func:`stale_seats`."""

    root: str
    days: int
    total_dirs: int = 0
    seats: int = 0
    real_agents: list[str] = field(default_factory=list)
    stale: list[StaleSeat] = field(default_factory=list)

    @property
    def stale_bytes(self) -> int:
        return sum(s.size_bytes for s in self.stale)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "days": self.days,
            "total_dirs": self.total_dirs,
            "seats": self.seats,
            "real_agents": self.real_agents,
            "stale_count": len(self.stale),
            "stale_bytes": self.stale_bytes,
            "stale": [s.__dict__ for s in self.stale],
        }


def _walk_stats(path: Path) -> tuple[float, int, int]:
    """Newest mtime, total bytes and file count under ``path`` (no symlink follow)."""
    newest = 0.0
    size = 0
    files = 0
    try:
        newest = path.lstat().st_mtime
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            if name in filenames:
                files += 1
                size += st.st_size
    return newest, size, files


def stale_seats(
    root: Path,
    days: int = 14,
    now: Optional[float] = None,
    active: Optional[str] = None,
) -> StaleReport:
    """List seat folders whose newest file is older than ``days``.

    Read-only. Real agents (registered, active, soul or identity present,
    structural) are never listed, whatever their age.
    """
    root = Path(root).expanduser()
    now = time.time() if now is None else now
    active = active_agent_from_env() if active is None else active
    registered = registered_memory_profiles(root) or set()
    cutoff = now - days * 86400
    report = StaleReport(root=str(root), days=days)
    for agent_dir in iter_agent_dirs(root):
        report.total_dirs += 1
        if not is_ephemeral_seat(agent_dir, registered, active):
            report.real_agents.append(agent_dir.name)
            continue
        report.seats += 1
        newest, size, files = _walk_stats(agent_dir)
        if newest < cutoff:
            report.stale.append(
                StaleSeat(
                    name=agent_dir.name,
                    path=str(agent_dir),
                    last_activity=newest,
                    size_bytes=size,
                    files=files,
                    reason=seat_reason(agent_dir),
                )
            )
    return report


def archive_seats(report: StaleReport, archive_dir: Path) -> list[tuple[str, str]]:
    """Move every stale seat in ``report`` into ``archive_dir`` (never deletes).

    Each seat is re-checked against the same rules right before it moves, so
    a folder that gained a soul, a registration or new activity since the
    report was built is left alone.

    Returns:
        ``(name, destination)`` for each folder moved.
    """
    archive_dir = Path(archive_dir).expanduser()
    archive_dir.mkdir(parents=True, exist_ok=True)
    root = Path(report.root)
    registered = registered_memory_profiles(root) or set()
    active = active_agent_from_env()
    cutoff = time.time() - report.days * 86400
    moved: list[tuple[str, str]] = []
    for seat in report.stale:
        src = Path(seat.path)
        if not src.is_dir() or not is_ephemeral_seat(src, registered, active):
            continue
        if _walk_stats(src)[0] >= cutoff:
            continue
        dest = archive_dir / seat.name
        if dest.exists():
            dest = archive_dir / f"{seat.name}.{int(time.time())}"
        shutil.move(str(src), str(dest))
        moved.append((seat.name, str(dest)))
    return moved
