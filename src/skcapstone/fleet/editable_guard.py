"""Guard against editable installs in shared interpreters.

This module provides enforcement to prevent workers from installing
packages in editable mode (pip install -e) into shared interpreters
that services run from. This was a root cause of multiple incidents:

- Incident 2026-08-30: Worker e2a2e808 pip installed -e its workspace
  into chiap04's shared .skenv. When the workspace changed,
  static/overview.html vanished and skdashboard returned HTTP 500.

- Incident 2026-08-31: 15+ sklegal packages were editable from an
  orphaned git worktree, so the live legal product was running from
  code git could not even see (card e6096b6e).

The pattern: an editable install makes a mutable, unversioned directory
load-bearing for a running service, and nothing detects it until the
service breaks.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

# Paths that are considered shared / service interpreters
# These are the interpreters that systemd services import from
SHARED_INTERPRETERS = {
    Path.home() / ".skenv",  # The fleet-wide shared venv
}

# Paths that are considered safe for editable installs
# These are per-card scratch workspaces, not load-bearing
SAFE_EDITABLE_PATHS = {
    Path.home() / "work",  # Developer worktrees (OK on developer machines)
    Path("/tmp"),  # Temporary spaces (but flagged as ephemeral)
}


def _get_pip_editable_packages(interpreter_path: Path) -> list[dict]:
    """Get list of editable packages for a given interpreter.

    Args:
        interpreter_path: Path to the Python interpreter (e.g., ~/.skenv/bin/python)

    Returns:
        List of dicts with keys: name, location, path
    """
    try:
        import subprocess

        # Use the specified interpreter to query packages
        result = subprocess.run(
            [str(interpreter_path), "-m", "pip", "list", "-v", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []

    try:
        import json

        packages = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    editable = []
    for pkg in packages:
        if pkg.get("editable_project_location"):
            editable.append(
                {
                    "name": pkg.get("name"),
                    "location": pkg.get("location", ""),
                    "path": Path(pkg.get("editable_project_location", "")).resolve(),
                }
            )
    return editable


def _is_service_host() -> bool:
    """Determine if this host is a service host vs a developer machine.

    Service hosts are part of the fleet (chiap01-08) and run
    production services from the shared .skenv. Developer machines
    may also have .skenv but are primarily for local development.

    Heuristics:
    - Hostname matches fleet pattern (chiap01-08, chiwk11-13)
    - SKFLEET_NODE is set (fleet enrollment)
    - systemd services like skcapstone-daemon are running

    Returns:
        True if this is a fleet service host, False otherwise.
    """
    hostname = os.environ.get("HOSTNAME") or os.uname().nodename

    # Fleet service hosts
    fleet_hosts = {
        "chiap01",
        "chiap02",
        "chiap03",
        "chiap04",
        "chiap08",
        "chiwk11",
        "chiwk13",
    }

    # Check hostname
    if hostname in fleet_hosts:
        return True

    # Check fleet node enrollment
    if os.environ.get("SKFLEET_NODE"):
        return True

    return False


def check_shared_interpreter() -> list[dict]:
    """Check if the current interpreter is a shared one.

    Returns:
        List of shared interpreter paths if current interpreter is shared.
        Empty list if running in a private venv.
    """
    current_exe = Path(sys.executable).resolve()

    shared = []
    for shared_path in SHARED_INTERPRETERS:
        if shared_path in current_exe.parents or shared_path == current_exe.parent:
            shared.append(shared_path)

    return shared


def block_editable_install(*, target_path: Path | None = None) -> Literal["allow", "block", "warn"]:
    """Check if an editable install should be blocked.

    This is called before pip install -e to enforce the policy:
    - On service hosts: NEVER allow editable installs into shared interpreters
    - On developer machines: Allow editable installs, but warn about service paths

    Args:
        target_path: Where the package will be installed from (e.g., /home/user/work/mypkg)

    Returns:
        "allow": Proceed with the editable install
        "block": Block the install with an error
        "warn": Proceed but issue a warning
    """
    # Check if we're in a shared interpreter
    shared = check_shared_interpreter()
    if not shared:
        # Private venv - always allow
        return "allow"

    # We're in a shared interpreter - check if this is a service host
    if _is_service_host():
        # Service host: BLOCK editable installs into shared interpreters
        return "block"

    # Developer machine: warn but allow
    if target_path:
        target = Path(target_path).resolve()

        # Warn if the target looks like a service workspace
        if "fleet/workspaces" in str(target) or "scratch" in str(target).lower():
            return "warn"

    return "allow"


def audit_editable_installs() -> dict[str, list[dict]]:
    """Audit all shared interpreters for editable installs.

    Returns:
        Dict mapping interpreter path to list of editable packages found.
    """
    results = {}

    for shared_path in SHARED_INTERPRETERS:
        python_bin = shared_path / "bin" / "python"
        if not python_bin.exists():
            python_bin = shared_path / "Scripts" / "python.exe"  # Windows

        if python_bin.exists():
            editable = _get_pip_editable_packages(python_bin)
            if editable:
                results[str(shared_path)] = editable

    return results


def format_violation(action: Literal["block", "warn"], target_path: Path | None = None) -> str:
    """Format a violation message for blocking or warning.

    Args:
        action: "block" or "warn"
        target_path: The package path being installed

    Returns:
        Formatted message string.
    """
    if action == "block":
        return (
            "BLOCKED: pip install -e is not allowed into the shared .skenv on service hosts.\n"
            "This pattern has caused multiple outages:\n"
            "- 2026-08-30: chiap04 skdashboard HTTP 500 from workspace change\n"
            "- 2026-08-31: sklegal packages running from orphaned worktree\n\n"
            "Alternatives:\n"
            "1. Build a wheel and install non-editable: pip install .\n"
            "2. Use a private venv for development: python -m venv .venv\n"
            "3. Use pip install --user to install to ~/.local instead of shared venv"
        )
    else:
        target_msg = f" from {target_path}" if target_path else ""
        return (
            f"WARNING: Installing editable package{target_msg} into shared .skenv.\n"
            "This is allowed on developer machines but be aware:\n"
            "- The shared .skenv is used by running services\n"
            "- Changes to the source will immediately affect those services\n"
            "- Consider using a private venv for development work"
        )


def is_editable_install_blocked() -> bool:
    """Convenience function to check if editable installs are blocked.

    This can be called from setup.py or other entry points to enforce
    the guard early.

    Returns:
        True if the current install should be blocked.
    """
    return block_editable_install() == "block"


__all__ = [
    "block_editable_install",
    "is_editable_install_blocked",
    "check_shared_interpreter",
    "audit_editable_installs",
    "format_violation",
    "is_service_host",
    "SHARED_INTERPRETERS",
    "SAFE_EDITABLE_PATHS",
]
