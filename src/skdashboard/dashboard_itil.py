"""Dashboard ITIL cockpit API: KPIs, SLA breach-risk, CAB queue, lineage, KEDB.

Phase 3 of the interactive SKDashboard. Three-tier information architecture:
overview cockpit -> per-discipline (incident/problem/change) -> record detail.
All computed by folding the live ITILManager records; no charting library.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.dashboard.itil")

# SLA resolution targets in minutes (mirrors ITILManager.check_sla_breaches).
SLA_MINUTES = {"sev1": 5, "sev2": 15, "sev3": 60, "sev4": 240}
_OPEN_INCIDENT = {"detected", "acknowledged", "investigating", "escalated"}
_CHANGE_SUCCESS = {"verified"}
_CHANGE_FAIL = {"failed"}


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
    from skcoord.itil import ITILManager

    return ITILManager(Path(home).expanduser())


def _fmt_dur(minutes: Optional[float]) -> str:
    if minutes is None:
        return "-"
    if minutes < 60:
        return f"{round(minutes)}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def _change_outcome(change) -> str:
    """Classify deployment outcomes without treating review states as results."""
    status = change.status.value
    if status in _CHANGE_SUCCESS:
        return "successful"
    if status in _CHANGE_FAIL:
        return "failed"
    if status == "rejected":
        return "rejected"
    if status != "closed":
        return "pending"
    actions = [row.get("action", "") for row in change.timeline or [] if not row.get("conflicted")]
    if "status:verified->closed" in actions:
        return "successful"
    if "status:failed->closed" in actions:
        return "failed"
    if "status:rejected->closed" in actions:
        return "rejected"
    return "unknown"


def _metric(
    metric_id: str,
    label: str,
    *,
    value,
    unit: str,
    numerator,
    denominator,
    sample_size: int,
    window: str,
    classification: str,
    exclusions: list[str],
    legacy_coverage: dict,
    evidence_refs: list[str],
) -> dict:
    return {
        "metric_id": metric_id,
        "label": label,
        "value": value,
        "unit": unit,
        "truth_state": "current" if value is not None else "unknown",
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": sample_size,
        "window": window,
        "classification": classification,
        "exclusions": exclusions,
        "legacy_coverage": legacy_coverage,
        "evidence_refs": evidence_refs,
    }


def _has_action(record, action: str) -> bool:
    return any(
        row.get("action") == action and not row.get("conflicted") for row in record.timeline or []
    )


class ReliabilityProjectionProvider:
    """Read folded ITIL records only while the exact CapAuth decision is current."""

    def read(self, context, query, home, *, currentness_verifier):
        if currentness_verifier.check_before_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision is not current")
        projection = get_reliability_projection(home, query)
        if currentness_verifier.check_after_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision expired during owner read")
        return projection


def get_reliability_projection(home: Path, query: dict) -> dict:
    """Build one bounded read-only reliability projection from folded ITIL records."""
    mgr = _mgr(home)
    incidents = mgr.list_incidents()
    problems = mgr.list_problems()
    changes = mgr.list_changes()
    kedb = mgr.search_kedb("")
    now = _now()
    cutoff = now.timestamp() - 7 * 86400

    recent = []
    invalid_incident_dates = 0
    mtta_values: list[float] = []
    mttr_values: list[float] = []
    for incident in incidents:
        detected = _parse(incident.detected_at)
        if detected is None:
            invalid_incident_dates += 1
            continue
        if detected.timestamp() < cutoff:
            continue
        recent.append(incident)
        mtta = _minutes_between(incident.detected_at, incident.acknowledged_at)
        mttr = _minutes_between(incident.detected_at, incident.resolved_at)
        if mtta is not None and mtta >= 0:
            mtta_values.append(mtta)
        if mttr is not None and mttr >= 0:
            mttr_values.append(mttr)

    open_incidents = [item for item in incidents if item.status.value in _OPEN_INCIDENT]
    breach_rows = _breach_risk(open_incidents, limit=None)
    breach_count = sum(1 for row in breach_rows if row["over"])
    outcomes = [_change_outcome(change) for change in changes]
    outcome_denominator = sum(value in {"successful", "failed"} for value in outcomes)
    successful = outcomes.count("successful")
    change_lead_values = []
    for change, outcome in zip(changes, outcomes, strict=True):
        if outcome not in {"successful", "failed"}:
            continue
        final_rows = [
            row
            for row in change.timeline or []
            if row.get("action", "").endswith("->closed") and not row.get("conflicted")
        ]
        lead = _minutes_between(
            change.created_at,
            final_rows[-1].get("ts") if final_rows else None,
        )
        if lead is not None and lead >= 0:
            change_lead_values.append(lead)

    linked_problems = [problem for problem in problems if problem.related_incident_ids]
    recurring = sum(len(problem.related_incident_ids) > 1 for problem in linked_problems)
    kedb_used = sum(bool(problem.kedb_id) for problem in problems)
    successful_changes = [
        change
        for change, outcome in zip(changes, outcomes, strict=True)
        if outcome == "successful"
    ]
    pir_count = sum(
        _has_action(change, "status:deployed->verified") for change in successful_changes
    )
    legacy = {
        "incident_records": len(incidents),
        "incident_aliases": sum(item.id.lower().startswith("inc-") for item in incidents),
        "problem_records": len(problems),
        "problem_aliases": sum(item.id.lower().startswith("prb-") for item in problems),
        "change_records": len(changes),
        "change_aliases": sum(item.id.lower().startswith("chg-") for item in changes),
    }
    refs = ["skcoord.itil:folded-records"]
    unknown_reason = "No approved owner record is available."
    metrics = [
        _metric(
            "service.availability_sli",
            "User-facing availability SLI",
            value=None,
            unit="percent",
            numerator=None,
            denominator=None,
            sample_size=0,
            window="7d",
            classification="user_facing_sli",
            exclusions=[unknown_reason],
            legacy_coverage=legacy,
            evidence_refs=[],
        ),
        _metric(
            "service.slo_target",
            "Approved service-level target",
            value=None,
            unit="percent",
            numerator=None,
            denominator=None,
            sample_size=0,
            window="approved target window unknown",
            classification="approved_slo",
            exclusions=[unknown_reason],
            legacy_coverage=legacy,
            evidence_refs=[],
        ),
        _metric(
            "service.error_budget_remaining",
            "Error budget remaining",
            value=None,
            unit="percent",
            numerator=None,
            denominator=None,
            sample_size=0,
            window="approved target window unknown",
            classification="error_budget",
            exclusions=["Requires an approved SLO and user-facing SLI observations."],
            legacy_coverage=legacy,
            evidence_refs=[],
        ),
        _metric(
            "itil.mtta_minutes",
            "Mean time to acknowledge",
            value=round(sum(mtta_values) / len(mtta_values), 2) if mtta_values else None,
            unit="minutes",
            numerator=round(sum(mtta_values), 2),
            denominator=len(mtta_values),
            sample_size=len(recent),
            window="7d by detected_at",
            classification="incident_response",
            exclusions=[
                "Missing, negative, or out-of-window timestamps.",
                f"{invalid_incident_dates} incident records have invalid detected_at.",
            ],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
        _metric(
            "itil.mttr_minutes",
            "Mean time to resolve",
            value=round(sum(mttr_values) / len(mttr_values), 2) if mttr_values else None,
            unit="minutes",
            numerator=round(sum(mttr_values), 2),
            denominator=len(mttr_values),
            sample_size=len(recent),
            window="7d by detected_at",
            classification="incident_recovery",
            exclusions=["Open, missing, negative, or out-of-window resolution timestamps."],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
        _metric(
            "itil.open_sla_breaches",
            "Open incident response-target breaches",
            value=breach_count,
            unit="incidents",
            numerator=breach_count,
            denominator=len(breach_rows),
            sample_size=len(open_incidents),
            window="current open incidents",
            classification="legacy_response_target_not_slo",
            exclusions=[
                f"{len(open_incidents) - len(breach_rows)} open incidents lack a valid detected_at.",
                "The display list is capped separately and never supplies this numerator.",
            ],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
        _metric(
            "itil.incident_recurrence_rate",
            "Problem-linked recurrence",
            value=round(100 * recurring / len(linked_problems), 2) if linked_problems else None,
            unit="percent",
            numerator=recurring,
            denominator=len(linked_problems),
            sample_size=len(problems),
            window="all folded records",
            classification="problem_linkage",
            exclusions=["Unlinked incidents and Problems with no incident links."],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
        _metric(
            "itil.change_lead_time_minutes",
            "Closed change lead time",
            value=round(sum(change_lead_values) / len(change_lead_values), 2)
            if change_lead_values
            else None,
            unit="minutes",
            numerator=round(sum(change_lead_values), 2),
            denominator=len(change_lead_values),
            sample_size=len(changes),
            window="all folded records",
            classification="terminal_deployment_outcome",
            exclusions=[
                "Pending, rejected, unknown-outcome, conflicted, or timestamp-incomplete changes."
            ],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
        _metric(
            "itil.change_success_rate",
            "Change success rate",
            value=round(100 * successful / outcome_denominator, 2)
            if outcome_denominator
            else None,
            unit="percent",
            numerator=successful,
            denominator=outcome_denominator,
            sample_size=len(changes),
            window="all folded records",
            classification="verified_success_vs_failed",
            exclusions=[
                f"{outcomes.count('rejected')} rejected, {outcomes.count('pending')} pending, and {outcomes.count('unknown')} unknown-outcome changes."
            ],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
        _metric(
            "itil.pir_coverage_rate",
            "PIR evidence coverage",
            value=round(100 * pir_count / len(successful_changes), 2)
            if successful_changes
            else None,
            unit="percent",
            numerator=pir_count,
            denominator=len(successful_changes),
            sample_size=len(changes),
            window="all folded records",
            classification="successful_change_pir",
            exclusions=["Changes without a successful terminal outcome."],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
        _metric(
            "itil.kedb_use_rate",
            "Problem KEDB linkage",
            value=round(100 * kedb_used / len(problems), 2) if problems else None,
            unit="percent",
            numerator=kedb_used,
            denominator=len(problems),
            sample_size=len(problems),
            window="all folded records",
            classification="problem_kedb_linkage",
            exclusions=["No KEDB use is inferred from free text."],
            legacy_coverage=legacy,
            evidence_refs=refs,
        ),
    ]

    items = {
        "incidents": [
            {
                "id": item.id,
                "legacy_alias": item.id,
                "title": item.title,
                "severity": item.severity.value,
                "status": item.status.value,
                "services": item.affected_services,
                "problem_id": item.related_problem_id,
                "detected_at": item.detected_at,
                "acknowledged_at": item.acknowledged_at,
                "resolved_at": item.resolved_at,
            }
            for item in incidents[:200]
        ],
        "problems": [
            {
                "id": item.id,
                "legacy_alias": item.id,
                "title": item.title,
                "status": item.status.value,
                "incident_ids": item.related_incident_ids,
                "kedb_id": item.kedb_id,
                "change_id": item.related_change_id,
                "workaround_recorded": bool(item.workaround),
            }
            for item in problems[:200]
        ],
        "changes": [
            {
                "id": item.id,
                "legacy_alias": item.id,
                "title": item.title,
                "status": item.status.value,
                "outcome": outcome,
                "problem_id": item.related_problem_id,
                "validation": "passed"
                if item.validation and item.validation.get("passed")
                else "failed"
                if item.validation
                else "unknown",
                "cab_required": item.cab_required,
                "cab_votes": len(mgr.get_cab_votes(item.id)),
                "scheduled": item.scheduled_window is not None,
                "deployed": _has_action(item, "status:implementing->deployed"),
                "verified": _has_action(item, "status:deployed->verified"),
                "pir_recorded": _has_action(item, "status:deployed->verified"),
                "rollback_plan_recorded": bool(item.rollback_plan),
                "rollback_event_recorded": _has_action(item, "status:failed->closed"),
            }
            for item, outcome in list(zip(changes, outcomes, strict=True))[:200]
        ],
        "kedb": [
            {
                "id": item.id,
                "title": item.title,
                "problem_id": item.related_problem_id,
                "change_id": item.permanent_fix_change_id,
                "root_cause_recorded": bool(item.root_cause),
                "workaround_recorded": bool(item.workaround),
            }
            for item in kedb[:200]
        ],
        "breach_risk": breach_rows[:8],
    }
    source = [item.model_dump(mode="json") for item in [*incidents, *problems, *changes, *kedb]]
    if not source:
        for metric in metrics:
            metric.update(value=None, truth_state="unknown", numerator=None, denominator=None)
    watermark = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "1.0.0",
        "projection_id": "reliability-latest",
        "projection_hash": f"sha256:{watermark}",
        "source_owner": "SKCapstone ITIL",
        "scope": dict(query),
        "observed_at": now.isoformat(),
        "truth_state": "current" if source else "unknown",
        "visibility": {"state": "visible", "authorization": "authorized"},
        "source_watermarks": [{"source": "skcoord.itil", "value": f"sha256:{watermark}"}],
        "metrics": metrics,
        "items": items,
        "display_limit": 200,
        "record_counts": {
            "incidents": len(incidents),
            "problems": len(problems),
            "changes": len(changes),
            "kedb": len(kedb),
        },
        "errors": []
        if source
        else [{"code": "SOURCE_UNKNOWN", "message": "No folded ITIL records are available."}],
    }


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

    # Change success / failure rate over classified deployment outcomes.
    outcomes = [_change_outcome(change) for change in changes]
    done = [outcome for outcome in outcomes if outcome in {"successful", "failed"}]
    succ = done.count("successful")
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


def _breach_risk(open_inc, *, limit: int | None = 8) -> list[dict]:
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
        rows.append(
            {
                "id": i.id,
                "title": i.title,
                "severity": i.severity.value,
                "remaining_min": round(remaining),
                "over": remaining < 0,
                "service": (i.affected_services or [None])[0],
            }
        )
    rows.sort(key=lambda r: r["remaining_min"])
    return rows if limit is None else rows[:limit]


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
        out.append(
            {
                "id": c.id,
                "title": c.title,
                "change_type": c.change_type.value,
                "risk": c.risk.value,
                "rollback": c.rollback_plan,
                "approve": approve,
                "reject": reject,
                "voters": [v.agent for v in votes],
            }
        )
    return out


def _recent_activity(incidents, changes) -> list[dict]:
    """Latest timeline entries across incidents + changes."""
    events = []
    for rec in list(incidents) + list(changes):
        for entry in rec.timeline or []:
            events.append(
                {
                    "ts": entry.get("ts", ""),
                    "record": rec.id,
                    "kind": rec.type,
                    "agent": entry.get("agent", ""),
                    "action": entry.get("action", ""),
                    "note": entry.get("note", ""),
                }
            )
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:10]


def _service_strip(open_inc) -> list[dict]:
    """Services with active incidents, worst severity first."""
    rank = {"sev1": 0, "sev2": 1, "sev3": 2, "sev4": 3}
    svc = {}
    for i in open_inc:
        for s in i.affected_services or []:
            cur = svc.get(s)
            if cur is None or rank.get(i.severity.value, 9) < rank.get(cur, 9):
                svc[s] = i.severity.value
    return [
        {"service": s, "severity": v}
        for s, v in sorted(svc.items(), key=lambda kv: rank.get(kv[1], 9))
    ]


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
        rows.append(
            {
                "id": i.id,
                "title": i.title,
                "severity": i.severity.value,
                "status": i.status.value,
                "service": (i.affected_services or [None])[0],
                "age": _fmt_dur((now - det).total_seconds() / 60.0 if det else None),
                "mttr": _fmt_dur(_minutes_between(i.detected_at, i.resolved_at)),
                "problem": i.related_problem_id,
                "open": i.status.value in _OPEN_INCIDENT,
            }
        )
    rows.sort(key=lambda r: (not r["open"], r["severity"]))
    return {"incidents": rows}


def get_problems(home: Path) -> dict:
    mgr = _mgr(home)
    rows = []
    for p in mgr.list_problems():
        rows.append(
            {
                "id": p.id,
                "title": p.title,
                "status": p.status.value,
                "incidents": len(p.related_incident_ids or []),
                "kedb": p.kedb_id,
                "change": p.related_change_id,
                "workaround": bool(p.workaround),
            }
        )
    return {"problems": rows}


def get_changes(home: Path) -> dict:
    mgr = _mgr(home)
    changes = mgr.list_changes()
    cab = _cab_queue(mgr, [c for c in changes if c.status.value == "reviewing"])
    rows = []
    for c in changes:
        rows.append(
            {
                "id": c.id,
                "title": c.title,
                "status": c.status.value,
                "change_type": c.change_type.value,
                "risk": c.risk.value,
                "problem": c.related_problem_id,
            }
        )
    order = [
        "proposed",
        "reviewing",
        "approved",
        "implementing",
        "deployed",
        "verified",
        "failed",
        "rejected",
        "closed",
    ]
    rows.sort(key=lambda r: order.index(r["status"]) if r["status"] in order else 99)
    return {"cab_queue": cab, "changes": rows}


def search_kedb(home: Path, query: str) -> dict:
    try:
        entries = _mgr(home).search_kedb(query or "")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "results": []}
    return {
        "results": [
            {
                "id": e.id,
                "title": e.title,
                "root_cause": e.root_cause,
                "workaround": e.workaround,
                "symptoms": e.symptoms,
            }
            for e in entries
        ]
    }


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
            inc = next(
                (i for i in mgr.list_incidents() if i.id == prb.related_incident_ids[0]), None
            )
        if chg is None and prb.related_change_id:
            chg = next((c for c in mgr.list_changes() if c.id == prb.related_change_id), None)
    chain = []
    if inc is not None:
        chain.append(
            {
                "kind": "incident",
                "id": inc.id,
                "title": inc.title,
                "state": f"SEV{inc.severity.value[-1]} {inc.status.value}",
            }
        )
    if prb is not None:
        chain.append(
            {"kind": "problem", "id": prb.id, "title": prb.title, "state": prb.status.value}
        )
    if chg is not None:
        chain.append(
            {"kind": "change", "id": chg.id, "title": chg.title, "state": chg.status.value}
        )
    return chain
