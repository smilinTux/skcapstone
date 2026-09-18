"""Last-activity claim TTL (opt-in, mode-gated, dry-run safe).

The reaper's original claim expiry read the claim timestamp: a worker that
ran 17 hours with a live log kept its claim only because the log had ANY
content, and nobody noticed. That is the bug this module closes. The fix
replaces the clock: a claim expires when the worker has written NO card
event -- move, describe, evidence, or verdict -- for the TTL window.

Wiring
--------
    skfleet-rotate.py calls _expire_idle_claims() (module scope, right after
    reap_dead_claims()). It is opt-in: nothing runs unless
    ``SKFLEET_CLAIM_TTL_MODE`` is ``report`` or ``enforce``. ``report``
    logs and releases nothing; only ``enforce`` releases. Both are safe
    under ``DRY`` (no store mutations).

Environment
------------
    SKFLEET_CLAIM_TTL_MODE  off (default) | report | enforce
    SKFLEET_CLAIM_TTL_H     TTL in hours, default 12. Values below 1 hour
                             are clamped to 1 to keep a full rotation cycle
                             inside the window.
    SKFLEET_CLAIM_TTL_MIN_HOUR  minimum hours, default 1 (clamp floor)

Data sources
-------------
    The observe() helper derives last-activity from card events in BOTH
    CardStore files:
      ~/.skcapstone/cards.jsonl   (coordination events)
      ~/.skcapstone/status.jsonl  (card state transitions)
    A worker actively writing events has a recent _last_activity_at; a stuck
    worker's last-activity timestamp goes stale and the TTL releases the
    claim back to the pool.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

DEFAULT_TTL_SECONDS: int = 12 * 3600
_MIN_TTL_SECONDS: int = 3600          # one-hour floor
_VALID_MODES = ("off", "report", "enforce")

# CardStore event types that count as worker activity. Any of these events
# means the worker is alive and making progress.
_ACTIVITY_EVENT_TYPES = frozenset({
    "card.move", "card.describe", "card.evidence", "card.verdict",
    "card.coordination", "card.link", "card.status", "card.label",
    "card.depends_on",
})


def ttl_seconds_from_env(env: Optional[dict[str, Any]] = None) -> int:
    """TTL in seconds, clamped to a minimum of one hour.

    Reads ``SKFLEET_CLAIM_TTL_H`` (hours, float, default 12, min 1).
    """
    env = env if env is not None else os.environ
    raw = str(env.get("SKFLEET_CLAIM_TTL_H", "")).strip()
    hours = 12.0
    if raw:
        try:
            hours = float(raw)
        except ValueError:
            pass
    return max(int(max(hours, 1.0) * 3600), _MIN_TTL_SECONDS)


def mode_from_env(env: Optional[dict[str, Any]] = None) -> str:
    """Return the active TTL mode. One of off / report / enforce."""
    env = env if env is not None else os.environ
    raw = str(env.get("SKFLEET_CLAIM_TTL_MODE", "")).strip().lower()
    if raw not in _VALID_MODES:
        return "off"
    return raw


def _read_jsonl_events(path: Path) -> List[dict[str, Any]]:
    """Read a CardStore JSONL file, tolerating blank lines and malformed JSON."""
    if not path.is_file():
        return []
    events = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    events.append(rec)
            except json.JSONDecodeError:
                continue
    return events


def _last_activity_at(rec: dict[str, Any]) -> Optional[int]:
    """Extract _last_activity_at (Unix timestamp) from a card event record.

    Accepts the key at top level or inside an envelope dict. Returns None
    when absent or malformed.
    """
    # Top level
    ts = rec.get("_last_activity_at")
    if ts is None:
        # Envelope form
        ev = rec.get("event")
        if isinstance(ev, dict):
            ts = ev.get("_last_activity_at")
    if ts is None:
        return None
    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


def _extract_card_id(rec: dict[str, Any]) -> Optional[str]:
    """Extract the card id from a card event record."""
    cid = rec.get("card") or rec.get("card_id") or rec.get("cardId")
    if cid is None:
        ev = rec.get("event")
        if isinstance(ev, dict):
            cid = ev.get("card") or ev.get("card_id")
    return str(cid) if cid is not None else None


def observe(root: Path) -> List[dict[str, Any]]:
    """Derive per-card last-activity observations from CardStore event files.

    Reads both CardStore JSONL files under ``root/.skcapstone/`` and
    returns a list of observation dicts with keys:
        card_id, last_activity_at, source_file

    Cards with no activity events are omitted (nothing to measure).
    A card's most recent activity event wins.
    """
    root = Path(root)
    stores = [
        root / ".skcapstone" / "cards.jsonl",
        root / ".skcapstone" / "status.jsonl",
    ]
    # card_id -> (ts, source_file)
    best: dict[str, tuple[Optional[int], str]] = {}
    for store_path in stores:
        for rec in _read_jsonl_events(store_path):
            cid = _extract_card_id(rec)
            if cid is None:
                continue
            ts = _last_activity_at(rec)
            if ts is None:
                continue
            prev = best.get(cid)
            if prev is None or ts > prev[0] or prev[0] is None:
                best[cid] = (ts, store_path.name)

    return [
        {"card_id": cid, "last_activity_at": ts, "source_file": src}
        for cid, (ts, src) in sorted(best.items())
    ]


def evaluate(
    observations: Sequence[dict[str, Any]],
    now: float,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> List[dict[str, Any]]:
    """Return the list of cards whose last activity is older than TTL.

    Each verdict dict has: card_id, last_activity_at, age_seconds,
    ttl_seconds, stale: True.
    """
    verdicts = []
    for obs in observations:
        ts = obs.get("last_activity_at")
        if ts is None:
            continue
        age = now - ts
        if age > ttl_seconds:
            verdicts.append({
                "card_id": obs.get("card_id"),
                "last_activity_at": ts,
                "age_seconds": age,
                "ttl_seconds": ttl_seconds,
                "stale": True,
                "source_file": obs.get("source_file", "unknown"),
            })
    return verdicts


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "evaluate",
    "mode_from_env",
    "observe",
    "ttl_seconds_from_env",
]
