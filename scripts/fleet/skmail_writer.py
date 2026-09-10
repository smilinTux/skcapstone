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
        raise ValueError("identity is empty, unsafe, or an unexpanded placeholder")
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
    sender, recipient, host = _identity(sender), _identity(recipient), _identity(host)
    if priority not in PRIORITIES:
        raise ValueError("priority is invalid")
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
        _replace_atomically(path, (existing + line).encode(), 0o600)
    return hashlib.sha256(line.encode()).hexdigest()


def quarantine_foreign(boxdir: Path, sender: str, host: str) -> int:
    """Relocate foreign records out of this writer's mailbox, losslessly.

    A recurring cross-host defect appends records carrying a foreign
    ``from``/``host`` directly into another writer's file (cards fda425ac and
    4a3d1119), which blocks every subsequent canonical append for that
    writer. This moves each foreign record byte-for-byte into its canonical
    ``<from>@<host>.jsonl`` file. Nothing is ever deleted; unparseable lines
    stay in place so the failure stays visible. Two-phase (destinations
    written before the source rewrite) prefers duplicate-on-crash over loss.

    Returns:
        The number of records moved.

    Raises:
        ValueError: On partial records or a contaminated destination file.
    """
    sender, host = _identity(sender), _identity(host)
    path = boxdir / f"{sender.lower()}@{host}.jsonl"
    lock_path = boxdir / f"{path.name}.lock"
    # ponytail: lock order is always source-then-destination; two simultaneous
    # quarantines over swapped file pairs could interleave, escalate to a
    # per-writer registry if quarantine ever becomes concurrent-hot.
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            raise ValueError("mailbox ends with a partial record")
        keep: list[str] = []
        foreign: dict[tuple[str, str], list[str]] = {}
        for raw in existing.splitlines():
            line = raw + "\n"
            try:
                prior = json.loads(raw)
            except json.JSONDecodeError:
                keep.append(line)  # never delete what we cannot parse
                continue
            f, h = str(prior.get("from", "")), str(prior.get("host", ""))
            if not f or not h or f.lower() == sender.lower() and h == host:
                keep.append(line)
            else:
                foreign.setdefault((f.lower(), h), []).append(line)
        moved = sum(len(v) for v in foreign.values())
        if moved:
            for (f, h), lines in foreign.items():
                dest = boxdir / f"{f}@{h}.jsonl"
                dest_lock = boxdir / f".{dest.name}.lock"
                with dest_lock.open("a") as dl:
                    fcntl.flock(dl, fcntl.LOCK_EX)
                    dexisting = (
                        dest.read_text(encoding="utf-8") if dest.exists() else ""
                    )
                    if dexisting and not dexisting.endswith("\n"):
                        raise ValueError("destination ends with a partial record")
                    for raw in dexisting.splitlines():
                        p = json.loads(raw)
                        if (
                            str(p.get("from", "")).lower() != f
                            or str(p.get("host", "")) != h
                        ):
                            raise ValueError(
                                "destination writer identity does not match "
                                "its filename"
                            )
                    _replace_atomically(
                        dest, (dexisting + "".join(lines)).encode(), 0o600
                    )
            _replace_atomically(path, "".join(keep).encode(), 0o600)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sender")
    parser.add_argument("recipient", nargs="?")
    parser.add_argument("priority", nargs="?", choices=sorted(PRIORITIES))
    parser.add_argument("subject", nargs="?")
    parser.add_argument("body", nargs="?")
    parser.add_argument(
        "--quarantine",
        action="store_true",
        help="relocate foreign records out of sender's mailbox instead of appending",
    )
    parser.add_argument("--boxdir", type=Path, required=True)
    parser.add_argument("--host", default=os.uname().nodename)
    args = parser.parse_args()
    if args.quarantine:
        print(quarantine_foreign(args.boxdir, args.sender, args.host))
        return 0
    if args.recipient is None or args.priority is None:
        parser.error("append mode requires recipient, priority, subject, and body")
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
