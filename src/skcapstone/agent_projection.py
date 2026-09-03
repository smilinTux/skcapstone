"""Truthful read-only projection of historical agent heartbeat records."""

from __future__ import annotations

from datetime import datetime, timezone

STALE_AFTER_SECONDS = 15 * 60


def display_state(agent, *, now: datetime | None = None) -> str:
    """Return display liveness without rewriting the historical agent record."""

    if agent.state.value == "offline":
        return "offline"
    try:
        observed = datetime.fromisoformat(agent.last_seen.replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return "stale"
    if age < 0 or age > STALE_AFTER_SECONDS:
        return "stale"
    return "active" if agent.current_task else "idle"
