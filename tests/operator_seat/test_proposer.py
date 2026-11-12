"""The operator reasons over a firing brief; invalid actions are dropped."""

from __future__ import annotations

from skcapstone.operator_seat import proposer

_EXPLAIN = {
    "actions": [
        {"name": "restart_service", "blast_radius": "low", "reversible": True},
        {"name": "rerun_cronjob", "blast_radius": "low", "reversible": True},
        {"name": "delete_object", "blast_radius": "delete", "reversible": False},
    ]
}


def test_quiet_brief_proposes_nothing_without_calling_the_model():
    calls = []

    def _chat(_p):
        calls.append(1)
        return "[]"

    out = proposer.propose({"quiet": True, "firing": []}, _EXPLAIN, chat=_chat)
    assert out == []
    assert calls == []  # no model call on a quiet brief


def test_firing_brief_returns_validated_proposals():
    firing = {
        "quiet": False,
        "firing": [{"app": "fleet", "object": "web", "type": "Ready", "status": "False"}],
    }
    reply = (
        'Here is my plan: [{"app": "fleet", "condition": "Ready", '
        '"action": "restart_service", "object": "web", '
        '"change_class": "normal", "rationale": "web Ready went False"}]'
    )
    out = proposer.propose(firing, _EXPLAIN, chat=lambda _p: reply)
    assert len(out) == 1
    assert out[0]["action"] == "restart_service"
    assert out[0]["object"] == "web"


def test_unknown_action_is_dropped():
    firing = {
        "quiet": False,
        "firing": [{"app": "fleet", "object": "x", "type": "Ready", "status": "False"}],
    }
    reply = (
        '[{"app":"fleet","condition":"Ready","action":"nuke_everything","object":"x"},'
        '{"app":"fleet","condition":"Ready","action":"rerun_cronjob","object":"x"}]'
    )
    out = proposer.propose(firing, _EXPLAIN, chat=lambda _p: reply)
    assert [p["action"] for p in out] == ["rerun_cronjob"]  # unknown dropped


def test_unbound_proposal_is_dropped_even_when_action_is_known():
    firing = {
        "quiet": False,
        "firing": [{"app": "fleet", "object": "x", "type": "Ready", "status": "False"}],
    }
    reply = '[{"app":"skgateway","condition":"Ready","action":"restart_service","object":"x"}]'
    assert proposer.propose(firing, _EXPLAIN, chat=lambda _p: reply) == []


def test_malformed_reply_yields_no_proposals():
    firing = {
        "quiet": False,
        "firing": [{"app": "fleet", "object": "x", "type": "Ready", "status": "False"}],
    }
    out = proposer.propose(firing, _EXPLAIN, chat=lambda _p: "sorry, I cannot help")
    assert out == []


def test_extract_json_array_tolerates_fences():
    assert proposer._extract_json_array('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert proposer._extract_json_array("no json here") == []


def _stamped_firing(**extra):
    entry = {"app": "fleet", "object": "web", "type": "Ready", "status": "False", **extra}
    return {"quiet": False, "firing": [entry]}


_REPLY = (
    '[{"app": "fleet", "condition": "Ready", "action": "restart_service", '
    '"object": "web", "change_class": "normal", "rationale": "web Ready went False"}]'
)


def test_proposal_evidence_is_stamped_from_the_observation_not_the_model():
    firing = _stamped_firing(observed_at="2026-08-20T00:00:00Z")
    # The model tries to supply its own, newer evidence; it must be ignored.
    reply = _REPLY.replace(
        '"rationale"',
        '"evidence": {"source": "x", "observed_at": "2030-01-01T00:00:00Z"}, "rationale"',
    )
    out = proposer.propose(firing, _EXPLAIN, chat=lambda _p: reply)
    assert out[0]["evidence"] == {
        "source": "observation:fleet/Ready/web",
        "observed_at": "2026-08-20T00:00:00Z",
    }


def test_no_observation_time_means_no_evidence_so_actuation_fails_closed():
    out = proposer.propose(_stamped_firing(), _EXPLAIN, chat=lambda _p: _REPLY)
    assert "evidence" not in out[0]
