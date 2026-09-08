"""Deterministic, read-only quality measurements for CardStore work.

The harness deliberately treats lifecycle events and evidence as separate inputs.
It never writes CardStore records or changes a card.  Reports are JSON produced
with a serializer so they can be hashed and independently checked.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from skcoord.card_store import CardStore

_STRUCTURAL = {"create", "move", "claim", "assign", "complete", "reopen", "void"}
_REPAIRS = {"repair", "rework", "reopen"}
_EVIDENCE_KEYS = {"evidence", "evidence_link", "artifact", "artifact_sha256", "test_claim"}


def _sha(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _events(store: CardStore, card_id: str) -> list[dict[str, Any]]:
    """Read through CardStore's verified parser, never the JSONL files."""
    return list(store._read_events(card_id))  # noqa: SLF001 - read-only CardStore API


def _evidence(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for event in events:
        links = event.get("evidence") or event.get("evidence_link")
        if isinstance(links, dict):
            links = [links]
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict) and (set(link) & (_EVIDENCE_KEYS | {"key", "value"})):
                    out.append({"event_id": event.get("event_id"), "link": link, "event_sha256": _sha(event)})
        for key in _EVIDENCE_KEYS & set(event):
            if key in {"evidence", "evidence_link"}:
                continue
            out.append({"event_id": event.get("event_id"), "key": key, "value": event[key], "event_sha256": _sha(event)})
    return out


def _card_metrics(card: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    evidence = _evidence(events)
    passes = [e for e in events if e.get("action") == "verdict" and e.get("verdict") == "PASS"]
    independent = [e for e in passes if e.get("independent") is True or e.get("reviewer") is True]
    repairs = sum(e.get("action") in _REPAIRS for e in events)
    claims = [e for e in events if e.get("action") in {"test_claim", "tests"}]
    mismatches = sum(bool(e.get("mismatch") or e.get("test_claim_mismatch")) for e in claims)
    return {
        "card_id": card.id,
        "worker": card.owner or card.originator or "unassigned",
        "packet_admission_success": any(e.get("action") in {"admit", "packet_admitted"} for e in events),
        "workspace_readiness": any(e.get("action") in {"workspace_ready", "workspace_readiness"} for e in events),
        "first_independent_pass": bool(independent),
        "repair_count": repairs,
        "test_claim_mismatch": mismatches,
        "no_progress_recovery": sum(e.get("action") in {"no_progress_recovery", "recover"} for e in events),
        "terminalization_latency_seconds": _latency(events),
        "escaped_defect_class": sorted({str(e.get("defect_class")) for e in events if e.get("escaped") and e.get("defect_class")}),
        "event_sha256": [_sha(e) for e in events],
        "evidence": evidence,
    }


def _latency(events: list[dict[str, Any]]) -> float | None:
    times = [e.get("ts") for e in events if e.get("action") in {"create", "complete", "void"}]
    if len(times) < 2:
        return None
    from datetime import datetime
    try:
        return max(0.0, (datetime.fromisoformat(times[-1].replace("Z", "+00:00")) - datetime.fromisoformat(times[0].replace("Z", "+00:00"))).total_seconds())
    except (TypeError, ValueError):
        return None


def build_report(home: Path) -> dict[str, Any]:
    store = CardStore(Path(home))
    rows = []
    for card in sorted(store.list_cards(include_archived=True), key=lambda c: c.id):
        rows.append(_card_metrics(card, _events(store, card.id)))
    first_pass = sum(r["first_independent_pass"] for r in rows)
    report = {"schema": "sklegal-quality-v1", "cards": rows, "baseline": {
        "card_count": len(rows), "first_pass_pass_rate": first_pass / len(rows) if rows else 0.0,
        "repair_loops": sum(r["repair_count"] for r in rows),
        "workers": sorted({r["worker"] for r in rows}),
    }}
    defects = Counter(d for r in rows for d in r["escaped_defect_class"])
    report["prevention_proposals"] = [{"defect_class": d, "count": n, "bounded": True, "duplicate_or_owned": False} for d, n in sorted(defects.items()) if n >= 2]
    report["report_sha256"] = _sha(report)
    return report


def write_report(home: Path, destination: Path | None = None) -> tuple[Path, str]:
    report = build_report(home)
    destination = destination or Path(home) / "evidence" / "quality" / "baseline.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return destination, digest
