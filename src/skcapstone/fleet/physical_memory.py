"""Bound WSL worker memory by fresh physical Windows host headroom."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_COMMAND = (
    "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
    "$m=Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 3;"
    "@{total_kb=[int64]$m.TotalVisibleMemorySize;"
    "available_kb=[int64]$m.FreePhysicalMemory} | ConvertTo-Json -Compress"
)


def physical_headroom_kb(
    *, runner=subprocess.run, kernel_release: str | None = None
) -> int | None:
    """Return Windows free RAM minus its reserve, or None on native Linux.

    A detected WSL host must supply fresh valid physical evidence. Missing
    interoperability or a failed query raises instead of advertising guest RAM.
    """
    if kernel_release is None:
        kernel_release = Path("/proc/sys/kernel/osrelease").read_text()
    if "microsoft" not in kernel_release.lower():
        return None
    result = runner(
        [
            _POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(_COMMAND.encode("utf-16le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("physical Windows memory evidence unavailable")
    try:
        data = json.loads(result.stdout)
        total, available = data["total_kb"], data["available_kb"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("physical Windows memory evidence malformed") from exc
    if (
        type(total) is not int
        or type(available) is not int
        or total <= 0
        or not 0 <= available <= total
    ):
        raise ValueError("physical Windows memory values invalid")
    reserve = max(512 * 1024, total // 10)
    return max(0, available - reserve)


def constrain_meminfo(
    text: str, *, runner=subprocess.run, kernel_release: str | None = None
) -> str:
    """Clamp guest MemAvailable; preserve existing Linux swap/reserve checks."""
    physical = physical_headroom_kb(runner=runner, kernel_release=kernel_release)
    if physical is None:
        return text
    lines = text.splitlines()
    matches = [i for i, line in enumerate(lines) if line.startswith("MemAvailable:")]
    if len(matches) != 1:
        raise ValueError("guest MemAvailable evidence missing or duplicate")
    index = matches[0]
    fields = lines[index].split()
    if len(fields) != 3 or fields[2] != "kB":
        raise ValueError("guest MemAvailable evidence malformed")
    available = int(fields[1])
    if available < 0:
        raise ValueError("guest MemAvailable evidence invalid")
    lines[index] = f"MemAvailable: {min(available, physical)} kB"
    return "\n".join(lines) + "\n"
