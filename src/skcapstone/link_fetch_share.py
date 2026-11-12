"""One forge fetch per link-producer run, shared by both stages, with a request budget log.

The producer wrapper runs the lineage reconciler and then the observation producer,
and both read every open PR from the forges. On 2026-10-13 that doubled the skgit
request count (about 160 per stage) and the wall time, which led to the 180s start
timeout and, at higher concurrency, a forge 403. The wrapper now creates a private
per-run cache file; the first stage stores each repository's rows, the second reuses
them.

Safety rules, each one a refusal to serve a cached row:

* The cache path and run id come only from the wrapper's environment. No path, no
  run id, or a run id that differs from the file's means a live fetch.
* The file must be a regular, non-symlink file owned by this user with no group or
  other permissions; anything else is ignored.
* An entry older than ``MAX_AGE`` seconds, or stamped in the future, is ignored.
  The cache never outlives the run: the wrapper deletes it, and the run id is fresh
  for every run, so a later run can never read an earlier run's entries.
* A failed live fetch is never cached.

The forge's pull and status endpoints send no ETag or Last-Modified (probed
2026-10-13), so conditional requests are not available for them.
"""

from __future__ import annotations

import atexit
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import forgejo

ENV_PATH = "SKFLEET_LINK_FETCH_CACHE"
ENV_RUN = "SKFLEET_LINK_RUN_ID"
MAX_AGE = 600.0
SCHEMA = "skfleet.link-fetch-share/v1"


def _read_cache(path: str, run_id: str) -> dict[str, Any]:
    """Return the entries of a trusted cache file, or ``{}`` when anything is off."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 64 * 1024 * 1024
        ):
            return {}
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError):
        return {}
    finally:
        os.close(fd)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("run_id") != run_id:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _write_cache(path: str, run_id: str, entries: Mapping[str, Any]) -> None:
    """Replace the cache atomically, owner-only. Failure only costs the reuse."""
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": SCHEMA, "run_id": run_id, "entries": entries}, handle)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


class SharedFetch:
    """Wrap a read-only PR connector with the per-run cache and a budget log line."""

    def __init__(self, connector: Any, stage: str, *, clock=time.time) -> None:
        self.connector, self.stage, self.clock = connector, stage, clock
        self.path = os.environ.get(ENV_PATH, "")
        self.run_id = os.environ.get(ENV_RUN, "")
        self.live = 0
        self.reused = 0
        self.started = clock()
        self.requests_before = forgejo.request_count()

    def list_open(self, repository: str) -> Sequence[Mapping[str, Any]]:
        usable = bool(self.path and self.run_id)
        entries = _read_cache(self.path, self.run_id) if usable else {}
        entry = entries.get(repository)
        if isinstance(entry, dict) and isinstance(entry.get("rows"), list):
            try:
                age = self.clock() - datetime.fromisoformat(entry["fetched_at"]).timestamp()
            except (KeyError, TypeError, ValueError):
                age = None
            if age is not None and -30.0 <= age <= MAX_AGE:
                self.reused += 1
                return entry["rows"]
        rows = list(self.connector.list_open(repository))
        self.live += 1
        if usable:
            fetched = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
            entries[repository] = {"fetched_at": fetched, "rows": rows}
            _write_cache(self.path, self.run_id, entries)
        return rows

    def log_at_exit(self) -> "SharedFetch":
        """Print the budget line when the stage ends, including on error."""
        atexit.register(self.log_budget)
        return self

    def log_budget(self) -> None:
        wall = self.clock() - self.started
        used = forgejo.request_count() - self.requests_before
        print(
            f"link_fetch_budget stage={self.stage} forge_requests={used} "
            f"repos_live={self.live} repos_reused={self.reused} wall_seconds={wall:.1f}",
            file=sys.stderr,
            flush=True,
        )


def fresh_run_env(directory: Path, run_id: str) -> dict[str, str]:
    """Environment entries the wrapper passes to both stages."""
    return {ENV_PATH: str(directory / "fetch.json"), ENV_RUN: run_id}
