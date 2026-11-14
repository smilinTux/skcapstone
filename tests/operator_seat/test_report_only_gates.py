"""Card f8dd9441 (fewest gates), review conditions on #1361.

The loop drafts auto-normal changes as ``operator`` so they auto-approve and actuate;
one decision record per episode, ended only from a definite clear and only on the node
that observed it; the report-only hint follows the proposal's class; ``honor-pending``
exits 3 when an intent can never actuate.
"""

from __future__ import annotations

import json

from skcapstone import cab_approval
from skcapstone.fleet.paths import self_node_name
from skcapstone.operator_seat import cli, decisions, dispatch, loop

from .test_dispatch_e2e import _change, _loop_pass, world  # noqa: F401

_OPT = {"app": "a", "condition": "C", "object": "o", "action": "x"}


def test_the_loop_drafts_an_auto_normal_change_as_operator_and_it_actuates(world):  # noqa: F811
    res = _loop_pass(world, standard=False)
    outcome = res["outcomes"][0]
    change = _change(world, world.ledger.list_intents()[0])
    assert outcome["disposition"] == "auto"
    assert outcome["outcome"] == "verified"
    assert change.change_type.value == "normal"
    assert "auto-normal" in change.tags
    # The fold's auto-normal rule needs the operator as author; the drafter is the manager.
    assert change.created_by == "operator"
    assert change.managed_by != "operator"
    assert cab_approval.auto_approved(change, [])
    assert change.status.value == "deployed"
    assert len(world.calls) == 1


def test_one_decision_record_per_episode_through_the_full_cycle(tmp_path):
    d = tmp_path / "dec"
    first = decisions.park(d, [_OPT], decision_id="abc", created_iso="t1")
    assert first["id"] == "abc" and first["status"] == "pending"
    # Reused while pending.
    assert decisions.park(d, [_OPT], decision_id="abc", created_iso="t2")["id"] == "abc"
    # Pending records are never ended, even when cleared.
    assert decisions.end_cleared_episodes(d, lambda o: True, ended_iso="t2b") == []
    decisions.resolve(d, "abc", approve=False, choice=None, by="chef", resolved_iso="t3")
    # Reused while resolved but not yet cleared.
    assert decisions.park(d, [_OPT], decision_id="abc", created_iso="t4")["id"] == "abc"
    assert decisions.end_cleared_episodes(d, lambda o: False, ended_iso="t4b") == []
    assert decisions.park(d, [_OPT], decision_id="abc", created_iso="t4c")["id"] == "abc"
    # Cleared: the episode ends, and the next escalation is a new record.
    assert decisions.end_cleared_episodes(d, lambda o: True, ended_iso="t5") == ["abc"]
    second = decisions.park(d, [_OPT], decision_id="abc", created_iso="t6")
    assert second["id"] == "abc-e2" and second["status"] == "pending"
    assert decisions.park(d, [_OPT], decision_id="abc", created_iso="t7")["id"] == "abc-e2"
    decisions.resolve(d, "abc-e2", approve=False, choice=None, by="chef", resolved_iso="t8")
    assert decisions.end_cleared_episodes(d, lambda o: True, ended_iso="t9") == ["abc-e2"]
    third = decisions.park(d, [_OPT], decision_id="abc", created_iso="t10")
    assert third["id"] == "abc-e3" and third["status"] == "pending"
    # Ending is idempotent: an ended record is not marked again.
    assert decisions.end_cleared_episodes(d, lambda o: True, ended_iso="t11") == []


def test_only_the_observing_node_ends_an_episode(tmp_path):
    d = tmp_path / "dec"
    decisions.park(d, [{**_OPT, "observed_on": "node-a"}], decision_id="abc", created_iso="t1")
    decisions.resolve(d, "abc", approve=False, choice=None, by="chef", resolved_iso="t2")
    assert decisions.end_cleared_episodes(d, lambda o: True, ended_iso="t3", node="node-b") == []
    assert decisions.park(d, [_OPT], decision_id="abc", created_iso="t4")["id"] == "abc"
    assert decisions.end_cleared_episodes(d, lambda o: True, ended_iso="t5", node="node-a") == [
        "abc"
    ]


def _escalate_then_acknowledge(w):
    res = _loop_pass(w, target_known=lambda p: False)
    assert res["outcomes"][0]["outcome"].startswith("escalated (report-only")
    (record,) = decisions.list_pending(str(w.decisions))
    assert record["options"][0]["observed_on"] == self_node_name()
    decisions.resolve(
        str(w.decisions), record["id"], approve=False, choice=None, by="chef", resolved_iso="t"
    )
    return w.decisions / f"{record['id']}.json"


def test_the_loop_ends_its_own_episode_once_the_condition_clears(world):  # noqa: F811
    path = _escalate_then_acknowledge(world)
    world.heal()
    _loop_pass(world, target_known=lambda p: False)
    assert json.loads(path.read_text()).get("episode_ended_at")


def test_the_loop_never_ends_another_nodes_episode(world):  # noqa: F811
    path = _escalate_then_acknowledge(world)
    record = json.loads(path.read_text())
    record["options"][0]["observed_on"] = "some-other-node"
    path.write_text(json.dumps(record))
    world.heal()
    _loop_pass(world, target_known=lambda p: False)
    assert not json.loads(path.read_text()).get("episode_ended_at")


def test_option_cleared_needs_a_definite_reading_and_unknown_never_ends_an_episode():
    def obs(status):
        return {"a": [{"type": "C", "object": "o", "status": status}]}

    ptypes = {"C"}  # problem when True
    assert loop._option_cleared(obs("False"), ptypes)(_OPT) is True
    assert loop._option_cleared(obs("True"), ptypes)(_OPT) is False
    assert loop._option_cleared(obs("Unknown"), ptypes)(_OPT) is False
    assert loop._option_cleared({}, ptypes)(_OPT) is False
    assert (
        loop._option_cleared({"a": [{"type": "Other", "status": "False"}]}, ptypes)(_OPT) is False
    )


def test_the_report_only_hint_follows_the_class():
    major = dispatch.report_only_reason("skchat", "kill-session", "major")
    assert "cannot help" in major and "by hand" in major and "apps ratify" not in major
    emergency = dispatch.report_only_reason("skchat", "kill-session", "emergency")
    assert "cannot help" in emergency
    normal = dispatch.report_only_reason("skchat", "restart-x", "normal")
    assert "skoperator apps ratify skchat restart-x" in normal and "auto-normal" in normal
    generic = dispatch.report_only_reason("skchat", "restart-x")
    assert "skoperator apps ratify skchat restart-x" in generic


def test_a_parked_record_carries_the_class_hint_into_the_approve_refusal(world):  # noqa: F811
    _loop_pass(world, target_known=lambda p: False)
    (record,) = decisions.list_pending(str(world.decisions))
    option = record["options"][0]
    assert option["change_class"] == "standard"
    try:
        dispatch.resolve_decision(
            str(world.decisions),
            world.itil,
            record["id"],
            approve=True,
            choice=0,
            subject="chef",
            resolved_iso="t",
        )
    except dispatch.ReportOnlyDecisionError as exc:
        assert option["report_only_reason"] in str(exc)
    else:
        raise AssertionError("approve did not refuse")


def test_honor_pending_exits_3_when_an_intent_is_report_only():
    outcomes = [
        dispatch.DispatchOutcome("act-1", "verified"),
        dispatch.DispatchOutcome("act-2", f"{dispatch.REPORT_ONLY_REFUSAL}: no"),
    ]
    line, code = cli.honor_pending_summary(outcomes)
    assert code == cli.EXIT_NEVER_ACTUATES == 3
    assert "1 verified" in line and "1 report-only" in line
    assert cli.honor_pending_summary(outcomes[:1])[1] == 0
    assert cli.honor_pending_summary([])[1] == 0
