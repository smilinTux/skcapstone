"""The decision layer classifies proposals and disposes auto vs escalate."""

from __future__ import annotations

from skcapstone.operator_seat import plan

_EXPLAIN = {
    "actions": [
        {"name": "restart_service", "standard": True, "reversible": True, "blast_radius": "low"},
        {"name": "rerun_cronjob", "standard": True, "reversible": True, "blast_radius": "low"},
        {
            "name": "delete_object",
            "standard": False,
            "reversible": False,
            "blast_radius": "delete",
        },
    ]
}


def _proposal(action):
    return {"action": action, "object": "x", "change_class": "normal", "rationale": "r"}


def test_ratified_standard_action_auto():
    out = plan.plan_actions([_proposal("restart_service")], _EXPLAIN)
    assert out[0]["classification"]["change_class"] == "standard"
    assert out[0]["disposition"] == "auto"


def test_reversible_operator_normal_is_auto():
    # rerun_cronjob is not in the ratified standard catalog, but it is reversible
    # and operator-authored, so it is an auto-normal.
    out = plan.plan_actions([_proposal("rerun_cronjob")], _EXPLAIN)
    assert out[0]["classification"]["change_class"] == "normal"
    assert out[0]["disposition"] == "auto"


def test_irreversible_delete_escalates():
    out = plan.plan_actions([_proposal("delete_object")], _EXPLAIN)
    assert out[0]["classification"]["change_class"] == "major"
    assert out[0]["disposition"] == "escalate"


def test_unknown_action_escalates():
    out = plan.plan_actions([_proposal("mystery")], _EXPLAIN)
    assert out[0]["disposition"] == "escalate"  # no catalog metadata -> not auto


def test_non_operator_author_escalates():
    out = plan.plan_actions([_proposal("rerun_cronjob")], _EXPLAIN, author="someone")
    assert out[0]["disposition"] == "escalate"


def test_unresolvable_target_escalates_instead_of_auto():
    # The skoperator incident: the proposer named the app label 'skgateway' as
    # the object, while the real objects are 'upstreams' and 'connection-pool'.
    # plan_actions validated the action against the catalog but never the
    # target, so an unresolvable object still classified auto and executed.
    out = plan.plan_actions([_proposal("restart_service")], _EXPLAIN, target_known=lambda p: False)
    assert out[0]["disposition"] == "escalate"
    assert out[0]["unresolved_target"] is True


def test_resolvable_target_still_auto():
    out = plan.plan_actions([_proposal("restart_service")], _EXPLAIN, target_known=lambda p: True)
    assert out[0]["disposition"] == "auto"
    assert out[0]["unresolved_target"] is False


def test_no_validator_preserves_existing_behavior():
    # Default (no predicate) must behave exactly as before: validation is
    # opt-in so callers without fleet access are unaffected.
    out = plan.plan_actions([_proposal("restart_service")], _EXPLAIN)
    assert out[0]["disposition"] == "auto"


def test_unresolvable_target_does_not_rescue_an_escalation():
    # A target that resolves must never upgrade an escalation to auto.
    out = plan.plan_actions([_proposal("delete_object")], _EXPLAIN, target_known=lambda p: True)
    assert out[0]["disposition"] == "escalate"


def test_unratified_app_condition_binding_escalates():
    out = plan.plan_actions(
        [_proposal("restart_service")], _EXPLAIN, action_allowed=lambda proposal: False
    )
    assert out[0]["disposition"] == "escalate"
    assert out[0]["binding_denied"] is True
