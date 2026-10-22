"""Fresh claim-bound silence observations for bounded, read-only crew diagnostics.

Session silence is a diagnostic signal, not useful-output proof or worker death.
This module only publishes/reads telemetry; it never changes cards or launches work.
The controller separately revalidates the complete current parent mandate.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, fields
from datetime import datetime, timezone

from .crew_store import CrewStore, digest, identifier
from .worker_watchdog import ProgressObservation, classify_progress

SCHEMA = "skfleet.crew-progress/v1"
MAX_SAMPLE_AGE_S = 600
MAX_HOSTS = 64
_KEYS = {"schema", "host", "observed_at", "source", "observation"}
_OBSERVATION_KEYS = {field.name for field in fields(ProgressObservation)}


def _timestamp(value):
    """Parse a bounded timezone-aware timestamp, rejecting implicit local time."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid progress timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("progress timestamp requires timezone")
    return stamp.astimezone(timezone.utc)


def _identity(value):
    """Allow exact worker identity strings, never paths or unbounded context."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError("invalid progress identity")
    return value


def _validate(row, host, card_id):
    """Validate the original observation without coercing malformed evidence."""
    if not isinstance(row, dict) or set(row) != _KEYS or row["schema"] != SCHEMA:
        raise ValueError("invalid crew progress record")
    if row["host"] != host or identifier(host) != host:
        raise ValueError("progress host mismatch")
    if row["source"] not in ("session-mtime", "workspace-mtime"):
        raise ValueError("invalid progress source")
    sample = _timestamp(row["observed_at"])
    data = row["observation"]
    if not isinstance(data, dict) or set(data) != _OBSERVATION_KEYS:
        raise ValueError("invalid progress observation")
    for key in ("owner", "session_id", "claim_revision", "expected_claim_revision"):
        _identity(data[key])
    if (
        not isinstance(card_id, str)
        or not re.fullmatch(r"[0-9a-f]{8}", card_id)
        or data["card_id"] != card_id
        or data["claim_revision"] != data["expected_claim_revision"]
    ):
        raise ValueError("progress card or claim mismatch")
    for key in ("process_alive", "session_alive"):
        if data[key] is not None and type(data[key]) is not bool:
            raise ValueError("invalid progress liveness field")
    if type(data["terminal_evidence_seen"]) is not bool:
        raise ValueError("invalid terminal evidence field")
    size = data["transcript_bytes"]
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError("invalid transcript size")
    if data["progress_at"] is not None and _timestamp(data["progress_at"]) > sample:
        raise ValueError("progress is newer than its observation")
    return ProgressObservation(**data), sample


class _ProgressStore(CrewStore):
    """Reuse bounded nofollow reads, local locking and atomic publication."""

    def __init__(self, paths, host):
        super().__init__(paths, host)
        self.directory = paths.root.absolute() / "crew-progress" / self.node


def publish_progress(paths, observation, *, host, observed_at, source, receipt_local, claim_owner):
    """Persist only a local admission with its freshly observed exact owner/claim.

    Returns False for unbound observations or an older replay. No card state is
    read or changed here; the caller supplies its existing fresh claim readback.
    """
    if receipt_local is not True or not isinstance(observation, ProgressObservation):
        return False
    if claim_owner != observation.owner:
        return False
    row = {
        "schema": SCHEMA,
        "host": host,
        "observed_at": observed_at,
        "source": source,
        "observation": asdict(observation),
    }
    current, sample = _validate(row, host, observation.card_id)
    store = _ProgressStore(paths, host)
    name = f"{observation.card_id}.json"
    with store.lock():
        previous = store._read(name)
        if previous is not None:
            old, old_sample = _validate(previous, host, observation.card_id)
            if old_sample > sample or (old_sample == sample and previous != row):
                return False
            same_session = (old.claim_revision, old.session_id) == (
                current.claim_revision,
                current.session_id,
            )
            if (
                same_session
                and old.progress_at is not None
                and current.progress_at is not None
                and _timestamp(old.progress_at) > _timestamp(current.progress_at)
            ):
                return False
        store._write(name, row)
    return True


def _host_names(paths):
    """List only bounded host directories; never enumerate cards or workspaces."""
    store = _ProgressStore(paths, "inventory")
    store.directory = paths.root.absolute() / "crew-progress"
    with store._reading() as directory:
        if directory is None:
            return []
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(names) >= MAX_HOSTS:
                    raise ValueError("progress host bound exceeded")
                identifier(entry.name)
                if not entry.is_dir(follow_symlinks=False):
                    raise ValueError("unsafe progress host directory")
                names.append(entry.name)
        return sorted(names)


def stalled_evidence(paths, manifest, now):
    """Return one stable silence digest for a fresh, current crew parent sample.

    A newer observation suppresses older ones across hosts. Sample time is not in
    the digest, so repeated unchanged telemetry cannot create repeated helpers.
    The caller must revalidate the sealed mandate against the current parent.
    Missing, malformed, stale or conflicting telemetry yields no automatic work.
    """
    try:
        packet = manifest["packet"]
        card_id = packet["parent_id"]
        if not isinstance(card_id, str) or not re.fullmatch(r"[0-9a-f]{8}", card_id):
            return None
        now = _timestamp(now.isoformat())
        samples = []
        for host in _host_names(paths):
            row = _ProgressStore(paths, host)._read(f"{card_id}.json")
            if row is None:
                continue
            observation, sample = _validate(row, host, card_id)
            if sample > now:
                return None
            samples.append((sample, row, observation))
        if not samples:
            return None
        latest = max(sample for sample, _, _ in samples)
        if (now - latest).total_seconds() > MAX_SAMPLE_AGE_S:
            return None
        latest_rows = [(row, obs) for sample, row, obs in samples if sample == latest]
        if len(latest_rows) != 1:
            return None
        row, observation = latest_rows[0]
        if (
            observation.owner != manifest["authorizer"]
            or observation.claim_revision != packet["parent_claim_revision"]
            or observation.session_alive is not True
            or row["source"] != "session-mtime"
            or classify_progress(observation, now=latest) != "progress-stale"
        ):
            return None
        return digest({key: value for key, value in row.items() if key != "observed_at"})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
