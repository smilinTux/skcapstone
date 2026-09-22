"""Run one non-overlapping Atlas, Seraph, and Niobe seat generation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from skcapstone.estate import sovereign_home
from skcapstone.niobe_activation import parse_activation

_ATLAS = "skfleet-atlas.service"
_SERAPH = "skfleet-seraph.service"
_NIOBE_LIVE = "skfleet-niobe-live.service"
_NIOBE_SHADOW = "skfleet-niobe.service"
_GOVERNED_SERVICES = (_ATLAS, _SERAPH, _NIOBE_SHADOW, _NIOBE_LIVE)
_GENERATION_BUDGET_SECONDS = 600


def _recovery_marker(home: Path) -> Path:
    return home / "coordination" / "seat-cycles" / "recovery-required"


def _acquire_generation_lock(home: Path) -> int | None:
    """Acquire the process-lifetime generation lock without waiting."""

    path = home / "coordination" / "seat-cycles" / "generation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _arm_recovery_fence(home: Path) -> None:
    """Durably fence future generations before any governed seat starts."""

    path = _recovery_marker(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, b"recovery-required\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _clear_recovery_fence(home: Path) -> None:
    """Clear the fence only after durable success and exact inactivity proof."""

    path = _recovery_marker(home)
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def select_niobe_service(home: Path) -> str:
    """Select live Niobe only when its existing activation still validates."""

    activation = home / "coordination" / "niobe-activation.json"
    try:
        value = json.loads(activation.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            return _NIOBE_SHADOW
        parse_activation(value, home=home)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _NIOBE_SHADOW
    return _NIOBE_LIVE


def select_niobe_timer(home: Path) -> str:
    """Select the independent Niobe timer matching the activation contract."""

    if select_niobe_service(home) == _NIOBE_LIVE:
        return "skfleet-niobe-live.timer"
    return "skfleet-niobe.timer"


def _append_receipt(home: Path, receipt: dict[str, Any]) -> None:
    """Append and fsync one generation receipt under an advisory file lock."""

    path = home / "coordination" / "seat-cycles" / "orchestrator.health.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _recovery_required(home: Path) -> bool:
    """Return whether the last durable generation receipt aborted unsafe."""

    path = home / "coordination" / "seat-cycles" / "orchestrator.health.jsonl"
    try:
        last = path.read_text(encoding="utf-8").splitlines()[-1]
        receipt = json.loads(last)
    except FileNotFoundError:
        return False
    except (OSError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(receipt, dict):
        return True
    if receipt.get("schema") != "skfleet.seat-cycle-generation/v1":
        return True
    aborted = receipt.get("aborted")
    if not isinstance(aborted, bool):
        return True
    if aborted:
        return True
    if type(receipt.get("failures")) is not int:
        return True
    timestamps = []
    for timestamp in ("started_at", "finished_at"):
        value = receipt.get(timestamp)
        if not isinstance(value, str):
            return True
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return True
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return True
        timestamps.append(parsed)
    if timestamps[1] < timestamps[0]:
        return True
    seats = receipt.get("seats")
    if not isinstance(seats, list):
        return True
    expected_units = [_ATLAS, _SERAPH, select_niobe_service(home)]
    if [seat.get("unit") for seat in seats if isinstance(seat, dict)] != expected_units:
        return True
    for seat in seats:
        returncode = seat.get("returncode")
        error = seat.get("error")
        cleanup = seat.get("timeout_cleanup_proven")
        if type(returncode) is not int:
            return True
        if error is not None and not isinstance(error, str):
            return True
        if cleanup is not None and not isinstance(cleanup, bool):
            return True
        if returncode == 0 and (error is not None or cleanup is not None):
            return True
        if returncode != 0 and (not isinstance(error, str) or cleanup is not True):
            return True
    failures = sum(seat["returncode"] != 0 for seat in seats)
    return receipt["failures"] != failures


def _prove_recovery_inactive(runner: Callable[..., Any], *, cancel_jobs: bool = False) -> bool:
    """Fail closed unless every governed service is exactly inactive."""

    for unit in _GOVERNED_SERVICES:
        try:
            if cancel_jobs:
                stopped = runner(
                    ["systemctl", "--user", "stop", unit],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if int(getattr(stopped, "returncode", 1)) != 0:
                    return False
            shown = runner(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState,ActiveState,Job",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        state = dict(
            line.split("=", 1)
            for line in str(getattr(shown, "stdout", "")).splitlines()
            if "=" in line
        )
        known_absent = (
            state.get("LoadState") == "not-found" and state.get("ActiveState") == "inactive"
        )
        if int(getattr(shown, "returncode", 1)) != 0 and not known_absent:
            return False
        if state.get("LoadState") not in {"loaded", "not-found"}:
            return False
        if state.get("ActiveState") != "inactive":
            return False
        if state.get("Job") not in {"", "0", "n/a"}:
            return False
    return True


def _stop_and_prove_inactive(unit: str, runner: Callable[..., Any]) -> bool:
    """Stop a timed-out seat and prove its service reached exact inactivity."""

    try:
        stopped = runner(
            ["systemctl", "--user", "stop", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        shown = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,Job",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    state = dict(
        line.split("=", 1)
        for line in str(getattr(shown, "stdout", "")).splitlines()
        if "=" in line
    )
    return (
        int(getattr(stopped, "returncode", 1)) == 0
        and int(getattr(shown, "returncode", 1)) == 0
        and state.get("LoadState") == "loaded"
        and state.get("ActiveState") == "inactive"
        and state.get("Job") in {"", "0", "n/a"}
    )


def _run_generation_locked(
    home: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run one generation while the caller holds the process-lifetime lock."""

    started_at = datetime.now(timezone.utc).isoformat()
    deadline = clock() + _GENERATION_BUDGET_SECONDS
    recovery_required = _recovery_marker(home).exists() or _recovery_required(home)
    _arm_recovery_fence(home)
    if recovery_required and not _prove_recovery_inactive(runner, cancel_jobs=True):
        receipt = {
            "schema": "skfleet.seat-cycle-generation/v1",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "seats": [],
            "failures": 1,
            "aborted": True,
            "recovery": "governed_service_inactivity_unproven",
        }
        _append_receipt(home, receipt)
        return receipt
    units = (_ATLAS, _SERAPH, select_niobe_service(home))
    seats: list[dict[str, Any]] = []
    aborted = False
    abort_reason = None
    for unit in units:
        if clock() >= deadline:
            aborted = True
            abort_reason = "generation_budget_exhausted"
            break
        try:
            completed = runner(
                ["systemctl", "--user", "start", "--wait", unit],
                check=False,
                timeout=310,
            )
            returncode = int(completed.returncode)
            error = None if returncode == 0 else "systemctl_start_failed"
            cleanup_ok = None if returncode == 0 else _stop_and_prove_inactive(unit, runner)
        except subprocess.TimeoutExpired:
            returncode = 124
            error = "systemctl_wait_timeout"
            cleanup_ok = _stop_and_prove_inactive(unit, runner)
        except OSError as exc:
            returncode = 126
            error = type(exc).__name__
            cleanup_ok = _stop_and_prove_inactive(unit, runner)
        seats.append(
            {
                "unit": unit,
                "returncode": returncode,
                "error": error,
                "timeout_cleanup_proven": cleanup_ok,
            }
        )
        if returncode != 0:
            aborted = True
        if cleanup_ok is False:
            break
    receipt = {
        "schema": "skfleet.seat-cycle-generation/v1",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seats": seats,
        "failures": sum(seat["returncode"] != 0 for seat in seats),
        "aborted": aborted,
    }
    if abort_reason is not None:
        receipt["recovery"] = abort_reason
    _append_receipt(home, receipt)
    if not aborted:
        _clear_recovery_fence(home)
    return receipt


def run_generation(
    home: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run one serialized generation or fail closed on live contention."""

    lock_fd = _acquire_generation_lock(home)
    if lock_fd is None:
        return {
            "schema": "skfleet.seat-cycle-generation/v1",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "seats": [],
            "failures": 1,
            "aborted": True,
            "recovery": "generation_lock_contended",
        }
    try:
        return _run_generation_locked(home, runner=runner, clock=clock)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main() -> int:
    """Run one generation from the systemd entrypoint."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=sovereign_home())
    args = parser.parse_args()
    result = run_generation(args.home)
    return 1 if result["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
