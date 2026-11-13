"""Round 9 (last tiny round): a reused escalation re-binds its decision, an upgrade only ever
replaces an option for the SAME proposal, and approving checks the change's binding."""

from __future__ import annotations

import logging

import pytest

from skcapstone.operator_seat import decisions, dispatch
from tests.cab_grants import decision_grant

from .test_dispatch_e2e import (  # noqa: F401  (world is a fixture)
    BIND,
    _iso,
    _loop_pass,
    _normal_change,
    _sweep,
    world,
)

SUBJECT = "capauth:approver@scratch"


def _resolve(world, decision_id):
    return dispatch.resolve_decision(
        str(world.decisions),
        world.itil,
        decision_id,
        approve=True,
        choice=0,
        subject=SUBJECT,
        resolved_iso=_iso(world.time.now),
        authorization=decision_grant(
            str(world.decisions), decision_id, 0, SUBJECT, role="approver"
        ),
    )


# --------------------------------------------------------------------------
# 1: a reuse re-binds the decision (the Syncthing race the unbound copy can win)
# --------------------------------------------------------------------------


def test_a_reused_escalation_rebinds_a_decision_an_unbound_copy_replaced(world):
    first = _loop_pass(world, target_known=lambda p: False)
    intent_id = first["outcomes"][0]["intent_id"]
    change_id = world.ledger.read_intent(intent_id).itil_change_id
    [bound] = decisions.list_pending(str(world.decisions))
    assert bound["options"][0]["itil_change_id"] == change_id
    # A non-actuator's unbound copy of the same decision wins the replication race.
    unbound = {
        **bound,
        "options": [{k: v for k, v in bound["options"][0].items() if k != "itil_change_id"}],
    }
    decisions._dump(decisions._record_path(str(world.decisions), bound["id"]), unbound)
    [raced] = decisions.list_pending(str(world.decisions))
    assert not raced["options"][0].get("itil_change_id")
    # The actuator's next pass REUSES the open intent (no new change) and must re-bind.
    changes_before = len(world.itil.list_changes())
    again = _loop_pass(world, target_known=lambda p: False)
    assert again["outcomes"][0]["intent_id"] == intent_id
    assert len(world.itil.list_changes()) == changes_before
    [healed] = decisions.list_pending(str(world.decisions))
    assert healed["options"][0]["itil_change_id"] == change_id
    _resolve(world, healed["id"])  # used to raise UnauthorizedDecisionError forever
    assert _sweep(world)[0].outcome == "verified"


# --------------------------------------------------------------------------
# 2: an upgrade is only for the SAME proposal; approving checks the binding
# --------------------------------------------------------------------------

_BASE = {"app": "skchat", "condition": "BridgeAlive", "object": "telegram-bridge"}


def _opt(action="restart-telegram-bridge", **extra):
    return {**_BASE, "action": action, **extra}


def test_an_upgrade_replaces_only_an_option_for_the_same_proposal(tmp_path):
    store = tmp_path / "d"
    decisions.park(store, [_opt()], decision_id="d1", created_iso="t1")
    out = decisions.park(store, [_opt(itil_change_id="chg-1")], decision_id="d1", created_iso="t2")
    assert out["options"] == [_opt(itil_change_id="chg-1")]


@pytest.mark.parametrize(
    "different",
    [
        {"action": "restart-daemon"},
        {"object": "something-else"},
        {"app": "fleet"},
        {"condition": "DaemonReady"},
    ],
)
def test_an_upgrade_for_a_different_proposal_is_refused_and_logged(tmp_path, caplog, different):
    store = tmp_path / "d"
    decisions.park(store, [_opt()], decision_id="d1", created_iso="t1")
    bound_other = {**_opt(itil_change_id="chg-1"), **different}
    with caplog.at_level(logging.WARNING):
        out = decisions.park(store, [bound_other], decision_id="d1", created_iso="t2")
    assert out["options"] == [_opt()], "the record must not be replaced"
    assert any("refus" in r.message.lower() for r in caplog.records)


def test_an_upgrade_must_keep_the_number_of_options(tmp_path, caplog):
    store = tmp_path / "d"
    decisions.park(store, [_opt()], decision_id="d1", created_iso="t1")
    two = [_opt(itil_change_id="chg-1"), _opt(action="restart-daemon", itil_change_id="chg-2")]
    with caplog.at_level(logging.WARNING):
        out = decisions.park(store, two, decision_id="d1", created_iso="t2")
    assert out["options"] == [_opt()]


def _decision_bound_to(world, change, **option):
    record = _opt(itil_change_id=change.id, **option)
    decisions.park(str(world.decisions), [record], decision_id="d-x", created_iso="t")
    return "d-x"


def test_approving_refuses_a_change_bound_to_a_different_action(world):
    change = _normal_change(world, bind="op-bind:skchat|restart-daemon|telegram-bridge")
    decision_id = _decision_bound_to(world, change)  # the option is restart-telegram-bridge
    with pytest.raises(dispatch.UnauthorizedDecisionError, match="not bound"):
        _resolve(world, decision_id)
    assert world.itil.get_cab_votes(change.id) == [], "no vote may be written"
    assert decisions.list_pending(str(world.decisions)), "the decision stays pending"


def test_approving_refuses_a_change_with_no_binding(world):
    change = world.itil.propose_change(
        title="x", change_type="normal", risk="low", rollback_plan="r", created_by="atlas", tags=[]
    )
    decision_id = _decision_bound_to(world, change)
    with pytest.raises(dispatch.UnauthorizedDecisionError, match="not bound"):
        _resolve(world, decision_id)


def test_approving_refuses_a_change_that_does_not_exist(world):
    class _Ghost:
        id = "chg-ghost"

    decision_id = _decision_bound_to(world, _Ghost())
    with pytest.raises(dispatch.UnauthorizedDecisionError, match="not bound"):
        _resolve(world, decision_id)


def test_approving_a_correctly_bound_change_still_works(world):
    change = _normal_change(world)  # BIND is skchat|restart-telegram-bridge|telegram-bridge
    assert BIND in change.tags
    decision_id = _decision_bound_to(world, change)
    _resolve(world, decision_id)
    assert world.itil.get_cab_votes(change.id)
