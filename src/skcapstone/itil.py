"""
SKCapstone ITIL Service Management — Incident, Problem, and Change Management.

Conflict-free design (prb-7810b08e / chg-11d0e1c7): each mutable record is a
directory keyed only by its id.  The immutable birth-facts live in a write-once
``core.json``; every subsequent change is an append-only event line in a
per-writer log (``events/<agent>@<host>.jsonl``).  Current state (status,
severity, timeline, timestamps, resolution) is *folded* deterministically on
read by sorting all events across every writer file and replaying them through
the lifecycle transition tables.  Single-writer-per-file means disjoint write
sets, so Syncthing has nothing to conflict.  CAB votes already used this
per-agent-file pattern; this generalizes it to the whole record set.

Directory layout:
    ~/.skcapstone/coordination/itil/
    ├── incidents/<id>/{core.json, events/<agent>@<host>.jsonl}
    ├── problems/<id>/{core.json, events/<agent>@<host>.jsonl}
    ├── changes/<id>/{core.json, events/<agent>@<host>.jsonl}
    ├── cab-decisions/<change_id>-<agent>.json   # per-agent CAB vote (unchanged)
    ├── kedb/<id>.json                           # write-once (slug dropped)
    └── ITIL-BOARD.md                            # regenerated on one pinned node
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.itil")

# Node identifier used to make every writer file globally unique
# (``events/<agent>@<host>.jsonl``).  A bare ``<agent>.jsonl`` is forbidden -
# that is the heartbeat-v1 collision documented in ~/.skcapstone/.stignore.
_HOSTNAME = socket.gethostname()


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

# Severity rank - higher is more severe.  Folded severity takes the max
# (escalate-only), which is safer for alerting and reproduces the historical
# escalate-only behavior across concurrent writers.
_SEV_RANK: dict[str, int] = {"sev1": 4, "sev2": 3, "sev3": 2, "sev4": 1}


def _max_severity(a: str, b: str) -> str:
    """Return the more severe of two severity strings (sev1 wins)."""
    return a if _SEV_RANK.get(a, 0) >= _SEV_RANK.get(b, 0) else b


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
    detected_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    related_incident_ids: list[str] = Field(default_factory=list)
    related_change_id: Optional[str] = None
    kedb_id: Optional[str] = None
    gtd_item_ids: list[str] = Field(default_factory=list)
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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tags: list[str] = Field(default_factory=list)


class CABDecision(BaseModel):
    change_id: str
    agent: str
    decision: CABDecisionValue = CABDecisionValue.ABSTAIN
    conditions: str = ""
    decided_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug (kept for the human ``label``)."""
    slug = text.lower().strip()
    slug = re.sub(r'[/\\:*?"<>|]', "-", slug)
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")[:40]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_timeline_entry(agent: str, action: str, note: str = "") -> dict[str, str]:
    return {
        "ts": _now_iso(),
        "agent": agent,
        "action": action,
        "note": note,
    }


def _auto_incident_id(service: str, failure_class: str, day_bucket: str) -> str:
    """Compute a deterministic incident id for an auto-detected outage.

    Two nodes detecting the same failure of the same service on the same day
    compute the identical id, so they converge on one ``core.json``.

    Args:
        service: Affected service name (e.g. ``"skvector (Qdrant)"``).
        failure_class: Coarse failure classification (e.g. ``"unreachable"``).
        day_bucket: ``YYYY-MM-DD`` window key.

    Returns:
        A stable ``inc-<hash>`` id.
    """
    key = f"{service}|{failure_class}|{day_bucket}"
    return "inc-" + hashlib.blake2b(key.encode("utf-8"), digest_size=4).hexdigest()


# ---------------------------------------------------------------------------
# ITILManager
# ---------------------------------------------------------------------------


class ITILManager:
    """Manages ITIL records on disk as immutable core + folded event logs.

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

    # ── File I/O: immutable core + append-only per-writer event logs ──────

    def _writer_id(self, agent: str) -> str:
        """Return this process's globally-unique writer key ``<agent>@<host>``."""
        return f"{agent or 'unknown'}@{_HOSTNAME}"

    def _write_core(self, directory: Path, record_id: str, core: dict) -> Path:
        """Write ``<dir>/<id>/core.json`` write-once, create-if-absent.

        Uses ``O_CREAT|O_EXCL`` so a concurrent create race on the same
        deterministic id is safe: the loser gets ``FileExistsError`` and the
        existing (byte-identical, for deterministic ids) core wins.

        Args:
            directory: The record-type directory (incidents/problems/changes).
            record_id: Stable record id (the directory name - never a slug).
            core: The immutable birth-facts dict.

        Returns:
            Path to ``core.json`` (existing one on a create race).
        """
        self.ensure_dirs()
        rec_dir = directory / record_id
        rec_dir.mkdir(parents=True, exist_ok=True)
        core_path = rec_dir / "core.json"
        payload = (json.dumps(core, indent=2, default=str) + "\n").encode("utf-8")
        try:
            fd = os.open(str(core_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return core_path
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return core_path

    def _append_event(
        self, directory: Path, record_id: str, agent: str, kind: str, **payload: Any
    ) -> None:
        """Append one event line to this writer's own log (flock-guarded).

        ``seq`` is assigned as the current line count of the writer's file, so
        it monotonically increases per-writer and tie-breaks equal timestamps
        from the same writer.

        Args:
            directory: The record-type directory.
            record_id: Stable record id.
            agent: The logical writer (agent) name.
            kind: Event kind (``status``/``severity``/``note``/...).
            **payload: Kind-specific fields (``to``, ``note``, ``id``, ...).
        """
        rec_dir = directory / record_id
        events_dir = rec_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{self._writer_id(agent)}.jsonl"
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                seq = sum(1 for _ in fh)
                event = {
                    "event_id": uuid.uuid4().hex,
                    "ts": _now_iso(),
                    "writer": agent,
                    "node": _HOSTNAME,
                    "seq": seq,
                    "kind": kind,
                }
                event.update(payload)
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(event, default=str) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read_events(self, directory: Path, record_id: str) -> list[dict]:
        """Read + totally-order every event across all writer files.

        The sort key ``(ts, node, writer, seq)`` is present in the data and
        identical on every replica once Syncthing converges, giving a
        deterministic CRDT-style op-log order with no locking.
        """
        events: list[dict] = []
        events_dir = directory / record_id / "events"
        if not events_dir.is_dir():
            return events
        for f in sorted(events_dir.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed event line in %s", f.name)
                    continue
        events.sort(
            key=lambda e: (
                e.get("ts", ""),
                e.get("node", ""),
                e.get("writer", ""),
                e.get("seq", 0),
            )
        )
        return events

    def _load_core(self, directory: Path, record_id: str) -> Optional[dict]:
        """Load a record's immutable ``core.json`` (or None if absent/bad)."""
        core_path = directory / record_id / "core.json"
        if not core_path.exists():
            return None
        try:
            return json.loads(core_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bad core.json for %s: %s", record_id, exc)
            return None

    def _resolve_id(self, directory: Path, record_id: str) -> str:
        """Follow ``redirect.json`` stubs (from migration merges) to canonical id."""
        seen: set[str] = set()
        cur = record_id
        while cur not in seen:
            seen.add(cur)
            rec_dir = directory / cur
            if (rec_dir / "core.json").exists():
                return cur
            redirect = rec_dir / "redirect.json"
            if not redirect.exists():
                return cur
            try:
                cur = json.loads(redirect.read_text(encoding="utf-8"))["canonical"]
            except Exception:  # noqa: BLE001
                return cur
        return cur

    def _record_exists(self, directory: Path, record_id: str) -> bool:
        """True if the (redirect-resolved) record has a ``core.json``."""
        rid = self._resolve_id(directory, record_id)
        return (directory / rid / "core.json").exists()

    def _writer_has_kind(self, directory: Path, record_id: str, agent: str, kind: str) -> bool:
        """True if *agent*'s own writer file already holds an event of *kind*.

        Reads only this writer's own file (cheap, no sync lag).  Replaces the
        old fragile last-3-timeline-notes host-tag guard with a structural,
        own-file check that bounds recovery-note volume to one edge per host.
        """
        path = directory / record_id / "events" / f"{self._writer_id(agent)}.jsonl"
        if not path.exists():
            return False
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("kind") == kind:
                        return True
                except json.JSONDecodeError:
                    continue
        except OSError:
            return False
        return False

    def _load_records(self, directory: Path, model_class: type) -> list:
        """Fold every record directory under *directory* into models.

        Keeps skip-and-warn tolerance for legacy flat ``<id>-<slug>.json``
        files so an old/new mixed tree during migration never crashes a read.
        """
        records: list = []
        if not directory.exists():
            return records
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir():
                if entry.suffix == ".json":
                    logger.warning(
                        "Skipping legacy flat ITIL file %s - run itil_migrate_events.py",
                        entry.name,
                    )
                continue
            # Bulk load only real records. A redirect-only directory (a merge
            # or re-key stub with no core.json) is a pointer for direct
            # lookups, not a record of its own - skipping it avoids counting
            # the canonical record twice.
            if not (entry / "core.json").exists():
                continue
            rec = self._fold_record(directory, entry.name, model_class)
            if rec is not None:
                records.append(rec)
        return records

    def _fold_record(self, directory: Path, record_id: str, model_class: type):
        """Load core + events for one record and return the folded model."""
        rec_dir = directory / record_id
        core = self._load_core(directory, record_id)
        if core is None:
            redirect = rec_dir / "redirect.json"
            if redirect.exists():
                try:
                    target = json.loads(redirect.read_text(encoding="utf-8"))["canonical"]
                    if target != record_id:
                        return self._fold_record(directory, target, model_class)
                except Exception:  # noqa: BLE001
                    return None
            return None
        events = self._read_events(directory, record_id)
        try:
            if model_class is Incident:
                return self._fold_incident(core, events)
            if model_class is Problem:
                return self._fold_problem(core, events)
            if model_class is Change:
                votes = self.get_cab_votes(core.get("id", record_id))
                return self._fold_change(core, events, votes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fold failed for %s: %s", record_id, exc)
            return None
        return None

    # ── Fold algorithms (pure derivation of state from the event log) ─────

    def _fold_incident(self, core: dict, events: list[dict]) -> Incident:
        """Fold an incident's event log into a fully-populated Incident."""
        status = "detected"
        severity = core.get("severity_at_creation") or "sev3"
        timeline: list[dict[str, Any]] = []
        acknowledged_at = resolved_at = closed_at = None
        resolution_summary = None
        related_problem_id = core.get("related_problem_id")
        tags = list(core.get("tags") or [])
        gtd_ids: list[str] = []
        title = core.get("title", "")
        seen_created = False

        for e in events:
            kind = e.get("kind")
            note = e.get("note", "") or ""
            agent = e.get("writer", "") or ""
            ts = e.get("ts", "")
            if kind == "created":
                if seen_created:
                    continue
                seen_created = True
                timeline.append({"ts": ts, "agent": agent, "action": "created", "note": note})
            elif kind == "status":
                to = e.get("to")
                if to in _INCIDENT_TRANSITIONS.get(status, set()):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                        }
                    )
                    status = to
                    if to == "acknowledged" and not acknowledged_at:
                        acknowledged_at = ts
                    elif to == "resolved":
                        if not resolved_at:
                            resolved_at = ts
                        if e.get("resolution_summary"):
                            resolution_summary = e["resolution_summary"]
                    elif to == "closed" and not closed_at:
                        closed_at = ts
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "reopen":
                if status == "resolved":
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": "status:resolved->investigating",
                            "note": note or "reopened",
                        }
                    )
                    status = "investigating"
                    resolved_at = None
                    resolution_summary = None
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"reopen:{status}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "severity":
                to = e.get("to")
                timeline.append(
                    {
                        "ts": ts,
                        "agent": agent,
                        "action": f"severity:{severity}->{to}",
                        "note": note,
                    }
                )
                severity = _max_severity(severity, to)
            elif kind == "ack":
                if not acknowledged_at:
                    acknowledged_at = ts
                timeline.append({"ts": ts, "agent": agent, "action": "acknowledged", "note": note})
            elif kind == "resolution":
                if e.get("resolution_summary"):
                    resolution_summary = e["resolution_summary"]
            elif kind == "link_problem":
                if e.get("id"):
                    related_problem_id = e["id"]
            elif kind == "title":
                if e.get("text"):
                    title = e["text"]
                timeline.append(
                    {"ts": ts, "agent": agent, "action": "title", "note": e.get("text", "")}
                )
            elif kind == "tags":
                for t in e.get("add") or []:
                    if t not in tags:
                        tags.append(t)
            elif kind == "gtd_link":
                gid = e.get("id")
                if gid and gid not in gtd_ids:
                    gtd_ids.append(gid)
            elif kind in ("note", "recovery"):
                timeline.append({"ts": ts, "agent": agent, "action": "note", "note": note})
            # any other kind (gtd_complete, etc.) is timeline-silent

        return Incident(
            id=core["id"],
            type="incident",
            title=title,
            severity=Severity(severity),
            status=IncidentStatus(status),
            source=core.get("source", "manual"),
            affected_services=list(core.get("affected_services") or []),
            impact=core.get("impact", ""),
            managed_by=core.get("managed_by", ""),
            created_by=core.get("created_by", ""),
            detected_at=core.get("detected_at") or _now_iso(),
            acknowledged_at=acknowledged_at,
            resolved_at=resolved_at,
            closed_at=closed_at,
            timeline=timeline,
            related_problem_id=related_problem_id,
            gtd_item_ids=gtd_ids,
            resolution_summary=resolution_summary,
            tags=tags,
        )

    def _fold_problem(self, core: dict, events: list[dict]) -> Problem:
        """Fold a problem's event log into a fully-populated Problem."""
        status = "identified"
        timeline: list[dict[str, Any]] = []
        root_cause = core.get("root_cause")
        workaround = core.get("workaround")
        kedb_id = core.get("kedb_id")
        related_change_id = core.get("related_change_id")
        tags = list(core.get("tags") or [])
        gtd_ids: list[str] = []
        title = core.get("title", "")
        seen_created = False

        for e in events:
            kind = e.get("kind")
            note = e.get("note", "") or ""
            agent = e.get("writer", "") or ""
            ts = e.get("ts", "")
            if kind == "created":
                if seen_created:
                    continue
                seen_created = True
                timeline.append({"ts": ts, "agent": agent, "action": "created", "note": note})
            elif kind == "status":
                to = e.get("to")
                if to in _PROBLEM_TRANSITIONS.get(status, set()):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                        }
                    )
                    status = to
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "root_cause":
                if e.get("text"):
                    root_cause = e["text"]
            elif kind == "workaround":
                if e.get("text"):
                    workaround = e["text"]
            elif kind == "link_kedb":
                if e.get("id"):
                    kedb_id = e["id"]
            elif kind == "link_change":
                if e.get("id"):
                    related_change_id = e["id"]
            elif kind == "title":
                if e.get("text"):
                    title = e["text"]
                timeline.append(
                    {"ts": ts, "agent": agent, "action": "title", "note": e.get("text", "")}
                )
            elif kind == "tags":
                for t in e.get("add") or []:
                    if t not in tags:
                        tags.append(t)
            elif kind == "gtd_link":
                gid = e.get("id")
                if gid and gid not in gtd_ids:
                    gtd_ids.append(gid)
            elif kind == "note":
                timeline.append({"ts": ts, "agent": agent, "action": "note", "note": note})

        return Problem(
            id=core["id"],
            type="problem",
            title=title,
            status=ProblemStatus(status),
            root_cause=root_cause,
            workaround=workaround,
            managed_by=core.get("managed_by", ""),
            created_by=core.get("created_by", ""),
            created_at=core.get("created_at") or _now_iso(),
            related_incident_ids=list(core.get("related_incident_ids") or []),
            related_change_id=related_change_id,
            kedb_id=kedb_id,
            gtd_item_ids=gtd_ids,
            timeline=timeline,
            tags=tags,
        )

    def _fold_change(self, core: dict, events: list[dict], votes: list["CABDecision"]) -> Change:
        """Fold a change's event log + CAB votes into a Change.

        Standard-change auto-approval and CAB approval/rejection are pure
        fold-time derivations (no writer ever mutates the change record for
        them), reproducing the old ``_evaluate_cab`` logic exactly.
        """
        change_type = core.get("change_type", "normal")
        status = "proposed"
        timeline: list[dict[str, Any]] = []
        tags = list(core.get("tags") or [])
        gtd_ids: list[str] = []
        title = core.get("title", "")
        related_problem_id = core.get("related_problem_id")
        seen_created = False

        for e in events:
            kind = e.get("kind")
            note = e.get("note", "") or ""
            agent = e.get("writer", "") or ""
            ts = e.get("ts", "")
            if kind in ("created", "proposed"):
                if seen_created:
                    continue
                seen_created = True
                timeline.append({"ts": ts, "agent": agent, "action": "proposed", "note": note})
            elif kind == "status":
                to = e.get("to")
                if to in _CHANGE_TRANSITIONS.get(status, set()):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                        }
                    )
                    status = to
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "link_problem":
                if e.get("id"):
                    related_problem_id = e["id"]
            elif kind == "title":
                if e.get("text"):
                    title = e["text"]
                timeline.append(
                    {"ts": ts, "agent": agent, "action": "title", "note": e.get("text", "")}
                )
            elif kind == "tags":
                for t in e.get("add") or []:
                    if t not in tags:
                        tags.append(t)
            elif kind == "gtd_link":
                gid = e.get("id")
                if gid and gid not in gtd_ids:
                    gtd_ids.append(gid)
            elif kind in ("note", "auto-approved"):
                action = "auto-approved" if kind == "auto-approved" else "note"
                timeline.append({"ts": ts, "agent": agent, "action": action, "note": note})

        # Standard changes auto-approve at fold time (never stored).
        if change_type == "standard" and status == "proposed":
            status = "approved"
            timeline.append(
                {
                    "ts": core.get("created_at") or _now_iso(),
                    "agent": core.get("created_by", ""),
                    "action": "auto-approved",
                    "note": "Standard change",
                }
            )

        # CAB derivation - mirrors the old _evaluate_cab guard exactly:
        # any rejection blocks; else >=1 human approval unblocks.
        if status in ("proposed", "reviewing") and votes:
            rejections = [v for v in votes if v.decision == CABDecisionValue.REJECTED]
            approvals = [v for v in votes if v.decision == CABDecisionValue.APPROVED]
            if rejections:
                status = "rejected"
                timeline.append(
                    {
                        "ts": max(v.decided_at for v in rejections),
                        "agent": "cab-system",
                        "action": "status:proposed->rejected",
                        "note": "Rejected by: " + ", ".join(v.agent for v in rejections),
                    }
                )
            elif any(v.agent == "human" for v in approvals):
                status = "approved"
                timeline.append(
                    {
                        "ts": max(v.decided_at for v in approvals),
                        "agent": "cab-system",
                        "action": "status:proposed->approved",
                        "note": "Approved by: " + ", ".join(v.agent for v in approvals),
                    }
                )

        return Change(
            id=core["id"],
            type="change",
            title=title,
            change_type=ChangeType(change_type),
            status=ChangeStatus(status),
            risk=Risk(core.get("risk", "medium")),
            rollback_plan=core.get("rollback_plan", ""),
            test_plan=core.get("test_plan", ""),
            managed_by=core.get("managed_by", ""),
            created_by=core.get("created_by", ""),
            implementer=core.get("implementer"),
            cab_required=bool(core.get("cab_required", change_type != "standard")),
            created_at=core.get("created_at") or _now_iso(),
            related_problem_id=related_problem_id,
            gtd_item_ids=gtd_ids,
            timeline=timeline,
            tags=tags,
        )

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
        failure_class: str | None = None,
    ) -> Incident:
        """Create a new incident record.

        Auto-detected incidents (``source == "service_health"``) get a
        deterministic id (``_auto_incident_id``) so two nodes converge on one
        record; all other sources keep a random id.  ``failure_class`` is an
        optional hint (used only for the deterministic id) - the public
        callers (MCP/CLI) never pass it, so their call sites are unchanged.

        Args:
            failure_class: Coarse failure class for the deterministic id
                (default ``"unreachable"`` for service_health).
        """
        agent = managed_by or created_by or "unknown"
        services = affected_services or []
        if source == "service_health":
            svc = services[0] if services else title
            fc = failure_class or "unreachable"
            day_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            record_id = _auto_incident_id(svc, fc, day_bucket)
            dedup_key = f"{svc}:{fc}"
        else:
            record_id = f"inc-{uuid.uuid4().hex[:8]}"
            dedup_key = None

        detected_at = _now_iso()
        core = {
            "id": record_id,
            "type": "incident",
            "title": title,
            "label": _slugify(title),
            "severity_at_creation": Severity(severity).value,
            "source": source,
            "affected_services": services,
            "impact": impact,
            "managed_by": agent,
            "created_by": created_by or agent,
            "detected_at": detected_at,
            "tags": tags or [],
        }
        if dedup_key:
            core["dedup_key"] = dedup_key

        self._write_core(self.incidents_dir, record_id, core)
        self._append_event(
            self.incidents_dir,
            record_id,
            agent,
            "created",
            note=f"Incident detected: {title}",
        )

        # Auto-create GTD item and link it via an event (no whole-file rewrite).
        inc = self._fold_record(self.incidents_dir, record_id, Incident)
        gtd_id = self._create_gtd_item_for_incident(inc)
        if gtd_id:
            self._append_event(self.incidents_dir, record_id, agent, "gtd_link", id=gtd_id)

        self._publish_event(
            "itil.incident.created",
            {
                "id": record_id,
                "title": title,
                "severity": Severity(severity).value,
                "managed_by": agent,
            },
        )

        return self._fold_record(self.incidents_dir, record_id, Incident)

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
        """Append one event per non-None argument, then return the folded state.

        Transition validation now lives in the fold: a losing concurrent
        transition is flagged ``conflicted`` in the timeline and excluded from
        state, so this method no longer raises on an invalid transition (it
        still raises ``ValueError`` when the incident does not exist).
        """
        rid = self._resolve_id(self.incidents_dir, incident_id)
        if not self._record_exists(self.incidents_dir, rid):
            raise ValueError(f"Incident {incident_id} not found")

        if new_status:
            payload: dict[str, Any] = {"to": new_status, "note": note}
            if new_status == "resolved" and resolution_summary:
                payload["resolution_summary"] = resolution_summary
            self._append_event(self.incidents_dir, rid, agent, "status", **payload)
            if new_status == "resolved":
                folded = self._fold_record(self.incidents_dir, rid, Incident)
                self._complete_gtd_items(folded.gtd_item_ids)

        if severity:
            self._append_event(self.incidents_dir, rid, agent, "severity", to=severity, note=note)
            self._publish_event(
                "itil.incident.escalated",
                {"id": rid, "new_severity": severity},
            )

        if related_problem_id:
            self._append_event(
                self.incidents_dir, rid, agent, "link_problem", id=related_problem_id
            )

        if note and not new_status and not severity:
            self._append_event(self.incidents_dir, rid, agent, "note", note=note)

        return self._fold_record(self.incidents_dir, rid, Incident)

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
            incidents = [i for i in incidents if service in i.affected_services]
        return incidents

    def find_open_incident_for_service(self, service: str) -> Optional[Incident]:
        """Find an existing open incident for a service (convenience dedup read).

        No longer the create authority: create-if-absent on the deterministic
        id is.  This remains a cheap convenience so a single node does not
        re-emit a ``created`` event every health cycle while a service is down.
        """
        open_statuses = {"detected", "acknowledged", "investigating", "escalated"}
        for inc in self.list_incidents():
            if inc.status.value in open_statuses and service in inc.affected_services:
                return inc
        return None

    def note_recovery(self, incident_id: str, agent: str, note: str) -> None:
        """Append a one-per-host recovery note to an open incident.

        Structurally bounded: appends at most one ``recovery`` event per writer
        (own-file check), so a service flapping up cannot balloon the log.
        """
        rid = self._resolve_id(self.incidents_dir, incident_id)
        if not self._record_exists(self.incidents_dir, rid):
            return
        if self._writer_has_kind(self.incidents_dir, rid, agent, "recovery"):
            return
        self._append_event(self.incidents_dir, rid, agent, "recovery", note=note)

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
        record_id = f"prb-{uuid.uuid4().hex[:8]}"
        core = {
            "id": record_id,
            "type": "problem",
            "title": title,
            "label": _slugify(title),
            "managed_by": agent,
            "created_by": created_by or agent,
            "created_at": _now_iso(),
            "related_incident_ids": related_incident_ids or [],
            "workaround": workaround or None,
            "tags": tags or [],
        }
        self._write_core(self.problems_dir, record_id, core)
        self._append_event(
            self.problems_dir,
            record_id,
            agent,
            "created",
            note=f"Problem identified: {title}",
        )

        prb = self._fold_record(self.problems_dir, record_id, Problem)
        gtd_id = self._create_gtd_project_for_problem(prb)
        if gtd_id:
            self._append_event(self.problems_dir, record_id, agent, "gtd_link", id=gtd_id)

        self._publish_event(
            "itil.problem.created",
            {
                "id": record_id,
                "title": title,
                "related_incidents": related_incident_ids or [],
            },
        )

        return self._fold_record(self.problems_dir, record_id, Problem)

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
        """Append problem events, optionally spawn a KEDB entry, return folded."""
        rid = self._resolve_id(self.problems_dir, problem_id)
        if not self._record_exists(self.problems_dir, rid):
            raise ValueError(f"Problem {problem_id} not found")

        if new_status:
            self._append_event(self.problems_dir, rid, agent, "status", to=new_status, note=note)
            if new_status == ProblemStatus.RESOLVED.value:
                folded = self._fold_record(self.problems_dir, rid, Problem)
                self._complete_gtd_items(folded.gtd_item_ids)

        if root_cause:
            self._append_event(self.problems_dir, rid, agent, "root_cause", text=root_cause)
        if workaround:
            self._append_event(self.problems_dir, rid, agent, "workaround", text=workaround)

        if note and not new_status:
            self._append_event(self.problems_dir, rid, agent, "note", note=note)

        # Auto-create KEDB entry when a root cause is known.
        if create_kedb:
            prb = self._fold_record(self.problems_dir, rid, Problem)
            if prb.root_cause:
                kedb = self.create_kedb_entry(
                    title=prb.title,
                    symptoms=[],
                    root_cause=prb.root_cause,
                    workaround=prb.workaround or "",
                    related_problem_id=prb.id,
                    managed_by=agent,
                )
                self._append_event(self.problems_dir, rid, agent, "link_kedb", id=kedb.id)

        return self._fold_record(self.problems_dir, rid, Problem)

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
        """Propose a new change (RFC).

        Status is never stored: standard-change auto-approval and CAB outcome
        are pure fold-time derivations.
        """
        agent = managed_by or created_by or "unknown"
        ct = ChangeType(change_type)
        record_id = f"chg-{uuid.uuid4().hex[:8]}"
        core = {
            "id": record_id,
            "type": "change",
            "title": title,
            "label": _slugify(title),
            "change_type": ct.value,
            "risk": Risk(risk).value,
            "rollback_plan": rollback_plan,
            "test_plan": test_plan,
            "managed_by": agent,
            "created_by": created_by or agent,
            "implementer": implementer,
            "cab_required": ct != ChangeType.STANDARD,
            "related_problem_id": related_problem_id,
            "created_at": _now_iso(),
            "tags": tags or [],
        }
        self._write_core(self.changes_dir, record_id, core)
        self._append_event(self.changes_dir, record_id, agent, "created", note=f"RFC: {title}")

        self._publish_event(
            "itil.change.proposed",
            {
                "id": record_id,
                "title": title,
                "change_type": ct.value,
                "cab_required": ct != ChangeType.STANDARD,
            },
        )

        return self._fold_record(self.changes_dir, record_id, Change)

    def update_change(
        self,
        change_id: str,
        agent: str,
        new_status: str | None = None,
        note: str = "",
    ) -> Change:
        """Append a change event and return the folded change.

        Like the other updaters, transition validation is folded (no raise on
        a losing concurrent transition); still raises if the change is unknown.
        """
        rid = self._resolve_id(self.changes_dir, change_id)
        core = self._load_core(self.changes_dir, rid)
        if core is None:
            raise ValueError(f"Change {change_id} not found")

        if new_status:
            self._append_event(self.changes_dir, rid, agent, "status", to=new_status, note=note)
            if new_status == "approved":
                self._publish_event(
                    "itil.change.approved",
                    {
                        "id": rid,
                        "title": core.get("title", ""),
                        "implementer": core.get("implementer"),
                    },
                )
                implementer = core.get("implementer")
                if implementer:
                    gtd_id = self._gtd_emit(
                        f"[ITIL:{rid}] Implement: {core.get('title', '')}",
                        rid,
                        "next",
                        "high",
                    )
                    if gtd_id:
                        self._append_event(self.changes_dir, rid, agent, "gtd_link", id=gtd_id)
            elif new_status == "deployed":
                self._publish_event(
                    "itil.change.deployed",
                    {"id": rid, "title": core.get("title", "")},
                )

        if note and not new_status:
            self._append_event(self.changes_dir, rid, agent, "note", note=note)

        return self._fold_record(self.changes_dir, rid, Change)

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
        """Submit a CAB vote for a change (per-agent file, already conflict-free).

        The change's approved/rejected status is now a pure fold-time
        derivation from these vote files - no write back to the change record.
        """
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
        return vote

    def get_cab_votes(self, change_id: str) -> list[CABDecision]:
        """Get all CAB votes for a change."""
        votes: list[CABDecision] = []
        if not self.cab_dir.exists():
            return votes
        for f in self.cab_dir.glob(f"{change_id}-*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                votes.append(CABDecision.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return votes

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
        """Create a Known Error Database entry (write-once, slug dropped)."""
        self.ensure_dirs()
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
        path = self.kedb_dir / f"{entry.id}.json"
        path.write_text(
            json.dumps(entry.model_dump(), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return entry

    def _load_kedb(self) -> list[KEDBEntry]:
        """Load all KEDB entries (flat write-once files)."""
        entries: list[KEDBEntry] = []
        if not self.kedb_dir.exists():
            return entries
        for f in sorted(self.kedb_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                entries.append(KEDBEntry.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return entries

    def search_kedb(self, query: str) -> list[KEDBEntry]:
        """Search KEDB entries by matching query against title, symptoms, root_cause."""
        entries = self._load_kedb()
        query_lower = query.lower()
        results = []
        for e in entries:
            searchable = " ".join(
                [
                    e.title,
                    " ".join(e.symptoms),
                    e.root_cause,
                    e.workaround,
                    " ".join(e.tags),
                ]
            ).lower()
            if query_lower in searchable:
                results.append(e)
        return results

    # ── Status dashboard ──────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return a dashboard summary of all ITIL records."""
        incidents = self._load_records(self.incidents_dir, Incident)
        problems = self._load_records(self.problems_dir, Problem)
        changes = self._load_records(self.changes_dir, Change)
        kedb = self._load_kedb()

        open_inc_statuses = {"detected", "acknowledged", "investigating", "escalated"}
        open_incidents = [i for i in incidents if i.status.value in open_inc_statuses]
        active_problems = [p for p in problems if p.status.value != "resolved"]
        pending_changes = [
            c
            for c in changes
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
        """Auto-close incidents that have been resolved for stable_hours.

        The close is a plain append (via ``update_incident``) to this node's
        own ``auto-close@<host>.jsonl`` writer file; concurrent closes from
        multiple nodes fold idempotently (the first valid close wins).
        """
        now = datetime.now(timezone.utc)
        closed_ids = []
        for inc in self.list_incidents(status="resolved"):
            if inc.resolved_at:
                try:
                    resolved = datetime.fromisoformat(inc.resolved_at.replace("Z", "+00:00"))
                    hours = (now - resolved).total_seconds() / 3600
                    if hours >= stable_hours:
                        self.update_incident(
                            inc.id,
                            "auto-close",
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
                    detected = datetime.fromisoformat(inc.detected_at.replace("Z", "+00:00"))
                    elapsed_min = (now - detected).total_seconds() / 60
                    limit = sla_minutes.get(inc.severity.value, 60)
                    if elapsed_min > limit:
                        breaches.append(
                            {
                                "id": inc.id,
                                "severity": inc.severity.value,
                                "breach_type": "unacknowledged",
                                "elapsed_minutes": round(elapsed_min),
                                "sla_minutes": limit,
                            }
                        )
                        self._publish_event(
                            "itil.sla.breach",
                            {
                                "id": inc.id,
                                "severity": inc.severity.value,
                                "breach_type": "unacknowledged",
                            },
                        )
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
                lines.append(f"- **[{p['id']}]** {p['title']} ({p['status']}) @{p['managed_by']}")
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
        """Write ITIL-BOARD.md to the ITIL directory (run on one pinned node)."""
        self.ensure_dirs()
        content = self.generate_board_md()
        path = self.itil_dir / "ITIL-BOARD.md"
        path.write_text(content, encoding="utf-8")
        return path

    # ── GTD integration — ITIL is a push adapter on the skos gtd-ingest port ──

    def _gtd_emit(
        self, text: str, source_ref: str, status: str, priority: Optional[str] = None
    ) -> Optional[str]:
        """Emit a GTD item through the skos gtd-ingest capture() sink (deduped by
        (source, source_ref)). Falls back to the legacy direct writer if skos is
        not importable, so skcapstone never hard-depends on skos at runtime.
        See skos/docs/gtd-ingest-architecture.md."""
        try:
            from skos.gtd_ingest import GtdCapture, capture  # the port

            return capture(
                GtdCapture(
                    text=text,
                    source="itil",
                    source_ref=source_ref,
                    context="@ops",
                    priority=priority,
                    status=status,
                    meta={"itil_id": source_ref},
                )
            )
        except Exception:
            try:
                from .mcp_tools.gtd_tools import _make_item, _load_list, _save_list

                list_for = {
                    "next": "next-actions",
                    "project": "projects",
                    "waiting": "waiting-for",
                    "someday": "someday-maybe",
                }.get(status, "inbox")
                item = _make_item(text=text, source="itil", context="@ops")
                item["status"] = status
                item["source_ref"] = source_ref
                if priority:
                    item["priority"] = priority
                items = _load_list(list_for)
                items.append(item)
                _save_list(list_for, items)
                return item["id"]
            except Exception:
                logger.debug("Failed to emit GTD item for %s", source_ref)
                return None

    def _create_gtd_item_for_incident(self, incident: Incident) -> Optional[str]:
        """Auto-create a GTD next-action (sev1/sev2) or inbox item (sev3/sev4)."""
        priority = {"sev1": "critical", "sev2": "high", "sev3": "medium", "sev4": "low"}.get(
            incident.severity.value, "medium"
        )
        status = "next" if incident.severity.value in ("sev1", "sev2") else "inbox"
        return self._gtd_emit(
            f"[ITIL:{incident.id}] {incident.title}", incident.id, status, priority
        )

    def _create_gtd_project_for_problem(self, problem: Problem) -> Optional[str]:
        """Auto-create a GTD project for a problem investigation."""
        return self._gtd_emit(
            f"[ITIL:{problem.id}] Investigate: {problem.title}", problem.id, "project"
        )

    def _complete_gtd_items(self, gtd_item_ids: list[str]) -> None:
        """Mark linked GTD items as done when the owning ITIL record resolves."""
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
