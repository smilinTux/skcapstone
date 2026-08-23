"""CR-9.1 AC2 freeze drill: objects/_freeze.json mid-run stands the loop down.

Proves the kill switch wins FIRST and pre-observe: with the freeze set, one loop
pass observes nothing, reasons nothing, and actuates nothing, even with execution
and honoring wired. Re-verified with the honor apply_fn attached (the execute-on
case Chef will flip), so freeze is proven to win over the physical-actuation path.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import act_dispatch, loop


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _freeze(paths):
    human = store.Writer(role="operator", node="cli", identity="chef")
    store.set_frozen(paths, True, writer=human)


def _boom_observe(paths=None, now_iso=None):
    raise AssertionError("observe ran while frozen: the loop did NOT stand down pre-observe")


def test_freeze_stands_the_loop_down_before_observe(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _freeze(paths)
    assert store.is_frozen(paths) is True

    # Any observe/propose/apply firing while frozen is a drill failure.
    monkeypatch.setattr(loop, "ADAPTERS", {"canary": _boom_observe})

    def _boom_propose(brief, route):
        raise AssertionError("proposer ran while frozen")

    res = loop.run_once(
        paths,
        now_iso="2026-08-07T00:00:00Z",
        propose=_boom_propose,
        extra_observers={"canary2": _boom_observe},
    )
    assert res["frozen"] is True
    assert res["proposals"] == []
    assert res["brief"] is None
    assert "standing down" in res["report"]


def test_freeze_wins_even_with_execute_and_honor_wired(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _freeze(paths)

    monkeypatch.setattr(loop, "ADAPTERS", {"canary": _boom_observe})

    def _boom_apply(prop, cls):
        raise AssertionError("apply_fn ran while frozen: freeze did NOT win over execute")

    # Wire the real honor apply_fn too: even physical actuation must never run.
    honor_apply = act_dispatch.build_apply_fn(paths, "2026-08-07T00:00:00Z", runner=None)

    for apply_fn in (_boom_apply, honor_apply):
        res = loop.run_once(
            paths,
            now_iso="2026-08-07T00:00:00Z",
            propose=lambda b, r: [{"action": "restart-telegram-bridge", "object": "x"}],
            apply_fn=apply_fn,
            execute=True,
        )
        assert res["frozen"] is True
        assert res["outcomes"] == []


def test_unfreeze_lets_the_loop_run_again(tmp_path):
    paths = _paths(tmp_path)
    _freeze(paths)
    human = store.Writer(role="operator", node="cli", identity="chef")
    store.set_frozen(paths, False, writer=human)
    assert store.is_frozen(paths) is False

    # With freeze lifted, a quiet observe pass runs and reports (writes nothing).
    res = loop.run_once(
        paths,
        now_iso="2026-08-07T00:00:00Z",
        propose=lambda b, r: [],
        extra_observers={"q": lambda p=None, n=None: {"conditions": []}},
    )
    assert res["frozen"] is False
    assert res["brief"] is not None


def test_agent_seat_cannot_toggle_freeze(tmp_path):
    """The autonomous seat can never unfreeze itself (freeze is human-only)."""
    paths = _paths(tmp_path)
    seat = store.Writer(role="operator", node="n", identity="operator", agent_seat=True)
    with pytest.raises(store.OwnershipError):
        store.set_frozen(paths, True, writer=seat)
