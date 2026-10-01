"""Memory admission based on live node resources and actual worker cgroups."""

import subprocess
from pathlib import Path


def available_worker_memory(meminfo, units):
    """Reserve unfinished worker allowances without counting their memory twice."""
    memory = {}
    for line in meminfo.splitlines():
        fields = line.split()
        if fields and fields[0] in {"MemTotal:", "MemAvailable:"}:
            if len(fields) != 3 or fields[2] != "kB":
                raise ValueError("node memory evidence is malformed")
            memory[fields[0]] = int(fields[1]) * 1024
    if memory.get("MemTotal:", 0) <= 0 or memory.get("MemAvailable:", -1) < 0:
        raise ValueError("node memory evidence is missing")
    reserved = 0
    for unit in units:
        maximum = int(unit["MemoryMax"])
        current = int(unit["MemoryCurrent"])
        if maximum <= 0 or current < 0 or maximum >= 2**63:
            raise ValueError("worker memory reservation is unknown")
        reserved += max(0, maximum - current)
    headroom = max(512 * 1024**2, memory["MemTotal:"] // 10)
    return max(0, memory["MemAvailable:"] - reserved - headroom)


def local_worker_admission(policy, host, worker_units, *, runner=subprocess.run):
    """Read fresh resources before claiming, without changing a node or worker."""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        units = []
        names = [row["unit"] for row in worker_units]
        if names:
            result = runner(
                [
                    "systemctl",
                    "--user",
                    "show",
                    *names,
                    "--property=Id,ActiveState,MemoryMax,MemoryCurrent",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if result.returncode:
                raise ValueError("worker resource query failed")
            rows = [
                dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                for block in result.stdout.strip().split("\n\n")
            ]
            if {row.get("Id") for row in rows} != set(names):
                raise ValueError("worker resource response incomplete")
            for row in rows:
                if row.get("ActiveState") in {"active", "activating"}:
                    units.append(row)
                elif row.get("ActiveState") not in {"inactive", "failed"}:
                    raise ValueError("worker resource custody is uncertain")
        available = available_worker_memory(meminfo, units)
        required = policy["node_quotas"][host]["memory_max_bytes"]
        return available >= required, "memory_available=%d required=%d" % (available, required)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False, "node-resource-evidence-unavailable"
