"""LaunchD service management for the SKCapstone daemon (macOS).

Installs, manages, and queries launchd user agents — the macOS
equivalent of systemd user services. No root required.

Generates plist files dynamically with the correct agent name,
paths, and environment. Copies them to ~/Library/LaunchAgents/
and loads via launchctl.

Usage:
    from skcapstone.launchd import install_service, service_status
    install_service(agent_name="myagent")
    status = service_status()
"""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.launchd")

LABEL_PREFIX = "com.skcapstone"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Plist definitions: (suffix, program_args, schedule, nice)
# program_args use {skenv} and {agent} as placeholders
_SERVICE_DEFS: list[dict] = [
    {
        "suffix": "daemon",
        "args": ["{skenv}/skcapstone", "daemon", "start", "--foreground"],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "OLLAMA_KEEP_ALIVE": "5m",
            "SKCAPSTONE_AGENT": "{agent}",
        },
        "keep_alive": True,
        "throttle": 10,
        "logs": "{logs}/daemon",
    },
    {
        "suffix": "memory-compress",
        "args": ["{skenv}/skcapstone", "memory", "compress"],
        "env": {"PYTHONUNBUFFERED": "1"},
        "calendar": {"Weekday": 0, "Hour": 0, "Minute": 0},
        "nice": 15,
        "logs": "{logs}/memory-compress",
    },
    {
        "suffix": "skcomm-heartbeat",
        "args": ["{skenv}/skcomm", "heartbeat"],
        "env": {},
        "interval": 60,
        "nice": 19,
        "logs": "{logs}/skcomm-heartbeat",
    },
    {
        "suffix": "skcomm-queue-drain",
        "args": ["{skenv}/skcomm", "queue", "drain"],
        "env": {},
        "interval": 120,
        "nice": 19,
        "logs": "{logs}/skcomm-queue-drain",
    },
]

# Optional services from other repos
_OPTIONAL_DEFS: list[dict] = [
    {
        "suffix": "skchat-daemon",
        "args": ["{skenv}/skchat", "daemon", "start", "--interval", "5",
                 "--log-file", "{home}/.skchat/daemon.log"],
        "env": {"SKCHAT_IDENTITY": "capauth:{agent}@skworld.io"},
        "keep_alive": True,
        "throttle": 5,
        "logs": "{skchat}/launchd",
        "requires_bin": "skchat",
    },
    {
        "suffix": "skcomm-api",
        "args": ["{skenv}/python3", "-m", "uvicorn", "skcomm.api:app",
                 "--host", "127.0.0.1", "--port", "9384", "--log-level", "info"],
        "env": {"SKCHAT_IDENTITY": "capauth:{agent}@skworld.io"},
        "keep_alive": True,
        "throttle": 5,
        "logs": "{skcomm}/launchd",
        "requires_bin": "skcomm",
    },
    {
        "suffix": "skcomm-daemon",
        "args": ["{skenv}/skcomm", "daemon", "--all-agents", "--interval", "5"],
        "env": {},
        "keep_alive": True,
        "throttle": 5,
        "logs": "{skcomm}/daemon",
        "requires_bin": "skcomm",
    },
]


def _require_macos() -> None:
    """Raise RuntimeError if not running on macOS."""
    if platform.system() != "Darwin":
        raise RuntimeError(
            "launchd is only available on macOS. Use systemd on Linux."
        )


def _skenv_bin() -> str:
    """Return the skenv bin directory."""
    return str(Path.home() / ".skenv" / "bin")


def _expand(s: str, agent: str) -> str:
    """Expand placeholders in a string."""
    home = str(Path.home())
    return (
        s.replace("{skenv}", _skenv_bin())
        .replace("{agent}", agent)
        .replace("{home}", home)
        .replace("{logs}", f"{home}/.skcapstone/logs")
        .replace("{skchat}", f"{home}/.skchat")
        .replace("{skcomm}", f"{home}/.skcomm")
    )


def _build_plist(defn: dict, agent: str) -> dict:
    """Build a plist dict from a service definition."""
    label = f"{LABEL_PREFIX}.{defn['suffix']}"
    skenv = _skenv_bin()
    home = str(Path.home())

    plist: dict = {
        "Label": label,
        "ProgramArguments": [_expand(a, agent) for a in defn["args"]],
        "EnvironmentVariables": {
            k: _expand(v, agent) for k, v in defn.get("env", {}).items()
        },
    }

    # Ensure PATH includes skenv and Homebrew
    plist["EnvironmentVariables"]["PATH"] = (
        f"{skenv}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    )

    if defn.get("keep_alive"):
        plist["RunAtLoad"] = True
        plist["KeepAlive"] = {"SuccessfulExit": False}

    if defn.get("throttle"):
        plist["ThrottleInterval"] = defn["throttle"]

    if defn.get("interval"):
        plist["StartInterval"] = defn["interval"]

    if defn.get("calendar"):
        plist["StartCalendarInterval"] = defn["calendar"]

    if defn.get("nice"):
        plist["Nice"] = defn["nice"]

    logs_base = _expand(defn.get("logs", f"{home}/.skcapstone/logs/misc"), agent)
    plist["StandardOutPath"] = f"{logs_base}.stdout.log"
    plist["StandardErrorPath"] = f"{logs_base}.stderr.log"

    return plist


def _label(suffix: str) -> str:
    return f"{LABEL_PREFIX}.{suffix}"


def _launchctl_boot(label: str, plist_path: Path, load: bool = True) -> bool:
    """Load or unload a plist via launchctl."""
    uid = os.getuid()
    domain = f"gui/{uid}"

    if not load:
        # Unload
        r = subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0

    # Load — try modern bootstrap first, fall back to legacy load
    r = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode == 0:
        return True

    r = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


@dataclass
class ServiceStatus:
    """Status of an SK launchd service."""
    installed: bool = False
    loaded: bool = False
    running: bool = False
    pid: int = 0
    exit_code: int = 0
    label: str = ""


def launchd_available() -> bool:
    """Check if launchd is available (i.e., we're on macOS)."""
    return platform.system() == "Darwin"


def list_available_services(agent: str = "sovereign") -> list[dict]:
    """Return all service definitions, marking which are available.

    Args:
        agent: Agent name for path expansion.

    Returns:
        List of dicts with 'suffix', 'label', 'available', 'description'.
    """
    skenv = _skenv_bin()
    services = []

    for defn in _SERVICE_DEFS:
        services.append({
            "suffix": defn["suffix"],
            "label": _label(defn["suffix"]),
            "available": True,
            "description": defn["suffix"].replace("-", " ").title(),
        })

    for defn in _OPTIONAL_DEFS:
        req_bin = defn.get("requires_bin")
        available = bool(shutil.which(req_bin, path=skenv)) if req_bin else True
        services.append({
            "suffix": defn["suffix"],
            "label": _label(defn["suffix"]),
            "available": available,
            "description": defn["suffix"].replace("-", " ").title(),
        })

    return services


def install_service(
    agent_name: str = "sovereign",
    services: Optional[list[str]] = None,
    enable: bool = True,
    start: bool = False,
) -> dict:
    """Install launchd user agents for skcapstone.

    Generates plist files dynamically with the given agent name,
    writes them to ~/Library/LaunchAgents/, and optionally loads them.

    Args:
        agent_name: Agent name (used in SKCAPSTONE_AGENT env var and paths).
        services: List of service suffixes to install. None = all core services.
        enable: Write plists to LaunchAgents (always True for launchd).
        start: Load/start services immediately after installing.

    Returns:
        dict with 'installed', 'loaded', 'services' list.
    """
    _require_macos()
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure log directories exist
    log_dirs = [
        Path.home() / ".skcapstone" / "logs",
        Path.home() / ".skchat",
        Path.home() / ".skcomm",
    ]
    for d in log_dirs:
        d.mkdir(parents=True, exist_ok=True)

    all_defs = _SERVICE_DEFS + _OPTIONAL_DEFS
    if services:
        all_defs = [d for d in all_defs if d["suffix"] in services]

    result = {"installed": False, "loaded": False, "services": []}
    installed_count = 0

    for defn in all_defs:
        suffix = defn["suffix"]
        label = _label(suffix)

        # Skip optional services whose binary isn't installed
        req_bin = defn.get("requires_bin")
        if req_bin and not shutil.which(req_bin, path=_skenv_bin()):
            logger.debug("Skipping %s — %s not found", suffix, req_bin)
            continue

        plist_data = _build_plist(defn, agent_name)
        plist_path = LAUNCH_AGENTS_DIR / f"{label}.plist"

        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)

        svc_result = {"suffix": suffix, "label": label, "installed": True, "loaded": False}
        installed_count += 1

        if start:
            # Unload first if already loaded
            _launchctl_boot(label, plist_path, load=False)
            loaded = _launchctl_boot(label, plist_path, load=True)
            svc_result["loaded"] = loaded

        result["services"].append(svc_result)

    result["installed"] = installed_count > 0
    result["loaded"] = start and all(s["loaded"] for s in result["services"])
    logger.info("Installed %d launchd plist(s) for agent '%s'", installed_count, agent_name)
    return result


def uninstall_service() -> dict:
    """Uninstall all SK launchd user agents.

    Unloads running services and removes plist files.

    Returns:
        dict with 'stopped', 'removed' bools and 'services' list.
    """
    _require_macos()
    result = {"stopped": False, "removed": False, "services": []}
    removed = 0

    for plist_path in sorted(LAUNCH_AGENTS_DIR.glob(f"{LABEL_PREFIX}.*.plist")):
        label = plist_path.stem  # e.g., com.skcapstone.daemon
        _launchctl_boot(label, plist_path, load=False)
        plist_path.unlink(missing_ok=True)
        result["services"].append(label)
        removed += 1

    result["stopped"] = removed > 0
    result["removed"] = removed > 0
    logger.info("Uninstalled %d launchd plist(s)", removed)
    return result


def service_status(suffix: str = "daemon") -> ServiceStatus:
    """Query the status of a specific SK launchd service.

    Args:
        suffix: Service suffix (e.g., 'daemon', 'skcomm-heartbeat').

    Returns:
        ServiceStatus with current state.
    """
    _require_macos()
    label = _label(suffix)
    status = ServiceStatus(label=label)

    plist_path = LAUNCH_AGENTS_DIR / f"{label}.plist"
    status.installed = plist_path.exists()
    if not status.installed:
        return status

    r = subprocess.run(
        ["launchctl", "list"],
        capture_output=True, text=True, timeout=10,
    )
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2] == label:
            status.loaded = True
            try:
                pid = int(parts[0])
                if pid > 0:
                    status.running = True
                    status.pid = pid
            except (ValueError, IndexError):
                pass
            try:
                status.exit_code = int(parts[1])
            except (ValueError, IndexError):
                pass
            break

    return status


def service_logs(suffix: str = "daemon", lines: int = 50) -> str:
    """Get recent logs for an SK launchd service.

    Reads the stdout/stderr log files written by launchd.

    Args:
        suffix: Service suffix.
        lines: Number of tail lines.

    Returns:
        Combined log output.
    """
    _require_macos()
    logs_dir = Path.home() / ".skcapstone" / "logs"
    stdout_log = logs_dir / f"{suffix}.stdout.log"
    stderr_log = logs_dir / f"{suffix}.stderr.log"

    output_parts = []
    for log_path in (stdout_log, stderr_log):
        if log_path.exists():
            try:
                r = subprocess.run(
                    ["tail", "-n", str(lines), str(log_path)],
                    capture_output=True, text=True, timeout=5,
                )
                if r.stdout.strip():
                    output_parts.append(f"--- {log_path.name} ---\n{r.stdout}")
            except Exception as exc:
                logger.warning("Failed to read launchd log %s: %s", log_path, exc)

    return "\n".join(output_parts) if output_parts else f"No logs found in {logs_dir}"
