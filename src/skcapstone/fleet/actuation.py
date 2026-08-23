"""Unit-level actuation verbs for sknoded (spec 5.2, section 6 step 2).

The trustee verb vocabulary (state, start/restart, logs on failure)
applied to systemd --user units and Docker containers, modeled on
skcapstone.systemd's _systemctl pattern. The dispatch functions (state_of,
start, restart, failure_logs) key off a normalized service spec's
"runtime" field so converge.py never branches on runtime itself. Every
verb takes an injectable runner so tests never touch a real unit or
container, and every failure degrades to a safe answer (unknown / False /
"") instead of raising into the converge loop.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run one actuation command, captured, bounded, never check=True."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)


@dataclass(frozen=True)
class UnitState:
    """Observed state of one unit or container.

    Attributes:
        state: active|failed|inactive|activating|missing|unknown.
        pid: Main PID when running, else None.
        since: Start timestamp string as reported, "" when unknown.
    """

    state: str
    pid: int | None
    since: str


_UNKNOWN = UnitState(state="unknown", pid=None, since="")


def systemd_state(unit: str, *, runner: Runner) -> UnitState:
    """Observe one systemd --user unit. Degrades to unknown, never raises."""
    try:
        out = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp",
            ]
        )
    except Exception:
        return _UNKNOWN
    if out.returncode != 0:
        return _UNKNOWN
    props: dict[str, str] = {}
    for line in (out.stdout or "").splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    if props.get("LoadState") == "not-found":
        return UnitState(state="missing", pid=None, since="")
    active = props.get("ActiveState", "unknown")
    if active not in {"active", "failed", "inactive", "activating"}:
        active = "unknown"
    pid: int | None = None
    try:
        pid = int(props.get("MainPID", "0")) or None
    except ValueError:
        pid = None
    return UnitState(state=active, pid=pid, since=props.get("ActiveEnterTimestamp", ""))


def _verb(cmd: list[str], runner: Runner) -> bool:
    try:
        return runner(cmd).returncode == 0
    except Exception:
        return False


def systemd_start(unit: str, *, runner: Runner) -> bool:
    """Start a unit. True on rc=0, False on failure (never raises)."""
    return _verb(["systemctl", "--user", "start", unit], runner)


def systemd_restart(unit: str, *, runner: Runner) -> bool:
    """Restart a unit. True on rc=0, False on failure (never raises)."""
    return _verb(["systemctl", "--user", "restart", unit], runner)


def systemd_logs(unit: str, lines: int = 30, *, runner: Runner) -> str:
    """Tail the unit's journal (logs-on-failure verb). "" when unavailable."""
    try:
        out = runner(["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager"])
    except Exception:
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def docker_state(container: str, *, runner: Runner) -> UnitState:
    """Observe one container. Degrades to unknown, never raises."""
    try:
        out = runner(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}|{{.State.Pid}}|{{.State.StartedAt}}",
                container,
            ]
        )
    except Exception:
        return _UNKNOWN
    if out.returncode != 0:
        if "No such object" in (out.stderr or ""):
            return UnitState(state="missing", pid=None, since="")
        return _UNKNOWN
    parts = (out.stdout or "").strip().split("|")
    if len(parts) != 3:
        return _UNKNOWN
    status, pid_raw, since = parts
    mapping = {
        "running": "active",
        "restarting": "activating",
        "created": "inactive",
        "paused": "inactive",
        "exited": "failed",
        "dead": "failed",
    }
    try:
        pid = int(pid_raw) or None
    except ValueError:
        pid = None
    return UnitState(state=mapping.get(status, "unknown"), pid=pid, since=since)


def docker_start(container: str, *, runner: Runner) -> bool:
    """Start a container. True on rc=0 (never raises)."""
    return _verb(["docker", "start", container], runner)


def docker_restart(container: str, *, runner: Runner) -> bool:
    """Restart a container. True on rc=0 (never raises)."""
    return _verb(["docker", "restart", container], runner)


def compose_up(file: str, service: str, *, runner: Runner) -> bool:
    """Bring one compose service up detached. True on rc=0 (never raises)."""
    return _verb(["docker", "compose", "-f", file, "up", "-d", service], runner)


def docker_logs(container: str, lines: int = 30, *, runner: Runner) -> str:
    """Tail container logs (logs-on-failure verb). "" when unavailable."""
    try:
        out = runner(["docker", "logs", "--tail", str(lines), container])
    except Exception:
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def state_of(spec: dict, *, runner: Runner) -> UnitState:
    """Observe a normalized service spec's unit, whatever its runtime."""
    if spec["runtime"] == "docker":
        return docker_state(spec["unit"], runner=runner)
    return systemd_state(spec["unit"], runner=runner)


def start(spec: dict, *, runner: Runner) -> bool:
    """Start per runtime; compose-backed docker services use compose up."""
    if spec["runtime"] == "docker":
        if spec.get("compose"):
            return compose_up(spec["compose"]["file"], spec["compose"]["service"], runner=runner)
        return docker_start(spec["unit"], runner=runner)
    return systemd_start(spec["unit"], runner=runner)


def restart(spec: dict, *, runner: Runner) -> bool:
    """Restart per runtime."""
    if spec["runtime"] == "docker":
        return docker_restart(spec["unit"], runner=runner)
    return systemd_restart(spec["unit"], runner=runner)


def failure_logs(spec: dict, *, runner: Runner) -> str:
    """Logs-on-failure per runtime."""
    if spec["runtime"] == "docker":
        return docker_logs(spec["unit"], runner=runner)
    return systemd_logs(spec["unit"], runner=runner)
