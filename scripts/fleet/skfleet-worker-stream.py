#!/usr/bin/env python3
"""Project live fleet worker activity from pi's own session files.

WHY THIS EXISTS, and why the obvious signals are wrong.

A fleet worker's progress cannot be read from any of the surfaces that look
like they should carry it:

  * systemd unit state says `active` for a worker that has stopped doing
    anything; the process is genuinely alive.
  * CPU says nothing: a worker blocked in epoll on a lost gateway response
    and a worker between tool calls both read 0%.
  * The worker's stdout log (`~/.skcapstone/fleet/logs/<card>-<stamp>.log`)
    is a plain file the wrapper opens with `args.stdout.open("wb")`, and it
    stays 0 bytes for the whole run: content lands at exit. Measured
    2026-09-19: every live worker's log was empty, completed ones held a few
    hundred bytes.
  * Workspace file mtime is the trap. Measured 2026-09-19, it showed five of
    six long-running workers "silent" for 111 to 276 minutes. All five were
    working. Fleet workers do most of their work through tool calls that READ
    (git, `skcapstone coord status`, file reads), so a fully engaged worker
    leaves its workspace untouched for hours. A detector built on this fires
    on healthy workers.

`pi` writes a newline-delimited JSON session file as it works, at
`~/.pi/agent/sessions/<workspace-slug>/<ts>_<uuid>.jsonl`, where the slug
contains the card id. Events are appended continuously. Measured across all
five chi hosts, every live worker had written within the last minute, while
the same workers looked idle to workspace mtime.

That file is therefore the only honest progress signal available, and it is
what this projects.

It emits a PROJECTION, never the raw events: sessions run 45-57 MB with a
~14 KB average event, so streaming them verbatim to a dashboard is not
viable.
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

CARD_RE = re.compile(r"[0-9a-f]{8}")
SESSION_ROOT = os.path.expanduser("~/.pi/agent/sessions")
PREVIEW = 140


def unit_started_at(unit: str) -> float | None:
    """Unix seconds when this worker unit became active, or None."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p",
             "ActiveEnterTimestampMonotonic", "--value"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        mono = int(out) / 1_000_000.0
    except Exception:
        return None
    if mono <= 0:
        return None
    try:
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
    except OSError:
        return None
    return time.time() - (up - mono)


def failed_workers() -> list[dict]:
    """Worker units systemd still lists as failed.

    `list-units` without a state filter includes these, and they are NOT
    workers. Measured 2026-09-19: skfleet-worker-glm-l-25ab78c6-repair had
    been failed for 8.5 days with MainPID=0, and counting it as active made
    its 8.4-day-old session read as a live worker gone silent. They are worth
    reporting in their own right, as cruft holding a unit name, but never as
    activity.
    """
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-units", "skfleet-worker-*",
             "--no-legend", "--plain", "--state=failed"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        found = CARD_RE.findall(unit)
        rows.append({"unit": unit, "card": found[-1] if found else None})
    return rows


def active_workers() -> list[tuple[str, float | None]]:
    """(card id, unit start time) for every ACTIVE skfleet-worker unit."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-units", "skfleet-worker-*",
             "--no-legend", "--plain", "--state=active"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return []
    cards = []
    for line in out.splitlines():
        unit = line.split()[0] if line.split() else ""
        found = CARD_RE.findall(unit)
        if found:
            cards.append((found[-1], unit_started_at(unit)))
    return cards


def session_file(card: str, started: float | None) -> str | None:
    """Newest pi session jsonl for THIS RUN of the card, or None.

    A card that has been worked before leaves session files from earlier runs
    in a directory the glob still matches. Taking the newest unconditionally
    reported one worker as silent for 8.4 days when it had in fact just
    started: the match was a session from a previous attempt. Anything that
    predates the unit by more than a small skew is a different run, and saying
    so is better than reporting a stale mtime as this worker's progress.
    """
    hits = glob.glob(os.path.join(SESSION_ROOT, f"*{card}*", "*.jsonl"))
    if not hits:
        return None
    newest = max(hits, key=lambda p: os.path.getmtime(p))
    if started is not None and os.path.getmtime(newest) < started - 120:
        return None
    return newest


def project(raw: str) -> dict | None:
    """One session event -> a compact row. Returns None for noise."""
    try:
        d = json.loads(raw)
    except Exception:
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
            t = part.get("text")
            if t and not text:
                text = str(t)
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
    for card, started in active_workers():
        f = session_file(card, started)
        if not f:
            rows.append({"host": host, "card": card, "events": 0,
                         "silent_s": None,
                         "note": "no session file for this run"})
            continue
        try:
            with open(f, errors="replace") as fh:
                n = sum(1 for _ in fh)
        except OSError:
            n = 0
        rows.append({
            "host": host, "card": card, "events": n,
            "silent_s": int(now - os.path.getmtime(f)),
            "session": os.path.basename(f),
        })
    return rows


def follow(host: str, poll: float) -> None:
    """Tail every active worker's session file, emitting projected rows."""
    offsets: dict[str, int] = {}
    while True:
        for card, started in active_workers():
            f = session_file(card, started)
            if not f:
                continue
            try:
                size = os.path.getsize(f)
                start = offsets.get(f)
                if start is None:
                    offsets[f] = size      # begin at the tail, not the 57MB history
                    continue
                if size <= start:
                    continue
                with open(f, errors="replace") as fh:
                    fh.seek(start)
                    chunk = fh.read()
                offsets[f] = start + len(chunk.encode("utf-8", "replace"))
            except OSError:
                continue
            for line in chunk.splitlines():
                row = project(line)
                if not row:
                    continue
                row["host"] = host
                row["card"] = card
                print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(poll)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--follow", action="store_true",
                    help="stream projected events as they are appended")
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()
    host = socket.gethostname().split(".")[0]
    if a.follow:
        try:
            follow(host, a.poll)
        except KeyboardInterrupt:
            return 0
        return 0
    for row in snapshot(host):
        print(json.dumps(row, ensure_ascii=False))
    for row in failed_workers():
        row["host"] = host
        row["state"] = "failed"
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
