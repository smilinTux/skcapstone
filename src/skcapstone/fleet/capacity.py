"""Node capacity probe.

Reuses skharness.autocode.autoscale as a library (the single source of
capacity math fleet-wide, spec section 10). A same-shape fallback covers a
fresh box that does not have skharness installed yet (bootstrap, spec 9).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _fallback_resources() -> dict:
    """Mirror autoscale.resources() keys without importing skharness."""
    ram_gb = 8.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                ram_gb = int(line.split()[1]) / 2**20
                break
    except OSError:
        pass
    try:
        disk_gb = shutil.disk_usage(Path.home()).free / 2**30
    except OSError:
        disk_gb = 20.0
    return {
        "cores": os.cpu_count() or 2,
        "ram_gb": round(ram_gb, 1),
        "disk_gb": round(disk_gb, 1),
    }


def _gpu_info() -> dict | None:
    """GPU name and VRAM via nvidia-smi, or None when absent."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    first = out.stdout.strip().splitlines()[0]
    try:
        name, mem = (part.strip() for part in first.split(",", 1))
        return {"name": name, "vram_gb": round(float(mem) / 1024, 1)}
    except ValueError:
        return None


RESERVE_CORES = 1
RESERVE_RAM_GB = 1.0
RESERVE_DISK_GB = 5.0


def allocatable(capacity: dict) -> dict:
    """Schedulable headroom: capacity minus fixed host reserves (spec 5.1).

    Mirrors the autoscale discipline (leave the host a core and some RAM)
    so the fleet scheduler and the local worker pool agree on what is spare.
    """
    return {
        "cores": max(1, int(capacity.get("cores", 1)) - RESERVE_CORES),
        "ram_gb": round(max(0.0, float(capacity.get("ram_gb", 0.0)) - RESERVE_RAM_GB), 1),
        "disk_gb": round(max(0.0, float(capacity.get("disk_gb", 0.0)) - RESERVE_DISK_GB), 1),
    }


def node_capacity() -> dict:
    """Current host capacity for the node.json self-report."""
    try:
        from skharness.autocode.autoscale import resources

        base = resources()
    except Exception:
        base = _fallback_resources()
    gpu = _gpu_info()
    return {
        "cores": base["cores"],
        "ram_gb": base["ram_gb"],
        "disk_gb": base["disk_gb"],
        "gpu": gpu["name"] if gpu else None,
        "vram_gb": gpu["vram_gb"] if gpu else None,
    }
