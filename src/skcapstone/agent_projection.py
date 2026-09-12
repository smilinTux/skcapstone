"""Truthful read-only projection of historical agent heartbeat records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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


def _projection_age(value: object, now: datetime) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        age = (now - observed).total_seconds()
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None or age < 0:
        return None
    return age


def _age_bucket(age: float | None) -> str:
    if age is None:
        return "unknown"
    if age < 3600:
        return "under_1h"
    if age < 86400:
        return "1h_to_24h"
    if age < 30 * 86400:
        return "1d_to_30d"
    return "30d_plus"


def projection_summary(
    directory: Path,
    *,
    now: datetime | None = None,
    folded_claim: Callable[[str], tuple[str | None, str]],
) -> dict[str, dict[str, int]]:
    """Group historical projections without treating them as live capacity."""

    observed_now = now or datetime.now(timezone.utc)
    summary: dict[str, dict[str, int]] = {
        name: {} for name in ("idle", "stale", "malformed", "task_bearing_non_exact")
    }

    def add(group: str, bucket: str) -> None:
        summary[group][bucket] = summary[group].get(bucket, 0) + 1

    if not directory.is_dir():
        return summary
    for path in sorted(directory.glob("*.json")):
        if ".sync-conflict-" in path.name:
            add("malformed", "unknown")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            agent = payload.get("agent")
            if not isinstance(payload, dict) or not isinstance(agent, str) or agent != path.stem:
                raise ValueError("projection identity mismatch")
        except (OSError, json.JSONDecodeError, AttributeError, ValueError):
            add("malformed", "unknown")
            continue
        age = _projection_age(payload.get("last_seen"), observed_now)
        bucket = _age_bucket(age)
        task = payload.get("current_task")
        if task in (None, ""):
            group = "stale" if age is None or age > STALE_AFTER_SECONDS else "idle"
            add(group, bucket)
            continue
        if not isinstance(task, str):
            add("malformed", bucket)
            continue
        revision = str(payload.get("_claim_revision") or "")
        try:
            owner, current_revision = folded_claim(task)
        except (OSError, TypeError, ValueError):
            add("malformed", bucket)
            continue
        if owner != agent or not revision or current_revision != revision:
            add("task_bearing_non_exact", bucket)
        elif age is None or age > STALE_AFTER_SECONDS:
            add("stale", bucket)
    return summary
