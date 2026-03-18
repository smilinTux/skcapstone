"""
SKCapstone ITIL Service Management — Incident, Problem, and Change Management.

Conflict-free design: each ITIL record has a ``managed_by`` field — only that
agent writes to the file.  CAB votes use per-agent files to avoid conflicts.

Directory layout:
    ~/.skcapstone/coordination/itil/
    ├── incidents/           # One JSON per incident (managed_by agent owns it)
    ├── problems/            # One JSON per problem
    ├── changes/             # One JSON per RFC
    ├── kedb/                # Known Error Database entries
    ├── cab-decisions/       # Per-agent CAB vote files (conflict-free)
    └── ITIL-BOARD.md        # Auto-generated overview
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.itil")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(str, Enum):
    DETECTED = "detected"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    CLOSED = "closed"


class ProblemStatus(str, Enum):
    IDENTIFIED = "identified"
    ANALYZING = "analyzing"
    KNOWN_ERROR = "known_error"
    RESOLVED = "resolved"


class ChangeType(str, Enum):
    STANDARD = "standard"
    NORMAL = "normal"
    EMERGENCY = "emergency"


class ChangeStatus(str, Enum):
    PROPOSED = "proposed"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    REJECTED = "rejected"
    IMPLEMENTING = "implementing"
    DEPLOYED = "deployed"
    VERIFIED = "verified"
    FAILED = "failed"
    CLOSED = "closed"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CABDecisionValue(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    ABSTAIN = "abstain"


# ---------------------------------------------------------------------------
# Lifecycle state machines — valid transitions
# ---------------------------------------------------------------------------

_INCIDENT_TRANSITIONS: dict[str, set[str]] = {
    "detected": {"acknowledged", "escalated", "resolved"},
    "acknowledged": {"investigating", "escalated", "resolved"},
    "investigating": {"escalated", "resolved"},
    "escalated": {"investigating", "resolved"},
    "resolved": {"closed"},
    "closed": set(),
}

_PROBLEM_TRANSITIONS: dict[str, set[str]] = {
    "identified": {"analyzing"},
    "analyzing": {"known_error", "resolved"},
    "known_error": {"resolved"},
    "resolved": set(),
}

_CHANGE_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"reviewing", "approved", "rejected"},
    "reviewing": {"approved", "rejected"},
    "approved": {"implementing", "rejected"},
    "rejected": {"closed"},
    "implementing": {"deployed", "failed"},
    "deployed": {"verified", "failed"},
    "verified": {"closed"},
    "failed": {"implementing", "closed"},
    "closed": set(),
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TimelineEntry(BaseModel):
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    agent: str
    action: str
    note: str = ""


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: f"inc-{uuid.uuid4().hex[:8]}")
    type: str = "incident"
    title: str
    severity: Severity = Severity.SEV3
    status: IncidentStatus = IncidentStatus.DETECTED
    source: str = "manual"
    affected_services: list[str] = Field(default_factory=list)
    impact: str = ""
    managed_by: str = ""
    created_by: str = ""
    detected_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    acknowledged_at: Optional[str] = None
    resolved_at: Optional[str] = None
    closed_at: Optional[str] = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    related_problem_id: Optional[str] = None
    gtd_item_ids: list[str] = Field(default_factory=list)
    resolution_summary: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class Problem(BaseModel):
    id: str = Field(default_factory=lambda: f"prb-{uuid.uuid4().hex[:8]}")
    type: str = "problem"
    title: str
    status: ProblemStatus = ProblemStatus.IDENTIFIED
    root_cause: Optional[str] = None
    workaround: Optional[str] = None
    managed_by: str = ""
    created_by: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    related_incident_ids: list[str] = Field(default_factory=list)
    related_change_id: Optional[str] = None
    kedb_id: Optional[str] = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Change(BaseModel):
    id: str = Field(default_factory=lambda: f"chg-{uuid.uuid4().hex[:8]}")
    type: str = "change"
    title: str
    change_type: ChangeType = ChangeType.NORMAL
    status: ChangeStatus = ChangeStatus.PROPOSED
    risk: Risk = Risk.MEDIUM
    rollback_plan: str = ""
    test_plan: str = ""
    managed_by: str = ""
    created_by: str = ""
    implementer: Optional[str] = None
    cab_required: bool = True
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    related_problem_id: Optional[str] = None
    gtd_item_ids: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class KEDBEntry(BaseModel):
    id: str = Field(default_factory=lambda: f"ke-{uuid.uuid4().hex[:8]}")
    title: str
    symptoms: list[str] = Field(default_factory=list)
    root_cause: str = ""
    workaround: str = ""
    permanent_fix_change_id: Optional[str] = None
    related_problem_id: Optional[str] = None
    managed_by: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    tags: list[str] = Field(default_factory=list)


class CABDecision(BaseModel):
    change_id: str
    agent: str
    decision: CABDecisionValue = CABDecisionValue.ABSTAIN
    conditions: str = ""
    decided_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r'[/\\:*?"<>|]', '-', slug)
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug.strip('-')[:40]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_timeline_entry(agent: str, action: str, note: str = "") -> dict[str, str]:
    return {
        "ts": _now_iso(),
        "agent": agent,
        "action": action,
        "note": note,
    }


# ---------------------------------------------------------------------------
# ITILManager
# ---------------------------------------------------------------------------


class ITILManager:
    """Manages ITIL records on disk with lifecycle validation.

    Args:
        home: Path to the shared root (``~/.skcapstone`` or equivalent).
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.itil_dir = self.home / "coordination" / "itil"
        self.incidents_dir = self.itil_dir / "incidents"
        self.problems_dir = self.itil_dir / "problems"
        self.changes_dir = self.itil_dir / "changes"
        self.kedb_dir = self.itil_dir / "kedb"
        self.cab_dir = self.itil_dir / "cab-decisions"

    def ensure_dirs(self) -> None:
        """Create ITIL directories if they don't exist."""
        for d in (
            self.incidents_dir,
            self.problems_dir,
            self.changes_dir,
            self.kedb_dir,
            self.cab_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ── File I/O ──────────────────────────────────────────────────────

    def _write_record(self, directory: Path, record_id: str, title: str, data: dict) -> Path:
        """Write a record JSON file."""
        self.ensure_dirs()
        slug = _slugify(title)
        filename = f"{record_id}-{slug}.json" if slug else f"{record_id}.json"
        path = directory / filename
        path.write_text(
            json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return path

    def _load_records(self, directory: Path, model_class: type) -> list:
        """Load all JSON records from a directory, validating with model_class."""
        records = []
        if not directory.exists():
            return records
        for f in sorted(directory.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                records.append(model_class.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return records

    def _find_record_path(self, directory: Path, record_id: str) -> Optional[Path]:
        """Find a record file by ID prefix in filename."""
        if not directory.exists():
            return None
        for f in directory.glob(f"{record_id}*.json"):
            return f
        return None

    def _load_record(self, directory: Path, record_id: str, model_class: type):
        """Load a single record by ID."""
        path = self._find_record_path(directory, record_id)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return model_class.model_validate(data)
        except (json.JSONDecodeError, Exception):
            return None

    def _update_record(self, directory: Path, record_id: str, title: str, data: dict) -> Path:
        """Update a record, removing old file if slug changed."""
        old_path = self._find_record_path(directory, record_id)
        new_path = self._write_record(directory, record_id, title, data)
        if old_path and old_path != new_path and old_path.exists():
            old_path.unlink()
        return new_path

    # ── Incidents ─────────────────────────────────────────────────────

    def create_incident(
        self,
        title: str,
        severity: str = "sev3",
        source: str = "manual",
        affected_services: list[str] | None = None,
        impact: str = "",
        managed_by: str = "",
        created_by: str = "",
        tags: list[str] | None = None,
    ) -> Incident:
        """Create a new incident record."""
        agent = managed_by or created_by or "unknown"
        incident = Incident(
            title=title,
            severity=Severity(severity),
            source=source,
            affected_services=affected_services or [],
            impact=impact,
            managed_by=agent,
            created_by=created_by or agent,
            tags=tags or [],
        )
        incident.timeline.append(
            _make_timeline_entry(agent, "created", f"Incident detected: {title}")
        )
        self._write_record(
            self.incidents_dir, incident.id, title, incident.model_dump()
        )

        # Publish event
        self._publish_event("itil.incident.created", {
            "id": incident.id,
            "title": title,
            "severity": severity,
            "managed_by": agent,
        })

        # Auto-create GTD item
        gtd_id = self._create_gtd_item_for_incident(incident)
        if gtd_id:
            incident.gtd_item_ids.append(gtd_id)
            self._update_record(
                self.incidents_dir, incident.id, title, incident.model_dump()
            )

        return incident

    def update_incident(
        self,
        incident_id: str,
        agent: str,
        new_status: str | None = None,
        severity: str | None = None,
        note: str = "",
        resolution_summary: str | None = None,
        related_problem_id: str | None = None,
    ) -> Incident:
        """Update an incident's status, severity, or metadata."""
        inc = self._load_record(self.incidents_dir, incident_id, Incident)
        if inc is None:
            raise ValueError(f"Incident {incident_id} not found")

        if new_status:
            current = inc.status.value
            if new_status not in _INCIDENT_TRANSITIONS.get(current, set()):
                raise ValueError(
                    f"Invalid transition: {current} -> {new_status}. "
                    f"Valid: {_INCIDENT_TRANSITIONS.get(current, set())}"
                )
            old_status = current
            inc.status = IncidentStatus(new_status)
            inc.timeline.append(
                _make_timeline_entry(agent, f"status:{old_status}->{new_status}", note)
            )

            if new_status == "acknowledged":
                inc.acknowledged_at = _now_iso()
            elif new_status == "resolved":
                inc.resolved_at = _now_iso()
                if resolution_summary:
                    inc.resolution_summary = resolution_summary
                self._complete_gtd_items(inc.gtd_item_ids)
            elif new_status == "closed":
                inc.closed_at = _now_iso()

        if severity and severity != inc.severity.value:
            old_sev = inc.severity.value
            inc.severity = Severity(severity)
            inc.timeline.append(
                _make_timeline_entry(agent, f"severity:{old_sev}->{severity}", note)
            )
            self._publish_event("itil.incident.escalated", {
                "id": inc.id,
                "old_severity": old_sev,
                "new_severity": severity,
            })

        if related_problem_id:
            inc.related_problem_id = related_problem_id

        if note and not new_status and not severity:
            inc.timeline.append(_make_timeline_entry(agent, "note", note))

        self._update_record(
            self.incidents_dir, inc.id, inc.title, inc.model_dump()
        )
        return inc

    def list_incidents(
        self,
        status: str | None = None,
        severity: str | None = None,
        service: str | None = None,
    ) -> list[Incident]:
        """List incidents with optional filters."""
        incidents = self._load_records(self.incidents_dir, Incident)
        if status:
            incidents = [i for i in incidents if i.status.value == status]
        if severity:
            incidents = [i for i in incidents if i.severity.value == severity]
        if service:
            incidents = [
                i for i in incidents if service in i.affected_services
            ]
        return incidents

    def find_open_incident_for_service(self, service: str) -> Optional[Incident]:
        """Find an existing open incident for a service (dedup check)."""
        open_statuses = {"detected", "acknowledged", "investigating", "escalated"}
        for inc in self.list_incidents():
            if inc.status.value in open_statuses and service in inc.affected_services:
                return inc
        return None

    # ── Problems ──────────────────────────────────────────────────────

    def create_problem(
        self,
        title: str,
        managed_by: str = "",
        created_by: str = "",
        related_incident_ids: list[str] | None = None,
        workaround: str = "",
        tags: list[str] | None = None,
    ) -> Problem:
        """Create a new problem record."""
        agent = managed_by or created_by or "unknown"
        problem = Problem(
            title=title,
            managed_by=agent,
            created_by=created_by or agent,
            related_incident_ids=related_incident_ids or [],
            workaround=workaround,
            tags=tags or [],
        )
        problem.timeline.append(
            _make_timeline_entry(agent, "created", f"Problem identified: {title}")
        )
        self._write_record(
            self.problems_dir, problem.id, title, problem.model_dump()
        )

        self._publish_event("itil.problem.created", {
            "id": problem.id,
            "title": title,
            "related_incidents": related_incident_ids or [],
        })

        # Auto-create GTD project
        self._create_gtd_project_for_problem(problem)

        return problem

    def update_problem(
        self,
        problem_id: str,
        agent: str,
        new_status: str | None = None,
        root_cause: str | None = None,
        workaround: str | None = None,
        note: str = "",
        create_kedb: bool = False,
    ) -> Problem:
        """Update a problem's status or metadata."""
        prb = self._load_record(self.problems_dir, problem_id, Problem)
        if prb is None:
            raise ValueError(f"Problem {problem_id} not found")

        if new_status:
            current = prb.status.value
            if new_status not in _PROBLEM_TRANSITIONS.get(current, set()):
                raise ValueError(
                    f"Invalid transition: {current} -> {new_status}. "
                    f"Valid: {_PROBLEM_TRANSITIONS.get(current, set())}"
                )
            prb.status = ProblemStatus(new_status)
            prb.timeline.append(
                _make_timeline_entry(agent, f"status:{current}->{new_status}", note)
            )

        if root_cause:
            prb.root_cause = root_cause
        if workaround:
            prb.workaround = workaround

        if note and not new_status:
            prb.timeline.append(_make_timeline_entry(agent, "note", note))

        # Auto-create KEDB entry when transitioning to known_error
        if create_kedb and prb.root_cause:
            kedb = self.create_kedb_entry(
                title=prb.title,
                symptoms=[],
                root_cause=prb.root_cause,
                workaround=prb.workaround or "",
                related_problem_id=prb.id,
                managed_by=agent,
            )
            prb.kedb_id = kedb.id

        self._update_record(
            self.problems_dir, prb.id, prb.title, prb.model_dump()
        )
        return prb

    def list_problems(self, status: str | None = None) -> list[Problem]:
        """List problems with optional status filter."""
        problems = self._load_records(self.problems_dir, Problem)
        if status:
            problems = [p for p in problems if p.status.value == status]
        return problems

    # ── Changes ───────────────────────────────────────────────────────

    def propose_change(
        self,
        title: str,
        change_type: str = "normal",
        risk: str = "medium",
        rollback_plan: str = "",
        test_plan: str = "",
        managed_by: str = "",
        created_by: str = "",
        implementer: str | None = None,
        related_problem_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Change:
        """Propose a new change (RFC)."""
        agent = managed_by or created_by or "unknown"
        ct = ChangeType(change_type)
        change = Change(
            title=title,
            change_type=ct,
            risk=Risk(risk),
            rollback_plan=rollback_plan,
            test_plan=test_plan,
            managed_by=agent,
            created_by=created_by or agent,
            implementer=implementer,
            cab_required=ct != ChangeType.STANDARD,
            related_problem_id=related_problem_id,
            tags=tags or [],
        )
        change.timeline.append(
            _make_timeline_entry(agent, "proposed", f"RFC: {title}")
        )

        # Standard changes auto-approve
        if ct == ChangeType.STANDARD:
            change.status = ChangeStatus.APPROVED
            change.timeline.append(
                _make_timeline_entry(agent, "auto-approved", "Standard change")
            )

        self._write_record(
            self.changes_dir, change.id, title, change.model_dump()
        )

        self._publish_event("itil.change.proposed", {
            "id": change.id,
            "title": title,
            "change_type": change_type,
            "cab_required": change.cab_required,
        })

        return change

    def update_change(
        self,
        change_id: str,
        agent: str,
        new_status: str | None = None,
        note: str = "",
    ) -> Change:
        """Update a change's status."""
        chg = self._load_record(self.changes_dir, change_id, Change)
        if chg is None:
            raise ValueError(f"Change {change_id} not found")

        if new_status:
            current = chg.status.value
            if new_status not in _CHANGE_TRANSITIONS.get(current, set()):
                raise ValueError(
                    f"Invalid transition: {current} -> {new_status}. "
                    f"Valid: {_CHANGE_TRANSITIONS.get(current, set())}"
                )
            chg.status = ChangeStatus(new_status)
            chg.timeline.append(
                _make_timeline_entry(agent, f"status:{current}->{new_status}", note)
            )

            if new_status == "approved":
                self._publish_event("itil.change.approved", {
                    "id": chg.id, "title": chg.title, "implementer": chg.implementer,
                })
                # Auto-create GTD next-action for implementer
                if chg.implementer:
                    self._create_gtd_item_for_change(chg)
            elif new_status == "deployed":
                self._publish_event("itil.change.deployed", {
                    "id": chg.id, "title": chg.title,
                })

        if note and not new_status:
            chg.timeline.append(_make_timeline_entry(agent, "note", note))

        self._update_record(
            self.changes_dir, chg.id, chg.title, chg.model_dump()
        )
        return chg

    def list_changes(self, status: str | None = None) -> list[Change]:
        """List changes with optional status filter."""
        changes = self._load_records(self.changes_dir, Change)
        if status:
            changes = [c for c in changes if c.status.value == status]
        return changes

    # ── CAB ───────────────────────────────────────────────────────────

    def submit_cab_vote(
        self,
        change_id: str,
        agent: str,
        decision: str = "abstain",
        conditions: str = "",
    ) -> CABDecision:
        """Submit a CAB vote for a change (per-agent file)."""
        self.ensure_dirs()
        vote = CABDecision(
            change_id=change_id,
            agent=agent,
            decision=CABDecisionValue(decision),
            conditions=conditions,
        )
        filename = f"{change_id}-{agent}.json"
        path = self.cab_dir / filename
        path.write_text(
            json.dumps(vote.model_dump(), indent=2, default=str) + "\n",
            encoding="utf-8",
        )

        # Check if all required votes are in and auto-approve/reject
        self._evaluate_cab(change_id)

        return vote

    def get_cab_votes(self, change_id: str) -> list[CABDecision]:
        """Get all CAB votes for a change."""
        votes = []
        if not self.cab_dir.exists():
            return votes
        for f in self.cab_dir.glob(f"{change_id}-*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                votes.append(CABDecision.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return votes

    def _evaluate_cab(self, change_id: str) -> None:
        """Evaluate CAB votes and auto-transition if decisive."""
        chg = self._load_record(self.changes_dir, change_id, Change)
        if chg is None or chg.status.value not in ("proposed", "reviewing"):
            return

        votes = self.get_cab_votes(change_id)
        if not votes:
            return

        rejections = [v for v in votes if v.decision == CABDecisionValue.REJECTED]
        approvals = [v for v in votes if v.decision == CABDecisionValue.APPROVED]

        # Any rejection blocks the change
        if rejections:
            try:
                self.update_change(change_id, "cab-system", new_status="rejected",
                                   note=f"Rejected by: {', '.join(v.agent for v in rejections)}")
            except ValueError:
                pass
            return

        # Need at least one human approval for normal changes
        human_approvals = [v for v in approvals if v.agent == "human"]
        if human_approvals:
            try:
                self.update_change(change_id, "cab-system", new_status="approved",
                                   note=f"Approved by: {', '.join(v.agent for v in approvals)}")
            except ValueError:
                pass

    # ── KEDB ──────────────────────────────────────────────────────────

    def create_kedb_entry(
        self,
        title: str,
        symptoms: list[str],
        root_cause: str = "",
        workaround: str = "",
        permanent_fix_change_id: str | None = None,
        related_problem_id: str | None = None,
        managed_by: str = "",
        tags: list[str] | None = None,
    ) -> KEDBEntry:
        """Create a Known Error Database entry."""
        entry = KEDBEntry(
            title=title,
            symptoms=symptoms,
            root_cause=root_cause,
            workaround=workaround,
            permanent_fix_change_id=permanent_fix_change_id,
            related_problem_id=related_problem_id,
            managed_by=managed_by,
            tags=tags or [],
        )
        self._write_record(
            self.kedb_dir, entry.id, title, entry.model_dump()
        )
        return entry

    def search_kedb(self, query: str) -> list[KEDBEntry]:
        """Search KEDB entries by matching query against title, symptoms, root_cause."""
        entries = self._load_records(self.kedb_dir, KEDBEntry)
        query_lower = query.lower()
        results = []
        for e in entries:
            searchable = " ".join([
                e.title,
                " ".join(e.symptoms),
                e.root_cause,
                e.workaround,
                " ".join(e.tags),
            ]).lower()
            if query_lower in searchable:
                results.append(e)
        return results

    # ── Status dashboard ──────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return a dashboard summary of all ITIL records."""
        incidents = self._load_records(self.incidents_dir, Incident)
        problems = self._load_records(self.problems_dir, Problem)
        changes = self._load_records(self.changes_dir, Change)
        kedb = self._load_records(self.kedb_dir, KEDBEntry)

        open_inc_statuses = {"detected", "acknowledged", "investigating", "escalated"}
        open_incidents = [i for i in incidents if i.status.value in open_inc_statuses]
        active_problems = [p for p in problems if p.status.value != "resolved"]
        pending_changes = [
            c for c in changes
            if c.status.value in ("proposed", "reviewing", "approved", "implementing")
        ]

        return {
            "incidents": {
                "total": len(incidents),
                "open": len(open_incidents),
                "by_severity": {
                    sev.value: sum(1 for i in open_incidents if i.severity == sev)
                    for sev in Severity
                },
                "open_list": [
                    {
                        "id": i.id,
                        "title": i.title,
                        "severity": i.severity.value,
                        "status": i.status.value,
                        "managed_by": i.managed_by,
                        "detected_at": i.detected_at,
                    }
                    for i in open_incidents
                ],
            },
            "problems": {
                "total": len(problems),
                "active": len(active_problems),
                "active_list": [
                    {
                        "id": p.id,
                        "title": p.title,
                        "status": p.status.value,
                        "managed_by": p.managed_by,
                    }
                    for p in active_problems
                ],
            },
            "changes": {
                "total": len(changes),
                "pending": len(pending_changes),
                "pending_list": [
                    {
                        "id": c.id,
                        "title": c.title,
                        "status": c.status.value,
                        "change_type": c.change_type.value,
                        "managed_by": c.managed_by,
                    }
                    for c in pending_changes
                ],
            },
            "kedb": {
                "total": len(kedb),
            },
        }

    # ── Auto-close / Escalation (for scheduled tasks) ─────────────────

    def auto_close_resolved(self, stable_hours: int = 24) -> list[str]:
        """Auto-close incidents that have been resolved for stable_hours."""
        now = datetime.now(timezone.utc)
        closed_ids = []
        for inc in self.list_incidents(status="resolved"):
            if inc.resolved_at:
                try:
                    resolved = datetime.fromisoformat(
                        inc.resolved_at.replace("Z", "+00:00")
                    )
                    hours = (now - resolved).total_seconds() / 3600
                    if hours >= stable_hours:
                        self.update_incident(
                            inc.id, "auto-close",
                            new_status="closed",
                            note=f"Auto-closed after {int(hours)}h stable",
                        )
                        closed_ids.append(inc.id)
                except (ValueError, TypeError):
                    continue
        return closed_ids

    def check_sla_breaches(self) -> list[dict[str, Any]]:
        """Check for SLA breaches on open incidents."""
        now = datetime.now(timezone.utc)
        breaches = []
        sla_minutes = {"sev1": 5, "sev2": 15, "sev3": 60, "sev4": 240}

        for inc in self.list_incidents():
            if inc.status.value in ("resolved", "closed"):
                continue
            if inc.status.value == "detected" and inc.detected_at:
                try:
                    detected = datetime.fromisoformat(
                        inc.detected_at.replace("Z", "+00:00")
                    )
                    elapsed_min = (now - detected).total_seconds() / 60
                    limit = sla_minutes.get(inc.severity.value, 60)
                    if elapsed_min > limit:
                        breaches.append({
                            "id": inc.id,
                            "severity": inc.severity.value,
                            "breach_type": "unacknowledged",
                            "elapsed_minutes": round(elapsed_min),
                            "sla_minutes": limit,
                        })
                        self._publish_event("itil.sla.breach", {
                            "id": inc.id,
                            "severity": inc.severity.value,
                            "breach_type": "unacknowledged",
                        })
                except (ValueError, TypeError):
                    continue
        return breaches

    # ── ITIL Board generation ─────────────────────────────────────────

    def generate_board_md(self) -> str:
        """Generate an ITIL-BOARD.md overview."""
        status = self.get_status()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            "# ITIL Service Management Board",
            f"*Auto-generated {now} — do not edit manually*",
            "",
        ]

        # Incidents
        inc = status["incidents"]
        lines.append(f"## Open Incidents ({inc['open']})")
        lines.append("")
        if inc["open_list"]:
            for i in inc["open_list"]:
                sev_icon = {"sev1": "P1", "sev2": "P2", "sev3": "P3", "sev4": "P4"}.get(
                    i["severity"], "?"
                )
                lines.append(
                    f"- **[{i['id']}]** {sev_icon} {i['title']} "
                    f"({i['status']}) @{i['managed_by']}"
                )
        else:
            lines.append("*No open incidents*")
        lines.append("")

        # Problems
        prb = status["problems"]
        lines.append(f"## Active Problems ({prb['active']})")
        lines.append("")
        if prb["active_list"]:
            for p in prb["active_list"]:
                lines.append(
                    f"- **[{p['id']}]** {p['title']} ({p['status']}) @{p['managed_by']}"
                )
        else:
            lines.append("*No active problems*")
        lines.append("")

        # Changes
        chg = status["changes"]
        lines.append(f"## Pending Changes ({chg['pending']})")
        lines.append("")
        if chg["pending_list"]:
            for c in chg["pending_list"]:
                lines.append(
                    f"- **[{c['id']}]** {c['title']} ({c['status']}, "
                    f"{c['change_type']}) @{c['managed_by']}"
                )
        else:
            lines.append("*No pending changes*")
        lines.append("")

        # KEDB
        lines.append(f"## Known Errors ({status['kedb']['total']})")
        lines.append("")

        return "\n".join(lines)

    def write_board_md(self) -> Path:
        """Write ITIL-BOARD.md to the ITIL directory."""
        self.ensure_dirs()
        content = self.generate_board_md()
        path = self.itil_dir / "ITIL-BOARD.md"
        path.write_text(content, encoding="utf-8")
        return path

    # ── GTD integration helpers ───────────────────────────────────────

    def _create_gtd_item_for_incident(self, incident: Incident) -> Optional[str]:
        """Auto-create a GTD inbox/next-action item for an incident."""
        try:
            from .mcp_tools.gtd_tools import _make_item, _load_list, _save_list

            priority_map = {"sev1": "critical", "sev2": "high", "sev3": "medium", "sev4": "low"}
            priority = priority_map.get(incident.severity.value, "medium")

            text = f"[ITIL:{incident.id}] {incident.title}"
            item = _make_item(text=text, source="itil", context="@ops")
            item["priority"] = priority

            if incident.severity.value in ("sev1", "sev2"):
                # Urgent: go straight to next-actions
                item["status"] = "next"
                items = _load_list("next-actions")
                items.append(item)
                _save_list("next-actions", items)
            else:
                # Minor: inbox for processing
                items = _load_list("inbox")
                items.append(item)
                _save_list("inbox", items)

            return item["id"]
        except Exception:
            logger.debug("Failed to create GTD item for incident %s", incident.id)
            return None

    def _create_gtd_project_for_problem(self, problem: Problem) -> Optional[str]:
        """Auto-create a GTD project for a problem investigation."""
        try:
            from .mcp_tools.gtd_tools import _make_item, _load_list, _save_list

            text = f"[ITIL:{problem.id}] Investigate: {problem.title}"
            item = _make_item(text=text, source="itil", context="@ops")
            item["status"] = "project"

            projects = _load_list("projects")
            projects.append(item)
            _save_list("projects", projects)

            return item["id"]
        except Exception:
            logger.debug("Failed to create GTD project for problem %s", problem.id)
            return None

    def _create_gtd_item_for_change(self, change: Change) -> Optional[str]:
        """Auto-create a GTD next-action for an approved change."""
        try:
            from .mcp_tools.gtd_tools import _make_item, _load_list, _save_list

            text = f"[ITIL:{change.id}] Implement: {change.title}"
            item = _make_item(text=text, source="itil", context="@ops")
            item["status"] = "next"
            item["priority"] = "high"

            items = _load_list("next-actions")
            items.append(item)
            _save_list("next-actions", items)

            change.gtd_item_ids.append(item["id"])
            return item["id"]
        except Exception:
            logger.debug("Failed to create GTD item for change %s", change.id)
            return None

    def _complete_gtd_items(self, gtd_item_ids: list[str]) -> None:
        """Mark linked GTD items as done when an incident is resolved."""
        try:
            from .mcp_tools.gtd_tools import (
                _find_item_across_lists,
                _remove_item_from_list,
                _load_archive,
                _save_archive,
            )

            for item_id in gtd_item_ids:
                source_list, item, _ = _find_item_across_lists(item_id)
                if source_list and item:
                    _remove_item_from_list(source_list, item_id)
                    item["status"] = "done"
                    item["completed_at"] = _now_iso()
                    archive = _load_archive()
                    archive.append(item)
                    _save_archive(archive)
        except Exception:
            logger.debug("Failed to complete GTD items: %s", gtd_item_ids)

    # ── PubSub helper ─────────────────────────────────────────────────

    def _publish_event(self, topic: str, payload: dict) -> None:
        """Publish an ITIL event via PubSub (best-effort)."""
        try:
            from .pubsub import PubSub

            agent_name = payload.get("managed_by", "itil-system")
            bus = PubSub(self.home, agent_name=agent_name)
            bus.publish(topic, payload, ttl_seconds=86400)
        except Exception:
            logger.debug("Failed to publish event %s", topic)

        # Also push to activity bus
        try:
            from . import activity
            activity.push(topic, payload)
        except Exception as exc:
            logger.warning("Failed to push ITIL event %s to activity bus: %s", topic, exc)
