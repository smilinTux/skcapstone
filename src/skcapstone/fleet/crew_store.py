"""Bounded durable crew records on one explicitly identified coordinator host.

The local flock is not a distributed lease. Synced copies do not authorize a
second coordinator, and callers must verify the manifest's coordinator binding.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import threading
import uuid
from contextlib import contextmanager

MAX_BYTES = 262144
MAX_RECORDS = 4096
_IMMUTABLE = (
    "crew_id",
    "request_id",
    "slot_id",
    "packet",
    "packet_sha256",
    "dedup_key",
    "canonical_request_id",
)
_CUSTODY = ("helper_packet", "helper_id", "receipt", "receipt_sha256")


def identifier(value):
    """Require a bounded identifier safe for a single local filename."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("invalid crew record identifier")
    return value


def _pairs(pairs):
    """Reject duplicate JSON keys rather than accepting ambiguous state."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate crew record field")
        result[key] = value
    return result


def _encode(value):
    """Return a bounded canonical object serialization without special floats."""
    if not isinstance(value, dict):
        raise ValueError("crew record must be an object")
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid crew record JSON") from exc
    if len(data) > MAX_BYTES:
        raise ValueError("crew record exceeds byte bound")
    return data


def digest(value):
    """Hash the canonical bounded JSON object."""
    return hashlib.sha256(_encode(value)).hexdigest()


class CrewStore:
    """Serialize mutations; unlocked reads are bounded atomic snapshots."""

    def __init__(self, paths, node=None):
        self.node = identifier(socket.gethostname().strip().lower() if node is None else node)
        self.directory = paths.root.absolute() / "crews" / self.node
        self._local = threading.local()

    def _open_directory(self, *, create=True):
        """Create/open each directory component without following symlinks."""
        path = self.directory
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                if part in {".", ".."}:
                    raise ValueError("unsafe crew directory")
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def lock(self):
        """Hold one local process lock across a read/modify/write transaction."""
        if getattr(self._local, "fd", None) is not None:
            raise ValueError("nested crew lock is not supported")
        directory = self._open_directory()
        lock_fd = None
        try:
            lock_fd = os.open(
                ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError("unsafe crew lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._local.fd = directory
            yield self
        finally:
            self._local.fd = None
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(directory)

    def _fd(self):
        """Require a transaction held by the current thread."""
        fd = getattr(self._local, "fd", None)
        if fd is None:
            raise ValueError("crew store lock required")
        return fd

    @contextmanager
    def _reading(self):
        """Read existing records without creating a directory or lock file."""
        held = getattr(self._local, "fd", None)
        if held is not None:
            yield held
            return
        try:
            fd = self._open_directory(create=False)
        except FileNotFoundError:
            yield None
            return
        try:
            yield fd
        finally:
            os.close(fd)

    def _read(self, name):
        """Read a bounded regular record; corruption is never an empty queue."""
        with self._reading() as directory:
            if directory is None:
                return None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("crew record must be regular")
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("crew record changed or exceeds byte bound")
        try:
            row = json.loads(data, object_pairs_hook=_pairs)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid crew record JSON") from exc
        _encode(row)
        return row

    def _write(self, name, row):
        """Atomically replace one regular record and flush its parent directory."""
        data = _encode(row)
        directory = self._fd()
        try:
            existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError("unsafe crew record destination")
        except FileNotFoundError:
            if len(os.listdir(directory)) >= MAX_RECORDS:
                raise ValueError("crew store record bound reached")
        temporary = f".tmp-{uuid.uuid4().hex}"
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        return json.loads(data)

    def _names(self, prefix):
        """Return bounded deterministic record names for a local scan."""
        with self._reading() as directory:
            names = [] if directory is None else os.listdir(directory)
        if len(names) > MAX_RECORDS:
            raise ValueError("crew store record bound exceeded")
        return sorted(name for name in names if name.startswith(prefix) and name.endswith(".json"))

    def get_manifest(self, crew_id):
        """Read one manifest by its exact crew identity."""
        row = self._read(f"manifest-{identifier(crew_id)}.json")
        if row is not None and (
            not isinstance(row.get("packet"), dict) or row["packet"].get("crew_id") != crew_id
        ):
            raise ValueError("crew manifest identity mismatch")
        return row

    def put_manifest(self, row):
        """Persist an immutable mandate or return its byte-equivalent replay."""
        crew_id = identifier(row["packet"]["crew_id"])
        existing = self.get_manifest(crew_id)
        if existing is not None and existing != row:
            raise ValueError("crew manifest replay conflict")
        return existing if existing is not None else self._write(f"manifest-{crew_id}.json", row)

    def list_manifests(self):
        """List the bounded local manifest set."""
        return [self.get_manifest(name[9:-5]) for name in self._names("manifest-")]

    def scan_manifests(self):
        """Isolate corrupt records so one crew does not starve healthy crews."""
        records, errors = [], []
        for name in self._names("manifest-"):
            crew_id = name[9:-5]
            try:
                row = self.get_manifest(crew_id)
                if row is not None:
                    records.append(row)
            except (ValueError, OSError) as exc:
                errors.append({"crew_id": crew_id[:64], "state": "held", "reason": str(exc)[:512]})
        return records, errors

    def _request_name(self, crew_id, request_id):
        """Keep distinct crew/request pairs separate even with punctuation."""
        key = f"{identifier(crew_id)}:{identifier(request_id)}".encode()
        return f"{self._request_prefix(crew_id)}{hashlib.sha256(key).hexdigest()}.json"

    def _request_prefix(self, crew_id):
        """Partition discovery before parsing another crew's mutable records."""
        return f"request-{hashlib.sha256(identifier(crew_id).encode()).hexdigest()}-"

    def get_request(self, crew_id, request_id):
        """Read a request scoped to its immutable crew identity."""
        row = self._read(self._request_name(crew_id, request_id))
        if row is not None and (row.get("crew_id"), row.get("request_id")) != (
            crew_id,
            request_id,
        ):
            raise ValueError("crew request identity mismatch")
        return row

    def save_request(self, row):
        """Advance controller state without replacing input or helper custody."""
        if not isinstance(row, dict) or not set(_IMMUTABLE).issubset(row):
            raise ValueError("missing immutable request fields")
        old = self.get_request(row["crew_id"], row["request_id"])
        if old is not None and any(old.get(key) != row.get(key) for key in _IMMUTABLE):
            raise ValueError("immutable request input changed")
        if old is not None and any(key in old and old[key] != row.get(key) for key in _CUSTODY):
            raise ValueError("immutable request custody changed")
        return self._write(self._request_name(row["crew_id"], row["request_id"]), row)

    def list_requests(self, crew_id=None):
        """List bounded request state, optionally for one exact crew."""
        prefix = "request-" if crew_id is None else self._request_prefix(crew_id)
        rows = []
        for name in self._names(prefix):
            row = self._read(name)
            if row is None or not set(_IMMUTABLE).issubset(row):
                raise ValueError("missing immutable request fields")
            if (crew_id is not None and row["crew_id"] != crew_id) or self._request_name(
                row["crew_id"], row["request_id"]
            ) != name:
                raise ValueError("crew request identity mismatch")
            rows.append(row)
        return rows
