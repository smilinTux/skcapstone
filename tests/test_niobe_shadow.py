"""Focused safety tests for the Niobe shadow dispatcher seam."""

from datetime import datetime, timezone

from skcapstone.niobe_shadow import (
    SHADOW_ONLY,
    DispatcherObservation,
    HandoffEvidence,
    compare_observations,
    evidence_hash,
    validate_handoff,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _observation() -> DispatcherObservation:
    evidence = {"card_id": "card-1", "owner": "jarvis", "state": "active"}
    return DispatcherObservation("rec-1", "rev-1", {"state": "active"}, evidence_hash(evidence))


def _handoff(**changes) -> HandoffEvidence:
    values = {
        "card_id": "card-1",
        "claim_revision": "rev-1",
        "scope": ("skcapstone", "skdashboard", "skworld"),
        "expires_at": datetime(2026, 9, 6, 13, 0, tzinfo=timezone.utc),
        "rollback": "revoke packet and return to shadow_only",
        "recommendation": "review successor readiness",
    }
    values.update(changes)
    return HandoffEvidence(**values)


def test_shadow_parity_includes_identity_revision_process_hash_and_duplicate_suppression():
    result = compare_observations(_observation(), _observation())
    assert result.equal and result.duplicate and result.differences == ()
    changed = DispatcherObservation(
        "rec-2", "rev-1", {"state": "active"}, _observation().evidence_hash
    )
    assert compare_observations(_observation(), changed).differences == ("recommendation_id",)


def test_handoff_is_deterministic_and_preserves_audit_context():
    first = _handoff().as_record()
    assert first == _handoff().as_record()
    assert first["card_id"] == "card-1"
    assert first["claim_revision"] == "rev-1"
    assert first["scope"] == ["skcapstone", "skdashboard", "skworld"]
    assert first["authority"] == "none"
    assert first["mode"] == SHADOW_ONLY


def test_inactive_seat_denies_activation_and_stale_or_replay_conflicts():
    assert validate_handoff(_handoff(), observed_revision="rev-1", now=NOW) == (
        True,
        "shadow-recommendation-only",
    )
    assert (
        validate_handoff(_handoff(claim_revision="old"), observed_revision="rev-1", now=NOW)[1]
        == "stale-revision"
    )
    assert (
        validate_handoff(
            _handoff(), observed_revision="rev-1", now=NOW, active_dispatcher="niobe"
        )[1]
        == "active-dispatcher-conflict"
    )
    assert (
        validate_handoff(_handoff(authority="claim"), observed_revision="rev-1", now=NOW)[1]
        == "inactive-seat-denial"
    )


def test_shadow_functions_do_not_mutate_inputs_or_actuate():
    evidence = {"b": 2, "a": 1}
    before = dict(evidence)
    digest = evidence_hash(evidence)
    assert evidence == before and len(digest) == 64
    packet = _handoff()
    assert packet.mode == SHADOW_ONLY
