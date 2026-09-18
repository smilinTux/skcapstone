"""Unit tests for the worker liveness classifier (card 6764111c)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skcapstone.fleet.worker_liveness import (
    AssistanceRequest,
    LivenessDecision,
    LivenessObservation,
    _attributable,
    _retirement_receipt,
    classify,
)

REFRESH_EVERY_MINUTES = 5
ASSIST_AFTER_MINUTES = 60
CHECKPOINT_AFTER_MINUTES = 180


def make_observation(**overrides):
    observed_at = "2026-08-30T12:00:00Z"
    defaults = {
        "host": "host-a",
        "observer_host": "host-a",
        "observed_at": observed_at,
        "owner": "pi-pi-codex-aa11",
        "card_id": "bc7f2daa",
        "claim_generation": "rev-1",
        "process_identity": "pid:1234",
        "session_id": "session-1",
        "managed_session": True,
        "unit": "skfleet-host-a.service",
        "pid": "1234",
        "process_tree": ["1234", "5678"],
        "cgroup": "/sys/fs/cgroup/skfleet-host-a.service",
        "process_observed_at": observed_at,
        "cgroup_observed_at": observed_at,
        "beat_id": "b1",
        "heartbeat_at": observed_at,
        "workspace_custody": True,
        "workspace_path": "/home/skuser01/.skcapstone/fleet/workspaces/ws-1",
        "workspace_repository": "skcapstone",
        "workspace_head": "a" * 40,
        "workspace_custody_at": observed_at,
    }
    defaults.update(overrides)
    return LivenessObservation(**defaults)


def observed_at_dt(obs):
    return datetime.fromisoformat(obs.observed_at.replace("Z", "+00:00"))


def test_attribution_requires_matching_host_and_full_identity():
    obs = make_observation()
    assert _attributable(obs) is True
    # observer on a different host -> not attributable
    assert _attributable(make_observation(observer_host="host-b")) is False
    # missing process identity -> not attributable
    assert _attributable(make_observation(process_identity="")) is False
    # missing session -> not attributable
    assert _attributable(make_observation(session_id="")) is False


def test_retirement_receipt_requires_exact_fence():
    obs = make_observation()
    receipt = _retirement_receipt(obs)
    assert receipt is not None
    assert receipt.pid == obs.pid
    assert receipt.process_tree == obs.process_tree
    # pid missing from process_tree -> no receipt
    bad = make_observation(process_tree=["999"])
    assert _retirement_receipt(bad) is None


def test_fresh_worker_is_running_and_not_assistance_due():
    obs = make_observation(heartbeat_at="2026-08-30T11:59:00Z")
    now = observed_at_dt(obs) + timedelta(seconds=30)
    d = classify(obs, now=now, refresh_every_minutes=REFRESH_EVERY_MINUTES,
                  assist_after_minutes=ASSIST_AFTER_MINUTES,
                  checkpoint_after_minutes=CHECKPOINT_AFTER_MINUTES)
    assert d.state == "running"
    assert d.assistance_request is None
    assert d.retire is False


def test_quiet_past_assist_after_triggers_assistance_request():
    obs = make_observation(heartbeat_at="2026-08-30T09:00:00Z")
    now = observed_at_dt(obs) + timedelta(minutes=ASSIST_AFTER_MINUTES + 5)
    d = classify(obs, now=now, refresh_every_minutes=REFRESH_EVERY_MINUTES,
                  assist_after_minutes=ASSIST_AFTER_MINUTES,
                  checkpoint_after_minutes=CHECKPOINT_AFTER_MINUTES)
    assert d.state == "assistance-due"
    req = d.assistance_request
    assert req is not None
    assert req.reason == "no-heartbeat"
    assert req.requested_by == obs.owner
    assert req.card_id == obs.card_id
    assert req.claim_generation == obs.claim_generation
    assert req.observed_at == obs.observed_at
    assert req.process_identity == obs.process_identity
    # A stale observation may ask for help; only the retirement path retires.
    assert d.retire is False


def test_quiet_past_checkpoint_quarantines_and_preserves_workspace():
    obs = make_observation(heartbeat_at="2026-08-30T08:00:00Z")
    now = observed_at_dt(obs) + timedelta(minutes=CHECKPOINT_AFTER_MINUTES + 5)
    d = classify(obs, now=now, refresh_every_minutes=REFRESH_EVERY_MINUTES,
                  assist_after_minutes=ASSIST_AFTER_MINUTES,
                  checkpoint_after_minutes=CHECKPOINT_AFTER_MINUTES)
    assert d.state == "checkpoint-due"
    assert d.quarantine is True
    assert d.preserve_workspace is True
    assert d.assistance_request is not None


def test_needs_human_is_true_when_checkpoints_are_unanswered():
    obs = make_observation(heartbeat_at="2026-08-30T08:00:00Z")
    now = observed_at_dt(obs) + timedelta(minutes=CHECKPOINT_AFTER_MINUTES + 5)
    d = classify(obs, now=now, refresh_every_minutes=REFRESH_EVERY_MINUTES,
                  assist_after_minutes=ASSIST_AFTER_MINUTES,
                  checkpoint_after_minutes=CHECKPOINT_AFTER_MINUTES)
    assert d.needs_human is True


def test_needs_human_defaults_to_false_for_running_workers():
    obs = make_observation(heartbeat_at="2026-08-30T11:59:00Z")
    now = observed_at_dt(obs) + timedelta(minutes=5)
    d = classify(obs, now=now, refresh_every_minutes=REFRESH_EVERY_MINUTES,
                  assist_after_minutes=ASSIST_AFTER_MINUTES,
                  checkpoint_after_minutes=CHECKPOINT_AFTER_MINUTES)
    assert d.needs_human is False


def test_unattributable_observation_has_empty_reason():
    obs = make_observation(card_id="")
    now = observed_at_dt(obs) + timedelta(seconds=10)
    d = classify(obs, now=now, refresh_every_minutes=REFRESH_EVERY_MINUTES,
                  assist_after_minutes=ASSIST_AFTER_MINUTES,
                  checkpoint_after_minutes=CHECKPOINT_AFTER_MINUTES)
    assert d.state == "unattributable"
    assert d.reason == ""
