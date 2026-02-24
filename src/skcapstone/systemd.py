"""Systemd service management for the SKCapstone daemon.

Installs, manages, and queries the skcapstone systemd user service.
Uses user-level systemd (systemctl --user) so no root is needed.

The service unit runs `skcapstone daemon start --foreground` and
restarts on failure. Security hardening restricts filesystem access
to only the agent's data directories.

Usage:
    from skcapstone.systemd import install_service, service_status
    install_service()            # copies unit + enables + starts
    status = service_status()    # check if running
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.systemd")

SERVICE_NAME = "skcapstone.service"
SOCKET_NAME = "skcapstone-api.socket"
HEARTBEAT_SERVICE = "skcomm-heartbeat.service"
HEARTBEAT_TIMER = "skcomm-heartbeat.timer"
QUEUE_DRAIN_SERVICE = "skcomm-queue-drain.service"
QUEUE_DRAIN_TIMER = "skcomm-queue-drain.timer"

ALL_UNITS = [
    SERVICE_NAME,
    SOCKET_NAME,
    HEARTBEAT_SERVICE,
    HEARTBEAT_TIMER,
    QUEUE_DRAIN_SERVICE,
    QUEUE_DRAIN_TIMER,
]

TIMER_UNITS = [HEARTBEAT_TIMER, QUEUE_DRAIN_TIMER]

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"

BUNDLED_DIR = Path(__file__).parent.parent.parent / "systemd"


@dataclass
class ServiceStatus:
    """Status of the skcapstone systemd service.

    Attributes:
        installed: Whether the unit file exists.
        enabled: Whether the service is enabled at boot.
        active: Whether the service is currently running.
        pid: PID of the running service (0 if not running).
        uptime: How long the service has been running.
        memory: Memory usage string from systemd.
        exit_code: Last exit code if the service stopped.
    """

    installed: bool = False
    enabled: bool = False
    active: bool = False
    pid: int = 0
    uptime: str = ""
    memory: str = ""
    exit_code: str = ""


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run a command and capture output.

    Args:
        cmd: Command and arguments.
        check: Raise on non-zero exit.

    Returns:
        CompletedProcess with stdout/stderr.
    """
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, check=check,
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run a systemctl --user command.

    Args:
        *args: Arguments to pass to systemctl.

    Returns:
        CompletedProcess result.
    """
    return _run(["systemctl", "--user", *args])


def systemd_available() -> bool:
    """Check if systemd user session is available.

    Returns:
        bool: True if systemctl --user works.
    """
    result = _run(["systemctl", "--user", "--version"])
    return result.returncode == 0


def install_service(
    unit_dir: Optional[Path] = None,
    source_dir: Optional[Path] = None,
    enable: bool = True,
    start: bool = True,
) -> dict:
    """Install the skcapstone systemd user service.

    Copies unit files to ~/.config/systemd/user/, reloads the
    daemon, and optionally enables + starts the service.

    Args:
        unit_dir: Target directory for unit files.
        source_dir: Directory containing the .service/.socket files.
        enable: Whether to enable the service at login.
        start: Whether to start the service immediately.

    Returns:
        dict: Result with 'installed', 'enabled', 'started' bools.
    """
    target = unit_dir or SYSTEMD_USER_DIR
    source = source_dir or BUNDLED_DIR

    target.mkdir(parents=True, exist_ok=True)

    result = {"installed": False, "enabled": False, "started": False, "timers_enabled": False}

    service_src = source / SERVICE_NAME
    if not service_src.exists():
        logger.error("Service unit not found: %s", service_src)
        return result

    copied = 0
    for unit_name in ALL_UNITS:
        src = source / unit_name
        if src.exists():
            shutil.copy2(src, target / unit_name)
            copied += 1

    _systemctl("daemon-reload")
    result["installed"] = True
    logger.info("Installed %d unit file(s) to %s", copied, target)

    if enable:
        r = _systemctl("enable", SERVICE_NAME)
        result["enabled"] = r.returncode == 0

        timers_ok = True
        for timer in TIMER_UNITS:
            if (target / timer).exists():
                r = _systemctl("enable", timer)
                if r.returncode != 0:
                    timers_ok = False
        result["timers_enabled"] = timers_ok

    if start:
        r = _systemctl("start", SERVICE_NAME)
        result["started"] = r.returncode == 0

        for timer in TIMER_UNITS:
            if (target / timer).exists():
                _systemctl("start", timer)

    return result


def uninstall_service(unit_dir: Optional[Path] = None) -> dict:
    """Uninstall the skcapstone systemd user service.

    Stops, disables, and removes the unit files.

    Args:
        unit_dir: Directory containing the installed unit files.

    Returns:
        dict: Result with 'stopped', 'disabled', 'removed' bools.
    """
    target = unit_dir or SYSTEMD_USER_DIR
    result = {"stopped": False, "disabled": False, "removed": False}

    for timer in TIMER_UNITS:
        _systemctl("stop", timer)
        _systemctl("disable", timer)
    _systemctl("stop", SERVICE_NAME)
    result["stopped"] = True

    _systemctl("disable", SERVICE_NAME)
    result["disabled"] = True

    for name in ALL_UNITS:
        unit_path = target / name
        if unit_path.exists():
            unit_path.unlink()

    _systemctl("daemon-reload")
    result["removed"] = True
    logger.info("Uninstalled service from %s", target)

    return result


def service_status() -> ServiceStatus:
    """Query the current status of the skcapstone service.

    Returns:
        ServiceStatus: Detailed status information.
    """
    status = ServiceStatus()

    unit_path = SYSTEMD_USER_DIR / SERVICE_NAME
    status.installed = unit_path.exists()

    if not status.installed:
        return status

    r = _systemctl("is-enabled", SERVICE_NAME)
    status.enabled = r.stdout.strip() == "enabled"

    r = _systemctl("is-active", SERVICE_NAME)
    status.active = r.stdout.strip() == "active"

    r = _systemctl("show", SERVICE_NAME,
                    "--property=MainPID,ActiveEnterTimestamp,MemoryCurrent,ExecMainStatus")
    for line in r.stdout.strip().splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "MainPID":
            try:
                status.pid = int(value)
            except ValueError:
                pass
        elif key == "ActiveEnterTimestamp":
            status.uptime = value
        elif key == "MemoryCurrent":
            try:
                mem_bytes = int(value)
                if mem_bytes > 0:
                    status.memory = f"{mem_bytes / 1024 / 1024:.1f} MB"
            except (ValueError, ZeroDivisionError):
                pass
        elif key == "ExecMainStatus":
            status.exit_code = value

    return status


def service_logs(lines: int = 50, follow: bool = False) -> str:
    """Get recent journal logs for the skcapstone service.

    Args:
        lines: Number of recent lines to return.
        follow: If True, returns only the command to run (can't stream).

    Returns:
        str: Log output or the follow command.
    """
    if follow:
        return f"journalctl --user -u {SERVICE_NAME} -f"

    r = _run(["journalctl", "--user", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager"])
    return r.stdout


def restart_service() -> bool:
    """Restart the skcapstone service.

    Returns:
        bool: True if the restart command succeeded.
    """
    r = _systemctl("restart", SERVICE_NAME)
    return r.returncode == 0


def generate_unit_file(
    python_path: Optional[str] = None,
    extra_env: Optional[dict] = None,
) -> str:
    """Generate a customized systemd unit file as a string.

    Useful for systems where the bundled unit needs adjustment.

    Args:
        python_path: Override the Python/skcapstone path.
        extra_env: Additional environment variables.

    Returns:
        str: Complete unit file content.
    """
    exec_cmd = python_path or "skcapstone"
    env_lines = ""
    if extra_env:
        for k, v in extra_env.items():
            env_lines += f"Environment={k}={v}\n"

    return f"""[Unit]
Description=SKCapstone Sovereign Agent Daemon
Documentation=https://github.com/smilinTux/skcapstone
After=network-online.target syncthing.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_cmd} daemon start --foreground
ExecStop={exec_cmd} daemon stop
Restart=on-failure
RestartSec=10
WatchdogSec=120

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.skcapstone %h/.skmemory %h/.capauth %h/.cloud9 %h/.skcomm %h/.skchat
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true

Environment=PYTHONUNBUFFERED=1
{env_lines}
[Install]
WantedBy=default.target
"""
