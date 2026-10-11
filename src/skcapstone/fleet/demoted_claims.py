"""Release demoted claims: ready cards still owned by an agent that moved on.

skcoord keeps one current task per agent. A new claim demotes the previous
current task to ready but leaves it claimed, by design. The scheduler treats an
owned card as claimed, so every demoted card is excluded from dispatch until
someone releases it. On chi (2026-10-11) 84 ready cards were held this way:
73 by ``jarvis`` and 7 by ``niobe``, some for four days.

A claim is released only when all of these hold:
- the card is in ``ready`` (a demoted claim, never active work);
- the owner is not a fleet worker (``pi-*``) or a requalification claimant,
  which the reaper and requalification already govern;
- the owner's current task is a different card;
- the exact claim and the owner's last activity on the card are older than
  the idle window.

Each release goes through the audited board CLI with the exact owner and claim
revision, so a newer claim generation is never released, and the card is put
back in ``ready``, where its owner left it.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

IDLE = timedelta(hours=6)
WORKER_PREFIXES = ("pi-", "niobe-requal-")
REASON = "demoted claim: owner moved on, card idle in ready"


def _skcapstone() -> str:
    return shutil.which("skcapstone") or str(Path.home() / ".skenv/bin/skcapstone")


def _ts(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _actor(event: dict) -> str | None:
    return event.get("writer") or event.get("owner") or event.get("actor")


def demoted_claims(store, current_tasks: dict, *, now: datetime, idle: timedelta = IDLE):
    """Yield (card_id, owner, claim_revision) for each releasable demoted claim."""
    for card in store.list_cards():
        owner = card.owner
        status = getattr(card.status, "value", card.status)
        revision = (card.meta or {}).get("_claim_revision")
        if (
            card.archived
            or status != "ready"
            or not owner
            or owner.startswith(WORKER_PREFIXES)
            or not revision
            or current_tasks.get(owner) == card.id
        ):
            continue
        events = store._read_events(card.id)
        claims = [
            event
            for event in events
            if event.get("action") == "claim"
            and (event.get("claim_revision") or event.get("event_id")) == revision
        ]
        if len(claims) != 1:
            continue
        claimed_at = _ts(claims[0].get("ts"))
        recent = [_ts(event.get("ts")) for event in events if _actor(event) == owner]
        if claimed_at is None or now - claimed_at < idle:
            continue
        if any(stamp is None or now - stamp < idle for stamp in recent):
            continue
        yield card.id, owner, revision


def release_demoted_claims(
    home: Path,
    *,
    actor: str = "niobe",
    limit: int = 4,
    now: datetime | None = None,
    run=subprocess.run,
) -> list[dict]:
    """Release up to ``limit`` demoted claims; each result names its card and state."""
    from skcoord.card_store import CardStore
    from skcoord.coordination import Board

    home = Path(home)
    now = now or datetime.now(timezone.utc)
    current = {agent.agent: agent.current_task for agent in Board(home).load_agents()}
    skc = _skcapstone()
    results = []
    for card_id, owner, revision in demoted_claims(CardStore(home), current, now=now):
        if len(results) >= limit:
            break
        released = run(
            [
                skc,
                "coord",
                "release-claim",
                card_id,
                "--owner",
                owner,
                "--expected-claim-revision",
                revision,
                "--agent",
                actor,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if released.returncode:
            results.append({"card": card_id, "owner": owner, "state": "refused"})
            continue
        # release-claim always lands a card in backlog; restore ready.
        moved = run(
            [skc, "coord", "move", card_id, "ready", "--agent", actor],
            capture_output=True,
            text=True,
            timeout=60,
        )
        state = "released" if moved.returncode == 0 else "released-in-backlog"
        results.append({"card": card_id, "owner": owner, "state": state})
    return results
