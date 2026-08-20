"""Read-only ATLAS operator cockpit projection.

The dashboard deliberately reads durable operator artifacts instead of calling
the operator loop.  A broken or missing source is represented as ``unknown``;
an observation surface must never manufacture a healthy result.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("root is not an object")
        return value, None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, str(exc)


def _iso_age(value: Any, now: datetime) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (now - stamp).total_seconds())
    except ValueError:
        return None


def _source(path: Path, now: datetime) -> dict[str, Any]:
    payload, error = _read_json(path)
    modified = None
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        pass
    return {
        "path": str(path),
        "available": payload is not None,
        "age_seconds": max(0.0, (now - modified).total_seconds()) if modified else None,
        "error": error,
        "payload": payload,
    }


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _conditions(brief: dict[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
    if not brief:
        return []
    raw = brief.get("conditions", [])
    if isinstance(raw, dict):
        raw = [dict(value, type=key) if isinstance(value, dict) else {"type": key, "status": value}
               for key, value in raw.items()]
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        observed = item.get("observed_at") or brief.get("observed_at") or brief.get("generated_at")
        result.append({
            "type": str(item.get("type") or item.get("condition") or "unknown"),
            "status": item.get("status"),
            "subject": item.get("subject") or item.get("object"),
            "observed_at": observed,
            "age_seconds": _iso_age(observed, now),
            "ttl_seconds": item.get("ttl_seconds") or item.get("ttl"),
            "provenance": item.get("provenance") or item.get("evidence_ref"),
        })
    return result


def _ledger(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    intents, errors = [], []
    for core_path in sorted((root / "intents").glob("ai-*.json")):
        core, error = _read_json(core_path)
        if error or core is None:
            errors.append(f"{core_path.name}: {error}")
            continue
        event_path = root / "events" / f"{core_path.stem}.jsonl"
        events = []
        try:
            for line_number, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), 1):
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError(f"line {line_number} is not an object")
                events.append(event)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{event_path.name}: {exc}")
            continue
        latest = events[-1] if events else {}
        intents.append({
            "intent_id": core.get("intent_id", core_path.stem),
            "application": core.get("application"),
            "target_kind": core.get("target_kind"),
            "target_id": core.get("target_id"),
            "action": core.get("action"),
            "change_id": core.get("itil_change_id"),
            "state": latest.get("state", "unknown"),
            "updated_at": latest.get("occurred_at"),
            "verification": core.get("verification") or {},
            "rollback": core.get("rollback") or {},
            "event_count": len(events),
        })
    intents.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return intents, errors


def get_operator_cockpit(home: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Fold ATLAS evidence into one stable, read-only dashboard document."""
    now = now or datetime.now(timezone.utc)
    shared = Path(home).expanduser()
    fleet = Path(os.environ.get("SKFLEET_ROOT", shared / "fleet")).expanduser()
    atlas = Path(os.environ.get("SKATLAS_ROOT", fleet / "atlas")).expanduser()
    brief_path = Path(os.environ.get("SKATLAS_BRIEF_JSON", atlas / "brief" / "brief.json"))
    safety = _source(atlas / "state" / "execution-state.json", now)
    brief = _source(brief_path, now)
    freeze = _source(fleet / "objects" / "_freeze.json", now)
    watchdog = _source(
        Path(os.environ.get("SK_WATCHDOG_DIR", shared / "watchdog"))
        / "digests" / "latest" / "digest.json", now
    )
    cmdb = _source(Path(os.environ.get("SKATLAS_CMDB_STATUS", atlas / "cmdb" / "latest.json")), now)
    brain = _source(Path(os.environ.get("SKBRAIN_OPERATOR_HEALTH", shared / "skbrain" / "operator-health.json")), now)
    actions, ledger_errors = _ledger(Path(os.environ.get("SKATLAS_ACTION_LEDGER", atlas / "action-ledger")))

    cmdb_payload = cmdb["payload"] or {}
    brain_payload = brain["payload"] or {}

    safety_actions = (safety["payload"] or {}).get("actions", {})
    circuits = []
    if isinstance(safety_actions, dict):
        for fingerprint, state in safety_actions.items():
            if isinstance(state, dict):
                circuits.append({"fingerprint": fingerprint, **state})

    return {
        "schema_id": "skdashboard.atlas-operator-cockpit/v1",
        "generated_at": now.isoformat(),
        "freeze": {
            "status": "frozen" if freeze["payload"] is None or freeze["payload"].get("frozen") else "active",
            "reason": (freeze["payload"] or {}).get("reason") or freeze["error"],
            "updated_at": (freeze["payload"] or {}).get("updatedAt"),
        },
        "conditions": _conditions(brief["payload"], now),
        "actions": actions,
        "ledger_errors": ledger_errors,
        "execution_controls": circuits,
        "watchdog": {key: watchdog[key] for key in ("available", "age_seconds", "error")},
        "cmdb": {
            **{key: cmdb[key] for key in ("available", "age_seconds", "error")},
            "scope_fingerprint": _first(cmdb_payload, "scope_fingerprint", "scope"),
            "complete": _first(cmdb_payload, "complete", "scan_complete"),
            "audit_clean": cmdb_payload.get("audit_clean"),
        },
        "skbrain": {
            **{key: brain[key] for key in ("available", "age_seconds", "error")},
            "status": brain_payload.get("status") or brain_payload.get("health"),
            "citations": brain_payload.get("citations") if isinstance(brain_payload.get("citations"), list) else [],
        },
        "sources": {
            "brief": {key: brief[key] for key in ("path", "available", "age_seconds", "error")},
            "safety": {key: safety[key] for key in ("path", "available", "age_seconds", "error")},
        },
    }
