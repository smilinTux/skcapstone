"""CMDB / asset management: event-sourced Configuration Items + relationships.

Phase 6 of the SKDashboard, and the missing ITIL leg. Configuration Items (CIs)
are services, hosts, agents, credentials, ports, datastores. Each is stored the
same conflict-free way as ITIL: an immutable ``core.json`` (write-once) plus
append-only per-writer ``events/<agent>@<host>.jsonl``, folded on read. CIs carry
relationships (depends_on / runs_on / hosts / connects_to) so we get a dependency
graph and impact analysis: which CIs a failure cascades to, and which open
incidents affect a CI (via the incident's affected_services).

    ~/.skcapstone/cmdb/<ci_id>/{core.json, events/<agent>@<host>.jsonl}
"""
from __future__ import annotations

import fcntl
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

logger = logging.getLogger("skcapstone.cmdb")

_HOST = socket.gethostname()


class CIType(str, Enum):
    SERVICE = "service"
    HOST = "host"
    AGENT = "agent"
    CREDENTIAL = "credential"
    PORT = "port"
    DATASTORE = "datastore"
    NETWORK = "network"


class CIStatus(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    DOWN = "down"
    RETIRED = "retired"


class Relationship(BaseModel):
    rel_type: str = "depends_on"   # depends_on | runs_on | hosts | connects_to
    target: str                    # target CI id


class ConfigItem(BaseModel):
    id: str
    ci_type: str = CIType.SERVICE.value
    name: str
    status: str = CIStatus.OPERATIONAL.value
    description: str = ""
    owner: str = ""
    node: str = ""                 # host it runs on (for services)
    attributes: dict = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "ci"


def make_ci_id(ci_type: str, name: str) -> str:
    """Deterministic id so re-seeding the same CI is idempotent."""
    return f"ci-{ci_type}-{_slug(name)}"


class CMDBManager:
    """Event-sourced Configuration Management Database."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.cmdb_dir = self.home / "cmdb"

    def ensure_dirs(self) -> None:
        self.cmdb_dir.mkdir(parents=True, exist_ok=True)

    def _writer_id(self, agent: str) -> str:
        safe = (agent or "unknown").replace("/", "-").replace("@", "-")
        return f"{safe}@{_HOST}"

    # ── writes ────────────────────────────────────────────────────────────

    def create_ci(self, name: str, ci_type: str = "service", description: str = "",
                  owner: str = "", node: str = "", attributes: Optional[dict] = None,
                  tags: Optional[list] = None, ci_id: Optional[str] = None) -> ConfigItem:
        """Create (or return existing) a CI. Write-once core, idempotent by id."""
        self.ensure_dirs()
        cid = ci_id or make_ci_id(ci_type, name)
        core = {
            "id": cid, "ci_type": ci_type, "name": name, "description": description,
            "owner": owner, "node": node, "attributes": attributes or {},
            "tags": tags or [], "created_at": _now_iso(),
        }
        rec_dir = self.cmdb_dir / cid
        rec_dir.mkdir(parents=True, exist_ok=True)
        core_path = rec_dir / "core.json"
        payload = (json.dumps(core, indent=2, default=str) + "\n").encode("utf-8")
        try:
            fd = os.open(str(core_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return self.get_ci(cid)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return self.get_ci(cid)

    def _append(self, ci_id: str, agent: str, action: str, **payload: Any) -> None:
        rec_dir = self.cmdb_dir / ci_id
        events_dir = rec_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{self._writer_id(agent)}.jsonl"
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                seq = sum(1 for _ in fh)
                event = {"ts": _now_iso(), "writer": agent, "node": _HOST,
                         "seq": seq, "action": action}
                event.update(payload)
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(event, default=str) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def set_status(self, ci_id: str, agent: str, status: str, note: str = "") -> None:
        self._append(ci_id, agent, "status", status=status, note=note)

    def set_attribute(self, ci_id: str, agent: str, key: str, value: Any) -> None:
        self._append(ci_id, agent, "attribute", key=key, value=value)

    def add_relationship(self, ci_id: str, agent: str, rel_type: str, target: str) -> None:
        self._append(ci_id, agent, "relate", rel_type=rel_type, target=target)

    def remove_relationship(self, ci_id: str, agent: str, rel_type: str, target: str) -> None:
        self._append(ci_id, agent, "unrelate", rel_type=rel_type, target=target)

    # ── reads ─────────────────────────────────────────────────────────────

    def _read_events(self, ci_id: str) -> list[dict]:
        events_dir = self.cmdb_dir / ci_id / "events"
        out: list[dict] = []
        if not events_dir.exists():
            return out
        for f in sorted(events_dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        out.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
        return out

    def get_ci(self, ci_id: str) -> Optional[ConfigItem]:
        core_path = self.cmdb_dir / ci_id / "core.json"
        if not core_path.exists():
            return None
        try:
            core = json.loads(core_path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        ci = ConfigItem(
            id=core["id"], ci_type=core.get("ci_type", "service"), name=core.get("name", ""),
            description=core.get("description", ""), owner=core.get("owner", ""),
            node=core.get("node", ""), attributes=dict(core.get("attributes", {})),
            tags=list(core.get("tags", [])), created_at=core.get("created_at", ""),
        )
        for e in self._read_events(ci_id):
            act = e.get("action")
            if act == "status":
                ci.status = e.get("status", ci.status)
            elif act == "attribute" and e.get("key"):
                ci.attributes[e["key"]] = e.get("value")
            elif act == "relate":
                rel = Relationship(rel_type=e.get("rel_type", "depends_on"), target=e.get("target", ""))
                if rel.target and rel not in ci.relationships:
                    ci.relationships.append(rel)
            elif act == "unrelate":
                ci.relationships = [r for r in ci.relationships
                                    if not (r.rel_type == e.get("rel_type") and r.target == e.get("target"))]
            ci.updated_at = e.get("ts", ci.updated_at)
        return ci

    def list_cis(self, ci_type: Optional[str] = None) -> list[ConfigItem]:
        if not self.cmdb_dir.exists():
            return []
        out = []
        for p in sorted(self.cmdb_dir.iterdir()):
            if not (p.is_dir() and (p / "core.json").exists()):
                continue
            ci = self.get_ci(p.name)
            if ci and (ci_type is None or ci.ci_type == ci_type):
                out.append(ci)
        return out

    def find_for_service(self, service_name: str) -> Optional[ConfigItem]:
        cid = make_ci_id(CIType.SERVICE.value, service_name)
        return self.get_ci(cid)

    def seed_from_inventory(self, agent: str = "cmdb-seed") -> dict:
        """Auto-populate CIs from the fleet + ITIL data (idempotent).

        Creates host CIs for the known nodes, agent CIs, and a service CI for
        every service referenced by an ITIL incident, wiring services to run on
        their host and reflecting current incident status as CI health.
        """
        hosts = {
            "noroc2027": {"desc": ".158 primary / dev source-of-truth", "ip": "192.168.0.158"},
            "cbrd21-laptop12thgenintelcore": {"desc": ".41 heavy-build mirror", "ip": "192.168.0.41"},
            "comfyui": {"desc": ".100 GPU (RTX 5060 Ti) / LLM + embeddings", "ip": "192.168.0.100"},
        }
        created = 0
        for name, meta in hosts.items():
            self.create_ci(name, CIType.HOST.value, description=meta["desc"],
                           attributes={"ip": meta["ip"]}, tags=["fleet"])
            created += 1
        for a in ("lumina", "opus", "jarvis"):
            self.create_ci(a, CIType.AGENT.value, description=f"{a} sovereign agent",
                           node="noroc2027", tags=["agent"])
            created += 1

        # Service CIs from ITIL incident affected_services; health from open incidents.
        services: dict[str, str] = {}   # service -> worst open severity
        try:
            from .itil import ITILManager
            mgr = ITILManager(self.home)
            rank = {"sev1": 0, "sev2": 1, "sev3": 2, "sev4": 3}
            for inc in mgr.list_incidents():
                open_ = inc.status.value not in ("resolved", "closed")
                for svc in (inc.affected_services or []):
                    if open_:
                        cur = services.get(svc)
                        if cur is None or rank.get(inc.severity.value, 9) < rank.get(cur, 9):
                            services[svc] = inc.severity.value
                    else:
                        services.setdefault(svc, None)
        except Exception:  # noqa: BLE001
            pass
        for svc, worst in services.items():
            ci = self.create_ci(svc, CIType.SERVICE.value, node="noroc2027", tags=["service"])
            status = (CIStatus.DOWN.value if worst in ("sev1", "sev2")
                      else CIStatus.DEGRADED.value if worst == "sev3"
                      else CIStatus.OPERATIONAL.value)
            if ci and ci.status != status:
                self.set_status(ci.id, agent, status, note="from incident health")
            self.add_relationship(ci.id, agent, "runs_on", make_ci_id(CIType.HOST.value, "noroc2027"))
            created += 1
        return {"cis": len(self.list_cis()), "touched": created}

    def impact_analysis(self, ci_id: str) -> dict:
        """What depends on this CI (cascade) + which open incidents affect it."""
        ci = self.get_ci(ci_id)
        if ci is None:
            return {"error": "CI not found", "id": ci_id}
        dependents = []
        for other in self.list_cis():
            for rel in other.relationships:
                if rel.target == ci_id and rel.rel_type in ("depends_on", "runs_on"):
                    dependents.append({"id": other.id, "name": other.name,
                                       "ci_type": other.ci_type, "rel": rel.rel_type})
        incidents = []
        try:
            from .itil import ITILManager
            mgr = ITILManager(self.home)
            for inc in mgr.list_incidents():
                if inc.status.value in ("resolved", "closed"):
                    continue
                if ci.name in (inc.affected_services or []) or ci_id in (inc.affected_services or []):
                    incidents.append({"id": inc.id, "severity": inc.severity.value,
                                      "status": inc.status.value, "title": inc.title})
        except Exception:  # noqa: BLE001
            pass
        return {"ci": ci.model_dump(), "dependents": dependents, "open_incidents": incidents}
