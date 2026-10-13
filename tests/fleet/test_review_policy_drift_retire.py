"""Authority retires unconsumed review offers sealed under an older policy."""

import json

import pytest

from skcapstone.fleet import review_recovery
from skcapstone.fleet import review_retire as retire

from . import test_review_admission_retire

pending = test_review_admission_retire.pending


def _policy(codex=True, glm=False, gateway="http://gw:1", quota=4):
    return {
        "authority_host": "chiap08",
        "gateway_url": gateway,
        "remote_review": {"enabled": True, "destinations": ["chiap03"], "card_ids": None},
        "node_quotas": {"chiap03": {"workers": quota}, "chiap04": {"workers": 1}},
        "lanes": {
            "codex": {"enabled": codex, "provider": "p"},
            "glm": {"enabled": glm, "provider": "g"},
        },
    }


OLD = _policy(codex=True, glm=False)
NEW = _policy(codex=False, glm=True)


@pytest.fixture
def drifted(pending, monkeypatch):
    """An unclaimed, unexpired, status-less offer sealed under OLD, current NEW."""
    p = pending
    p.status_path.unlink()
    p.events.pop()
    p.request["policy"] = dict(OLD)
    p.request["production"]["policy_sha256"] = retire.production_builder.digest(OLD)
    p.request["lease_expires_at"] = "2099-01-01T00:00:00Z"
    p.request_path.write_text(json.dumps(p.request))
    p.events[0].update(
        ts="2026-10-13T00:00:00Z", request_sha256=retire.production_builder.digest(p.request)
    )
    p.state.update(LoadState="not-found", ActiveState="inactive", SubState="dead", InvocationID="")
    p.core["links"]["worker_died"] = "older claim"
    monkeypatch.setattr(retire.production_builder, "policy", lambda: dict(NEW))
    return p


def sweep(p):
    return review_recovery.retire_policy_drifted_offers(p.paths, p.home, "niobe")


def test_drifted_unconsumed_offer_is_retired_for_reoffer(drifted):
    p = drifted
    [result] = sweep(p)
    assert result["state"] == "retired-prestart", result
    assert result["old_digest"] == retire.production_builder.digest(OLD)
    assert result["new_digest"] == retire.production_builder.digest(NEW)
    assert result["request_id"] == p.request["request_id"]
    assert not p.request_path.exists()
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}
    assert not any(e["action"] == "verdict" for e in p.events)


def test_idempotent_across_cycles(drifted):
    p = drifted
    assert sweep(p)[0]["state"] == "retired-prestart"
    before = len(p.events)
    assert sweep(p) == []
    assert len(p.events) == before


def test_current_policy_offer_untouched(drifted, monkeypatch):
    p = drifted
    monkeypatch.setattr(retire.production_builder, "policy", lambda: dict(OLD))
    assert sweep(p) == []
    assert p.request_path.exists()


def test_status_evidence_stays_held(drifted):
    p = drifted
    p.status_path.write_text(json.dumps(p.status))
    assert sweep(p) == []
    assert p.request_path.exists()


def test_launch_evidence_stays_held(drifted):
    p = drifted
    p.events.append(
        dict(
            action="review_assignment_launch",
            recommendation_id=p.request["request_id"],
            launched=True,
        )
    )
    [result] = sweep(p)
    assert result["state"] == "refused"
    assert p.request_path.exists()


def test_claim_evidence_stays_held(drifted):
    p = drifted
    p.events.append(dict(action="claim", ts="2026-10-13T00:01:00Z", writer="x"))
    assert sweep(p)[0]["state"] == "refused"
    assert p.request_path.exists()


def test_running_unit_stays_held(drifted):
    p = drifted
    p.state.update(LoadState="loaded", ActiveState="active", SubState="running", MainPID="42")
    assert sweep(p)[0]["state"] == "refused"
    assert p.request_path.exists()


def test_missing_policy_on_offer_is_ignored(drifted):
    p = drifted
    p.request.pop("policy")
    p.request_path.write_text(json.dumps(p.request))
    assert sweep(p) == []


def test_policy_drift_flag_refuses_same_policy(drifted, monkeypatch):
    p = drifted
    monkeypatch.setattr(retire.production_builder, "policy", lambda: dict(OLD))
    with pytest.raises(ValueError, match="not drifted"):
        retire.retire_prestart(
            p.paths,
            p.home,
            p.node,
            p.card,
            request_sha256=retire.source_bundle._sha(p.request_path.read_bytes()),
            card_sha256="f" * 64,
            previous_owner=None,
            previous_claim_revision=None,
            actor="niobe",
            reason="x",
            policy_drifted=True,
        )


@pytest.mark.parametrize(
    "current,compatible",
    [
        (_policy(codex=True, glm=True), True),  # other lane toggled on
        (_policy(codex=True, glm=False), True),  # identical
        (_policy(codex=False, glm=True), False),  # own lane disabled
        (_policy(gateway="http://other:1"), False),  # execution field changed
        (_policy(quota=9), False),  # executing host resources changed
    ],
)
def test_offer_policy_compatible(current, compatible):
    from skcapstone.fleet.production_policy import offer_policy_compatible

    assert offer_policy_compatible(OLD, current, "codex", "chiap03") is compatible


def test_other_host_quota_and_other_lane_do_not_invalidate():
    from skcapstone.fleet.production_policy import offer_policy_compatible

    current = _policy(codex=True, glm=True)
    current["node_quotas"]["chiap04"] = {"workers": 7}
    assert offer_policy_compatible(OLD, current, "codex", "chiap03")


def test_malformed_or_unknown_family_fails_closed():
    from skcapstone.fleet.production_policy import offer_policy_compatible

    assert not offer_policy_compatible(OLD, _policy(glm=True), "nope", "chiap03")
    assert not offer_policy_compatible(None, OLD, "codex", "chiap03")
    changed_provider = _policy(glm=True)
    changed_provider["lanes"]["codex"]["provider"] = "other"
    assert not offer_policy_compatible(OLD, changed_provider, "codex", "chiap03")


def test_toggle_of_other_lane_leaves_offer_untouched(drifted, monkeypatch):
    p = drifted
    monkeypatch.setattr(retire.production_builder, "policy", lambda: _policy(codex=True, glm=True))
    assert sweep(p) == []
    assert p.request_path.exists()


def test_node_holds_only_incompatible_offer(drifted):
    from skcapstone.fleet import review_dispatch

    p = drifted
    assert not review_dispatch._offer_policy_current(p.request, NEW)
    assert review_dispatch._offer_policy_current(p.request, _policy(codex=True, glm=True))
