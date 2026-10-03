"""Host-local proof for one dedicated builder execution generation."""

from __future__ import annotations

import hashlib
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ExecutionObservation:
    """Three-state execution observation; unknown never authorizes release."""

    alive: bool | None
    exit_code: int | None = None
    cgroup: str | None = None


def local_identity() -> tuple[str, str]:
    """Return the actual observer host and kernel boot identity."""
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not boot:
        raise ValueError("kernel boot identity is unavailable")
    return socket.gethostname(), boot


def unit_name(card_id: str, request_id: str, claim_revision: str) -> str:
    """Bind a non-reused service name to the exact source execution attempt."""
    if not re.fullmatch(r"[0-9a-f]{8}", card_id) or not request_id or not claim_revision:
        raise ValueError("builder execution identity is incomplete")
    digest = hashlib.sha256(f"{request_id}\0{claim_revision}".encode()).hexdigest()[:24]
    return f"skfleet-builder-{card_id}-{digest}.service"


def identity(request: dict, claim_revision: str) -> dict[str, str]:
    """Construct durable execution attribution before process creation."""
    host, boot = local_identity()
    return {
        "execution_host": host,
        "execution_boot_id": boot,
        "execution_unit": unit_name(request["card_id"], request["request_id"], claim_revision),
    }


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Read local service authority without a shell or unbounded wait."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)


def observe(
    status: dict,
    *,
    runner: Callable[[list[str]], object] = _run,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> ExecutionObservation:
    """Require local unit authority and complete cgroup extinction for death.

    A retained transient unit makes a completed launch discoverable even if the
    daemon crashed before writing its running status. Legacy PID-only attempts,
    unknown services, denied reads and prior boots retain custody.
    """
    unknown = ExecutionObservation(None)
    try:
        host, boot = local_identity()
        unit = unit_name(status["card_id"], status["request_id"], status["claim_revision"])
        if (
            status.get("execution_host"),
            status.get("execution_boot_id"),
            status.get("execution_unit"),
        ) != (host, boot, unit):
            return unknown
        result = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=Id,LoadState,ActiveState,SubState,MainPID,ControlPID,"
                "ExecMainPID,ExecMainCode,ExecMainStatus,ControlGroup,KillMode,Restart,RemainAfterExit",
            ]
        )
        if result.returncode:
            return unknown
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if any(
            fields.get(key) != value
            for key, value in {
                "Id": unit,
                "LoadState": "loaded",
                "KillMode": "control-group",
                "Restart": "no",
                "RemainAfterExit": "yes",
            }.items()
        ):
            return unknown
        if int(fields["MainPID"]) > 0 or int(fields["ControlPID"]) > 0:
            return ExecutionObservation(True, cgroup=fields.get("ControlGroup") or None)
        if not (
            fields["ActiveState"] in {"inactive", "failed"}
            or (fields["ActiveState"], fields["SubState"]) == ("active", "exited")
        ):
            return unknown
        # A retained but never-executed unit is not evidence of a dead attempt.
        if int(fields["ExecMainPID"]) <= 0 or int(fields["ExecMainCode"]) not in {1, 2, 3}:
            return unknown
        group = fields.get("ControlGroup") or status.get("execution_cgroup")
        if not isinstance(group, str) or not group.startswith("/"):
            return unknown
        relative = Path(group).relative_to("/")
        if ".." in relative.parts or relative.name != unit:
            return unknown
        trusted_root = cgroup_root.resolve(strict=True)
        directory = (trusted_root / relative).resolve(strict=False)
        if not directory.is_relative_to(trusted_root):
            return unknown
        try:
            events = dict(
                line.split()
                for line in (directory / "cgroup.events").read_text(encoding="ascii").splitlines()
            )
        except FileNotFoundError:
            # Only a previously recorded group removed by systemd, with the
            # current service reporting no group, establishes kernel absence.
            if fields.get("ControlGroup") or directory.exists():
                return unknown
            events = {"populated": "0"}
        if events.get("populated") == "1":
            return ExecutionObservation(True, cgroup=group)
        if events.get("populated") != "0":
            return unknown
        code = int(fields["ExecMainStatus"])
        if int(fields["ExecMainCode"]) != 1:
            code += 128
        return ExecutionObservation(False, code, group)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return unknown


def launch(command: list[str], workspace: Path, unit: str) -> None:
    """Start one retained systemd service; uncertainty is resolved by its unit."""
    result = subprocess.run(
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--service-type=exec",
            "--unit",
            unit,
            "--property=KillMode=control-group",
            "--property=RemainAfterExit=yes",
            "--property=Restart=no",
            "--working-directory",
            str(workspace),
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("builder managed launch did not confirm service creation")
