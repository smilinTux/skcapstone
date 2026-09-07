"""Deterministic zero-network qualification for the GLM wave candidate."""

from __future__ import annotations

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.fleet import glm_admission
from skcapstone.fleet.glm_admission import (
    AUTHORITY_HOST,
    SCHEMA,
    AdmissionDenied,
    AdmissionSnapshot,
    Hold,
    HostReport,
    QueueSample,
    WorkerBinding,
    _abort_wave,
    _admit_wave,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
HOLD_HASH = "bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05"
HOSTS = ("chiap01", "chiap02", "chiap03")


@pytest.fixture(autouse=True)
def isolated_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a fixed local authority directory and forbid all network access."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    authority = tmp_path
    authority.chmod(0o700)
    monkeypatch.setattr(glm_admission, "AUTHORITY_DIRECTORY", authority)
    monkeypatch.setattr(glm_admission, "LOCK_PATH", authority / "admission.lock")
    monkeypatch.setattr(glm_admission, "LEDGER_PATH", authority / "generation.json")
    monkeypatch.setattr(socket, "gethostname", lambda: "CHIAP08.")
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
    pressure_host: str | None = None,
    http_429: bool = False,
    queued: int = 0,
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
                http_429=http_429 and host == pressure_host,
                queue_samples=(
                    QueueSample(
                        observed_at=timestamp(second_at - timedelta(seconds=5)),
                        active=0,
                        queued=queued if host == pressure_host else 0,
                    ),
                    QueueSample(
                        observed_at=timestamp(second_at),
                        active=0,
                        queued=queued if host == pressure_host else 0,
                    ),
                ),
            )
            for host in HOSTS
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
    path.chmod(0o600)


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
    return _admit_wave(
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


@pytest.mark.parametrize("host", HOSTS)
def test_any_host_429_denies(tmp_path: Path, host: str) -> None:
    """A 429 reported only by any one authoritative host stops admission."""

    write_genesis(tmp_path / "generation.json")
    with pytest.raises(AdmissionDenied, match="reported 429"):
        run_admission(tmp_path, evidence=snapshot(pressure_host=host, http_429=True))


@pytest.mark.parametrize("host", HOSTS)
def test_two_positive_queue_samples_on_any_host_deny(tmp_path: Path, host: str) -> None:
    """Two consecutive positive queue samples on any one host stop admission."""

    write_genesis(tmp_path / "generation.json")
    with pytest.raises(AdmissionDenied, match="positive queue persisted"):
        run_admission(tmp_path, evidence=snapshot(pressure_host=host, queued=1))


def test_malformed_missing_stale_ledgers_deny(tmp_path: Path) -> None:
    """No malformed or untrustworthy ledger can bootstrap a wave."""

    ledger = tmp_path / "generation.json"
    with pytest.raises(AdmissionDenied, match="missing or malformed"):
        run_admission(tmp_path)
    ledger.write_text("{not json\n", encoding="utf-8")
    ledger.chmod(0o600)
    with pytest.raises(AdmissionDenied, match="missing or malformed"):
        run_admission(tmp_path)
    write_genesis(ledger, updated_at=NOW - timedelta(seconds=31))
    with pytest.raises(AdmissionDenied, match="stale ledger"):
        run_admission(tmp_path)


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


def test_abort_exact_live_wave_atomically_releases_reservation(tmp_path: Path) -> None:
    """Rollback changes only the exact reserved live wave to complete."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    run_admission(tmp_path)
    result = _abort_wave(bindings(), NOW)
    assert result["status"] == "complete"
    assert result["workers"] == []
    with pytest.raises(AdmissionDenied, match="does not match live"):
        _abort_wave(bindings(), NOW)


def test_hold_change_during_locked_decision_denies(tmp_path: Path) -> None:
    """A hold generation or hash change during admission denies publication."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    observations = iter((snapshot(), snapshot(hold_generation="hold-8")))
    with pytest.raises(AdmissionDenied, match="hold changed"):
        _admit_wave(
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


def test_non_authority_and_active_hold_cause_zero_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Physical non-authority hosts and an active hold publish nothing."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    old_bytes = ledger.read_bytes()
    monkeypatch.setattr(socket, "gethostname", lambda: "chiap03")
    with pytest.raises(AdmissionDenied, match="physical host is not chiap08"):
        run_admission(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "chiap08")
    with pytest.raises(AdmissionDenied, match="hold is active"):
        run_admission(tmp_path, evidence=snapshot(active_hold=True))
    assert ledger.read_bytes() == old_bytes


def test_caller_authority_and_path_overrides_are_impossible(tmp_path: Path) -> None:
    """The public admission interface accepts no authority or path overrides."""

    write_genesis(tmp_path / "generation.json")
    required = {
        "bindings": bindings(),
        "snapshot_reader": lambda: snapshot(),
        "now": NOW,
    }
    for name, value in (
        ("coordinator", object()),
        ("backend", "zai"),
        ("generation", 1),
        ("authority_host", "chiap08"),
        ("ledger_path", tmp_path / "alternate.json"),
        ("lock_path", tmp_path / "alternate.lock"),
    ):
        with pytest.raises(TypeError, match=name):
            _admit_wave(**required, **{name: value})


@pytest.mark.parametrize("name", ["generation.json", "admission.lock"])
def test_symlink_authority_files_fail_closed(tmp_path: Path, name: str) -> None:
    """Neither fixed authority file may be a symlink."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    target = tmp_path / "target"
    target.write_text("not authority state", encoding="utf-8")
    path = tmp_path / name
    if path.exists():
        path.unlink()
    path.symlink_to(target)
    with pytest.raises(AdmissionDenied, match="unsafe|missing or malformed"):
        run_admission(tmp_path)


@pytest.mark.parametrize("name", ["generation.json", "admission.lock"])
def test_non_regular_authority_files_fail_closed(tmp_path: Path, name: str) -> None:
    """Neither fixed authority file may be a directory or special file."""

    ledger = tmp_path / "generation.json"
    write_genesis(ledger)
    path = tmp_path / name
    if path.exists():
        path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(AdmissionDenied, match="unsafe|missing or malformed"):
        run_admission(tmp_path)
