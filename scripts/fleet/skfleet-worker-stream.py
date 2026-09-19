#!/usr/bin/env python3
"""Project live fleet worker activity from pi's own session files.

WHY THIS EXISTS, and why the obvious signals are all wrong.

A fleet worker's progress cannot be read from any of the surfaces that look
like they should carry it:

  * systemd unit state says ``active`` for a worker that has stopped doing
    anything; the process is genuinely alive either way.
  * CPU says nothing. A worker blocked in epoll on a lost gateway response
    and a worker between tool calls both read 0%.
  * The worker's stdout log (``~/.skcapstone/fleet/logs/<card>-<stamp>.log``)
    is a plain FILE, not a pipe or a pty: the wrapper opens it with
    ``args.stdout.open("wb")`` and hands the fd to ``subprocess.Popen``.
    Measured on chiap01-04 on 2026-09-19, every live worker's log was 0 bytes
    and completed ones held a few hundred; content lands at exit. Tailing it
    gives you nothing while the worker is running.
  * Workspace file mtime is the trap, and it is the reason this docstring is
    long. Measured 2026-09-19, it reported five of six long-running workers
    "silent" for 111 to 276 minutes. All five were working. Fleet workers do
    most of their work through tool calls that READ (git, ``skcapstone coord
    status``, file reads), so a fully engaged worker leaves its workspace
    untouched for hours. A liveness detector built on that signal fires on
    healthy workers, and one was built and had to be withdrawn.

``pi`` writes a newline-delimited JSON session file as it works, at
``~/.pi/agent/sessions/<workspace-slug>/<ts>_<uuid>.jsonl``, where the slug
contains the card id. Events are appended continuously, as they happen.
Measured across all five chi hosts, every live worker had written to its
session file within the last minute while the same workers looked idle to
workspace mtime.

That file is therefore the only honest progress signal available on the box,
and it needs no change whatsoever to the worker launch path to read.

This emits a PROJECTION, never the raw events: sessions run 45-57 MB with a
~14 KB average event, so streaming them verbatim to a dashboard is not
viable. One projected row is bounded at roughly 300 bytes.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import time

#: Card ids are 8 hex characters. Unit names embed model and lane names too,
#: so take the LAST hex-looking run in the unit name, which is the card.
CARD_RE = re.compile(r"[0-9a-f]{8}")
SESSION_ROOT = os.path.expanduser("~/.pi/agent/sessions")
#: A session file whose mtime predates its unit by more than this belongs to
#: a DIFFERENT run, not this one. See session_file().
RUN_SKEW_S = 120
PREVIEW = 140


def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except Exception:
        return ""


def unit_started_at(unit: str) -> float | None:
    """Unix seconds when this worker unit became active, or None.

    Monotonic is used rather than the wall-clock timestamp property because
    the latter is locale-formatted and has bitten parsers before.
    """
    raw = _systemctl("show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value").strip()
    try:
        mono = int(raw) / 1_000_000.0
    except ValueError:
        return None
    if mono <= 0:
        return None
    try:
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
    except OSError:
        return None
    return time.time() - (up - mono)


def _units(state: str) -> list[str]:
    out = _systemctl(
        "list-units", "skfleet-worker-*", "--no-legend", "--plain", f"--state={state}"
    )
    units = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            units.append(parts[0])
    return units


def failed_workers() -> list[dict]:
    """Worker units systemd still lists as failed.

    ``list-units`` WITHOUT a state filter includes these, and they are not
    workers. Measured 2026-09-19: skfleet-worker-glm-l-25ab78c6-repair had
    been failed on chiap02 since 2026-09-10 with MainPID=0, and counting it
    as active made its 8.4-day-old session file read as a live worker gone
    silent. They are worth reporting in their own right, as cruft holding a
    unit name, but never as activity.
    """
    rows = []
    for unit in _units("failed"):
        found = CARD_RE.findall(unit)
        rows.append({"unit": unit, "card": found[-1] if found else None, "state": "failed"})
    return rows


def active_workers() -> list[tuple[str, str, float | None]]:
    """(card id, unit name, unit start time) for every ACTIVE worker unit."""
    rows = []
    for unit in _units("active"):
        found = CARD_RE.findall(unit)
        if found:
            rows.append((found[-1], unit, unit_started_at(unit)))
    return rows


def session_file(card: str, started: float | None) -> str | None:
    """Newest pi session jsonl for THIS RUN of the card, or None.

    A card that has been worked before leaves session files from earlier runs
    in a directory the glob still matches. Taking the newest unconditionally
    reported one worker as silent for 8.4 days when it had in fact only just
    started: the match was a session from a previous attempt. Anything that
    predates the unit by more than a small skew belongs to a different run,
    and saying "no session for this run" is better than reporting a stale
    mtime as this worker's progress.
    """
    hits = glob.glob(os.path.join(SESSION_ROOT, f"*{card}*", "*.jsonl"))
    if not hits:
        return None
    newest = max(hits, key=lambda p: os.path.getmtime(p))
    if started is not None and os.path.getmtime(newest) < started - RUN_SKEW_S:
        return None
    return newest


def project(raw: str) -> dict | None:
    """One raw session event -> a compact row. None for events with no content.

    The projection is deliberately lossy. It keeps who spoke, which tool was
    invoked, and a truncated preview. It drops tool payloads, which is where
    essentially all of the 45-57 MB lives.
    """
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    msg = d.get("message") or {}
    role = msg.get("role") or d.get("type") or "?"
    tool = None
    text = ""
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                tool = part.get("name")
            chunk = part.get("text")
            if chunk and not text:
                text = str(chunk)
    elif isinstance(content, str):
        text = content
    return {
        "ts": d.get("timestamp"),
        "role": role,
        "tool": tool,
        "preview": " ".join(text.split())[:PREVIEW],
    }


def snapshot(host: str) -> list[dict]:
    """Per-worker liveness, from the session file's own mtime."""
    now = time.time()
    rows = []
    for card, unit, started in active_workers():
        path = session_file(card, started)
        if not path:
            rows.append(
                {
                    "host": host,
                    "card": card,
                    "unit": unit,
                    "state": "active",
                    "events": 0,
                    "silent_s": None,
                    "note": "no session file for this run",
                }
            )
            continue
        try:
            with open(path, "rb") as fh:
                events = sum(1 for _ in fh)
            silent = int(now - os.path.getmtime(path))
        except OSError:
            events, silent = 0, None
        rows.append(
            {
                "host": host,
                "card": card,
                "unit": unit,
                "state": "active",
                "events": events,
                "silent_s": silent,
                "session": os.path.basename(path),
            }
        )
    return rows


class Tail:
    """Byte-exact tail of one append-only jsonl file.

    Two things the obvious implementation gets wrong:

    * It must read BYTES. Seeking a text-mode handle to a byte offset taken
      from ``os.path.getsize`` and then advancing it by the length of the
      re-encoded string drifts the moment any event contains a multi-byte
      character, and tool output routinely does.
    * A poll can land in the middle of a line pi is still appending. Anything
      after the last newline is therefore held back and re-read next time,
      rather than handed to the parser as a truncated event.
    """

    def __init__(self, path: str, start_at_end: bool = True) -> None:
        self.path = path
        self.offset = os.path.getsize(path) if start_at_end else 0

    def read_lines(self) -> list[str]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:  # truncated or rotated: start over
            self.offset = 0
        if size == self.offset:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                blob = fh.read(size - self.offset)
        except OSError:
            return []
        cut = blob.rfind(b"\n")
        if cut < 0:  # no complete line yet
            return []
        self.offset += cut + 1
        return blob[: cut + 1].decode("utf-8", "replace").splitlines()


def follow(host: str, poll: float, from_start: bool) -> None:
    """Tail every active worker's session file, emitting projected rows."""
    tails: dict[str, Tail] = {}
    while True:
        for card, _unit, started in active_workers():
            path = session_file(card, started)
            if not path:
                continue
            tail = tails.get(path)
            if tail is None:
                try:
                    tail = tails[path] = Tail(path, start_at_end=not from_start)
                except OSError:
                    continue
                if not from_start:
                    continue  # begin at the tail, not the 57 MB of history
            for line in tail.read_lines():
                row = project(line)
                if not row:
                    continue
                row["host"] = host
                row["card"] = card
                print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--follow",
        action="store_true",
        help="stream projected events as they are appended",
    )
    ap.add_argument(
        "--from-start",
        action="store_true",
        help="with --follow, replay the whole session rather than starting at the tail",
    )
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args(argv)
    host = socket.gethostname().split(".")[0]
    if args.follow:
        try:
            follow(host, args.poll, args.from_start)
        except KeyboardInterrupt:
            return 0
        return 0
    for row in snapshot(host):
        print(json.dumps(row, ensure_ascii=False))
    for row in failed_workers():
        row["host"] = host
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
