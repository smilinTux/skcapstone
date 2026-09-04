"""Deterministic zero-network qualification for the GLM wave candidate."""

from __future__ import annotations

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.fleet.glm_admission import (
    AUTHORITY_HOST,
    SCHEMA,
    AdmissionDenied,
    AdmissionSnapshot,
    Hold,
    HostReport,
    QueueSample,
    WorkerBinding,
    admit_wave,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
HOLD_HASH = "bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05"
HOSTS = ("chiap01", "chiap02", "chiap03")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn any accidental network operation into an immediate test failure."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def timestamp(value: datetime) -> str:
    """Format a deterministic UTC timestamp."""

    return value.isoformat().replace("+00:00", "Z")


def bindings() -> tuple[WorkerBinding, ...]:
    """Build a valid host-distinct 3-by-3 wave."""

    result = []
    index = 0
    for host in HOSTS:
        for slot in range(3):
            index += 1
            result.append(
                WorkerBinding(
                    host=host,
                    card_id=f"card-{index}",
                    agent_id=f"glm-{host}-{slot}",
                    session_id=f"session-{index}",
                    claim_id=f"claim-{index}",
                    workspace=f"/fleet/{host}/card-{index}",
                )
            )
    return tuple(result)


def snapshot(
    *,
    active_hold: bool = False,
    host_sessions: int = 0,
    reachable: tuple[str, ...] = HOSTS,
    second_sample_age: int = 0,
    hold_generation: str = "hold-7",
    hold_hash: str = HOLD_HASH,
) -> AdmissionSnapshot:
    """Build frozen admission evidence without observing live systems."""

    second_at = NOW - timedelta(seconds=second_sample_age)
    return AdmissionSnapshot(
        hold=Hold(generation=hold_generation, sha256=hold_hash, active=active_hold),
        hosts=tuple(
            HostReport(
                host=host,
                reachable=host in reachable,
                glm_auto_sessions=host_sessions,
                observed_at=timestamp(NOW),
            )
            for host in HOSTS
        ),
        queue_samples=(
            QueueSample(
                observed_at=timestamp(second_at - timedelta(seconds=5)), active=0, queued=0
            ),
            QueueSample(observed_at=timestamp(second_at), active=0, queued=0),
        ),
    )


def write_genesis(path: Path, *, updated_at: datetime = NOW, generation: int = 0) -> None:
    """Write the exact valid initial ledger fixture."""

    path.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "authority_host": AUTHORITY_HOST,
                "generation": generation,
                "status": "complete",
                "updated_at": timestamp(updated_at),
                "hold": {"generation": "hold-7", "sha256": HOLD_HASH},
                "workers": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def run_admission(
    root: Path,
    *,
    evidence: AdmissionSnapshot | None = None,
    proposed: tuple[WorkerBinding, ...] | None = None,
    generation: int = 1,
    crash_hook=None,
):
    """Invoke the candidate against one temporary authority directory."""

    frozen = evidence or snapshot()
    return admit_wave(
        authority_host=AUTHORITY_HOST,
        ledger_path=root / "generation.json",
        lock_path=root / "admission.lock",
        proposed_generation=generation,
        bindings=proposed or bindings(),
        snapshot_reader=lambda: frozen,
        now=NOW,
        crash_hook=crash_hook,
    )


def test_concurrent_admission_serializes_one_whole_wave(tmp_path: Path) -> None:
    """Concurrent candidates produce one nine-worker generation and no refill."""

    write_genesis(tmp_path / "generation.json")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(run_admission, tmp_path) for _ in range(8)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result()["status"])
        except AdmissionDenied:
            outcomes.append("denied")
    assert outcomes.count("live") == 1
    assert outcomes.count("denied") == 7
    ledger = json.loads((tmp_path / "generation.json").read_text())
    assert len(ledger["workers"]) == 9
    assert ledger["generation"] == 1


@pytest.mark.parametrize(
    ("name", "evidence", "message"),
    [
        ("stale sessions", snapshot(host_sessions=1), "glm-auto session"),
        ("stale in-flight samples", snapshot(second_sample_age=11), "stale in-flight"),
        ("partial reachability", snapshot(reachable=("chiap01", "chiap02")), "unreachable"),
        ("active hold", snapshot(active_hold=True), "hold is active"),
    ],
)
def test_unsafe_observations_deny(
    tmp_path: Path, name: str, evidence: AdmissionSnapshot, message: str
) -> None:
    """Stale, partial, and held observations all fail closed."""

    write_genesis(tmp_path / "generation.json")
    with pytest.raises(AdmissionDenied, match=message):
        run_admission(tmp_path, evidence=evidence)
    assert json.loads((tmp_path / "generation.json").read_text())["status"] == "complete"


def test_malformed_missing_stale_and_non_monotonic_ledgers_deny(tmp_path: Path) -> None:
    """No malformed or untrustworthy ledger can bootstrap a wave."""

    ledger = tmp_path / "generation.json"
    with pytest.raises(AdmissionDenied, match="missing or malformed"):
        run_admission(tmp_path)
    ledger.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(AdmissionDenied, match="missing or malformed"):
        run_admission(tmp_path)
    write_genesis(ledger, updated_at=NOW - timedelta(seconds=31))
    with pytest.raises(AdmissionDenied, match="stale ledger"):
        run_admission(tmp_path)
    write_genesis(ledger)
    with pytest.raises(AdmissionDenied, match="non-monotonic proposed"):
        run_admission(tmp_path, generation=2)


def test_writer_crash_before_rename_preserves_old_generation(tmp_path: Path) -> None:
    """A crash after temp fsync but before rename leaves genesis authoritative."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    old_bytes = ledger.read_bytes()

    def crash(stage: str) -> None:
        if stage == "before_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_admission(tmp_path, crash_hook=crash)
    assert ledger.read_bytes() == old_bytes
    assert list(tmp_path.glob(".generation.json.*")) == []


def test_writer_crash_after_rename_exposes_complete_live_generation(tmp_path: Path) -> None:
    """A crash after rename exposes all new bytes and prevents refill."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)

    def crash(stage: str) -> None:
        if stage == "after_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_admission(tmp_path, crash_hook=crash)
    published = json.loads(ledger.read_text())
    assert published["status"] == "live"
    assert published["generation"] == 1
    assert len(published["workers"]) == 9
    with pytest.raises(AdmissionDenied, match="never refilled"):
        run_admission(tmp_path, generation=2)


def test_hold_change_during_locked_decision_denies(tmp_path: Path) -> None:
    """A hold generation or hash change during admission denies publication."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    observations = iter((snapshot(), snapshot(hold_generation="hold-8")))
    with pytest.raises(AdmissionDenied, match="hold changed"):
        admit_wave(
            authority_host=AUTHORITY_HOST,
            ledger_path=ledger,
            lock_path=tmp_path / "admission.lock",
            proposed_generation=1,
            bindings=bindings(),
            snapshot_reader=lambda: next(observations),
            now=NOW,
        )
    assert json.loads(ledger.read_text())["status"] == "complete"


def test_tenth_worker_and_duplicate_custody_are_denied(tmp_path: Path) -> None:
    """No tenth worker or duplicate card, agent, session, claim, or workspace fits."""

    write_genesis(tmp_path / "generation.json")
    tenth = WorkerBinding(
        host="chiap01",
        card_id="card-10",
        agent_id="glm-chiap01-10",
        session_id="session-10",
        claim_id="claim-10",
        workspace="/fleet/chiap01/card-10",
    )
    with pytest.raises(AdmissionDenied, match="exactly nine"):
        run_admission(tmp_path, proposed=bindings() + (tenth,))

    duplicate = list(bindings())
    duplicate[-1] = WorkerBinding(**{**asdict(duplicate[-1]), "claim_id": duplicate[0].claim_id})
    with pytest.raises(AdmissionDenied, match="conflicting worker claim_id"):
        run_admission(tmp_path, proposed=tuple(duplicate))


def test_non_authority_and_active_hold_cause_zero_publication(tmp_path: Path) -> None:
    """Host selectors and an active hold have no candidate dispatch path."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    old_bytes = ledger.read_bytes()
    with pytest.raises(AdmissionDenied, match="only chiap08"):
        admit_wave(
            authority_host="chiap02",
            ledger_path=ledger,
            lock_path=tmp_path / "admission.lock",
            proposed_generation=1,
            bindings=bindings(),
            snapshot_reader=lambda: snapshot(),
            now=NOW,
        )
    with pytest.raises(AdmissionDenied, match="hold is active"):
        run_admission(tmp_path, evidence=snapshot(active_hold=True))
    assert ledger.read_bytes() == old_bytes
