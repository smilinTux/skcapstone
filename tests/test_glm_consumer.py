"""Zero-provider tests for the fixed authoritative GLM consumer."""

from __future__ import annotations

import inspect
import socket
from pathlib import Path

import pytest

from skcapstone.fleet import glm_consumer
from skcapstone.fleet.glm_admission import AdmissionDenied, HostReport, QueueSample


def test_public_entrypoints_accept_no_injection_parameters() -> None:
    """The executable surface has no coordinator, backend, generation, or paths."""

    assert list(inspect.signature(glm_consumer.main).parameters) == []
    assert list(inspect.signature(glm_consumer.run_consumer).parameters) == []
    source = inspect.getsource(glm_consumer.run_consumer)
    for forbidden in (
        "coordinator",
        "backend",
        "generation",
        "authority",
        "lock_path",
        "ledger_path",
        "state_path",
    ):
        assert forbidden not in inspect.signature(glm_consumer.run_consumer).parameters
    assert "ENABLED_MARKER" in source


def test_fixed_workers_are_nine_distinct_and_three_per_host() -> None:
    """Fixed cards bind to unique identities, sessions, claims, and worktrees."""

    workers = glm_consumer._workers()
    assert len(workers) == 9
    host_counts = {
        host: sum(worker.host == host for worker in workers) for host in glm_consumer.WORKER_HOSTS
    }
    assert host_counts == {"chiap01": 3, "chiap02": 3, "chiap03": 3}
    for field in ("card_id", "agent_id", "session_id", "claim_id", "worktree"):
        assert len({getattr(worker, field) for worker in workers}) == 9


def test_wrong_host_and_disabled_marker_deny_before_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only physical chiap08 with the fixed marker can execute."""

    monkeypatch.setattr(socket, "gethostname", lambda: "chiap02")
    monkeypatch.setattr(glm_consumer, "_run", lambda *args, **kwargs: pytest.fail("command ran"))
    with pytest.raises(AdmissionDenied, match="physical host"):
        glm_consumer.run_consumer()
    monkeypatch.setattr(socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(glm_consumer, "ENABLED_MARKER", tmp_path / "absent")
    with pytest.raises(AdmissionDenied, match="disabled"):
        glm_consumer.run_consumer()


def test_snapshot_reader_folds_all_three_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot collection contacts every fixed host exactly once."""

    seen: list[str] = []
    monkeypatch.setattr(
        glm_consumer,
        "_json_command",
        lambda command: {"generation": "h", "sha256": "a" * 64, "active": True},
    )

    def host_snapshot(host: str) -> HostReport:
        seen.append(host)
        sample = QueueSample("2026-08-28T12:00:00Z", 0, 0)
        return HostReport(host, True, 0, "2026-08-28T12:00:00Z", False, (sample, sample))

    monkeypatch.setattr(glm_consumer, "_host_snapshot", host_snapshot)
    snapshot = glm_consumer._read_snapshot()
    assert seen == ["chiap01", "chiap02", "chiap03"]
    assert tuple(report.host for report in snapshot.hosts) == tuple(glm_consumer.WORKER_HOSTS)


def test_prepare_barrier_precedes_reservation_and_worker_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All nine idle sessions exist before reservation and Pi release."""

    marker = tmp_path / "enabled"
    marker.write_text("disabled test fixture only\n", encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(glm_consumer, "ENABLED_MARKER", marker)
    monkeypatch.setattr(glm_consumer, "_require_dependency_clear", lambda workers: None)
    events: list[str] = []
    monkeypatch.setattr(
        glm_consumer, "_claim", lambda worker: events.append(f"claim:{worker.card_id}")
    )
    monkeypatch.setattr(
        glm_consumer, "_prepare", lambda worker: events.append(f"prepare:{worker.card_id}")
    )
    monkeypatch.setattr(glm_consumer, "_read_snapshot", lambda: object())

    def reserve(**kwargs: object) -> None:
        assert len([event for event in events if event.startswith("prepare:")]) == 9
        events.append("reserve")

    def release(worker: glm_consumer._Worker) -> None:
        assert "reserve" in events
        events.append(f"release:{worker.card_id}")

    monkeypatch.setattr(glm_consumer, "_admit_wave", reserve)
    monkeypatch.setattr(glm_consumer, "_release_worker", release)
    assert glm_consumer.run_consumer() == 0
    assert [event.split(":", 1)[0] for event in events] == [
        *("claim" for _ in range(9)),
        *("prepare" for _ in range(9)),
        "reserve",
        *("release" for _ in range(9)),
    ]


def test_prepare_failure_stops_attempted_sessions_and_releases_all_claims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A prepare failure starts no Pi work and compensates every acquired claim."""

    marker = tmp_path / "enabled"
    marker.write_text("disabled test fixture only\n", encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(glm_consumer, "ENABLED_MARKER", marker)
    monkeypatch.setattr(glm_consumer, "_require_dependency_clear", lambda workers: None)
    claimed: list[str] = []
    prepared: list[str] = []
    stopped: list[str] = []
    released: list[str] = []
    monkeypatch.setattr(glm_consumer, "_claim", lambda worker: claimed.append(worker.card_id))

    def prepare(worker: glm_consumer._Worker) -> None:
        prepared.append(worker.card_id)
        if len(prepared) == 3:
            raise AdmissionDenied("synthetic prepare failure")

    monkeypatch.setattr(glm_consumer, "_prepare", prepare)
    monkeypatch.setattr(glm_consumer, "_stop", lambda worker: stopped.append(worker.card_id))
    monkeypatch.setattr(glm_consumer, "_release", lambda worker: released.append(worker.card_id))
    monkeypatch.setattr(
        glm_consumer, "_admit_wave", lambda **kwargs: pytest.fail("reservation attempted")
    )
    monkeypatch.setattr(
        glm_consumer, "_release_worker", lambda worker: pytest.fail("Pi release attempted")
    )
    with pytest.raises(AdmissionDenied, match="synthetic prepare"):
        glm_consumer.run_consumer()
    assert len(claimed) == 9
    assert stopped == list(reversed(prepared))
    assert released == list(reversed(claimed))


def test_rollback_attempts_every_compensator_and_retains_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stop, abort, and release failures do not prevent later compensation."""

    marker = tmp_path / "enabled"
    marker.write_text("disabled test fixture only\n", encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(glm_consumer, "ENABLED_MARKER", marker)
    monkeypatch.setattr(glm_consumer, "_require_dependency_clear", lambda workers: None)
    claimed: list[str] = []
    prepared: list[str] = []
    activated: list[str] = []
    stopped: list[str] = []
    claim_releases: list[str] = []
    aborts: list[int] = []
    monkeypatch.setattr(glm_consumer, "_claim", lambda worker: claimed.append(worker.card_id))
    monkeypatch.setattr(glm_consumer, "_prepare", lambda worker: prepared.append(worker.card_id))
    monkeypatch.setattr(glm_consumer, "_read_snapshot", lambda: object())
    monkeypatch.setattr(glm_consumer, "_admit_wave", lambda **kwargs: None)

    def activate(worker: glm_consumer._Worker) -> None:
        activated.append(worker.card_id)
        if len(activated) == 3:
            raise AdmissionDenied("synthetic release failure")

    def stop(worker: glm_consumer._Worker) -> None:
        stopped.append(worker.card_id)
        if worker.card_id == prepared[-1]:
            raise AdmissionDenied("synthetic stop failure")

    def abort(bindings: object, now: object) -> None:
        aborts.append(len(bindings))  # type: ignore[arg-type]
        raise AdmissionDenied("synthetic abort failure")

    def release(worker: glm_consumer._Worker) -> None:
        claim_releases.append(worker.card_id)
        if worker.card_id == claimed[-1]:
            raise AdmissionDenied("synthetic claim release failure")

    monkeypatch.setattr(glm_consumer, "_release_worker", activate)
    monkeypatch.setattr(glm_consumer, "_stop", stop)
    monkeypatch.setattr(glm_consumer, "_abort_wave", abort)
    monkeypatch.setattr(glm_consumer, "_release", release)
    with pytest.raises(glm_consumer._RollbackFailed) as raised:
        glm_consumer.run_consumer()
    assert raised.value.__cause__.args == ("synthetic release failure",)
    failure_records = [
        (failure.action, failure.card_id, failure.error_type) for failure in raised.value.failures
    ]
    assert failure_records == [
        ("stop", prepared[-1], "AdmissionDenied"),
        ("abort", None, "AdmissionDenied"),
        ("release", claimed[-1], "AdmissionDenied"),
    ]
    assert stopped == list(reversed(prepared))
    assert aborts == [9]
    assert claim_releases == list(reversed(claimed))


def test_prepare_and_release_commands_are_separate_and_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepare is provider-free and release carries the fixed Pi command."""

    commands: list[tuple[str, ...]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        glm_consumer,
        "_run",
        lambda command, **kwargs: commands.append(tuple(command)) or Result(),
    )
    worker = glm_consumer._workers()[0]
    glm_consumer._prepare(worker)
    glm_consumer._release_worker(worker)
    assert commands[0][:3] == ("ssh", "-oBatchMode=yes", "chiap01")
    assert commands[0][3:] == (
        "tmux",
        "new-session",
        "-d",
        "-s",
        "glm-b75f0cd8",
        "-c",
        "/var/tmp/skcapstone-glm-b75f0cd8",
    )
    assert "pi" not in commands[0]
    assert commands[1][:3] == ("ssh", "-oBatchMode=yes", "chiap01")
    assert "pi" in commands[1][3]
    assert "zai/glm-4.6" in commands[1][3]


@pytest.mark.parametrize(
    ("function_name", "message"),
    [
        ("_prepare", "session prepare failed"),
        ("_release_worker", "session release failed"),
        ("_stop", "session stop failed"),
        ("_release", "claim release failed"),
    ],
)
def test_worker_commands_reject_nonzero_results(
    monkeypatch: pytest.MonkeyPatch, function_name: str, message: str
) -> None:
    """Every worker command converts a nonzero result into a denial."""

    class Result:
        returncode = 1

    monkeypatch.setattr(glm_consumer, "_run", lambda command, **kwargs: Result())
    with pytest.raises(AdmissionDenied, match=message):
        getattr(glm_consumer, function_name)(glm_consumer._workers()[0])
