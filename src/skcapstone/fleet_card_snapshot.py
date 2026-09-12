"""Bounded immutable card input for one fleet rotation cycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from skcoord.card_store import CardStore


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def thaw_snapshot_value(value: object) -> object:
    """Return a mutable JSON-shaped copy of a frozen snapshot value."""
    if isinstance(value, Mapping):
        return {key: thaw_snapshot_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_snapshot_value(item) for item in value]
    return value


@dataclass(frozen=True)
class FleetCardSnapshot:
    """One bounded, immutable view of card cores and native event streams."""

    generation: str
    cores: Mapping[str, Mapping[str, object]]
    events: Mapping[str, tuple[Mapping[str, object], ...]]
    byte_count: int


def acquire_fleet_card_snapshot(
    cards_dir: Path, *, max_cards: int = 4096, max_bytes: int = 64 * 1024 * 1024
) -> FleetCardSnapshot:
    """Read each card core and native event file at most once for one cycle.

    Mutation-time admission checks must bypass this snapshot and read current
    CardStore state. The snapshot only coalesces equivalent selector scans.

    Args:
        cards_dir: CardStore ``cards`` directory.
        max_cards: Maximum card directories admitted to a cycle.
        max_bytes: Maximum source bytes admitted to a cycle.

    Returns:
        Immutable cores, event rows, byte count, and content generation.

    Raises:
        ValueError: If either configured bound is exceeded.
    """

    store = CardStore(cards_dir.parent)
    cores: dict[str, Mapping[str, object]] = {}
    events: dict[str, tuple[Mapping[str, object], ...]] = {}
    digest = hashlib.sha256()
    byte_count = 0
    card_ids = store.list_card_ids()
    if len(card_ids) > max_cards:
        raise ValueError("fleet card snapshot exceeds card bound")
    for card_id in card_ids:
        core = store._load_core(card_id)
        if core is None:
            continue
        core_bytes = json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
        byte_count += len(core_bytes)
        if byte_count > max_bytes:
            raise ValueError("fleet card snapshot exceeds byte bound")
        if not isinstance(core, dict) or core.get("id") != card_id:
            raise ValueError(f"card core identity mismatch: {card_id}")
        rows = store._read_events(card_id)
        event_bytes = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        byte_count += len(event_bytes)
        if byte_count > max_bytes:
            raise ValueError("fleet card snapshot exceeds byte bound")
        rows.sort(
            key=lambda row: (
                str(row.get("ts") or ""),
                str(row.get("writer") or ""),
                row.get("seq", 0),
            )
        )
        digest.update(card_id.encode())
        digest.update(b"\0" + core_bytes + b"\0")
        for row in rows:
            digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
            digest.update(b"\n")
        cores[card_id] = _freeze(core)
        events[card_id] = tuple(_freeze(row) for row in rows)
    return FleetCardSnapshot(
        generation=digest.hexdigest(),
        cores=MappingProxyType(cores),
        events=MappingProxyType(events),
        byte_count=byte_count,
    )
