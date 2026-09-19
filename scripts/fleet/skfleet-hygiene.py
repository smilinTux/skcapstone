#!/usr/bin/env python3
"""Bounded, opt-in cleanup for failed fleet units and orphaned worker sessions.

The default action is report-only. Selection is deliberately narrow: failed
systemd user units whose names start with ``skfleet-worker-`` and tmux sessions
whose names start with ``codex-auto-`` and have no live worker descendant.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

UNIT_PREFIX = "skfleet-worker-"
SESSION_PREFIX = "codex-auto-"


@dataclass(frozen=True)
class FailedUnit:
    name: str
    age: str = "unknown"


@dataclass(frozen=True)
class OrphanSession:
    name: str


def _run(args: Sequence[str]) -> str:
    return subprocess.run(args, check=False, capture_output=True, text=True).stdout


def parse_failed_units(output: str, now: dt.datetime | None = None) -> list[FailedUnit]:
    """Parse only units explicitly returned by systemd's failed filter."""
    result: list[FailedUnit] = []
    for line in output.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith(UNIT_PREFIX):
            continue
        # Defense in depth: even if a mocked or misconfigured systemctl
        # ignores --state=failed, never select an active/running unit.
        if len(fields) > 2 and fields[2] != "failed" and (len(fields) < 4 or fields[3] != "failed"):
            continue
        # list-units --plain has UNIT LOAD ACTIVE SUB DESCRIPTION.  Age is
        # obtained separately by the caller when available; retaining the
        # description here would make a report look like an age.
        result.append(FailedUnit(fields[0]))
    return result


def parse_sessions(output: str) -> list[str]:
    """Return exactly codex-auto sessions, never similarly named sessions."""
    return [line.strip() for line in output.splitlines()
            if line.strip().startswith(SESSION_PREFIX)]


def worker_descendant_pids(process_output: str, root_pid: int) -> set[int]:
    """Find descendants from ``ps -eo pid=,ppid=,args=`` output."""
    rows: list[tuple[int, int, str]] = []
    for line in process_output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
        except ValueError:
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid, _ in rows:
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return {pid for pid, _, args in rows if pid in descendants and
            re.search(r"(?:skfleet-worker|codex|pi-codex)", args, re.I)}


def session_has_live_worker(session: str, *, runner: Callable[[Sequence[str]], str] = _run) -> bool:
    panes = runner(["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"])
    pids = [int(p) for p in panes.split() if p.isdigit()]
    if not pids:
        return False
    processes = runner(["ps", "-eo", "pid=,ppid=,args="])
    return any(worker_descendant_pids(processes, pid) for pid in pids)


def find_orphans(sessions: Iterable[str], *, runner: Callable[[Sequence[str]], str] = _run) -> list[OrphanSession]:
    return [OrphanSession(name) for name in sessions if not session_has_live_worker(name, runner=runner)]


def report(*, runner: Callable[[Sequence[str]], str] = _run) -> tuple[list[FailedUnit], list[OrphanSession]]:
    failed = parse_failed_units(runner(["systemctl", "--user", "list-units", "--state=failed", "--no-legend", "--plain", "skfleet-worker-*.service"]))
    sessions = parse_sessions(runner(["tmux", "list-sessions", "-F", "#{session_name}"]))
    return failed, find_orphans(sessions, runner=runner)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true", help="reset failed units and kill selected orphan sessions")
    args = parser.parse_args(argv)
    failed, orphans = report()
    for item in failed:
        print(f"failed unit: {item.name} (age {item.age})")
    for item in orphans:
        print(f"orphan session: {item.name}")
    if args.cleanup:
        for item in failed:
            subprocess.run(["systemctl", "--user", "reset-failed", item.name], check=False)
        for item in orphans:
            subprocess.run(["tmux", "kill-session", "-t", item.name], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
