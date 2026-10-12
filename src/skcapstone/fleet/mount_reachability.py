"""Fail fast on paths whose network mount does not answer.

Every chi node hard-mounts NFS shares from chipv05. When that host went down
(2026-10-12), any open() under /mnt/cloud/onedrive blocked in the kernel with
no timeout. Legacy cards still carry candidate and evidence paths from when
sklegal lived on OneDrive, so rotate hung on one of them each cycle and the
Niobe dispatcher was killed at its 270s budget: no builder or qualification
launched anywhere. Callers treat an unreachable path exactly like a missing one.
"""

from __future__ import annotations

import os
import subprocess

NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "fuse.rclone", "9p"}
)
PROBE_SECONDS = 3.0

_PROBED: dict[str, bool] = {}


def _mounts(table: str = "/proc/mounts") -> list[tuple[str, str]]:
    """Return (mount point, filesystem type) rows; octal escapes decoded."""
    rows = []
    try:
        with open(table, encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) >= 3:
                    point = fields[1].encode().decode("unicode_escape")
                    rows.append((point, fields[2]))
    except OSError:
        return []
    return rows


def _mount_for(path: str, rows) -> tuple[str, str] | None:
    """The longest mount point containing the lexical absolute path."""
    absolute = os.path.abspath(os.path.expanduser(path))
    best = None
    for point, fstype in rows:
        prefix = point.rstrip("/") + "/"
        if absolute == point or absolute.startswith(prefix) or point == "/":
            if best is None or len(point) > len(best[0]):
                best = (point, fstype)
    return best


def _answers(point: str, run=subprocess.run) -> bool:
    """Probe in a killable child so a hung server cannot block this process."""
    try:
        result = run(
            ["stat", "-t", point],
            capture_output=True,
            timeout=PROBE_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def path_reachable(path: str, *, table: str = "/proc/mounts", run=subprocess.run) -> bool:
    """False only when the path lives on a network mount that does not answer.

    The probe result is cached per mount for the life of the process, so a
    cycle pays at most one bounded probe per network mount.
    """
    found = _mount_for(str(path), _mounts(table))
    if found is None or found[1] not in NETWORK_FILESYSTEMS:
        return True
    point = found[0]
    if point not in _PROBED:
        _PROBED[point] = _answers(point, run)
    return _PROBED[point]


def guard(path: str) -> str:
    """Return path, or raise OSError when its network mount is unreachable."""
    if not path_reachable(path):
        raise OSError("network mount for %s is unreachable" % path)
    return path
