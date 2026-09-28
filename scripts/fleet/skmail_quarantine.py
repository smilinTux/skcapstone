#!/usr/bin/env python3
"""Plan, review, apply, and restore lossless SKMail legacy quarantine."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIAGNOSTIC = re.compile(
    r"^skmail: INVALID_(?:JSON|SCHEMA) (.+):(\d+) sha256=([0-9a-f]{64}) reason=(.+)$"
)


def encoded(value: Any) -> bytes:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    json.loads(data)
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def metadata(path: Path) -> dict[str, Any]:
    value = path.stat()
    return {
        "mode": stat.S_IMODE(value.st_mode),
        "mtime_ns": value.st_mtime_ns,
        "size": value.st_size,
    }


def writer(raw: bytes) -> str | None:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    candidate = value.get("from") or value.get("sender") if isinstance(value, dict) else None
    return candidate if isinstance(candidate, str) else None


def write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def plan(args: argparse.Namespace) -> Path:
    box = args.box.resolve()
    archive_root = args.archive.resolve()
    cutoff_ns = int(args.legacy_before.timestamp() * 1_000_000_000)
    with (box.parent / ".skmail-quarantine.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = {str(p.resolve()): metadata(p) for p in box.iterdir() if p.is_file()}
        env = os.environ.copy()
        env["SKMAIL_DIR"] = str(box.parent)
        result = subprocess.run(
            [str(args.reader), "tail", "1000000000"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False,
        )
        if result.returncode:
            raise SystemExit(f"strict reader failed with status {result.returncode}")
        rejected: dict[Path, dict[int, tuple[str, str]]] = {}
        for text in result.stderr.splitlines():
            match = DIAGNOSTIC.match(text)
            if not match:
                raise SystemExit(f"unrecognized strict reader diagnostic: {text}")
            path = Path(match.group(1)).resolve()
            rejected.setdefault(path, {})[int(match.group(2))] = (match.group(4), match.group(3))
        after = {str(p.resolve()): metadata(p) for p in box.iterdir() if p.is_file()}
        if before != after:
            raise SystemExit("mailbox drifted during inventory")
        entries: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        blobs: dict[str, bytes] = {}
        for path, failures in sorted(rejected.items(), key=lambda item: str(item[0])):
            if path.parent != box or not path.is_file():
                raise SystemExit(f"diagnostic escaped mailbox: {path}")
            meta = metadata(path)
            if meta["mtime_ns"] > cutoff_ns:
                raise SystemExit(f"current writer may be malformed: {path}")
            raw_file = path.read_bytes()
            source_hash = digest(raw_file)
            blobs[source_hash] = raw_file
            records = lines(raw_file)
            for ordinal, (reason, expected_hash) in sorted(failures.items()):
                if ordinal < 1 or ordinal > len(records):
                    raise SystemExit(f"invalid diagnostic ordinal: {path}:{ordinal}")
                raw = records[ordinal - 1]
                if digest(raw) != expected_hash:
                    raise SystemExit(f"reader hash disagreement: {path}:{ordinal}")
                blobs[expected_hash] = raw
                entries.append({
                    "source_path": str(path), "ordinal": ordinal, "reason": reason,
                    "byte_count": len(raw), "mtime_ns": meta["mtime_ns"],
                    "writer": writer(raw), "sha256": expected_hash,
                    "restore_target": str(path),
                })
            sources.append({
                "path": str(path), "sha256": source_hash, "metadata": meta,
                "record_count": len(records), "rejected_ordinals": sorted(failures),
            })
        if not entries:
            raise SystemExit("strict reader rejected no envelopes")
        manifest = {
            "schema": 1, "card_id": args.card_id, "planner": args.planner,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "legacy_before": args.legacy_before.isoformat(),
            "box": str(box), "reader": str(args.reader.resolve()),
            "reader_sha256": digest(args.reader.read_bytes()),
            "entries": entries, "sources": sources,
            "counts": {"sources": len(sources), "rejected_records": len(entries),
                       "rejected_bytes": sum(item["byte_count"] for item in entries)},
        }
        manifest_bytes = encoded(manifest)
        archive_id = digest(manifest_bytes)
        destination = archive_root / "sha256" / archive_id
        if destination.exists():
            raise SystemExit(f"archive already exists: {destination}")
        staging = Path(tempfile.mkdtemp(prefix=".skmail-quarantine-", dir=archive_root))
        try:
            for blob_hash, raw in blobs.items():
                write_exclusive(staging / "blobs" / "sha256" / blob_hash, raw)
            write_exclusive(staging / "manifest.json", manifest_bytes)
            write_exclusive(staging / "manifest.sha256", (archive_id + "\n").encode())
            for blob in (staging / "blobs" / "sha256").iterdir():
                if digest(blob.read_bytes()) != blob.name:
                    raise SystemExit(f"archive verification failed: {blob}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return destination


def load_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if encoded(value) != raw:
        raise SystemExit("manifest is not canonical JSON")
    return value, raw


def apply(args: argparse.Namespace) -> None:
    manifest, raw_manifest = load_manifest(args.manifest)
    manifest_hash = digest(raw_manifest)
    review = json.loads(args.review.read_bytes())
    if review.get("verdict") != "PASS" or review.get("manifest_sha256") != manifest_hash:
        raise SystemExit("independent PASS review does not match manifest")
    if not isinstance(review.get("reviewer"), str) or review["reviewer"] == manifest["planner"]:
        raise SystemExit("reviewer must differ from planner")
    box = Path(manifest["box"])
    with (box.parent / ".skmail-quarantine.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        replacements: list[tuple[Path, Path | None, dict[str, Any]]] = []
        temporary: list[Path] = []
        try:
            for source in manifest["sources"]:
                path = Path(source["path"])
                current = path.read_bytes()
                if digest(current) != source["sha256"] or metadata(path) != source["metadata"]:
                    raise SystemExit(f"source drifted after review: {path}")
                rejected = set(source["rejected_ordinals"])
                kept = b"".join(raw for ordinal, raw in enumerate(lines(current), 1) if ordinal not in rejected)
                replacement = None
                if kept:
                    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=box)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(kept); stream.flush(); os.fsync(stream.fileno())
                    replacement = Path(name)
                    os.chmod(replacement, source["metadata"]["mode"])
                    os.utime(replacement, ns=(source["metadata"]["mtime_ns"], source["metadata"]["mtime_ns"]))
                    temporary.append(replacement)
                replacements.append((path, replacement, source["metadata"]))
            for path, replacement, _ in replacements:
                if replacement is None:
                    path.unlink()
                else:
                    os.replace(replacement, path)
                    temporary.remove(replacement)
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)


def restore(args: argparse.Namespace) -> None:
    manifest, _ = load_manifest(args.manifest)
    archive = args.manifest.parent
    box = Path(manifest["box"])
    with (box.parent / ".skmail-quarantine.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for source in manifest["sources"]:
            target = Path(source["path"])
            blob = archive / "blobs" / "sha256" / source["sha256"]
            raw = blob.read_bytes()
            if digest(raw) != source["sha256"]:
                raise SystemExit(f"restore blob failed verification: {blob}")
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=box)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            temp = Path(name)
            os.chmod(temp, source["metadata"]["mode"])
            os.utime(temp, ns=(source["metadata"]["mtime_ns"], source["metadata"]["mtime_ns"]))
            os.replace(temp, target)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp needs a timezone")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("plan")
    make.add_argument("--box", type=Path, required=True)
    make.add_argument("--archive", type=Path, required=True)
    make.add_argument("--reader", type=Path, required=True)
    make.add_argument("--legacy-before", type=timestamp, required=True)
    make.add_argument("--planner", required=True)
    make.add_argument("--card-id", default="6d98812c")
    execute = sub.add_parser("apply")
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--review", type=Path, required=True)
    undo = sub.add_parser("restore")
    undo.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "plan":
        print(plan(args))
    elif args.command == "apply":
        apply(args)
    else:
        restore(args)


if __name__ == "__main__":
    main()
