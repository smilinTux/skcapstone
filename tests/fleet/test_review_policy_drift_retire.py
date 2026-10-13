"""Authority retires unconsumed review offers sealed under an older policy."""

import json

import pytest

from skcapstone.fleet import review_recovery
from skcapstone.fleet import review_retire as retire

from . import test_review_admission_retire

pending = test_review_admission_retire.pending

OLD = {"authority_host": "chiap08", "lanes": "codex"}
NEW = {"authority_host": "chiap08", "lanes": "glm"}


@pytest.fixture
def drifted(pending, monkeypatch):
    """An unclaimed, unexpired, status-less offer sealed under OLD, current NEW."""
    p = pending
    p.status_path.unlink()
    p.events.pop()
    p.request["policy"] = dict(OLD)
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
