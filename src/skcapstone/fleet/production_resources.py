"""Memory admission based on live node resources and actual worker cgroups."""

import re
import subprocess
from pathlib import Path

from .physical_memory import constrain_meminfo


def successful_terminal_state(state):
    """Recognize exact retained-success metadata without interpreting unknown memory."""
    group, tasks = state.get("ControlGroup"), state.get("TasksCurrent")
    return bool(
        state.get("LoadState") == "loaded"
        and (state.get("ActiveState"), state.get("SubState")) == ("active", "exited")
        and re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))
        and state.get("MainPID") == state.get("ControlPID") == "0"
        and re.fullmatch(r"[1-9][0-9]*", state.get("ExecMainPID", ""))
        and state.get("ExecMainCode") == "1"
        and state.get("ExecMainStatus") == "0"
        and state.get("Result") == "success"
        and (
            (group == "" and tasks == "[not set]")
            or (
                isinstance(group, str)
                and group.startswith("/")
                and ".." not in Path(group).parts
                and Path(group).name == state.get("Id")
                and tasks == "0"
            )
        )
    )


def successful_terminal_absent(state):
    """Prove retained success has no process or populated owned cgroup."""
    if not successful_terminal_state(state):
        return False
    if Path("/proc", state["ExecMainPID"]).exists():
        return False
    group = state["ControlGroup"]
    if group == "":
        return True
    cgroup = Path("/sys/fs/cgroup") / group.lstrip("/")
    return not cgroup.exists() or (
        (cgroup / "cgroup.events").is_file()
        and "populated 0" in (cgroup / "cgroup.events").read_text().splitlines()
    )


def active_resource_units(home=None):
    """Include native builders/tests and Pi workers in actual quota reservations."""
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--state=active,activating,deactivating",
            "--no-legend",
            "--plain",
            "skfleet-worker-*.service",
            "skfleet-builder-*.service",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    units = {
        line.split()[0]: {"unit": line.split()[0]}
        for line in result.stdout.splitlines()
        if line.strip()
    }
    # A durable launch intent reserves memory even before systemd acknowledges it.
    from .production_tests import read_json

    home = Path(home) if home is not None else Path.home() / ".skcapstone"
    for path in (home / "fleet/test-runs").glob("*/launch.json"):
        if (path.parent / "terminal.json").exists():
            continue
        launch = read_json(path)
        unit = launch["unit"]
        units.setdefault(unit, {"unit": unit})["reserved_memory_max"] = launch["production"][
            "resources"
        ]["memory_max_bytes"]
    return list(units.values())


def available_worker_memory(meminfo, units, *, memory_floor_bytes=0):
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
    headroom = max(512 * 1024**2, memory["MemTotal:"] // 10, memory_floor_bytes)
    return max(0, memory["MemAvailable:"] - reserved - headroom)


def local_worker_admission(policy, host, worker_units, *, runner=subprocess.run):
    """Read fresh resources before claiming, without changing a node or worker."""
    try:
        meminfo = constrain_meminfo(Path("/proc/meminfo").read_text(), runner=runner)
        units = []
        names = [row["unit"] for row in worker_units]
        reservations = {
            row["unit"]: row["reserved_memory_max"]
            for row in worker_units
            if "reserved_memory_max" in row
        }
        if names:
            query = [
                "systemctl",
                "--user",
                "show",
                *names,
                "--property=Id,LoadState,ActiveState,SubState,InvocationID,MemoryMax,MemoryCurrent,"
                "MainPID,ControlPID,ExecMainPID,ExecMainCode,ExecMainStatus,Result,ControlGroup,TasksCurrent",
            ]
            result = runner(
                query,
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
                if (row.get("ActiveState"), row.get("SubState")) == ("active", "exited"):
                    if not successful_terminal_absent(row):
                        raise ValueError("retained terminal resource custody is uncertain")
                    repeat = runner(query, capture_output=True, text=True, check=False, timeout=5)
                    repeated = [
                        dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                        for block in repeat.stdout.strip().split("\n\n")
                    ]
                    exact = [item for item in repeated if item.get("Id") == row["Id"]]
                    if repeat.returncode or exact != [row]:
                        raise ValueError("retained terminal invocation changed")
                    if row["Id"] in reservations:
                        units.append({"MemoryMax": reservations[row["Id"]], "MemoryCurrent": 0})
                elif row.get("ActiveState") in {"active", "activating"}:
                    units.append(row)
                elif (
                    row.get("ActiveState") in {"inactive", "failed"} and row["Id"] in reservations
                ):
                    units.append({"MemoryMax": reservations[row["Id"]], "MemoryCurrent": 0})
                elif row.get("ActiveState") not in {"inactive", "failed"}:
                    raise ValueError("worker resource custody is uncertain")
        available = available_worker_memory(
            meminfo,
            units,
            memory_floor_bytes=policy.get("node_admission", {})
            .get(host, {})
            .get("memory_floor_bytes", 0),
        )
        required = policy["node_quotas"][host]["memory_max_bytes"]
        return available >= required, "memory_available=%d required=%d" % (available, required)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False, "node-resource-evidence-unavailable"
