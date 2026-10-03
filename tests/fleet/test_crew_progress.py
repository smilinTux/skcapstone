"""Claim-bound synthetic telemetry, without live fleet or model actions."""

from __future__ import annotations

import ast
import copy
import json
import socket
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.fleet import crew_progress as progress
from skcapstone.fleet.paths import FleetPaths
from skcapstone.fleet.worker_watchdog import ProgressObservation

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
CARD = "aabb0011"


@pytest.fixture
def sample(tmp_path):
    paths = FleetPaths(tmp_path / "fleet")
    observation = ProgressObservation(
        owner="jarvis",
        card_id=CARD,
        session_id="codex-auto-aabb0011",
        claim_revision="generation-1",
        expected_claim_revision="generation-1",
        progress_at=(NOW - timedelta(seconds=1800)).isoformat(),
        session_alive=True,
        transcript_bytes=1024,
    )
    manifest = {
        "authorizer": "jarvis",
        "packet": {
            "parent_id": CARD,
            "parent_claim_revision": "generation-1",
        },
    }
    return paths, observation, manifest


def publish(sample, *, host="fixture-node", now=NOW, **overrides):
    paths, observation, _ = sample
    kwargs = {
        "host": host,
        "observed_at": now.isoformat(),
        "source": "session-mtime",
        "receipt_local": True,
        "claim_owner": observation.owner,
    }
    kwargs.update(overrides)
    return progress.publish_progress(paths, observation, **kwargs)


def record_path(sample, host="fixture-node"):
    return sample[0].root / "crew-progress" / host / f"{CARD}.json"


def test_original_observation_retained_and_thirty_samples_share_digest(sample):
    assert publish(sample)
    row = json.loads(record_path(sample).read_text())
    assert row["observation"] == asdict(sample[1])
    assert row["host"] == "fixture-node"
    first = progress.stalled_evidence(sample[0], sample[2], NOW)
    assert len(first) == 64
    for minute in range(1, 31):
        now = NOW + timedelta(minutes=minute)
        assert publish(sample, now=now)
        assert progress.stalled_evidence(sample[0], sample[2], now) == first


@pytest.mark.parametrize(
    "overrides",
    [
        {"receipt_local": False},
        {"receipt_local": 1},
        {"claim_owner": "other"},
    ],
)
def test_unbound_publisher_creates_nothing(sample, overrides):
    assert not publish(sample, **overrides)
    assert not sample[0].root.exists()


def test_old_sample_or_same_time_conflict_cannot_overwrite(sample):
    assert publish(sample)
    before = record_path(sample).read_bytes()
    assert not publish(sample, now=NOW - timedelta(seconds=1))
    changed = (sample[0], replace(sample[1], session_id="different-session"), sample[2])
    assert not publish(changed)
    assert record_path(sample).read_bytes() == before


def test_progress_cannot_regress_inside_same_generation_and_session(sample):
    assert publish(sample)
    older = replace(sample[1], progress_at=(NOW - timedelta(hours=2)).isoformat())
    assert not publish((sample[0], older, sample[2]), now=NOW + timedelta(seconds=1))


def test_new_generation_is_not_evidence_for_old_manifest(sample):
    assert publish(sample)
    obs = replace(sample[1], claim_revision="generation-2", expected_claim_revision="generation-2")
    changed = (sample[0], obs, sample[2])
    assert publish(changed, now=NOW + timedelta(seconds=1))
    assert progress.stalled_evidence(sample[0], sample[2], NOW + timedelta(seconds=1)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"progress_at": None},
        {"progress_at": NOW.isoformat()},
        {"terminal_evidence_seen": True},
        {"session_alive": False},
        {"session_alive": None},
        {"owner": "other"},
    ],
)
def test_only_stale_live_exact_owner_observation_is_diagnostic(sample, changes):
    changed = (sample[0], replace(sample[1], **changes), sample[2])
    assert publish(changed)
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None


def test_workspace_mtime_is_reported_but_not_diagnostic(sample):
    assert publish(sample, source="workspace-mtime")
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None


def test_sample_staleness_and_future_clock_refuse(sample):
    assert publish(sample)
    assert progress.stalled_evidence(sample[0], sample[2], NOW - timedelta(seconds=1)) is None
    assert progress.stalled_evidence(sample[0], sample[2], NOW + timedelta(seconds=601)) is None
    assert progress.stalled_evidence(sample[0], sample[2], NOW + timedelta(seconds=600))


@pytest.mark.parametrize(
    "key,value",
    [
        ("claim_revision", "old-generation"),
        ("expected_claim_revision", "old-generation"),
        ("card_id", "ffffffff"),
        ("progress_at", "not-time"),
        ("progress_at", "2026-09-24T11:00:00"),
        ("progress_at", (NOW + timedelta(seconds=1)).isoformat()),
        ("session_alive", "true"),
        ("transcript_bytes", True),
        ("transcript_bytes", -1),
        ("owner", ""),
        ("session_id", "x" * 129),
    ],
)
def test_malformed_or_old_generation_record_never_triggers(sample, key, value):
    assert publish(sample)
    path = record_path(sample)
    row = json.loads(path.read_text())
    row["observation"][key] = value
    path.write_text(json.dumps(row))
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None


@pytest.mark.parametrize("payload", ["[]", "null", "{", '{"schema":1,"schema":2}', "x" * 262145])
def test_bounded_strict_json_reads(sample, payload):
    assert publish(sample)
    record_path(sample).write_text(payload)
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None


def test_newest_host_sample_suppresses_an_old_stall(sample):
    assert publish(sample, host="old-host")
    newer = (sample[0], replace(sample[1], progress_at=NOW.isoformat()), sample[2])
    assert publish(newer, host="new-host", now=NOW + timedelta(seconds=1))
    assert progress.stalled_evidence(sample[0], sample[2], NOW + timedelta(seconds=1)) is None


def test_equal_time_different_host_custody_is_ambiguous(sample):
    assert publish(sample, host="old-host")
    assert publish(sample, host="new-host")
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None


def test_progress_change_changes_the_stable_digest(sample):
    assert publish(sample)
    old = progress.stalled_evidence(sample[0], sample[2], NOW)
    newer = replace(sample[1], progress_at=(NOW - timedelta(seconds=1700)).isoformat())
    assert publish((sample[0], newer, sample[2]), now=NOW + timedelta(seconds=1))
    assert progress.stalled_evidence(sample[0], sample[2], NOW + timedelta(seconds=1)) != old


@pytest.mark.parametrize("location", ["leaf", "host", "ancestor"])
def test_symlinks_are_not_followed_for_publish_or_read(sample, tmp_path, location):
    target = tmp_path / "untouched"
    target.mkdir()
    (target / f"{CARD}.json").write_text("untouched")
    path = record_path(sample)
    if location == "leaf":
        path.parent.mkdir(parents=True)
        path.symlink_to(target / f"{CARD}.json")
    elif location == "host":
        path.parent.parent.mkdir(parents=True)
        path.parent.symlink_to(target, target_is_directory=True)
    else:
        sample[0].root.symlink_to(target, target_is_directory=True)
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None
    with pytest.raises((ValueError, OSError)):
        publish(sample)
    assert (target / f"{CARD}.json").read_text() == "untouched"


def test_reader_only_opens_exact_manifest_card_and_host_scan_is_bounded(sample, monkeypatch):
    assert publish(sample)
    (record_path(sample).parent / "unrelated.json").write_text("not-json")
    assert progress.stalled_evidence(sample[0], sample[2], NOW)
    monkeypatch.setattr(progress, "MAX_HOSTS", 1)
    (record_path(sample).parent.parent / "extra-host").mkdir()
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None


def test_missing_telemetry_does_not_create_storage(sample):
    assert progress.stalled_evidence(sample[0], sample[2], NOW) is None
    assert not sample[0].root.exists()


def test_thirty_unchanged_real_controller_cycles_assign_one_diagnostic(sample, monkeypatch):
    from skcoord.card_store import CardCore, CardStore

    from skcapstone.fleet import crew_controller, crew_requests, store

    paths, observation, _ = sample
    home = paths.root.parent
    monkeypatch.setattr(socket, "gethostname", lambda: "fixture-node")
    cards = CardStore(home)
    cards.create(
        CardCore(
            id=CARD,
            title="[M] Deliver",
            description="Bounded synthetic work",
            acceptance_criteria=["Verified result"],
            initial_owner="jarvis",
            initial_claim_revision="generation-1",
            initial_labels=["source-only"],
            meta={
                "repository": "https://example.invalid/repo.git",
                "base_ref": "main",
                "base_revision": "a" * 40,
            },
        )
    )
    operator = store.Writer(role="operator", node="fixture-node", identity="fixture")
    store.set_frozen(paths, False, writer=operator)
    packet = {
        "schema": "skfleet.crew/v1",
        "crew_id": "silent-crew",
        "parent_id": CARD,
        "parent_claim_revision": "generation-1",
        "coordinator_node": "fixture-node",
        "owner_paths": ["src/owner.py"],
        "max_active_helpers": 1,
        "max_helpers": 2,
        "slots": [
            {
                "slot_id": "diagnose",
                "role": "support",
                "trigger": "stale-progress",
                "packet": {
                    "request_id": "diagnose",
                    "title": "[S] Diagnose silence",
                    "objective": "Read-only exact progress diagnosis",
                    "criteria": ["Return evidence"],
                    "allowed_paths": [],
                    "verification_commands": ["Read exact source only"],
                },
            }
        ],
    }
    crew_requests.register(paths, home, "jarvis", packet)
    before = cards.fold(CARD).model_dump(mode="json")
    assert observation.owner == "jarvis"
    created = []
    for minute in range(30):
        now = NOW + timedelta(minutes=minute)
        assert publish(sample, now=now)
        report = crew_controller.reconcile_crews(paths, home, "fixture-node", now=now)
        created.extend(report["created"])
    assert len(created) == 1
    assert len(cards.list_cards()) == 2
    assert cards.fold(created[0]).owner is None
    assert cards.fold(CARD).model_dump(mode="json") == before
