"""Shared fixtures for the Mero blocker census suite.

Card 8fa7d8eb moved these helpers verbatim from the single 669-line test
module of card 2516480b so each test module stays under the 500-line bound.
Every fixture builds CardStore JSON through the real store serializer and
reads every line back through ``json.loads``; nothing is ever concatenated.

The fixture clock is pinned (NOW = 2026-09-02T12:00Z) and the fixture events
pin their ``ts`` explicitly, because the store stamps real wall-clock time by
default and a fixture built from the real clock detonates whenever the real
clock passes the test's pinned ``later`` instant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

__all__ = ["NOW", "_fixed_now", "_home", "_add", "_recs", "_census", "build_board", "_board_store"]


def _fixed_now():
    return lambda: NOW


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "skcapstone"
    (home / "coordination").mkdir(parents=True)
    (home / "cards").mkdir()
    return home


def _add(store: CardStore, cid: str, action: str, **payload) -> dict:
    return store.append_event(cid, action, payload.pop("writer", "jarvis"), **payload)


def _recs(store: CardStore, cid: str) -> list[dict]:
    rows = [e for e in store._read_events(cid) if e.get("action") == mc.RECOMMENDATION_EVENT]
    return sorted(rows, key=lambda e: str(e.get("ts")))


def _census(home: Path, **kwargs) -> mc.MeroBlockerCensus:
    kwargs.setdefault("now", _fixed_now())
    return mc.MeroBlockerCensus(home, **kwargs)


def _board_store(tmp_path: Path) -> CardStore:
    """A fresh store on a fresh home carrying the small shared board."""
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    build_board(store)
    return store


def build_board(store: CardStore) -> None:
    """The small shared board: one done dep, one voided card, one stuck card."""
    _add(store, "aaaa0001", "move", ts=NOW.isoformat(), column="done", order=0)

    store.create(
        CardCore(id="bbbb0002", title="stuck", created_by="jarvis", dependencies=["aaaa0001"])
    )
    _add(
        store,
        "bbbb0002",
        "claim",
        ts=NOW.isoformat(),
        writer="worker-a",
        owner="worker-a",
        claim_revision="rev-bbbb-1",
        transition_id="t-bbbb-claim",
    )
    _add(
        store,
        "bbbb0002",
        "verdict",
        ts=NOW.isoformat(),
        writer="worker-a",
        verdict="BLOCKED. blocked_on: card referent=ac:2",
        evidence_link="/tmp/e-bbbb.json",
        artifact_sha256="a" * 64,
    )

    store.create(CardCore(id="cccc0003", title="gate", created_by="jarvis"))
    _add(
        store,
        "cccc0003",
        "void",
        ts=NOW.isoformat(),
        reason="Superseded by aaaa0001 which is COMPLETE",
    )
