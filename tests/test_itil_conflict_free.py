"""Conflict-free ITIL persistence - fold, dedup, concurrency, CAB (prb-7810b08e).

Covers docs/itil-conflict-free-persistence.md section 8:
  (a) concurrent-writer no-conflict + deterministic-id convergence
  (b) fold correctness + idempotence + losing-transition-flagged
  (c) dedup-id convergence
  (e) filename stability under a title edit
  (f) CAB derivation (votes only, no update_change write)
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import skcapstone.itil as itil
from skcapstone.itil import ITILManager, _auto_incident_id


@pytest.fixture(autouse=True)
def _no_gtd_side_effects(monkeypatch) -> None:
    """Stub the GTD emit so tests never touch the real ~/.skcapstone GTD store."""
    monkeypatch.setattr(ITILManager, "_gtd_emit", lambda *a, **k: None)


def _write_core(directory: Path, record_id: str, core: dict) -> None:
    rec_dir = directory / record_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "core.json").write_text(json.dumps(core), encoding="utf-8")


def _write_events(
    directory: Path, record_id: str, writer: str, node: str, events: list[dict]
) -> None:
    ev_dir = directory / record_id / "events"
    ev_dir.mkdir(parents=True, exist_ok=True)
    for i, e in enumerate(events):
        e.setdefault("writer", writer)
        e.setdefault("node", node)
        e.setdefault("seq", i)
    (ev_dir / f"{writer}@{node}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


# ── (a) concurrent-writer no-conflict ────────────────────────────────────


def test_concurrent_writers_produce_no_conflict_and_all_events(tmp_path: Path):
    """N agents append to their own writer file concurrently - nothing conflicts."""
    mgr = ITILManager(tmp_path)
    inc = mgr.create_incident(title="multi-writer", managed_by="lumina")

    agents = [f"agent{i}" for i in range(8)]

    def _append(agent: str) -> None:
        mgr.update_incident(inc.id, agent=agent, note=f"note-from-{agent}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_append, agents))

    # Zero sync-conflict artifacts anywhere in the tree.
    assert list(mgr.itil_dir.rglob("*.sync-conflict*")) == []

    folded = mgr._fold_record(mgr.incidents_dir, inc.id, type(inc))
    notes = {r["note"] for r in folded.timeline if r["action"] == "note"}
    for agent in agents:
        assert f"note-from-{agent}" in notes


def test_two_nodes_same_deterministic_id_converge(tmp_path: Path, monkeypatch):
    """Two nodes auto-detecting one outage produce one id + byte-identical core."""
    monkeypatch.setattr(itil, "_now_iso", lambda: "2026-07-13T12:00:00+00:00")
    mgr_a = ITILManager(tmp_path / "a")
    mgr_b = ITILManager(tmp_path / "b")

    kwargs = dict(
        title="skvector (Qdrant) down",
        source="service_health",
        affected_services=["skvector (Qdrant)"],
        created_by="service_health",
        managed_by="lumina",
        failure_class="unreachable",
    )

    monkeypatch.setattr(itil, "_HOSTNAME", "nodeA")
    inc_a = mgr_a.create_incident(**kwargs)
    monkeypatch.setattr(itil, "_HOSTNAME", "nodeB")
    inc_b = mgr_b.create_incident(**kwargs)

    assert inc_a.id == inc_b.id
    core_a = (mgr_a.incidents_dir / inc_a.id / "core.json").read_bytes()
    core_b = (mgr_b.incidents_dir / inc_b.id / "core.json").read_bytes()
    assert core_a == core_b

    # Simulate Syncthing merging node B's writer file into node A's tree.
    ev_b = next((mgr_b.incidents_dir / inc_b.id / "events").glob("*@nodeB.jsonl"))
    dest = mgr_a.incidents_dir / inc_a.id / "events" / ev_b.name
    dest.write_bytes(ev_b.read_bytes())

    merged = mgr_a._fold_record(mgr_a.incidents_dir, inc_a.id, type(inc_a))
    writer_files = list((mgr_a.incidents_dir / inc_a.id / "events").glob("*.jsonl"))
    assert len(writer_files) == 2  # union of both nodes' logs
    assert merged.status.value == "detected"


# ── (b) fold correctness / idempotence / conflict flag ───────────────────


def test_fold_flags_losing_transition_and_is_idempotent(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    rid = "inc-craft01"
    _write_core(
        mgr.incidents_dir,
        rid,
        {
            "id": rid,
            "type": "incident",
            "title": "crafted",
            "severity_at_creation": "sev3",
            "source": "manual",
            "affected_services": [],
            "detected_at": "2026-07-13T00:00:00+00:00",
            "tags": [],
        },
    )
    _write_events(
        mgr.incidents_dir,
        rid,
        "winner",
        "n1",
        [
            {"kind": "created", "ts": "2026-07-13T00:00:01+00:00", "note": "c"},
            {"kind": "status", "to": "resolved", "ts": "2026-07-13T00:00:02+00:00", "note": ""},
        ],
    )
    _write_events(
        mgr.incidents_dir,
        rid,
        "loser",
        "n2",
        [
            {
                "kind": "status",
                "to": "escalated",
                "ts": "2026-07-13T00:00:03+00:00",
                "note": "late",
            },
        ],
    )

    folded = mgr._fold_record(mgr.incidents_dir, rid, itil.Incident)
    # Earlier-in-total-order resolve wins; later escalate is invalid from resolved.
    assert folded.status.value == "resolved"
    conflicted = [r for r in folded.timeline if r.get("conflicted")]
    assert len(conflicted) == 1
    assert "escalated" in conflicted[0]["action"]

    # Idempotence: folding the same on-disk state again yields the same model.
    again = mgr._fold_record(mgr.incidents_dir, rid, itil.Incident)
    assert folded.model_dump() == again.model_dump()


def test_fold_orders_by_total_order_key(tmp_path: Path):
    """Events across writers apply in (ts, node, writer, seq) order."""
    mgr = ITILManager(tmp_path)
    rid = "inc-order01"
    _write_core(
        mgr.incidents_dir,
        rid,
        {
            "id": rid,
            "type": "incident",
            "title": "ordered",
            "severity_at_creation": "sev4",
            "source": "manual",
            "affected_services": [],
            "detected_at": "2026-07-13T00:00:00+00:00",
            "tags": [],
        },
    )
    _write_events(
        mgr.incidents_dir,
        rid,
        "a",
        "n1",
        [
            {"kind": "status", "to": "acknowledged", "ts": "2026-07-13T00:01:00+00:00"},
            {"kind": "status", "to": "resolved", "ts": "2026-07-13T00:03:00+00:00"},
        ],
    )
    _write_events(
        mgr.incidents_dir,
        rid,
        "b",
        "n2",
        [
            {"kind": "status", "to": "investigating", "ts": "2026-07-13T00:02:00+00:00"},
        ],
    )
    folded = mgr._fold_record(mgr.incidents_dir, rid, itil.Incident)
    # detected->acknowledged->investigating->resolved all valid, in ts order.
    assert folded.status.value == "resolved"
    assert folded.acknowledged_at == "2026-07-13T00:01:00+00:00"
    assert folded.resolved_at == "2026-07-13T00:03:00+00:00"


def test_severity_folds_to_max(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    inc = mgr.create_incident(title="sev", severity="sev3", managed_by="x")
    mgr.update_incident(inc.id, agent="x", severity="sev1")
    mgr.update_incident(inc.id, agent="x", severity="sev4")  # cannot de-escalate
    folded = mgr._fold_record(mgr.incidents_dir, inc.id, itil.Incident)
    assert folded.severity.value == "sev1"


# ── (c) dedup-id convergence ─────────────────────────────────────────────


def test_auto_incident_id_is_deterministic_per_day(tmp_path: Path):
    a = _auto_incident_id("skvector (Qdrant)", "unreachable", "2026-07-13")
    b = _auto_incident_id("skvector (Qdrant)", "unreachable", "2026-07-13")
    c = _auto_incident_id("skvector (Qdrant)", "unreachable", "2026-07-14")
    assert a == b
    assert a != c
    assert a.startswith("inc-")


def test_repeated_service_health_create_folds_to_one_record(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    kwargs = dict(
        title="skchat down",
        source="service_health",
        affected_services=["skchat"],
        created_by="service_health",
        failure_class="refused",
    )
    inc1 = mgr.create_incident(**kwargs)
    inc2 = mgr.create_incident(**kwargs)
    assert inc1.id == inc2.id
    dirs = [d for d in mgr.incidents_dir.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    # The duplicated 'created' events fold to a single created row.
    created_rows = [r for r in inc2.timeline if r["action"] == "created"]
    assert len(created_rows) == 1


# ── (e) filename stability ───────────────────────────────────────────────


def test_title_edit_is_an_event_not_a_rename(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    inc = mgr.create_incident(title="Old title", managed_by="lumina")
    dir_name = (mgr.incidents_dir / inc.id).name

    mgr._append_event(mgr.incidents_dir, inc.id, "lumina", "title", text="New title")

    # Directory name unchanged; no slug-suffixed twins anywhere.
    assert (mgr.incidents_dir / inc.id).is_dir()
    assert [p.name for p in mgr.incidents_dir.iterdir() if p.is_dir()] == [dir_name]
    assert list(mgr.incidents_dir.glob(f"{inc.id}-*.json")) == []

    folded = mgr.list_incidents()[0]
    assert folded.title == "New title"


# ── (f) CAB derivation ───────────────────────────────────────────────────


def test_cab_human_approval_derives_approved(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="normal change", change_type="normal", managed_by="lumina")
    assert chg.status.value == "proposed"
    mgr.submit_cab_vote(chg.id, agent="lumina", decision="approved")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    folded = mgr.list_changes()[0]
    assert folded.status.value == "approved"


def test_cab_rejection_derives_rejected(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="risky", change_type="normal", managed_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    mgr.submit_cab_vote(chg.id, agent="sentinel", decision="rejected")
    folded = mgr.list_changes()[0]
    assert folded.status.value == "rejected"


def test_cab_agent_only_approval_stays_pending(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="needs human", change_type="normal", managed_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="lumina", decision="approved")
    folded = mgr.list_changes()[0]
    assert folded.status.value == "proposed"


def test_standard_change_auto_approves_at_fold_time(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="std", change_type="standard", managed_by="lumina")
    assert chg.status.value == "approved"
    assert chg.cab_required is False
    # No update_change write happened; the approval is a pure derivation.
    reloaded = mgr.list_changes()[0]
    assert reloaded.status.value == "approved"
