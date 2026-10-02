"""Selection snapshots reduce repeated folds without becoming claim authority."""

import ast
import copy
import importlib.util
from pathlib import Path

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.fleet.production_dispatch import authoritative_owner_state

ROOT = Path(__file__).resolve().parents[2]


def namespace(home, source=None):
    """Load real admission functions without running the launcher's side effects."""
    spec = importlib.util.spec_from_file_location(
        "claimability_fixture", ROOT / "tests/test_skfleet_claimability.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    ns = fixture._load_claimability()
    source = source or (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    names = {
        "_strict_card_events",
        "_authoritative_card_snapshot",
        "_authoritative_card_state",
        "authoritative_claimability",
        "lifecycle_state",
        "_dep_satisfied",
        "_still_assignable",
    }
    nodes = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    ns.update(
        HOME=str(home.parent),
        CARDS=str(home / "cards"),
        Path=Path,
        copy=copy,
        PRODUCTION_POLICY=True,
        CardStore=CardStore,
        authoritative_owner_state=authoritative_owner_state,
        _production_native_store=None,
        _claim_rows={},
        _selection_snapshots=None,
        _load_outcomes=lambda: {},
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "admission", "exec"), ns)
    return ns


def make_board(tmp_path):
    home = tmp_path / ".skcapstone"
    home.mkdir()
    store = CardStore(home)
    for cid in ("a1650001", "a1650002"):
        store.create(
            CardCore(
                id=cid,
                title="[M] Synthetic admission fixture",
                created_by="fixture",
                initial_labels=["dispatch-approved"],
            )
        )
    return home, store


def decisions(ns):
    """Exercise the repeated real lifecycle and admission reads in selection."""
    result = []
    for cid in sorted(path.name for path in Path(ns["CARDS"]).iterdir()):
        result.append((cid, ns["lifecycle_state"](cid)))
        result.append((cid, ns["authoritative_claimability"](cid)))
        result.append((cid, ns["lifecycle_state"](cid)))
        result.append((cid, ns["lifecycle_state"](cid)))
    return result


def test_snapshot_preserves_decisions_and_reduces_native_folds(tmp_path, monkeypatch):
    home, store = make_board(tmp_path)
    calls = []
    original = CardStore.fold
    monkeypatch.setattr(
        CardStore, "fold", lambda self, cid: (calls.append(cid), original(self, cid))[1]
    )
    baseline = decisions(namespace(home))
    baseline_calls = len(calls)
    calls.clear()
    ns = namespace(home)
    ns["_selection_snapshots"] = {}
    assert decisions(ns) == baseline
    assert len(calls) == 2
    assert baseline_calls == 8
    # Caller mutation must not poison subsequent eligibility decisions.
    ns["authoritative_claimability"]("a1650001")["labels"].append("do-not-claim")
    assert ns["authoritative_claimability"]("a1650001")["claimable"] is True


@pytest.mark.parametrize("change", ["claim", "hold", "source", "dependency"])
def test_preclaim_observes_changes_after_selection(tmp_path, change):
    home, store = make_board(tmp_path)
    cid = "a1650001"
    ns = namespace(home)
    ns["_selection_snapshots"] = {}
    selected = ns["authoritative_claimability"](cid)
    assert selected["claimable"] is True
    selected_admission = ns["_pool_v2_admission"](cid, selected["core"], selected)
    if change == "claim":
        store.append_event(cid, "claim", "fixture", owner="other-worker")
    elif change == "hold":
        store.append_event(cid, "add_label", "fixture", label="do-not-claim")
    elif change == "source":
        store.append_event(cid, "link", "fixture", link_key="base_revision", link_value="b" * 40)
    else:
        store.append_event(cid, "add_dependency", "fixture", dependency_id="a1650002")
    # The production scan closes this scope before preclaim or launch.
    ns["_selection_snapshots"] = None
    fresh = ns["authoritative_claimability"](cid, fresh=True)
    assert fresh["source_revision"] != selected["source_revision"]
    fresh_admission = ns["_pool_v2_admission"](cid, fresh["core"], fresh, fresh=True)
    assert not ns["_pool_v2_preclaim_matches"](selected_admission, fresh_admission)
    if change != "source":
        assert fresh["claimable"] is False
    else:
        assert fresh["core"]["links"]["base_revision"] == "b" * 40


def test_snapshot_is_disabled_before_dispatch():
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    start = source.index("_selection_snapshots = {}")
    end = source.index("_selection_snapshots = None", start)
    assert start < source.index("structural_leaf=leaf_eligibility_counts")
    assert source.index("_emit_shadow_pool_v2()", start) < end
    assert end < source.index("fresh_claimability=authoritative_claimability")


def test_fresh_read_bypasses_active_snapshot(tmp_path):
    home, store = make_board(tmp_path)
    ns = namespace(home)
    ns["_selection_snapshots"] = {}
    assert ns["authoritative_claimability"]("a1650001")["claimable"]
    store.append_event("a1650001", "add_label", "fixture", label="do-not-claim")
    assert not ns["authoritative_claimability"]("a1650001", fresh=True)["claimable"]


def test_dependency_reopened_after_selection_is_not_complete_at_preclaim(tmp_path):
    home, store = make_board(tmp_path)
    store.append_event("a1650002", "move", "fixture", column="done")
    store.append_event("a1650001", "add_dependency", "fixture", dependency_id="a1650002")
    ns = namespace(home)
    ns["_selection_snapshots"] = {}
    assert ns["authoritative_claimability"]("a1650001")["claimable"]
    store.append_event("a1650002", "reopen", "fixture", column="ready")
    ns["_selection_snapshots"] = None
    assert ns["authoritative_claimability"]("a1650001", fresh=True)["reason"] == "dependency"


def test_explicit_core_change_does_not_reuse_a_different_snapshot(tmp_path):
    home, store = make_board(tmp_path)
    ns = namespace(home)
    ns["_selection_snapshots"] = {}
    selected = ns["authoritative_claimability"]("a1650001")
    core = store._load_core("a1650001")
    core["initial_labels"].append("do-not-claim")
    changed = ns["authoritative_claimability"]("a1650001", core)
    assert not changed["claimable"]
    assert changed["source_revision"] != selected["source_revision"]


def test_missing_native_card_after_selection_fails_closed(tmp_path):
    home, store = make_board(tmp_path)
    ns = namespace(home)
    ns["_selection_snapshots"] = {}
    assert ns["authoritative_claimability"]("a1650001")["claimable"]
    # Isolated fixture only; never operate on an authoritative board.
    (home / "cards/a1650001/core.json").unlink()
    ns["_selection_snapshots"] = None
    assert not ns["authoritative_claimability"]("a1650001", fresh=True)["claimable"]
