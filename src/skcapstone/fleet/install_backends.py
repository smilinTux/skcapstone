"""Maps a required package/unit to the repo installer that provides it."""
from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path
from typing import Callable

UNSUPPORTED = "unsupported"

# (glob, backend_id) checked in order; first match wins.
_UNIT_RULES: list[tuple[str, str]] = [
    ("capauth-authz*", "capauth-authz"),
    ("skcomms*", "skcomms"),
    ("skcapstone*", "core"),
    ("sknoded*", "core"),
    ("skgateway*", "core"),
    ("skchat*", "skchat"),
    ("livekit-server*", "skchat"),
    ("jarvis-heartbeat*", "skchat"),
    ("skchat-coturn*", "skchat"),
    ("skmemory-*@*", "agent"),
    ("skwhisper@*", "agent"),
    ("cloud9-daemon@*", "agent"),
]

_TIER: dict[str, int] = {
    "packages": 1,
    "capauth-authz": 2,
    "skcomms": 3,
    "core": 4,
    "skchat": 5,
    "agent": 6,
    UNSUPPORTED: 9,
}


def tier_of(backend_id: str) -> int:
    """Return the install tier for a backend ID.

    Tiers determine install order: lower tiers install first.
    Unsupported units have tier 9 (last).

    Args:
        backend_id: The backend identifier.

    Returns:
        The tier number; 9 if backend_id is not recognized.
    """
    return _TIER.get(backend_id, 9)


def resolve(name: str, kind: str) -> str:
    """Resolve a required unit or package to its owning backend.

    Packages always resolve to the "packages" backend. Units are matched
    against glob patterns in order; the first match wins. Unmatched units
    resolve to UNSUPPORTED.

    Args:
        name: The unit or package name.
        kind: The kind: "unit" or "package".

    Returns:
        The backend ID (e.g., "core", "skchat", "agent") or UNSUPPORTED.
    """
    if kind == "package":
        return "packages"
    for glob, backend in _UNIT_RULES:
        if fnmatch.fnmatch(name, glob):
            return backend
    return UNSUPPORTED


def _repos_root() -> Path:
    """Resolve the skcapstone-repos checkout root.

    Uses the ``SKCAPSTONE_REPOS`` env var when set, otherwise defaults to
    ``~/clawd/skcapstone-repos`` (the standard fleet checkout location).

    Returns:
        The repos root directory as a Path.
    """
    env = os.environ.get("SKCAPSTONE_REPOS")
    return Path(env).expanduser() if env else Path("~/clawd/skcapstone-repos").expanduser()


def _flags(dry_run: bool, enable: bool, start: bool) -> list[str]:
    """Build the standard --dry-run/--enable/--start flag tail for a script.

    Args:
        dry_run: If True, nothing will actually be written, so only
            "--dry-run" is returned (enable/start would be meaningless).
        enable: If True and not dry_run, include "--enable".
        start: If True and not dry_run, include "--start".

    Returns:
        The flags to append to a command argv.
    """
    if dry_run:
        return ["--dry-run"]
    flags = []
    if enable:
        flags.append("--enable")
    if start:
        flags.append("--start")
    return flags


def _agent_name(unit: str) -> str:
    """Extract the agent name from an instanced unit like ``skwhisper@lumina.service``.

    Args:
        unit: The systemd unit name.

    Returns:
        The instance name between "@" and the trailing suffix; the unit
        itself if it has no "@" instance part.
    """
    after_at = unit.split("@", 1)[1] if "@" in unit else unit
    return after_at.rsplit(".", 1)[0]


def _run(runner: Callable, cmd: list[str], *, dry_run: bool) -> tuple[str, str]:
    """Execute cmd via runner and map the result to an (status, detail) pair.

    Always invokes runner (dry-run commands still shell out, just carrying a
    diff/dry-run flag so the underlying installer makes no real changes).

    Args:
        runner: Callable compatible with subprocess.run(cmd, capture_output=True,
            text=True).
        cmd: The command argv to execute.
        dry_run: If True, report "would-write" regardless of the runner's
            return code.

    Returns:
        A ("would-write", cmd_str) | ("failed", stderr_tail) | ("ok", "") tuple.
    """
    result = runner(cmd, capture_output=True, text=True)
    if dry_run:
        return "would-write", " ".join(str(c) for c in cmd)
    if getattr(result, "returncode", 0) != 0:
        return "failed", str(getattr(result, "stderr", "") or "")[-500:]
    return "ok", ""


def default_backends(runner: Callable = subprocess.run) -> dict[str, Callable]:
    """Build the default backend adapters that shell to per-repo installers.

    Each adapter has the signature ``fn(names, *, dry_run, enable, start) ->
    (status, detail)``. Adapters are thin wrappers: they build the argv for
    the owning repo's installer script and hand execution to `runner`
    (default `subprocess.run`) — they never reimplement installer logic.

    Args:
        runner: Callable compatible with subprocess.run(cmd, capture_output=True,
            text=True); injected so tests can fake process execution.

    Returns:
        A dict mapping backend_id to its adapter function.
    """
    repos = _repos_root()

    def packages(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        cmd = ["bash", str(repos / "skcapstone" / "scripts" / "install.sh")]
        cmd += _flags(dry_run, enable, start)
        return _run(runner, cmd, dry_run=dry_run)

    def skchat(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        cmd = ["bash", str(repos / "skchat" / "systemd" / "install.sh")]
        if dry_run:
            cmd.append("--diff")
        else:
            if enable:
                cmd.append("--enable")
            if start:
                cmd.append("--start")
        return _run(runner, cmd, dry_run=dry_run)

    def skcomms(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        cmd = ["bash", str(repos / "skcomms" / "scripts" / "bootstrap.sh"), "--no-service"]
        cmd += _flags(dry_run, enable, start)
        return _run(runner, cmd, dry_run=dry_run)

    def core(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        cmd = ["bash", str(repos / "skcapstone" / "scripts" / "install.sh")]
        if dry_run:
            cmd.append("--dry-run")
        status, detail = _run(runner, cmd, dry_run=dry_run)
        if status == "ok" and enable:
            for name in names:
                enable_cmd = ["systemctl", "--user", "enable", name]
                status, detail = _run(runner, enable_cmd, dry_run=False)
                if status == "failed":
                    break
        return status, detail

    def agent(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        status, detail = "ok", ""
        for name in names:
            if name.startswith("skwhisper@"):
                cmd = ["skwhisper", "install", "--agent", _agent_name(name)]
            else:
                cmd = ["bash", str(repos / "skmemory" / "scripts" / "install-systemd.sh")]
            cmd += _flags(dry_run, enable, start)
            status, detail = _run(runner, cmd, dry_run=dry_run)
            if status == "failed":
                break
        return status, detail

    def capauth_authz(
        names: list[str], *, dry_run: bool, enable: bool, start: bool
    ) -> tuple[str, str]:
        cmd = ["bash", str(repos / "capauth" / "deploy" / "capauth-service" / "deploy.sh")]
        cmd += _flags(dry_run, enable, start)
        return _run(runner, cmd, dry_run=dry_run)

    return {
        "packages": packages,
        "skchat": skchat,
        "skcomms": skcomms,
        "core": core,
        "agent": agent,
        "capauth-authz": capauth_authz,
    }
