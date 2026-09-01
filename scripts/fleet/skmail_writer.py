#!/usr/bin/env python3
"""Validated append-only SKMail writer and lossless legacy recovery."""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path

PRIORITIES = {"urgent", "normal", "fyi"}
PLACEHOLDERS = {"SKAGENT", "$SKAGENT", "${SKAGENT}", "AGENT", "YOUR_NAME"}


def _identity(value: str) -> str:
    value = value.strip()
    if not value or value in PLACEHOLDERS or any(x in value for x in ("/", "\\", "..")):
        raise ValueError("sender identity is empty, unsafe, or an unexpanded placeholder")
    return value


def append(boxdir: Path, sender: str, recipient: str, priority: str, subject: str,
           body: str, host: str, timestamp: str | None = None) -> str:
    """Validate, serialize, lock, append, flush, and sync one canonical record."""
    sender, recipient, host = _identity(sender), recipient.strip(), host.strip()
    if not recipient or not host or priority not in PRIORITIES:
        raise ValueError("recipient, host, or priority is invalid")
    stamp = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
    datetime.datetime.fromisoformat(stamp)
    record = {"ts": stamp, "from": sender, "to": recipient, "priority": priority,
              "re": subject, "body": body, "host": host}
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    boxdir.mkdir(parents=True, exist_ok=True)
    path = boxdir / f"{sender.lower()}@{host}.jsonl"
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        existing = stream.read()
        if existing and not existing.endswith("\n"):
            raise ValueError("mailbox ends with a partial record")
        for raw in existing.splitlines():
            prior = json.loads(raw)
            if prior.get("from", "").lower() != sender.lower() or prior.get("host") != host:
                raise ValueError("mailbox writer identity does not match its filename")
        stream.seek(0, os.SEEK_END)
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(line.encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sender")
    parser.add_argument("recipient")
    parser.add_argument("priority", choices=sorted(PRIORITIES))
    parser.add_argument("subject")
    parser.add_argument("body")
    parser.add_argument("--boxdir", type=Path, required=True)
    parser.add_argument("--host", default=os.uname().nodename)
    args = parser.parse_args()
    print(append(args.boxdir, args.sender, args.recipient, args.priority,
                 args.subject, args.body, args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
