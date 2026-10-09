"""Fair ordering for Niobe's builder offer loop.

The offer loop runs after the cycle's main selection and stops silently when the
cycle budget is spent. Walked in plain pool order it re-processed the same dead
head of the queue every cycle (two Node-environment cards and three not-ready
cards on 2026-10-09) and never reached the dozens of recipe-ready SKLegal cards
behind them. Remember each card's last offer outcome, skip recent dead outcomes
for a while, and serve the least recently tried card first.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEAD_PREFIXES = (
    "ineligible:",
    "deferred:card-not-ready-or-owned",
    "deferred:recent-qualification-failure",
)
DEAD_SECONDS = 1800
KEEP_SECONDS = 86400


def load(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _last(memory: dict, card: str) -> tuple[float, str]:
    row = memory.get(card)
    if not isinstance(row, dict):
        return 0.0, ""
    try:
        return float(row.get("ts", 0)), str(row.get("state", ""))
    except (TypeError, ValueError):
        return 0.0, ""


def skip(memory: dict, card: str, now: float | None = None) -> bool:
    """True while a card's last outcome is dead and recent."""
    now = time.time() if now is None else now
    ts, state = _last(memory, card)
    return state.startswith(DEAD_PREFIXES) and now - ts < DEAD_SECONDS


def order(candidates, memory: dict, qualified, now: float | None = None) -> list:
    """Qualified first, then never-tried, then least recently tried; drop dead ones."""
    now = time.time() if now is None else now
    # A recent dead outcome wins over a profile file: a stale Node profile
    # (dbe7c7b1, bc9e7038) or a not-ready backlog card still has one, and
    # treating "has a profile" as "qualified" kept them at the head every cycle.
    alive = [c for c in candidates if not skip(memory, c[2], now)]
    return sorted(alive, key=lambda c: (not qualified(c[2]), _last(memory, c[2])[0]))


def record(path: Path, card: str, state: str, now: float | None = None) -> None:
    """Remember one outcome; bounded and written atomically."""
    now = time.time() if now is None else now
    path = Path(path)
    memory = {
        key: row
        for key, row in load(path).items()
        if isinstance(row, dict) and now - float(row.get("ts", 0) or 0) < KEEP_SECONDS
    }
    memory[card] = {"ts": now, "state": str(state)[:120]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(memory, sort_keys=True))
    os.replace(tmp, path)
