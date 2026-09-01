"""Read-only observed-charter reporting over the CardStore."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

_PREFIX = re.compile(r"^\[([^\]]+)\]")
_STATE_ACTIONS = {"complete", "move", "archive", "reopen", "void"}
_EVIDENCE_KEYS = {"evidence", "evidence_sha256"}


def load_normalization(path: Path | None = None) -> dict[str, Any]:
    """Load the proposed, unratified workstream normalization data."""
    source = path or Path(str(files("skcapstone.data").joinpath("mero_workstreams.json")))
    return json.loads(source.read_text(encoding="utf-8"))


def title_family(title: str) -> str | None:
    """Return the first hyphen-delimited family in a well-formed leading token."""
    match = _PREFIX.match(title.strip())
    return match.group(1).split("-", 1)[0].strip().upper() if match else None


def _signals(
    events: Iterable[dict[str, Any]], links: dict[str, Any], now: datetime
) -> tuple[bool, bool, datetime | None]:
    """Apply the P0 rubric while keeping lifecycle and evidence independent."""
    events = list(events)
    claimed_recently = False
    structural: list[tuple[dict[str, Any], datetime]] = []
    linked = {key for key, value in links.items() if value}
    for event in events:
        try:
            timestamp = datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        action = event.get("action")
        if action == "claim" and timestamp >= now - timedelta(days=7):
            claimed_recently = True
        if action == "link" and event.get("link_value"):
            linked.add(str(event.get("link_key")))
        if action in _STATE_ACTIONS:
            structural.append((event, timestamp))
    if not structural:
        return claimed_recently, False, None
    terminal, terminal_at = structural[-1]
    ended_done = terminal.get("action") == "complete" or (
        terminal.get("action") == "move" and terminal.get("column") == "done"
    )
    delivered = ended_done and "verdict" in linked and bool(linked & _EVIDENCE_KEYS)
    return claimed_recently, delivered, terminal_at if delivered else None


def observe(store: Any, *, now: datetime | None = None, sample_size: int = 10) -> dict[str, Any]:
    """Build the observed charter without writing cards or events."""
    now = now or datetime.now(timezone.utc)
    config = load_normalization()
    aliases = config["aliases"]
    classes = config["class_tokens"]
    streams: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "card_count": 0,
            "delivered_cards": 0,
            "claimed_last_7_days": 0,
            "most_recent_delivered_card": None,
            "raw_families": set(),
        }
    )
    unprefixed = []
    excluded: dict[str, int] = defaultdict(int)
    cards = store.list_cards()

    for card in cards:
        family = title_family(card.title)
        if family is None:
            unprefixed.append({"id": card.id, "title": card.title})
            continue
        if family in classes:
            excluded[family] += 1
            continue
        workstream = aliases.get(family, {}).get("workstream", family)
        row = streams[workstream]
        row["card_count"] += 1
        row["raw_families"].add(family)
        claimed, delivered, delivered_at = _signals(store._read_events(card.id), card.links, now)
        row["claimed_last_7_days"] += int(claimed)
        row["delivered_cards"] += int(delivered)
        if delivered_at and (
            row["most_recent_delivered_card"] is None
            or delivered_at.isoformat() > row["most_recent_delivered_card"]
        ):
            row["most_recent_delivered_card"] = delivered_at.isoformat()

    output = []
    for name, row in sorted(streams.items(), key=lambda item: (-item[1]["card_count"], item[0])):
        latest = row["most_recent_delivered_card"]
        stalled = row["claimed_last_7_days"] > 0 and (
            latest is None or datetime.fromisoformat(latest) < now - timedelta(days=30)
        )
        output.append(
            {
                "workstream": name,
                "card_count": row["card_count"],
                "delivery_fraction_provisional": row["delivered_cards"] / row["card_count"],
                "claimed_last_7_days": row["claimed_last_7_days"],
                "most_recent_delivered_card": latest,
                "high_energy_no_delivery_30_days": stalled,
                "raw_families": sorted(row["raw_families"]),
            }
        )

    total = len(cards)
    return {
        "artifact": "Mero P1 observed charter",
        "normalization": {
            "status": config["status"],
            "ratified": config["ratified"],
            "message": "Alias mapping is proposed and unratified.",
            "aliases": aliases,
        },
        "membership_status": "observed from title prefixes",
        "delivery_status": "PROVISIONAL pending P0 accuracy gate card 48136bad",
        "workstreams": output,
        "pathologies": [row for row in output if row["high_energy_no_delivery_30_days"]],
        "excluded_class_tokens": [
            {"token": token, "count": excluded.get(token, 0), "reason": reason}
            for token, reason in classes.items()
        ],
        "unprefixed": {
            "count": len(unprefixed),
            "fraction": len(unprefixed) / total if total else 0.0,
            "sample": unprefixed[:sample_size],
            "judgment": "genuine scatter across unrelated titles, not one coherent workstream",
        },
    }
