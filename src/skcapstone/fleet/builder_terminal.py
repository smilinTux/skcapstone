"""Durable exact termination custody for builder continuation only."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import builder_retire as custody
from . import production_builder, source_bundle

SCHEMA = "skfleet.builder-terminal/v1"
RESOURCE_MESSAGE = "ae8f7b866b0347b9af31fe1c80b127c0"
START_MESSAGE = "39f53479d3a045ac8e11786248231fbf"
QUALIFICATION = {
    "version": "255.4-1ubuntu8.17",
    "unit_c_sha256": "424d429e714bb8c79d06c9da804b81696eea6144b78b03ee36abb394e3177093",
    "orig_sha256": "96e75bd08c57ad401677456fb88ef54a9f05bb1695693013bc6ecce839640fd5",
    "debian_sha256": "4695ff34f83b1f7e6e02bf3cfac2e2a44ac76b6cfc5a38c0081bac6919d547bb",
}


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if result.returncode or len(result.stdout.encode()) > source_bundle.MAX_EVIDENCE:
        raise ValueError("terminal observation unavailable or oversized")
    return result.stdout


def boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("current boot unavailable")
    return value


def binding(status):
    fields = (
        "card_id",
        "request_id",
        "node",
        "owner",
        "claim_revision",
        "attempt",
        "unit",
        "invocation",
        "pid",
        "pid_start_ticks",
        "production",
    )
    try:
        unit = production_builder.unit_name(status, status.get("attempt"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("exact terminal generation unavailable") from exc
    if (
        status.get("unit") != unit
        or not re.fullmatch(r"[0-9a-f]{32}", str(status.get("invocation", "")))
        or type(status.get("pid")) is not int
        or status["pid"] < 2
        or not re.fullmatch(r"[0-9]+", str(status.get("pid_start_ticks", "")))
    ):
        raise ValueError("exact terminal generation unavailable")
    return {key: status.get(key) for key in fields}


def path(home, status):
    binding(status)
    return (
        custody.directory(home, status["card_id"], status["request_id"]).parent.parent
        / "terminations"
        / status["request_id"]
        / (status["invocation"] + ".json")
    )


def snapshot(status):
    binding(status)
    raw = command(
        [
            "systemctl",
            "--user",
            "show",
            status["unit"],
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,MainPID,InvocationID,ExecMainStatus",
        ]
    )
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def absent(status):
    """Denied access is unknown; even a reused PID requires separate diagnosis."""
    binding(status)
    try:
        os.kill(status["pid"], 0)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise ValueError("dispatcher absence unavailable") from exc
    else:
        raise ValueError("dispatcher PID exists")
    cgroup = Path("/sys/fs/cgroup") / (
        f"user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/"
        + status["unit"]
    )
    try:
        cgroup.lstat()
    except FileNotFoundError:
        return
    raise ValueError("exact builder cgroup still exists")


def journal(status, boot):
    raw = command(
        [
            "journalctl",
            "--user",
            "--boot=" + boot,
            "USER_UNIT=" + status["unit"],
            "MESSAGE_ID=" + START_MESSAGE,
            "MESSAGE_ID=" + RESOURCE_MESSAGE,
            "--output=json",
            "--no-pager",
            "-n",
            "65",
        ]
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("collected unit journal is malformed")
    if not rows or len(rows) > 64:
        raise ValueError("bounded terminal journal history unavailable")
    manager = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/init.scope"
    for row in rows:
        if (
            row.get("_BOOT_ID") != boot
            or row.get("_UID") != str(os.getuid())
            or row.get("_EXE") != "/usr/lib/systemd/systemd"
            or row.get("_SYSTEMD_CGROUP") != manager
            or row.get("USER_UNIT") != status["unit"]
            or not re.fullmatch(r"[0-9]+", str(row.get("_PID", "")))
            or not re.fullmatch(r"[0-9a-f]{32}", str(row.get("USER_INVOCATION_ID", "")))
            or not isinstance(row.get("__CURSOR"), str)
            or not row["__CURSOR"]
            or not str(row.get("__MONOTONIC_TIMESTAMP", "")).isdigit()
        ):
            raise ValueError("untrusted or ambiguous terminal journal entry")
    return sorted(rows, key=lambda row: int(row["__MONOTONIC_TIMESTAMP"]))


def history(status, boot):
    rows = journal(status, boot)
    starts = [
        row
        for row in rows
        if row.get("MESSAGE_ID") == START_MESSAGE
        and row.get("JOB_TYPE") == "start"
        and row.get("JOB_RESULT") == "done"
        and row["USER_INVOCATION_ID"] == status["invocation"]
    ]
    if len(starts) != 1:
        raise ValueError("exact journal start unavailable or ambiguous")
    start = int(starts[0]["__MONOTONIC_TIMESTAMP"])
    birth = int(status["pid_start_ticks"]) * 1_000_000 // os.sysconf("SC_CLK_TCK")
    if start < birth or any(
        row["USER_INVOCATION_ID"] != status["invocation"]
        for row in rows
        if int(row["__MONOTONIC_TIMESTAMP"]) >= start
    ):
        raise ValueError("stale start or subsequent invocation")
    return rows, start


def loaded(values, status):
    return (
        values.get("LoadState") == "loaded"
        and values.get("InvocationID") == status["invocation"]
        and values.get("ActiveState") in {"inactive", "failed"}
        and values.get("MainPID") == "0"
    )


def persist(home, status, receipt):
    target = path(home, status)
    custody.private_directory(target.parent)
    source_bundle._once(target, custody.encoded(receipt))


def observe(home, status):
    """Record the first native loaded terminal observation without claiming success."""
    target = path(home, status)
    if target.exists():
        return
    values = snapshot(status)
    if not loaded(values, status):
        raise ValueError("exact loaded terminal unit required")
    absent(status)
    receipt = {
        "schema": SCHEMA,
        "binding": binding(status),
        "boot": boot_id(),
        "kind": "native-loaded",
        "snapshot": values,
        "monotonic_usec": time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000,
    }
    persist(home, status, receipt)


def prove(home, status, *, apply=False):
    """Keep the existing loaded-unit guard; only collected units use durable custody."""
    try:
        custody.prove_dead(status)
        return
    except ValueError:
        pass
    values = snapshot(status)
    if (
        values.get("LoadState") != "not-found"
        or values.get("InvocationID")
        or values.get("ActiveState") != "inactive"
        or values.get("MainPID") != "0"
    ):
        raise ValueError("collected exact unit required")
    absent(status)
    boot = boot_id()
    rows, start = history(status, boot)
    target = path(home, status)
    if target.exists():
        if (
            target.parent.resolve() != target.parent
            or target.parent.stat().st_mode & 0o777 != 0o700
            or target.stat().st_mode & 0o777 != 0o600
        ):
            raise ValueError("private terminal receipt required")
        receipt = json.loads(source_bundle._read(target, source_bundle.MAX_EVIDENCE))
        if (
            receipt.get("schema") != SCHEMA
            or receipt.get("binding") != binding(status)
            or receipt.get("boot") != boot
        ):
            raise ValueError("terminal receipt generation changed")
        if receipt.get("kind") == "native-loaded":
            if (
                not loaded(receipt.get("snapshot", {}), status)
                or type(receipt.get("monotonic_usec")) is not int
                or not start <= receipt["monotonic_usec"] <= time.monotonic_ns() // 1000
            ):
                raise ValueError("invalid native terminal receipt")
            return
    else:
        receipt = None
    version = command(["dpkg-query", "-W", "-f=${Version}", "systemd"]).strip()
    if version != QUALIFICATION["version"]:
        raise ValueError("journal terminal semantics not qualified for installed systemd")
    terminals = [
        row
        for row in rows
        if row.get("MESSAGE_ID") == RESOURCE_MESSAGE
        and row.get("CODE_FILE") == "src/core/unit.c"
        and row.get("CODE_FUNC") == "unit_log_resources"
        and row["USER_INVOCATION_ID"] == status["invocation"]
        and int(row["__MONOTONIC_TIMESTAMP"]) > start
    ]
    if len(terminals) != 1 or terminals[0] != rows[-1]:
        raise ValueError("exact terminal transition unavailable or ambiguous")
    entry = terminals[0]
    reconstructed = {
        "schema": SCHEMA,
        "binding": binding(status),
        "boot": boot,
        "kind": "qualified-journal",
        "entry": entry,
        "entry_sha256": custody.sha(custody.encoded(entry)),
        "qualification": QUALIFICATION,
    }
    if receipt is not None and receipt != reconstructed:
        raise ValueError("terminal journal receipt changed")
    if apply and receipt is None:
        persist(home, status, reconstructed)


def recover_collected(home, status):
    """Recover one lost invocation from the exact collected unit's journal."""
    if status.get("invocation") is not None:
        raise ValueError("recovery requires a missing invocation")
    try:
        unit = production_builder.unit_name(status, status["attempt"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("exact terminal generation unavailable") from exc
    if status.get("unit") != unit:
        raise ValueError("exact terminal generation unavailable")
    raw = command(
        [
            "journalctl",
            "--user",
            "--boot=" + boot_id(),
            "USER_UNIT=" + unit,
            "MESSAGE_ID=" + START_MESSAGE,
            "MESSAGE_ID=" + RESOURCE_MESSAGE,
            "--output=json",
            "--no-pager",
            "-n",
            "65",
        ]
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("collected unit journal is malformed")
    if any(not isinstance(row.get("USER_INVOCATION_ID"), str) for row in rows):
        raise ValueError("collected unit invocation is invalid")
    invocations = {row["USER_INVOCATION_ID"] for row in rows}
    if len(invocations) != 1:
        raise ValueError("collected unit invocation is ambiguous")
    invocation = invocations.pop()
    if not isinstance(invocation, str) or not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ValueError("collected unit invocation is invalid")
    recovered = {**status, "invocation": invocation}
    prove(home, recovered, apply=True)
    return recovered
