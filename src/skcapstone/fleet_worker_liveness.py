"""Worker liveness derived exclusively from pi's current session NDJSON file.

Systemd identifies the worker population.  The only activity signal is the
mtime of the current run's pi session file.  In particular, workspace mtimes,
process CPU, logs, and lifecycle state are not liveness signals.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

RUN_SKEW_SECONDS = 120.0
_UNIT = re.compile(r"^skfleet-worker-[^-]+-([0-9a-f]{8})\.service$")


@dataclass(frozen=True)
class WorkerLiveness:
    card_id: str
    unit: str
    session_file: str | None
    session_mtime: float | None
    active_enter_timestamp: float | None
    live: bool


@dataclass(frozen=True)
class FailedWorker:
    unit: str
    card_id: str | None
    state: str = "failed"


def _command(argv: Sequence[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _unit_rows(runner: Callable[[Sequence[str]], str], state: str) -> list[str]:
    # State is applied to list-units itself, so failed units can never become
    # active workers merely because their name matches the worker glob.
    output = runner(("systemctl", "--user", "--state=" + state, "list-units", "skfleet-worker-*", "--no-legend", "--plain"))
    return [line.split()[0] for line in output.splitlines() if line.split()]


def _card_id(unit: str) -> str | None:
    match = _UNIT.fullmatch(unit)
    return match.group(1) if match else None


def active_units(runner: Callable[[Sequence[str]], str] = _command) -> list[str]:
    return [u for u in _unit_rows(runner, "active") if _card_id(u)]


def failed_units(runner: Callable[[Sequence[str]], str] = _command) -> list[FailedWorker]:
    return [FailedWorker(u, _card_id(u)) for u in _unit_rows(runner, "failed")]


def active_enter_timestamp(unit: str, runner: Callable[[Sequence[str]], str] = _command) -> float | None:
    raw = runner(("systemctl", "--user", "show", unit, "-p", "ActiveEnterTimestamp", "--value")).strip()
    try:
        # systemd's timestamp is not portable to parse. Tests and production
        # adapters may provide epoch seconds through the monotonic property.
        return float(raw)
    except ValueError:
        raw = runner(("systemctl", "--user", "show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value")).strip()
        try:
            mono = float(raw) / 1_000_000
            uptime = float(Path("/proc/uptime").read_text().split()[0])
            return __import__("time").time() - uptime + mono
        except (OSError, ValueError):
            return None


def current_session_file(card_id: str, active_enter: float | None, session_root: Path | str | None = None) -> Path | None:
    root = Path(session_root or os.path.expanduser("~/.pi/agent/sessions"))
    candidates: list[Path] = []
    for path in glob.glob(str(root / f"*{card_id}*" / "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if active_enter is not None and mtime < active_enter - RUN_SKEW_SECONDS:
            continue
        candidates.append(Path(path))
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def observe(session_root: Path | str | None = None, runner: Callable[[Sequence[str]], str] = _command) -> tuple[list[WorkerLiveness], list[FailedWorker]]:
    failed = failed_units(runner)
    observations = []
    for unit in active_units(runner):
        card = _card_id(unit)
        assert card is not None
        started = active_enter_timestamp(unit, runner)
        session = current_session_file(card, started, session_root)
        mtime = session.stat().st_mtime if session else None
        observations.append(WorkerLiveness(card, unit, str(session) if session else None, mtime, started, session is not None))
    return observations, failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", type=Path)
    args = parser.parse_args(argv)
    workers, failed = observe(args.session_root)
    print(json.dumps({"workers": [asdict(w) for w in workers], "cruft": [asdict(f) for f in failed]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
