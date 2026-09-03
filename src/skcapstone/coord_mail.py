"""Agent-to-agent mailbox over the Syncthing coordination folder.

STORAGE: one file per WRITER PER HOST, ``coordination/skmail.d/<agent>@<host>.jsonl``.

That naming is the whole design, not a style choice. ``~/.skcapstone`` is a
Syncthing folder, and a single shared ``skmail.jsonl`` produced a real
sync-conflict on 2026-08-25 that silently swallowed a message: two hosts both
appended, Syncthing kept one version and moved the other aside. ``flock`` does
not help, because it is a single-host lock and the conflict is between hosts.
One writer per file means Syncthing never has two versions to reconcile. This
is the same pattern the CardStore already uses at
``cards/<id>/events/<writer>@<host>.jsonl``, which is why that store has never
conflicted.

``flock`` is still taken, because two agents on the SAME host can share one
writer file.

ESTATE ISOLATION: mailboxes never cross estates. ``lumina@noroc2027`` and
``lumina@chiap08`` are deliberately separate mailboxes with separate read
cursors, because nor and chi are different fleets on different Syncthing
shares. Reaching another estate's mail is an ad-hoc SSH operation, not a
federation feature.

Ported from the original 180-line bash implementation, keeping the on-disk
format byte-compatible so existing mailboxes stay readable.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

VALID_PRIORITIES = ("urgent", "normal", "fyi")

#: Subdirectories every coordination plane needs. ``bootstrap`` creates these.
COORD_SUBDIRS = ("skmail.d", "card_events", "locks", "tasks", "agents", "reviews")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mailbox_dir(home: Path) -> Path:
    """Directory holding one JSONL per writer."""
    return Path(home) / "coordination" / "skmail.d"


def writer_file(home: Path, sender: str, host: str | None = None) -> Path:
    """The single file this writer appends to, on this host.

    Lowercased sender so ``Jarvis``/``jarvis``/``JARVIS`` share one mailbox,
    matching the bash implementation's case-insensitive recipient rule.
    """
    host = host or socket.gethostname()
    return mailbox_dir(home) / f"{sender.lower()}@{host}.jsonl"


def send(home: Path, sender: str, to: str, priority: str, re: str, body: str,
         host: str | None = None) -> dict:
    """Append one message to this writer's own file. Never writes a shared file.

    ``to`` may be ``all``. Raises ValueError on an unknown priority rather than
    silently downgrading it: ``urgent`` means "stop what you are doing", so a
    typo must not quietly become a normal message.
    """
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"priority must be one of {'|'.join(VALID_PRIORITIES)}, got {priority!r}")
    if not sender or not to:
        raise ValueError("sender and recipient are both required")

    path = writer_file(home, sender, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _now(),
        "from": sender,
        "to": to,
        "priority": priority,
        "re": re,
        "body": body,
        "host": host or socket.gethostname(),
    }
    with open(path, "a", encoding="utf-8") as fh:
        # Same-host concurrency only; cross-host is solved by the filename.
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return rec


def _read_all(home: Path) -> list[dict]:
    """Every message across every writer file, oldest first.

    A malformed line is skipped rather than fatal: these files are appended by
    several processes and a partial line can exist mid-write.
    """
    out: list[dict] = []
    d = mailbox_dir(home)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    out.sort(key=lambda r: str(r.get("ts", "")))
    return out


def _addressed_to(rec: dict, me: str) -> bool:
    to = str(rec.get("to", "")).lower()
    me = me.lower()
    # `to` may be a comma list, and `all` reaches everyone.
    return to == "all" or me in [t.strip() for t in to.split(",")]


def _cursor_path(home: Path, me: str) -> Path:
    return Path(home) / "coordination" / f".skmail-cursor.{me.lower()}"


def read(home: Path, me: str) -> list[dict]:
    """Unread messages addressed to ``me``, oldest first. Does not advance the cursor."""
    cur = ""
    cp = _cursor_path(home, me)
    if cp.exists():
        cur = cp.read_text(encoding="utf-8").strip()
    return [r for r in _read_all(home)
            if _addressed_to(r, me) and str(r.get("ts", "")) > cur]


def ack(home: Path, me: str) -> int:
    """Mark everything currently visible to ``me`` as read. Returns the count."""
    pending = read(home, me)
    if not pending:
        return 0
    cp = _cursor_path(home, me)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(str(pending[-1].get("ts", "")), encoding="utf-8")
    return len(pending)


def tail(home: Path, n: int = 10) -> list[dict]:
    """Recent traffic between any peers, newest last."""
    return _read_all(home)[-n:]


def bootstrap(home: Path, agent: str | None = None) -> dict:
    """Create the coordination skeleton. Idempotent: creates only what is absent.

    Nothing in coordination.py ever created these directories, which is why a
    new node silently has no mailbox until someone makes one by hand. This is
    that step, made repeatable.
    """
    home = Path(home)
    created: list[str] = []
    for sub in COORD_SUBDIRS:
        p = home / "coordination" / sub
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(str(p.relative_to(home)))
    for top in ("cards", "evidence"):
        p = home / top
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(top)
    # Touch this agent's own mailbox so `read` works before the first `send`
    # and so the file exists for Syncthing to propagate.
    mailbox = None
    if agent:
        wf = writer_file(home, agent)
        if not wf.exists():
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.touch(mode=0o600)
            created.append(str(wf.relative_to(home)))
        mailbox = str(wf)
    return {"home": str(home), "created": created, "mailbox": mailbox,
            "already_present": [s for s in COORD_SUBDIRS
                                if str(Path("coordination") / s) not in created]}
