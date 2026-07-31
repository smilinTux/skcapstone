"""Report-only operator loop: it observes and reports, and never writes."""

from __future__ import annotations

from skcapstone.fleet import sknoded, store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import loop


def _enroll(tmp_path, monkeypatch):
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None},
    )
    paths = FleetPaths(root=tmp_path / "fleet")
    op = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False}, writer=op)
    sknoded.run_once(paths, "node-158")
    return paths, op


def test_loop_reports_and_returns_a_brief(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    out = []
    res = loop.run_once(paths, now_iso="2026-07-29T00:00:00Z", emit=out.append)
    assert res["frozen"] is False
    assert res["brief"] is not None
    assert res["route"] in ("ornith", "claude")
    assert res["report"] == out[0]


def test_loop_stands_down_when_frozen(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    human = store.Writer(role="operator", node="node-158", identity="chef")
    store.set_frozen(paths, True, writer=human, reason="drill")
    out = []
    res = loop.run_once(paths, now_iso="2026-07-29T00:00:00Z", emit=out.append)
    assert res["frozen"] is True
    assert res["proposals"] == []
    assert "standing down" in out[0]


def test_loop_writes_nothing_to_the_fleet(tmp_path, monkeypatch):
    # Report-only guarantee: a pass must not create or change any fleet file.
    paths, _ = _enroll(tmp_path, monkeypatch)

    def _snapshot():
        return {p: p.read_bytes() for p in (tmp_path / "fleet").rglob("*.json")}

    before = _snapshot()
    loop.run_once(paths, now_iso="2026-07-29T00:00:00Z", emit=lambda _s: None)
    assert _snapshot() == before  # no file created or modified


def test_loop_carries_injected_proposals_into_the_report(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    proposal = {
        "change_class": "normal",
        "action": "restart_service",
        "rationale": "service Ready went False",
    }
    out = []
    res = loop.run_once(
        paths, now_iso="2026-07-29T00:00:00Z", propose=lambda b, r: [proposal], emit=out.append
    )
    assert res["proposals"] == [proposal]
    assert "restart_service" in out[0]


def test_loop_auto_proposal_not_applied_when_execution_off(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    applied = []
    prop = {"action": "restart_service", "object": "web"}
    res = loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [prop],
        apply_fn=lambda p, c: applied.append(p),
        execute=False,
        emit=lambda _s: None,
    )
    assert applied == []  # execution off: nothing is applied
    assert res["outcomes"][0]["outcome"].startswith("auto-ready")


def test_loop_auto_proposal_applied_when_execution_on(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    applied = []
    prop = {"action": "restart_service", "object": "web"}
    res = loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [prop],
        apply_fn=lambda p, c: applied.append(p),
        execute=True,
        emit=lambda _s: None,
    )
    assert applied == [prop]
    assert res["outcomes"][0]["outcome"] == "applied"


def test_loop_escalation_parks_for_approval(tmp_path, monkeypatch):
    from skcapstone.operator_seat import decisions

    paths, _ = _enroll(tmp_path, monkeypatch)
    ddir = str(tmp_path / "decisions")
    prop = {"action": "delete_object", "object": "web"}  # irreversible -> escalate
    loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [prop],
        decisions_dir=ddir,
        execute=True,
        apply_fn=lambda p, c: None,
        emit=lambda _s: None,
    )
    assert len(decisions.list_pending(ddir)) == 1  # parked, not applied


def test_loop_merges_extra_observers_builtins_win(tmp_path, monkeypatch):
    # A manifest-discovered observer widens what Atlas observes; a built-in id
    # always wins a clash (fleet stays the real fleet adapter).
    paths, _ = _enroll(tmp_path, monkeypatch)
    seen = {}

    def _skbrain_observe(paths_, now_iso_):
        return {"conditions": [{"type": "OpsSchemaPresent", "status": "Unknown"}]}

    def _fake_fleet(paths_, now_iso_):
        seen["fleet_called"] = True
        return {"conditions": []}

    res = loop.run_once(
        paths,
        now_iso="2026-07-31T00:00:00Z",
        extra_observers={"skbrain": _skbrain_observe, "fleet": _fake_fleet},
        emit=lambda _s: None,
    )
    # The discovered observer was included; the built-in fleet observer overrode
    # the same-named extra (the fake fleet was never called).
    assert res["brief"] is not None
    assert "fleet_called" not in seen


def test_loop_extra_observers_none_is_byte_identical(tmp_path, monkeypatch):
    # Default (no extra observers) reasons exactly as before: report-only, no write.
    paths, _ = _enroll(tmp_path, monkeypatch)

    def _snapshot():
        return {p: p.read_bytes() for p in (tmp_path / "fleet").rglob("*.json")}

    before = _snapshot()
    loop.run_once(
        paths, now_iso="2026-07-31T00:00:00Z", extra_observers=None, emit=lambda _s: None
    )
    assert _snapshot() == before


def test_loop_frozen_never_applies_even_with_execute(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    human = store.Writer(role="operator", node="node-158", identity="chef")
    store.set_frozen(paths, True, writer=human)
    applied = []
    loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [{"action": "restart_service", "object": "x"}],
        apply_fn=lambda p, c: applied.append(p),
        execute=True,
        emit=lambda _s: None,
    )
    assert applied == []  # freeze wins even with execute=True
