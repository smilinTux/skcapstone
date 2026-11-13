"""Round 8 (delta review of caf83b8b..46a11500): the actuator rule is decided ONCE per pass and
dropped wiring can never fall into the inline lane; a non-actuator's unbound decision cannot
shadow the actuator's bound one; a failing void cannot replace an interrupt."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from skcapstone.operator_seat import (
    act_dispatch,
    action_ledger,
    decisions,
    dispatch,
    loop,
    node_local,
)
from tests.cab_grants import decision_grant

from .test_dispatch_e2e import (  # noqa: F401  (world is a fixture)
    _iso,
    _loop_pass,
    _stamp,
    _sweep,
    world,
)

NOT_ACTUATOR = "this node (node-b) is not the designated actuator (node-a)"


def _flapping(first=NOT_ACTUATOR):
    """The actuator rule: refuses on its first call, answers None on every later one (a
    designation file written mid-pass, or one failed read)."""
    calls = {"n": 0}

    def rule():
        calls["n"] += 1
        return first if calls["n"] == 1 else None

    return rule, calls


# --------------------------------------------------------------------------
# F1: one evaluation per pass, and dropped wiring never reaches the inline lane
# --------------------------------------------------------------------------


def test_a_designation_appearing_mid_pass_cannot_turn_a_refusal_into_an_inline_actuation(
    world, monkeypatch
):
    rule, calls = _flapping()
    monkeypatch.setattr(node_local, "actuator_refusal", rule)
    res = _loop_pass(world)
    assert world.calls == [], "no systemctl: this node was refused at the start of the pass"
    assert res["outcomes"][0]["outcome"].startswith("refused: ")
    assert "not the designated actuator" in res["outcomes"][0]["outcome"]
    assert world.ledger.list_intents() == []


def test_the_actuator_rule_is_evaluated_once_per_pass(world, monkeypatch):
    rule, calls = _flapping()
    monkeypatch.setattr(node_local, "actuator_refusal", rule)
    _loop_pass(world)
    assert calls["n"] == 1


def test_an_inline_actuation_is_refused_when_the_pass_disallows_the_inline_lane(world):
    res = loop.run_once(
        world.paths,
        now_iso=_iso(world.time.now),
        propose=lambda b, r: [
            {
                "app": "skchat",
                "condition": "BridgeAlive",
                "action": "restart-telegram-bridge",
                "object": "telegram-bridge",
                "evidence": _stamp(world),
            }
        ],
        explain=act_dispatch.merged_explain(),
        decisions_dir=str(world.decisions),
        apply_fn=lambda p, c: world.calls.append(p) or {"performed": True},
        execute=True,
        lifecycle_ledger=world.ledger,  # a ledger but NO itil: the inline lane
        allow_inline_actuation=False,
        clock=lambda: world.time.now,
        emit=lambda _s: None,
    )
    assert world.calls == []
    assert res["outcomes"][0]["outcome"].startswith("refused: inline actuation is not allowed")
    assert world.ledger.list_intents() == []


def test_the_inline_lane_still_works_by_default_for_passes_with_no_itil(world):
    res = loop.run_once(
        world.paths,
        now_iso=_iso(world.time.now),
        propose=lambda b, r: [
            {
                "app": "skchat",
                "condition": "BridgeAlive",
                "action": "restart-telegram-bridge",
                "object": "telegram-bridge",
                "evidence": _stamp(world),
            }
        ],
        explain=act_dispatch.merged_explain(),
        decisions_dir=str(world.decisions),
        apply_fn=lambda p, c: world.calls.append(p) or {"performed": True},
        execute=True,
        clock=lambda: world.time.now,
        emit=lambda _s: None,
    )
    assert len(world.calls) == 1
    assert res["outcomes"][0]["outcome"] == "performed, unverified"


def _run_cli(tmp_path, monkeypatch, args):
    from skcapstone.operator_seat import cli

    from .test_cli import _enroll

    _enroll(tmp_path, monkeypatch)
    monkeypatch.setattr("skcapstone.SHARED_ROOT", str(tmp_path / "shared"))
    monkeypatch.setattr(cli.signing, "capauth_signer", lambda: (lambda data: "sig"))
    monkeypatch.setattr(cli.signing, "capauth_verifier", lambda: (lambda data, sig: True))
    monkeypatch.setattr(cli.discovery, "discover_observers", lambda: {})
    seen = {}

    def capture(*_a, **kwargs):
        seen.update(kwargs)
        return {"brief": {"counts": {}}, "outcomes": [], "report": "", "frozen": False}

    monkeypatch.setattr(cli.loop, "run_once", capture)
    out = CliRunner().invoke(cli.operator, ["run", "--no-publish", "--no-bootstrap", *args])
    assert out.exit_code == 0, out.output
    return seen


def test_production_honor_mode_refuses_inline_actuation_outright(tmp_path, monkeypatch):
    seen = _run_cli(tmp_path, monkeypatch, ["--execute", "--honor"])
    assert seen["allow_inline_actuation"] is False
    assert seen["itil"] is not None and seen["lifecycle_ledger"] is not None


def test_annotation_only_execute_without_honor_keeps_the_inline_lane(tmp_path, monkeypatch):
    seen = _run_cli(tmp_path, monkeypatch, ["--execute"])
    assert seen["allow_inline_actuation"] is True
    assert seen["itil"] is None


# --------------------------------------------------------------------------
# F2: a non-actuator's unbound decision cannot shadow the actuator's bound one
# --------------------------------------------------------------------------


def test_the_actuator_upgrades_a_decision_a_non_actuator_parked_unbound(world, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(node_local, "actuator_refusal", lambda: NOT_ACTUATOR)
        first = _loop_pass(world, target_known=lambda p: False)
    assert first["outcomes"][0]["outcome"] == "escalated (parked for approval)"
    [unbound] = decisions.list_pending(str(world.decisions))
    assert not any(o.get("itil_change_id") for o in unbound["options"])
    # The actuator node runs the same pass against the same replicated decisions store.
    second = _loop_pass(world, target_known=lambda p: False)
    intent_id = second["outcomes"][0]["intent_id"]
    change_id = world.ledger.read_intent(intent_id).itil_change_id
    [bound] = decisions.list_pending(str(world.decisions))
    assert bound["id"] == unbound["id"], "still ONE decision for the standing issue"
    assert [o.get("itil_change_id") for o in bound["options"]] == [change_id]
    # ... and it can be approved, which used to raise UnauthorizedDecisionError for good.
    dispatch.resolve_decision(
        str(world.decisions),
        world.itil,
        bound["id"],
        approve=True,
        choice=0,
        subject="capauth:approver@scratch",
        resolved_iso=_iso(world.time.now),
        authorization=decision_grant(
            str(world.decisions), bound["id"], 0, "capauth:approver@scratch", role="approver"
        ),
    )
    assert _sweep(world)[0].outcome == "verified"


def test_a_non_actuator_cannot_downgrade_a_bound_decision(world, monkeypatch):
    _loop_pass(world, target_known=lambda p: False)  # the actuator parks the bound option
    [bound] = decisions.list_pending(str(world.decisions))
    with monkeypatch.context() as m:
        m.setattr(node_local, "actuator_refusal", lambda: NOT_ACTUATOR)
        _loop_pass(world, target_known=lambda p: False)
    [after] = decisions.list_pending(str(world.decisions))
    assert after["options"] == bound["options"]


def test_park_upgrades_only_a_pending_unbound_record(tmp_path):
    store = tmp_path / "d"
    unbound = {"action": "restart", "object": "x"}
    bound = {"action": "restart", "object": "x", "itil_change_id": "chg-1"}
    decisions.park(store, [unbound], decision_id="d1", created_iso="2026-01-01T00:00:00Z")
    upgraded = decisions.park(store, [bound], decision_id="d1", created_iso="2026-01-02T00:00:00Z")
    assert upgraded["options"] == [bound]
    assert upgraded["created"] == "2026-01-01T00:00:00Z", "the record keeps its birth time"
    # never downgraded
    again = decisions.park(store, [unbound], decision_id="d1", created_iso="2026-01-03T00:00:00Z")
    assert again["options"] == [bound]
    # a record that is already bound is not replaced by a different bound option
    other = {**bound, "itil_change_id": "chg-2"}
    assert decisions.park(store, [other], decision_id="d1", created_iso="x")["options"] == [bound]


def test_park_never_rewrites_a_resolved_record(tmp_path):
    store = tmp_path / "d"
    unbound = {"action": "restart", "object": "x"}
    decisions.park(store, [unbound], decision_id="d1", created_iso="2026-01-01T00:00:00Z")
    decisions.resolve(store, "d1", approve=False, choice=None, by="chef", resolved_iso="t")
    bound = {**unbound, "itil_change_id": "chg-1"}
    kept = decisions.park(store, [bound], decision_id="d1", created_iso="2026-01-02T00:00:00Z")
    assert kept["status"] != "pending" and kept["options"] == [unbound]


# --------------------------------------------------------------------------
# F3: a failing void cannot replace the original interrupt
# --------------------------------------------------------------------------


def test_a_failing_void_does_not_replace_a_keyboard_interrupt(world, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    def void_fails(*args, **kwargs):
        raise ValueError("ledger exploded while voiding")

    monkeypatch.setattr(dispatch, "dispatch_intent", interrupted)
    monkeypatch.setattr(dispatch, "void_proposed", void_fails)
    alerts = []
    with pytest.raises(KeyboardInterrupt):
        loop.run_once(
            world.paths,
            now_iso=_iso(world.time.now),
            propose=lambda b, r: [
                {
                    "app": "skchat",
                    "condition": "BridgeAlive",
                    "action": "restart-telegram-bridge",
                    "object": "telegram-bridge",
                    "evidence": _stamp(world),
                }
            ],
            explain=act_dispatch.merged_explain(),
            decisions_dir=str(world.decisions),
            apply_fn=lambda p, c: {"performed": True},
            execute=True,
            lifecycle_ledger=world.ledger,
            itil=world.itil,
            clock=lambda: world.time.now,
            emit=alerts.append,
        )
    assert any("could not void" in a for a in alerts), "the void failure is logged, not lost"
