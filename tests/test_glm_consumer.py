"""Zero-provider tests for the disabled chiap08 GLM consumer."""

from __future__ import annotations

import inspect
import json
import socket
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.fleet import glm_admission, glm_consumer
from skcapstone.fleet import glm_consumer_snapshots as snapshots
from skcapstone.fleet.glm_admission import Hold, QueueSample
from skcapstone.fleet.glm_consumer import PreparedWorker
from skcapstone.fleet.glm_consumer_snapshots import (
    CardCandidate,
    ConsumerDenied,
    HostSnapshot,
    PressureSample,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
HOLD_HASH = "bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05"
HOSTS = ("chiap01", "chiap02", "chiap03")


class FakeCoordinator:
    """In-memory supported claim and release adapter."""

    def __init__(self, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.claimed: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []

    def claim(self, card_id: str, agent_id: str) -> str:
        """Record a claim and return its fake supported receipt."""

        self.claimed.append((card_id, agent_id))
        return "duplicate" if self.duplicate else f"claim:{card_id}:{agent_id}"

    def release(self, card_id: str, agent_id: str) -> None:
        """Record a supported release."""

        self.released.append((card_id, agent_id))


class FakeBackend:
    """Non-provider staging backend with configurable commit result."""

    def __init__(self, tmp_path: Path, partial: bool = False) -> None:
        self.tmp_path = tmp_path
        self.partial = partial
        self.prepared: list[PreparedWorker] = []
        self.stopped: list[PreparedWorker] = []

    def stage(self, binding, transcript: Path) -> PreparedWorker:
        """Create a transcript fixture and return a staging token."""

        transcript = self.tmp_path / transcript.name
        transcript.write_text(f"transcript for {binding.session_id}\n", encoding="utf-8")
        item = PreparedWorker(binding, f"token:{binding.session_id}", transcript)
        self.prepared.append(item)
        return item

    def commit(self, prepared) -> set[str]:
        """Return all sessions, or deliberately omit one."""

        sessions = {item.binding.session_id for item in prepared}
        if self.partial:
            sessions.pop()
        return sessions

    def stop(self, prepared) -> None:
        """Record stop while deliberately preserving transcripts."""

        self.stopped.extend(prepared)


@pytest.fixture(autouse=True)
def zero_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forbid socket and process provider paths in every test."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider/network/process access is forbidden")

    monkeypatch.setattr(socket, "gethostname", lambda: "CHIAP08.")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(glm_consumer.subprocess, "run", forbidden)


def ts(value: datetime) -> str:
    """Format deterministic UTC time."""

    return value.isoformat().replace("+00:00", "Z")


def card(host: str, slot: int, **changes: object) -> CardCandidate:
    """Build one dependency-PASS unclaimed non-human card."""

    value = {
        "card_id": f"{host}-card-{slot}",
        "title": f"Card {slot}",
        "dependency_verdict": "PASS",
        "human_gate": False,
        "claim": None,
    }
    value.update(changes)
    return CardCandidate(**value)


def bundle(
    *,
    active_hold: bool = False,
    queued: tuple[int, int] = (0, 0),
    responses_429: tuple[int, int] = (0, 0),
    cards_by_host: dict[str, tuple[CardCandidate, ...]] | None = None,
) -> tuple[HostSnapshot, ...]:
    """Build three authoritative host snapshots."""

    queues = (
        QueueSample(ts(NOW - timedelta(seconds=5)), 0, queued[0]),
        QueueSample(ts(NOW), 0, queued[1]),
    )
    pressures = (
        PressureSample(queued[0], responses_429[0]),
        PressureSample(queued[1], responses_429[1]),
    )
    return tuple(
        HostSnapshot(
            host=host,
            observed_at=ts(NOW),
            reachable=True,
            glm_auto_sessions=0,
            hold=Hold("hold-7", HOLD_HASH, active_hold),
            queue_samples=queues,
            pressure_samples=pressures,
            cards=(cards_by_host or {}).get(host, tuple(card(host, slot) for slot in range(3))),
        )
        for host in HOSTS
    )


def patch_admission(monkeypatch: pytest.MonkeyPatch, expected_bundle) -> None:
    """Patch only filesystem/admission boundaries, retaining launch logic."""

    monkeypatch.setattr(snapshots, "read_bundle", lambda: expected_bundle)

    def admit(**kwargs: object) -> dict[str, object]:
        assert kwargs["snapshot_reader"]() == glm_consumer._admission_snapshot(expected_bundle)
        return {"status": "live"}

    monkeypatch.setattr(glm_consumer, "admit_wave", admit)


def run_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: tuple[HostSnapshot, ...],
    *,
    coordinator: FakeCoordinator | None = None,
    backend: FakeBackend | None = None,
):
    """Run launch logic with no real provider, process, or state access."""

    patch_admission(monkeypatch, evidence)
    monkeypatch.setattr(snapshots, "enabled", lambda: True)
    coord = coordinator or FakeCoordinator()
    worker = backend or FakeBackend(tmp_path)
    result = glm_consumer._launch_wave(generation=1, now=NOW, coordinator=coord, backend=worker)
    return result, coord, worker


def test_disabled_default_and_exact_physical_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing enablement is no-action and a spoofed non-chiap08 host is denied."""

    monkeypatch.setattr(snapshots, "enabled", lambda: False)
    assert glm_consumer.consume_once() is None
    monkeypatch.setattr(socket, "gethostname", lambda: "chiap08.example")
    with pytest.raises(ConsumerDenied, match="physical host is not chiap08"):
        glm_consumer.consume_once()


def test_public_interface_has_no_injection_parameters() -> None:
    """The public boundary has no generation, adapter, time, or path injection."""

    assert inspect.signature(glm_consumer.consume_once).parameters == {}
    for name in (
        "generation",
        "coordinator",
        "backend",
        "now",
        "authority_host",
        "snapshot_path",
        "lock_path",
        "ledger_path",
        "state_path",
    ):
        with pytest.raises(TypeError, match=name):
            glm_consumer.consume_once(**{name: "/tmp/spoof"})


def test_exact_cardinality_distribution_and_distinct_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful transaction has exactly nine and exactly three per host."""

    launched, coord, worker = run_launch(tmp_path, monkeypatch, bundle())
    assert len(launched) == len(coord.claimed) == len(worker.prepared) == 9
    assert Counter(item.host for item in launched) == Counter({host: 3 for host in HOSTS})
    for field in ("card_id", "agent_id", "session_id", "claim_id", "workspace"):
        assert len({getattr(item, field) for item in launched}) == 9


def test_duplicate_cards_and_claim_receipts_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate cards are rejected before claims and duplicate receipts roll back."""

    cards = {host: tuple(card(host, slot) for slot in range(3)) for host in HOSTS}
    cards["chiap03"] = (card("chiap03", 0, card_id="chiap01-card-0"),) + cards["chiap03"][1:]
    with pytest.raises(ConsumerDenied, match="distinct cards"):
        run_launch(tmp_path, monkeypatch, bundle(cards_by_host=cards))

    coord = FakeCoordinator(duplicate=True)
    with pytest.raises(ConsumerDenied, match="duplicate worker claim_id"):
        run_launch(tmp_path, monkeypatch, bundle(), coordinator=coord)
    assert len(coord.released) == 9


def test_stale_claim_non_pass_and_human_gated_cards_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any recorded claim and all non-PASS/human cards are ineligible."""

    stale = {"owner": "old", "claim_id": "old-1", "observed_at": ts(NOW - timedelta(days=1))}
    bad = (
        card("chiap01", 0, claim=stale),
        card("chiap01", 1, dependency_verdict="BLOCKED"),
        card("chiap01", 2, human_gate=True),
        card("chiap01", 3, title="[HUMAN] approve"),
    )
    cards = {"chiap01": bad}
    with pytest.raises(ConsumerDenied, match="lacks three dependency-clear cards"):
        run_launch(tmp_path, monkeypatch, bundle(cards_by_host=cards))


def test_queue_and_429_stop_before_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any 429 or two consecutive positive queues stops new dispatch."""

    for evidence in (bundle(responses_429=(1, 0)), bundle(queued=(1, 1))):
        coord = FakeCoordinator()
        with pytest.raises(ConsumerDenied, match="queue pressure or 429"):
            run_launch(tmp_path, monkeypatch, evidence, coordinator=coord)
        assert coord.claimed == []


@pytest.mark.parametrize("host", ("chiap02", "chiap03"))
@pytest.mark.parametrize(
    "pressures",
    (
        (PressureSample(0, 1), PressureSample(0, 0)),
        (PressureSample(1, 0), PressureSample(1, 0)),
    ),
)
def test_remote_host_pressure_stops_before_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    pressures: tuple[PressureSample, PressureSample],
) -> None:
    """Pressure appearing only on chiap02 or chiap03 fails closed."""

    evidence = tuple(
        replace(report, pressure_samples=pressures) if report.host == host else report
        for report in bundle()
    )
    coord = FakeCoordinator()
    with pytest.raises(ConsumerDenied, match="queue pressure or 429"):
        run_launch(tmp_path, monkeypatch, evidence, coordinator=coord)
    assert coord.claimed == []


def test_partial_launch_rolls_back_releases_claims_and_preserves_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial commit stops the wave, releases claims, and keeps every log."""

    coord = FakeCoordinator()
    worker = FakeBackend(tmp_path, partial=True)
    with pytest.raises(ConsumerDenied, match="partial worker launch"):
        run_launch(tmp_path, monkeypatch, bundle(), coordinator=coord, backend=worker)
    assert len(worker.stopped) == 9
    assert coord.released == list(reversed(coord.claimed))
    assert len(list(tmp_path.glob("*.log"))) == 9
    assert all(item.transcript.exists() for item in worker.prepared)


def test_active_hold_cannot_be_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The consumer exposes no hold writer and reviewed admission denies active hold."""

    evidence = bundle(active_hold=True)
    monkeypatch.setattr(snapshots, "read_bundle", lambda: evidence)
    monkeypatch.setattr(snapshots, "enabled", lambda: True)

    def deny_active(**kwargs: object) -> None:
        assert kwargs["snapshot_reader"]().hold.active is True
        raise glm_admission.AdmissionDenied("hold is active")

    monkeypatch.setattr(glm_consumer, "admit_wave", deny_active)
    coord = FakeCoordinator()
    with pytest.raises(ConsumerDenied, match="hold is active"):
        glm_consumer._launch_wave(
            generation=1,
            now=NOW,
            coordinator=coord,
            backend=FakeBackend(tmp_path),
        )
    assert len(coord.released) == 9
    assert not any("hold" in name and "clear" in name for name in dir(glm_consumer))


def valid_snapshot(host: str) -> dict[str, object]:
    """Build one on-disk exact snapshot payload."""

    return {
        "schema": snapshots.SNAPSHOT_SCHEMA,
        "host": host,
        "observed_at": ts(NOW),
        "reachable": True,
        "glm_auto_sessions": 0,
        "hold": {"generation": "hold-7", "sha256": HOLD_HASH, "active": False},
        "queue_samples": [
            {
                "observed_at": ts(NOW - timedelta(seconds=5)),
                "active": 0,
                "queued": 0,
                "responses_429": 0,
            },
            {"observed_at": ts(NOW), "active": 0, "queued": 0, "responses_429": 0},
        ],
        "cards": [
            {
                "card_id": f"{host}-card-{slot}",
                "title": f"Card {slot}",
                "dependency_verdict": "PASS",
                "human_gate": False,
                "claim": None,
            }
            for slot in range(3)
        ],
    }


def test_symlink_non_regular_and_malformed_fixed_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed snapshots reject symlinks, directories, and malformed JSON/shapes."""

    path = tmp_path / "chiap01.json"
    monkeypatch.setitem(snapshots.SNAPSHOT_PATHS, "chiap01", path)
    target = tmp_path / "target"
    target.write_text(json.dumps(valid_snapshot("chiap01")), encoding="utf-8")
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(ConsumerDenied, match="unsafe|missing"):
        snapshots.read_host_snapshot("chiap01")

    path.unlink()
    path.mkdir()
    with pytest.raises(ConsumerDenied, match="unsafe|missing"):
        snapshots.read_host_snapshot("chiap01")

    path.rmdir()
    path.write_text("{bad", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ConsumerDenied, match="malformed"):
        snapshots.read_host_snapshot("chiap01")

    malformed = valid_snapshot("chiap01")
    malformed["extra"] = True
    path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ConsumerDenied, match="malformed host snapshot"):
        snapshots.read_host_snapshot("chiap01")
