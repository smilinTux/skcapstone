#!/usr/bin/env python3
"""Validated append-only SKMail writer and lossless legacy recovery."""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

PRIORITIES = {"urgent", "normal", "fyi"}
PLACEHOLDERS = {"SKAGENT", "$SKAGENT", "${SKAGENT}", "AGENT", "YOUR_NAME"}


def _identity(value: str) -> str:
    value = value.strip()
    if not value or value in PLACEHOLDERS or any(x in value for x in ("/", "\\", "..")):
        raise ValueError("sender identity is empty, unsafe, or an unexpanded placeholder")
    return value


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte or raise without accepting a short write."""
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("mailbox temporary write made no progress")
        view = view[written:]


def _replace_atomically(path: Path, payload: bytes, mode: int) -> None:
    """Publish complete mailbox bytes with no partial target state."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def append(
    boxdir: Path,
    sender: str,
    recipient: str,
    priority: str,
    subject: str,
    body: str,
    host: str,
    timestamp: str | None = None,
) -> str:
    """Validate, serialize, lock, append, flush, and sync one canonical record."""
    sender, recipient, host = _identity(sender), recipient.strip(), host.strip()
    if not recipient or not host or priority not in PRIORITIES:
        raise ValueError("recipient, host, or priority is invalid")
    stamp = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
    datetime.datetime.fromisoformat(stamp)
    record = {
        "ts": stamp,
        "from": sender,
        "to": recipient,
        "priority": priority,
        "re": subject,
        "body": body,
        "host": host,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    boxdir.mkdir(parents=True, exist_ok=True)
    path = boxdir / f"{sender.lower()}@{host}.jsonl"
    lock_path = boxdir / f".{path.name}.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            raise ValueError("mailbox ends with a partial record")
        for raw in existing.splitlines():
            prior = json.loads(raw)
            if prior.get("from", "").lower() != sender.lower() or prior.get("host") != host:
                raise ValueError("mailbox writer identity does not match its filename")
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        _replace_atomically(path, (existing + line).encode(), mode)
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
    print(
        append(
            args.boxdir,
            args.sender,
            args.recipient,
            args.priority,
            args.subject,
            args.body,
            args.host,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
