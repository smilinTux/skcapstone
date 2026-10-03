"""Bounded crash-loop backoff for sknoded healing (spec 3.3, R4).

Delays double from 10s to a 300s cap; after CRASH_LOOP_AFTER attempts in
one episode the service is declared CrashLooping and healing STOPS until
the unit is observed healthy for HEALTHY_RESET_S (or sknoded restarts).
Trackers are in-process state, like the events dedupe map: a daemon
restart forgives the episode, which is the desired manual-recovery lever.
"""

from __future__ import annotations

BACKOFF_BASE_S = 10.0
BACKOFF_CAP_S = 300.0
CRASH_LOOP_AFTER = 5
HEALTHY_RESET_S = 120.0

_trackers: dict[tuple[str, str], dict] = {}


def reset_trackers() -> None:
    """Clear all backoff state (tests, daemon restart)."""
    _trackers.clear()


def tracker(node: str, name: str) -> dict:
    """The mutable backoff record for one service on one node."""
    return _trackers.setdefault((node, name), {"attempts": 0, "last_attempt": 0.0})


def next_delay(attempts: int) -> float:
    """Seconds to wait before the next heal attempt (0 for the first)."""
    if attempts <= 0:
        return 0.0
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempts - 1)))


def allowed(track: dict, now: float) -> bool:
    """True when the backoff window for the next attempt has passed."""
    return (now - float(track["last_attempt"])) >= next_delay(int(track["attempts"]))


def record_attempt(track: dict, now: float) -> None:
    """Count one heal attempt (start or restart), successful or not."""
    track["attempts"] = int(track["attempts"]) + 1
    track["last_attempt"] = now


def record_healthy(track: dict, now: float) -> None:
    """Reset the episode after the unit has been stably healthy."""
    if track["attempts"] and (now - float(track["last_attempt"])) >= HEALTHY_RESET_S:
        track["attempts"] = 0
        track["last_attempt"] = 0.0


def is_crash_looping(track: dict) -> bool:
    """True when the bounded attempt budget for this episode is spent."""
    return int(track["attempts"]) >= CRASH_LOOP_AFTER
