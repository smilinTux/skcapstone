"""Dashboard ITIL cockpit API: KPIs, SLA breach-risk, CAB queue, lineage, KEDB.

Phase 3 of the interactive SKDashboard. Three-tier information architecture:
overview cockpit -> per-discipline (incident/problem/change) -> record detail.
All computed by folding the live ITILManager records; no charting library.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.dashboard.itil")

# SLA resolution targets in minutes (mirrors ITILManager.check_sla_breaches).
SLA_MINUTES = {"sev1": 5, "sev2": 15, "sev3": 60, "sev4": 240}
_OPEN_INCIDENT = {"detected", "acknowledged", "investigating", "escalated"}
_CHANGE_SUCCESS = {"deployed", "verified"}
_CHANGE_FAIL = {"failed", "rejected"}


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _minutes_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return None
    return (db - da).total_seconds() / 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mgr(home: Path):
    from .itil import ITILManager
    return ITILManager(Path(home).expanduser())


def _fmt_dur(minutes: Optional[float]) -> str:
    if minutes is None:
        return "-"
    if minutes < 60:
        return f"{round(minutes)}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


# ---------------------------------------------------------------------------
# Tier 1: overview cockpit
# ---------------------------------------------------------------------------

def get_overview(home: Path) -> dict:
    """The cockpit: KPI row, open-by-severity, breach-risk, CAB queue, activity."""
    try:
        mgr = _mgr(home)
        incidents = mgr.list_incidents()
        changes = mgr.list_changes()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    open_inc = [i for i in incidents if i.status.value in _OPEN_INCIDENT]
    by_sev = {s: 0 for s in ("sev1", "sev2", "sev3", "sev4")}
    for i in open_inc:
        by_sev[i.severity.value] = by_sev.get(i.severity.value, 0) + 1

    # MTTA / MTTR over the last 7 days of incidents.
    cutoff = _now().timestamp() - 7 * 86400
    mtta_vals, mttr_vals = [], []
    for i in incidents:
        det = _parse(i.detected_at)
        if not det or det.timestamp() < cutoff:
            continue
        mtta = _minutes_between(i.detected_at, i.acknowledged_at)
        mttr = _minutes_between(i.detected_at, i.resolved_at)
        if mtta is not None and mtta >= 0:
            mtta_vals.append(mtta)
        if mttr is not None and mttr >= 0:
            mttr_vals.append(mttr)
    mtta = sum(mtta_vals) / len(mtta_vals) if mtta_vals else None
    mttr = sum(mttr_vals) / len(mttr_vals) if mttr_vals else None

    # Change success / failure rate over closed-out changes.
    done = [c for c in changes if c.status.value in _CHANGE_SUCCESS | _CHANGE_FAIL]
    succ = sum(1 for c in done if c.status.value in _CHANGE_SUCCESS)
    change_success = round(100 * succ / len(done)) if done else None
    change_fail = round(100 * (len(done) - succ) / len(done)) if done else None
    awaiting_cab = [c for c in changes if c.status.value == "reviewing"]

    return {
        "kpis": {
            "open_incidents": len(open_inc),
            "sev1": by_sev["sev1"],
            "sev2": by_sev["sev2"],
            "mtta": _fmt_dur(mtta),
            "mttr": _fmt_dur(mttr),
            "change_success": change_success,
            "change_fail": change_fail,
            "awaiting_cab": len(awaiting_cab),
        },
        "by_severity": by_sev,
        "breach_risk": _breach_risk(open_inc),
        "cab_queue": _cab_queue(mgr, awaiting_cab),
        "activity": _recent_activity(incidents, changes),
        "services": _service_strip(open_inc),
    }


def _breach_risk(open_inc) -> list[dict]:
    """Open incidents ranked by SLA time-remaining (most urgent first)."""
    rows = []
    now = _now()
    for i in open_inc:
        det = _parse(i.detected_at)
        if not det:
            continue
        age = (now - det).total_seconds() / 60.0
        target = SLA_MINUTES.get(i.severity.value, 60)
        remaining = target - age
        rows.append({
            "id": i.id,
            "title": i.title,
            "severity": i.severity.value,
            "remaining_min": round(remaining),
            "over": remaining < 0,
            "service": (i.affected_services or [None])[0],
        })
    rows.sort(key=lambda r: r["remaining_min"])
    return rows[:8]


def _cab_queue(mgr, awaiting_cab) -> list[dict]:
    """Changes in review with their live vote tally."""
    out = []
    for c in awaiting_cab:
        try:
            votes = mgr.get_cab_votes(c.id)
        except Exception:  # noqa: BLE001
            votes = []
        approve = sum(1 for v in votes if v.decision.value == "approved")
        reject = sum(1 for v in votes if v.decision.value == "rejected")
        out.append({
            "id": c.id,
            "title": c.title,
            "change_type": c.change_type.value,
            "risk": c.risk.value,
            "rollback": c.rollback_plan,
            "approve": approve,
            "reject": reject,
            "voters": [v.agent for v in votes],
        })
    return out


def _recent_activity(incidents, changes) -> list[dict]:
    """Latest timeline entries across incidents + changes."""
    events = []
    for rec in list(incidents) + list(changes):
        for entry in (rec.timeline or []):
            events.append({
                "ts": entry.get("ts", ""),
                "record": rec.id,
                "kind": rec.type,
                "agent": entry.get("agent", ""),
                "action": entry.get("action", ""),
                "note": entry.get("note", ""),
            })
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:10]


def _service_strip(open_inc) -> list[dict]:
    """Services with active incidents, worst severity first."""
    rank = {"sev1": 0, "sev2": 1, "sev3": 2, "sev4": 3}
    svc = {}
    for i in open_inc:
        for s in (i.affected_services or []):
            cur = svc.get(s)
            if cur is None or rank.get(i.severity.value, 9) < rank.get(cur, 9):
                svc[s] = i.severity.value
    return [{"service": s, "severity": v} for s, v in
            sorted(svc.items(), key=lambda kv: rank.get(kv[1], 9))]


# ---------------------------------------------------------------------------
# Tier 2: per-discipline
# ---------------------------------------------------------------------------

def get_incidents(home: Path) -> dict:
    mgr = _mgr(home)
    incidents = mgr.list_incidents()
    now = _now()
    rows = []
    for i in incidents:
        det = _parse(i.detected_at)
        rows.append({
            "id": i.id, "title": i.title, "severity": i.severity.value,
            "status": i.status.value, "service": (i.affected_services or [None])[0],
            "age": _fmt_dur((now - det).total_seconds() / 60.0 if det else None),
            "mttr": _fmt_dur(_minutes_between(i.detected_at, i.resolved_at)),
            "problem": i.related_problem_id,
            "open": i.status.value in _OPEN_INCIDENT,
        })
    rows.sort(key=lambda r: (not r["open"], r["severity"]))
    return {"incidents": rows}


def get_problems(home: Path) -> dict:
    mgr = _mgr(home)
    rows = []
    for p in mgr.list_problems():
        rows.append({
            "id": p.id, "title": p.title, "status": p.status.value,
            "incidents": len(p.related_incident_ids or []),
            "kedb": p.kedb_id, "change": p.related_change_id,
            "workaround": bool(p.workaround),
        })
    return {"problems": rows}


def get_changes(home: Path) -> dict:
    mgr = _mgr(home)
    changes = mgr.list_changes()
    cab = _cab_queue(mgr, [c for c in changes if c.status.value == "reviewing"])
    rows = []
    for c in changes:
        rows.append({
            "id": c.id, "title": c.title, "status": c.status.value,
            "change_type": c.change_type.value, "risk": c.risk.value,
            "problem": c.related_problem_id,
        })
    order = ["proposed", "reviewing", "approved", "implementing", "deployed",
             "verified", "failed", "rejected", "closed"]
    rows.sort(key=lambda r: order.index(r["status"]) if r["status"] in order else 99)
    return {"cab_queue": cab, "changes": rows}


def search_kedb(home: Path, query: str) -> dict:
    try:
        entries = _mgr(home).search_kedb(query or "")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "results": []}
    return {"results": [
        {"id": e.id, "title": e.title, "root_cause": e.root_cause,
         "workaround": e.workaround, "symptoms": e.symptoms}
        for e in entries
    ]}


# ---------------------------------------------------------------------------
# Tier 3: record detail + lineage
# ---------------------------------------------------------------------------

def get_record(home: Path, kind: str, record_id: str) -> dict:
    """A single incident/problem/change with its timeline + i->p->c lineage."""
    mgr = _mgr(home)
    finders = {
        "incident": lambda: next((i for i in mgr.list_incidents() if i.id == record_id), None),
        "problem": lambda: next((p for p in mgr.list_problems() if p.id == record_id), None),
        "change": lambda: next((c for c in mgr.list_changes() if c.id == record_id), None),
    }
    rec = finders.get(kind, lambda: None)()
    if rec is None:
        return {"error": "record not found", "kind": kind, "id": record_id}
    return {
        "kind": kind,
        "record": rec.model_dump(),
        "timeline": rec.timeline or [],
        "lineage": _lineage(mgr, kind, rec),
    }


def _lineage(mgr, kind, rec) -> list[dict]:
    """Build the incident -> problem -> change chain from the record's links."""
    inc = prb = chg = None
    if kind == "incident":
        inc = rec
        pid = rec.related_problem_id
        prb = next((p for p in mgr.list_problems() if p.id == pid), None) if pid else None
    elif kind == "problem":
        prb = rec
    elif kind == "change":
        chg = rec
        pid = rec.related_problem_id
        prb = next((p for p in mgr.list_problems() if p.id == pid), None) if pid else None
    if prb is not None:
        if inc is None and prb.related_incident_ids:
            inc = next((i for i in mgr.list_incidents()
                        if i.id == prb.related_incident_ids[0]), None)
        if chg is None and prb.related_change_id:
            chg = next((c for c in mgr.list_changes() if c.id == prb.related_change_id), None)
    chain = []
    if inc is not None:
        chain.append({"kind": "incident", "id": inc.id, "title": inc.title,
                      "state": f"SEV{inc.severity.value[-1]} {inc.status.value}"})
    if prb is not None:
        chain.append({"kind": "problem", "id": prb.id, "title": prb.title,
                      "state": prb.status.value})
    if chg is not None:
        chain.append({"kind": "change", "id": chg.id, "title": chg.title,
                      "state": chg.status.value})
    return chain
