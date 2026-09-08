#!/usr/bin/env python3
"""Incrementally count BLOCKED skmail verdicts by worker lane and reason."""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LANES = ("qwen", "glm", "kimi", "codex", "escalate")
REASONS = (
    "dependency",
    "capability",
    "context",
    "credentials",
    "test-failure",
    "other",
)
_REASON_PATTERNS = (
    ("dependency", re.compile(r"\b(?:dependenc(?:y|ies)|blocked_on\s*[=:]\s*dependency)\b", re.I)),
    ("capability", re.compile(r"\b(?:capabilit(?:y|ies)|blocked_on\s*[=:]\s*capability)\b", re.I)),
    ("context", re.compile(r"\b(?:context(?:\s+window)?|token\s+limit|out\s+of\s+context)\b", re.I)),
    ("credentials", re.compile(r"\b(?:credentials?|auth(?:entication|orization)?|login|token|permission)\b", re.I)),
    ("test-failure", re.compile(r"\b(?:test[- ]failure|tests?\s+fail(?:ed|ing|ure)?|pytest\s+fail)\b", re.I)),
)
_LANE_PATTERN = re.compile(r"(?:^|[-_])(qwen|glm|kimi|codex|escalate|esc)(?:[-_]|$)", re.I)
_BLOCKED_PATTERN = re.compile(r"^\s*BLOCKED\b", re.I)


def empty_counts() -> dict[str, dict[str, int]]:
    """Return a stable zero-filled counter matrix."""
    return {lane: {reason: 0 for reason in REASONS} for lane in LANES}


def blocked_reason(body: str) -> str:
    """Classify a BLOCKED body using the first controlled reason keyword."""
    for reason, pattern in _REASON_PATTERNS:
        if pattern.search(body):
            return reason
    return "other"


def verdict_lane(message: dict[str, Any]) -> str | None:
    """Return the controlled lane explicitly recorded or encoded by the sender."""
    explicit = message.get("lane")
    if isinstance(explicit, str):
        lane = explicit.strip().lower()
        if lane == "esc":
            lane = "escalate"
        if lane in LANES:
            return lane
    sender = message.get("from")
    if not isinstance(sender, str):
        return None
    match = _LANE_PATTERN.search(sender)
    if not match:
        return None
    lane = match.group(1).lower()
    return "escalate" if lane == "esc" else lane


def count_verdicts(messages: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count synthetic or decoded mailbox messages without inferring outcomes."""
    counts = empty_counts()
    for message in messages:
        body = message.get("body")
        lane = verdict_lane(message)
        if lane is not None and isinstance(body, str) and _BLOCKED_PATTERN.search(body):
            counts[lane][blocked_reason(body)] += 1
    return counts


def _merge(target: dict[str, dict[str, int]], additions: dict[str, dict[str, int]]) -> None:
    """Add one stable counter matrix into another in place."""
    for lane in LANES:
        for reason in REASONS:
            target[lane][reason] += additions[lane][reason]


def _load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, value: Any) -> None:
    """Atomically serialize JSON so readers never observe a partial artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _new_messages(mailbox: Path, cursor: int) -> tuple[list[dict[str, Any]], int]:
    """Parse complete JSON lines after cursor and preserve malformed tails for retry."""
    messages: list[dict[str, Any]] = []
    size = mailbox.stat().st_size
    if cursor > size:
        cursor = 0
    with mailbox.open("rb") as stream:
        stream.seek(cursor)
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line:
                return messages, stream.tell()
            if not line.endswith(b"\n"):
                return messages, start
            try:
                decoded = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return messages, start
            if isinstance(decoded, dict):
                messages.append(decoded)


def update(mail_dir: Path, evidence_dir: Path, now: datetime | None = None) -> str | None:
    """Count only newly appended mail and return a prior-day summary once per day."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(timezone.utc).date().isoformat()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    lock_path = evidence_dir / ".lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = evidence_dir / ".cursor.json"
        state = _load_json(state_path, {})
        if not isinstance(state, dict) or "files" not in state:
            # First observation establishes the no-backfill boundary.
            files = {str(path): path.stat().st_size for path in mail_dir.glob("*.jsonl")}
            _write_json(state_path, {"files": files, "last_summary_day": day})
            _write_json(evidence_dir / (day + ".json"), {"date": day, "counts": empty_counts()})
            return None

        cursors = state.get("files", {})
        if not isinstance(cursors, dict):
            cursors = {}
        daily_path = evidence_dir / (day + ".json")
        daily = _load_json(daily_path, {"date": day, "counts": empty_counts()})
        counts = daily.get("counts") if isinstance(daily, dict) else None
        if not isinstance(counts, dict):
            counts = empty_counts()
        normalized = empty_counts()
        for lane in LANES:
            for reason in REASONS:
                try:
                    normalized[lane][reason] = int(counts.get(lane, {}).get(reason, 0))
                except (AttributeError, TypeError, ValueError):
                    normalized[lane][reason] = 0

        for path in mail_dir.glob("*.jsonl"):
            key = str(path)
            # Files appearing after the initial no-backfill snapshot are new
            # evidence, so consume them from byte zero.
            cursor = int(cursors.get(key, 0))
            messages, cursors[key] = _new_messages(path, cursor)
            _merge(normalized, count_verdicts(messages))

        _write_json(daily_path, {"date": day, "counts": normalized})
        previous = state.get("last_summary_day")
        summary = None
        if isinstance(previous, str) and previous < day:
            artifact = _load_json(evidence_dir / (previous + ".json"), None)
            if isinstance(artifact, dict):
                compact = json.dumps(artifact.get("counts", {}), separators=(",", ":"), sort_keys=True)
                summary = "VERDICT_STATS|date=%s|counts=%s" % (previous, compact)
        state = {"files": cursors, "last_summary_day": day}
        _write_json(state_path, state)
        return summary
