"""Durable alert record store (P4, card c6a87139).

Chef's ask: "when the alert comes up, give me the option of next steps so I
can just say 'do it' and you do it" (design doc
docs/specs/2026-08-13-unified-consent-plane-arch.md section 1). That only
works if the alert is still findable by id days later (the concrete first
consumer is a GMKtec warranty-RMA alert on 2026-08-19). Unlike the gtd- and
inc-/prb-/chg- surfaces `agent_run.ensure_card` already knows, there is no
existing durable, id-addressable alert record anywhere in the fleet: pubsub
messages (`skcapstone.pubsub`) are ephemeral (24h TTL, pruned on publish, no
get-by-id) and `fleet.alerts.send_alert` / `operator_seat.notify` are
fire-and-forget notifications with no persisted record at all.

So this module is the small durable store the alert- surface needs, file-per-
id under ``<home>/coordination/alerts/<id>.json``, following the same
convention `operator_seat.decisions` already established for a very similar
shape (park a proposal, read it back by id). This module intentionally does
NOT reuse `operator_seat.decisions`: that module owns a different subsystem
(the Atlas fleet operator loop's own auto/escalate dispositions, human-only
resolve guard, 1-3 option cap) and conflating the two would couple this
card's alert surface to invariants it does not need and should not depend on.

Records only: this module performs no actuation and no notification. Writing
one down is orthogonal to whatever raised the alert (email watcher, fleet
health check, a cron) and to whatever renders it (Telegram, dashboard).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .atomic_io import atomic_write_text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alerts_dir(home: Path) -> Path:
    return Path(home).expanduser() / "coordination" / "alerts"


def _record_path(home: Path, alert_id: str) -> Path:
    return _alerts_dir(home) / f"{alert_id}.json"


def raise_alert(
    home: Path,
    alert_id: str,
    title: str,
    description: str = "",
    options: Optional[list[dict[str, Any] | str]] = None,
    *,
    priority: str = "high",
    labels: Optional[list[str]] = None,
    created_by: str = "alert",
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """Persist an alert record so it can be found by id later.

    Create-or-return-existing (idempotent), matching
    `operator_seat.decisions.park`: a persistent condition that fires
    repeatedly (the same watcher running every N minutes) must resolve to the
    SAME record, not a fresh one each pass, so a human resolves it once.

    Args:
        home: shared root.
        alert_id: stable id for this alert (no "alert-" prefix; that prefix
            is the CardStore shadow-card convention, added by callers of
            `agent_run.ensure_card`, not part of the alert's own id).
        title: short human-readable summary.
        description: longer context (e.g. the triggering email, condensed).
        options: 2-4 concrete next-step options, each ``{"text", "mode"}`` or
            a bare string (defaulted to ``mode="propose"``). This is the
            actual feature Chef asked for: the alert carries its own options,
            and `agent_run.ensure_card`'s "alert-" branch surfaces them
            verbatim as the card's suggested next steps.
        priority: initial card priority once materialized.
        labels: extra labels beyond the implicit "alert" label.
        created_by: attribution for who/what raised the alert.
        created_at: ISO timestamp; defaults to now.

    Returns:
        The stored record: ``{id, title, description, options, priority,
        labels, created_by, created_at}``.
    """
    path = _record_path(home, alert_id)
    if path.exists():
        return get_alert(home, alert_id)  # type: ignore[return-value]
    record: dict[str, Any] = {
        "id": alert_id,
        "title": title,
        "description": description,
        "options": list(options or []),
        "priority": priority,
        "labels": list(labels or []),
        "created_by": created_by,
        "created_at": created_at or _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def get_alert(home: Path, alert_id: str) -> Optional[dict[str, Any]]:
    """Read an alert record by id, or None if it does not exist / is corrupt.

    Fail-closed like every other lookup `ensure_card` performs: a missing or
    unparseable record is treated as "not found", never guessed at.
    """
    path = _record_path(home, alert_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def list_alerts(home: Path) -> list[dict[str, Any]]:
    """All alert records, sorted by creation time. Best-effort: a corrupt
    record is skipped rather than failing the whole listing."""
    out = []
    for path in sorted(_alerts_dir(home).glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda r: r.get("created_at", ""))
    return out
