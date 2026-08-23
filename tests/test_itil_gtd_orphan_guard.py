"""Guard: never emit a GTD item for an incident that did not persist as open.

Regression test for the recurring "ITIL orphan storm" (Jul 16/19/24/28 2026):
the health-check writer created ``[ITIL:inc-XXXX]`` GTD next-actions/inbox items
even when the incident core failed to persist (cross-node Syncthing divergence,
or an unreadable/corrupt core). The daily validator then batch-closed those
dangling items every morning. The fix gates the GTD emission on a re-read that
confirms an *open* incident record actually exists first.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    """Redirect _shared_root() so GTD files land in tmp_path, not ~/.skcapstone."""
    import skcapstone.mcp_tools._helpers as _helpers

    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))


def _itil_gtd_items() -> list[dict]:
    """Every GTD item across lists that references an ITIL id."""
    from skcapstone.mcp_tools.gtd_tools import _load_list

    out: list[dict] = []
    for lst in ("next-actions", "inbox", "waiting-for", "projects", "someday-maybe"):
        out.extend(i for i in _load_list(lst) if "[ITIL:" in i.get("text", ""))
    return out


def test_open_incident_still_emits_gtd(tmp_path: Path):
    """Happy path is unchanged: a persisted open incident gets its GTD item."""
    from skcapstone.itil import ITILManager

    mgr = ITILManager(str(tmp_path))
    inc = mgr.create_incident(title="widget down", severity="sev2", managed_by="opus")

    assert inc is not None and inc.status.value == "detected"
    assert inc.gtd_item_ids, "open incident should link a GTD item"
    assert any(inc.id in i["text"] for i in _itil_gtd_items())


def test_unpersisted_incident_emits_no_gtd(tmp_path: Path, monkeypatch):
    """If the core cannot be read back (persistence failed / cross-node divergence),
    no dangling GTD item is created and the call does not raise."""
    from skcapstone.itil import ITILManager

    mgr = ITILManager(str(tmp_path))

    # Simulate a core that never becomes readable (unpersisted / diverged core).
    monkeypatch.setattr(mgr, "_load_core", lambda directory, record_id: None)

    inc = mgr.create_incident(title="phantom down", severity="sev3", managed_by="opus")

    # Degrades gracefully; crucially, NO orphan GTD item is left behind.
    assert inc is None or not inc.gtd_item_ids
    assert _itil_gtd_items() == [], "must not create a GTD item for an unpersisted incident"


def test_resolved_incident_emits_no_gtd(tmp_path: Path, monkeypatch):
    """A folded-but-already-resolved incident must not spawn a fresh next-action."""
    from skcapstone.itil import IncidentStatus, ITILManager

    mgr = ITILManager(str(tmp_path))
    real_fold = mgr._fold_record

    def _fold_resolved(directory, record_id, model_class):
        inc = real_fold(directory, record_id, model_class)
        if inc is not None and getattr(inc, "status", None) is not None:
            inc.status = IncidentStatus.RESOLVED
        return inc

    monkeypatch.setattr(mgr, "_fold_record", _fold_resolved)

    mgr.create_incident(title="already fixed", severity="sev3", managed_by="opus")
    assert _itil_gtd_items() == [], "resolved incident should not emit a GTD item"


def _inject_orphan_gtd(itil_id: str, text: str) -> str:
    """Write an ITIL-linked next-action whose incident core does NOT exist.

    Mimics an orphan synced in from a divergent node - the exact shape the
    daily validator keeps finding (source ``itil`` + ``itil_id`` + no core).
    """
    from skcapstone.mcp_tools.gtd_tools import _load_list, _make_item, _save_list

    item = _make_item(text=text, source="itil", context="@ops")
    item["status"] = "next"
    item["source_ref"] = itil_id
    item["itil_id"] = itil_id
    items = _load_list("next-actions")
    items.append(item)
    _save_list("next-actions", items)
    return item["id"]


def test_reconcile_reaps_orphan_with_no_core(tmp_path: Path):
    """A GTD next-action tagged with an incident id that has no core is drained."""
    from skcapstone.itil import ITILManager
    from skcapstone.mcp_tools.gtd_tools import _load_archive

    orphan_id = _inject_orphan_gtd("inc-deadbeef", "[ITIL:inc-deadbeef] skmem-pg down .41")
    assert _itil_gtd_items(), "orphan seeded"

    reaped = ITILManager(str(tmp_path)).reconcile_gtd_orphans()

    assert orphan_id in reaped
    assert _itil_gtd_items() == [], "orphan must be removed from active lists"
    archived = [i for i in _load_archive() if i.get("id") == orphan_id]
    assert archived and archived[0]["status"] == "dropped", "reaped item lands in archive"


def test_reconcile_keeps_open_incident_item(tmp_path: Path):
    """The next-action for a genuinely open incident survives the sweep."""
    from skcapstone.itil import ITILManager

    mgr = ITILManager(str(tmp_path))
    inc = mgr.create_incident(title="real outage", severity="sev2", managed_by="opus")
    assert inc.gtd_item_ids

    reaped = mgr.reconcile_gtd_orphans()

    assert reaped == [], "open incident's item is not an orphan"
    assert any(inc.id in i["text"] for i in _itil_gtd_items()), "live item preserved"


def test_reconcile_is_idempotent(tmp_path: Path):
    """Second run finds nothing to reap - no double-archiving, no churn."""
    from skcapstone.itil import ITILManager

    _inject_orphan_gtd("inc-cafebabe", "[ITIL:inc-cafebabe] down")
    mgr = ITILManager(str(tmp_path))

    first = mgr.reconcile_gtd_orphans()
    second = mgr.reconcile_gtd_orphans()

    assert len(first) == 1
    assert second == [], "reconcile must be idempotent"
