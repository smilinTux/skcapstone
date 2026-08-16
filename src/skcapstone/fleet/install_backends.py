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
    # NOTE: cloud9-daemon@* is intentionally NOT mapped here. cloud9 ships only
    # a unit template; there is no installer that knows about it, so it must
    # resolve to UNSUPPORTED and surface as needs_manual rather than being
    # routed through the (wrong) skmemory installer.
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
    """Report what would run, or execute cmd via runner and map the result.

    CRITICAL: when dry_run is True, runner is NEVER invoked. Several of the
    real per-repo installers (e.g. skcapstone/scripts/install.sh) silently
    ignore unrecognized flags like --dry-run and perform a real install, so
    the only safe way to honor dry_run is to skip the subprocess call
    entirely rather than trust the target script to no-op.

    stdin is always wired to DEVNULL for the real (non-dry-run) call: no
    per-repo installer this dispatches to may block this process on a TTY
    prompt, and several (e.g. skcapstone/scripts/install.sh without
    --non-interactive) read from stdin interactively and default to "yes"
    on EOF, which must never happen unattended.

    Args:
        runner: Callable compatible with
            subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL).
        cmd: The command argv that would be (or is) executed.
        dry_run: If True, report "would-write" without calling runner at all.

    Returns:
        A ("would-write", cmd_str) | ("failed", stderr_tail) | ("ok", "") tuple.
    """
    if dry_run:
        return "would-write", " ".join(str(c) for c in cmd)
    result = runner(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
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

    def _enable_units(names: list[str]) -> tuple[str, str]:
        """Run `systemctl --user enable <name>` for each name, stopping on failure."""
        status, detail = "ok", ""
        for name in names:
            status, detail = _run(runner, ["systemctl", "--user", "enable", name], dry_run=False)
            if status == "failed":
                break
        return status, detail

    def packages(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        # install.sh recognizes --dev/--force/--non-interactive; it has no
        # units of its own to enable, so enable/start are no-ops here.
        # --non-interactive is mandatory: without it the script's Linux
        # systemd section blocks on a TTY read (hangs) or, on EOF under a
        # non-interactive subprocess, defaults every prompt to "Y" and
        # installs+enables+starts systemd units. This backend's contract is
        # copy-only (venv + pip install), never activate.
        cmd = ["bash", str(repos / "skcapstone" / "scripts" / "install.sh"), "--non-interactive"]
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
        # bootstrap.sh only recognizes --no-service (any other arg exits 2);
        # unit enablement is a separate systemctl step.
        cmd = ["bash", str(repos / "skcomms" / "scripts" / "bootstrap.sh"), "--no-service"]
        status, detail = _run(runner, cmd, dry_run=dry_run)
        if status == "ok" and enable:
            status, detail = _enable_units(names)
        return status, detail

    def core(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        # Same underlying installer as "packages", run the same way
        # (--non-interactive: never prompt, never let install.sh touch
        # systemd itself). "core" is what actually enables/starts units,
        # and it does so explicitly via systemctl below (the installer
        # itself takes no --enable flag).
        cmd = ["bash", str(repos / "skcapstone" / "scripts" / "install.sh"), "--non-interactive"]
        status, detail = _run(runner, cmd, dry_run=dry_run)
        if status == "ok" and enable:
            status, detail = _enable_units(names)
        return status, detail

    def agent(names: list[str], *, dry_run: bool, enable: bool, start: bool) -> tuple[str, str]:
        status, detail = "ok", ""
        for name in names:
            if name.startswith("skwhisper@"):
                # `skwhisper install` always enables; --start is the only optional flag.
                cmd = ["skwhisper", "install", "--agent", _agent_name(name)]
                if start:
                    cmd.append("--start")
            else:
                # install-systemd.sh requires --agents in non-interactive mode
                # (bare invocation hits an interactive `read` prompt and aborts
                # under subprocess); it takes no --dry-run/--enable/--start flags.
                cmd = [
                    "bash",
                    str(repos / "skmemory" / "scripts" / "install-systemd.sh"),
                    "--agents",
                    _agent_name(name),
                ]
            status, detail = _run(runner, cmd, dry_run=dry_run)
            if status == "failed":
                break
        return status, detail

    def capauth_authz(
        names: list[str], *, dry_run: bool, enable: bool, start: bool
    ) -> tuple[str, str]:
        # deploy.sh's $1 is a positional MODE (--test|--stop|--status|--provision|"");
        # it does not accept --dry-run/--enable/--start. Bare invocation
        # provisions secrets and deploys.
        cmd = ["bash", str(repos / "capauth" / "deploy" / "capauth-service" / "deploy.sh")]
        return _run(runner, cmd, dry_run=dry_run)

    return {
        "packages": packages,
        "skchat": skchat,
        "skcomms": skcomms,
        "core": core,
        "agent": agent,
        "capauth-authz": capauth_authz,
    }
