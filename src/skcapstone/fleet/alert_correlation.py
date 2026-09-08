"""Fleet-wide failure correlation primitives.

Detection is deliberately local; filing/reconciliation consume immutable event
records from the shared store.  Syncthing is not treated as a lock or CAS.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

# Timestamps, process IDs, and card IDs are host/run specific noise.
_NOISE = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+Z-]+\b"),
    re.compile(r"\b(?:pid|process)[=: ]+\d+\b", re.I),
    re.compile(r"\b(?:card|task)[=: -]?[0-9a-f]{8}\b", re.I),
)


def normalize_error(text: str) -> str:
    """Normalize volatile failure details while preserving the fault message."""
    value = text
    for pattern in _NOISE:
        value = pattern.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def fault_signature(unit: str, error_text: str) -> str:
    payload = f"{unit}\n{normalize_error(error_text)}".encode()
    return hashlib.sha256(payload).hexdigest()


def severity(host_count: int, fleet_size: int = 5) -> str:
    """One host is host-local; every host is a fleet outage."""
    if host_count >= fleet_size:
        return "critical"
    if host_count > 1:
        return "high"
    return "warning"


def _json_line(record: Mapping[str, object]) -> str:
    # Serialize and parse before append. Never construct JSON by concatenation.
    line = json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
    json.loads(line)
    return line + "\n"


def append_evidence(path: Path, record: Mapping[str, object]) -> None:
    """Append one validated evidence event to an append-only JSONL store."""
    line = _json_line(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)


def open_incident(records: Iterable[Mapping[str, object]], signature: str,
                  now: datetime, window_seconds: int = 900) -> Mapping[str, object] | None:
    """Return the newest open incident in the correlation window."""
    for record in records:
        if record.get("signature") != signature or record.get("state") not in {"open", "acknowledged", "investigating"}:
            continue
        try:
            age = (now - datetime.fromisoformat(str(record["created_at"]))).total_seconds()
        except (KeyError, ValueError):
            continue
        if 0 <= age <= window_seconds:
            return record
    return None


def reconcile(records: Iterable[Mapping[str, object]], now: datetime,
              window_seconds: int = 900) -> list[dict[str, object]]:
    """Produce merge events for duplicate opens; safe to rerun concurrently."""
    groups: dict[str, list[Mapping[str, object]]] = {}
    for record in records:
        if record.get("state") in {"open", "acknowledged", "investigating"} and record.get("signature"):
            groups.setdefault(str(record["signature"]), []).append(record)
    events: list[dict[str, object]] = []
    for signature, incidents in groups.items():
        eligible = [r for r in incidents if _within(r, now, window_seconds)]
        if len(eligible) < 2:
            continue
        winner = min(eligible, key=lambda r: str(r.get("created_at", "")))
        for duplicate in eligible:
            if duplicate is winner or duplicate.get("id") == winner.get("id"):
                continue
            events.append({"type": "incident_reconciled", "incident_id": duplicate.get("id"),
                           "state": "closed", "merged_into": winner.get("id"),
                           "signature": signature})
    return events


def _within(record: Mapping[str, object], now: datetime, window: int) -> bool:
    try:
        return 0 <= (now - datetime.fromisoformat(str(record["created_at"]))).total_seconds() <= window
    except (KeyError, ValueError):
        return False
