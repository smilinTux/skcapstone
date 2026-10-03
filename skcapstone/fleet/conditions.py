"""Node condition derivation (spec section 4 conventions).

Reuses skcapstone.doctor as a library for the sync-conflict probe (spec
3.4: a conflict file under the fleet tree is an ownership bug).
"""

from __future__ import annotations

from pathlib import Path

RAM_PRESSURE_GB = 2.0
DISK_PRESSURE_GB = 5.0


def _cond(type: str, active: bool, reason: str, message: str, now_iso: str) -> dict:
    return {
        "type": type,
        "status": "True" if active else "False",
        "reason": reason,
        "message": message,
        "lastTransition": now_iso,
    }


def node_conditions(capacity: dict, fleet_root: Path, now_iso: str) -> list[dict]:
    """Derive this node's conditions from a capacity snapshot."""
    from ..doctor import _check_sync_conflicts

    conds = [
        _cond("Ready", True, "SelfReport", "sknoded self-report alive", now_iso),
        _cond(
            "MemoryPressure",
            float(capacity.get("ram_gb", 0.0)) < RAM_PRESSURE_GB,
            "FreeRam",
            f"{capacity.get('ram_gb')}GB available",
            now_iso,
        ),
        _cond(
            "DiskPressure",
            float(capacity.get("disk_gb", 0.0)) < DISK_PRESSURE_GB,
            "FreeDisk",
            f"{capacity.get('disk_gb')}GB free",
            now_iso,
        ),
    ]
    check = _check_sync_conflicts(fleet_root)[0]
    conds.append(_cond("SyncConflict", not check.passed, "DoctorProbe", check.detail, now_iso))
    if capacity.get("gpu"):
        conds.append(_cond("GPUAvailable", True, "NvidiaSmi", str(capacity["gpu"]), now_iso))
    return conds


def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """True when a TCP connect to host:port succeeds (health probe)."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def probe_conditions(probes: list[dict], now_iso: str) -> list[dict]:
    """Operator-declared TCP probes as node conditions (spec 5.2 skmem-pg rule).

    This is the health-condition-only surface for workloads that stay OUT
    of fleet management (skmem-pg is local-per-node by incident decision):
    visibility, never actuation.
    """
    out: list[dict] = []
    for probe in probes:
        try:
            port = int(probe["port"])
            cond_type = str(probe["condition"])
        except (KeyError, TypeError, ValueError):
            continue
        ok = tcp_open(port)
        out.append(
            _cond(
                cond_type,
                ok,
                "TcpProbe",
                f"{probe.get('name', cond_type)} port {port} " f"{'open' if ok else 'closed'}",
                now_iso,
            )
        )
    return out


def merge_transitions(new: list[dict], old: list[dict]) -> list[dict]:
    """Keep old lastTransition when a condition's status is unchanged.

    Without this, every pass would stamp fresh timestamps and defeat the
    write-on-change discipline (R2).
    """
    prev = {c.get("type"): c for c in old}
    out = []
    for cond in new:
        before = prev.get(cond.get("type"))
        if before is not None and before.get("status") == cond.get("status"):
            cond = dict(cond, lastTransition=before.get("lastTransition"))
        out.append(cond)
    return out
