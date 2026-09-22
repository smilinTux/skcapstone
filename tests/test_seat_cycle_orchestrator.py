"""Contract tests for the single lifecycle-seat generation orchestrator."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import seat_cycle_orchestrator
from skcapstone.fleet.seat_cycle_orchestrator import run_generation, select_niobe_service


def test_generation_runs_exact_order_and_continues_after_failure(tmp_path, monkeypatch) -> None:
    """One failed seat cannot suppress later seats in the same generation."""

    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.select_niobe_service",
        lambda _home: "skfleet-niobe-live.service",
    )
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=inactive\nJob=\nMainPID=0\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=(
                1
                if command[2:4] == ["start", "--wait"] and command[-1] == "skfleet-atlas.service"
                else 0
            ),
            stdout="",
            stderr="",
        )

    result = run_generation(tmp_path, runner=runner)

    start_calls = [command for command in calls if command[2:4] == ["start", "--wait"]]
    assert [command[-1] for command in start_calls] == [
        "skfleet-atlas.service",
        "skfleet-seraph.service",
        "skfleet-niobe-live.service",
    ]
    assert result["failures"] == 1
    receipt = json.loads(
        (tmp_path / "coordination/seat-cycles/orchestrator.health.jsonl")
        .read_text()
        .splitlines()[-1]
    )
    assert [seat["unit"] for seat in receipt["seats"]] == [command[-1] for command in start_calls]
    assert [seat["returncode"] for seat in receipt["seats"]] == [1, 0, 0]


@pytest.mark.parametrize("state", ["active", "deactivating"])
def test_nonzero_start_aborts_when_service_cannot_be_proven_inactive(tmp_path, monkeypatch, state):
    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.select_niobe_service",
        lambda _home: "skfleet-niobe.service",
    )
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[2:4] == ["start", "--wait"]:
            return SimpleNamespace(returncode=7, stdout="", stderr="failed")
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout=f"LoadState=loaded\nActiveState={state}\nJob=\nMainPID=0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_generation(tmp_path, runner=runner)
    assert result["aborted"] is True
    assert [call for call in calls if call[2:4] == ["start", "--wait"]] == [
        ["systemctl", "--user", "start", "--wait", "skfleet-atlas.service"]
    ]


def test_timeout_stops_and_proves_seat_inactive_before_continuing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.select_niobe_service",
        lambda _home: "skfleet-niobe.service",
    )
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[2:4] == ["start", "--wait"] and command[-1] == "skfleet-atlas.service":
            raise subprocess.TimeoutExpired(command, 310)
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=inactive\nJob=\nMainPID=0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_generation(tmp_path, runner=runner)

    verbs = [(call[2], call[-1]) for call in calls]
    assert verbs[:3] == [
        ("start", "skfleet-atlas.service"),
        ("stop", "skfleet-atlas.service"),
        ("show", "--property=LoadState,ActiveState,Job"),
    ]
    assert ("start", "skfleet-seraph.service") in verbs
    assert result["aborted"] is False


def test_timeout_aborts_generation_when_inactive_state_cannot_be_proven(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.select_niobe_service",
        lambda _home: "skfleet-niobe.service",
    )
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[2:4] == ["start", "--wait"]:
            raise subprocess.TimeoutExpired(command, 310)
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=deactivating\nJob=\nMainPID=0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_generation(tmp_path, runner=runner)

    assert result["aborted"] is True
    assert [call for call in calls if call[2:4] == ["start", "--wait"]] == [
        ["systemctl", "--user", "start", "--wait", "skfleet-atlas.service"]
    ]


def test_timeout_cleanup_rejects_inactive_service_with_pending_job(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.select_niobe_service",
        lambda _home: "skfleet-niobe.service",
    )
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[2:4] == ["start", "--wait"]:
            raise subprocess.TimeoutExpired(command, 310)
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=inactive\nJob=99\nMainPID=0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_generation(tmp_path, runner=runner)
    assert result["aborted"] is True
    assert [call for call in calls if call[2:4] == ["start", "--wait"]] == [
        ["systemctl", "--user", "start", "--wait", "skfleet-atlas.service"]
    ]


def test_generation_admission_deadline_remains_600_seconds(tmp_path, monkeypatch) -> None:
    """A full generation never admits another seat at or after 600 seconds."""

    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.select_niobe_service",
        lambda _home: "skfleet-niobe.service",
    )
    now = 0.0
    starts: list[str] = []

    def clock() -> float:
        return now

    def runner(command, **_kwargs):
        nonlocal now
        if command[2:4] == ["start", "--wait"]:
            starts.append(command[-1])
            now += 300.0
        return SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=inactive\nJob=\n",
            stderr="",
        )

    result = run_generation(tmp_path, runner=runner, clock=clock)

    assert seat_cycle_orchestrator._GENERATION_ADMISSION_SECONDS == 600
    assert starts == ["skfleet-atlas.service", "skfleet-seraph.service"]
    assert result["aborted"] is True
    assert result["recovery"] == "generation_budget_exhausted"
    root = Path(__file__).parents[1]
    assert "TimeoutStartSec=960" in (root / "systemd/skfleet-seat-cycle.service").read_text()


def test_next_generation_stays_blocked_on_lingering_middle_seat(tmp_path, monkeypatch):
    prior = {
        "schema": "skfleet.seat-cycle-generation/v1",
        "aborted": True,
        "seats": [{"unit": "skfleet-seraph.service", "returncode": 124}],
    }
    path = tmp_path / "coordination/seat-cycles/orchestrator.health.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        unit = command[3]
        state = "active" if unit == "skfleet-seraph.service" else "inactive"
        return SimpleNamespace(
            returncode=0,
            stdout=f"LoadState=loaded\nActiveState={state}\nJob=\nMainPID=0\n",
            stderr="",
        )

    result = run_generation(tmp_path, runner=runner)

    assert result["aborted"] is True
    assert result["recovery"] == "governed_service_inactivity_unproven"
    assert not any(call[2:4] == ["start", "--wait"] for call in calls)


def test_malformed_recovery_receipt_fails_closed_before_any_seat_start(tmp_path):
    path = tmp_path / "coordination/seat-cycles/orchestrator.health.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nJob=\nMainPID=0\n",
            stderr="",
        )

    result = run_generation(tmp_path, runner=runner)
    assert result["aborted"] is True
    assert not any(call[2:4] == ["start", "--wait"] for call in calls)


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"schema": "wrong", "aborted": False, "failures": 0, "seats": []},
        {
            "schema": "skfleet.seat-cycle-generation/v1",
            "aborted": "false",
            "failures": 0,
            "seats": [],
        },
        {"schema": "skfleet.seat-cycle-generation/v1", "failures": 0, "seats": []},
        {
            "schema": "skfleet.seat-cycle-generation/v1",
            "aborted": False,
            "failures": 0,
            "seats": [{}],
        },
        {
            "schema": "skfleet.seat-cycle-generation/v1",
            "started_at": "2026-09-15T00:00:00+00:00",
            "finished_at": "2026-09-15T00:00:01+00:00",
            "aborted": False,
            "failures": 0,
            "seats": [],
        },
        {
            "schema": "skfleet.seat-cycle-generation/v1",
            "started_at": "2026-09-15T00:00:00+00:00",
            "finished_at": "2026-09-15T00:00:01+00:00",
            "aborted": False,
            "failures": 0,
            "seats": [
                {
                    "unit": "arbitrary.service",
                    "returncode": 0,
                    "error": None,
                    "timeout_cleanup_proven": None,
                }
            ],
        },
    ],
)
def test_untrusted_receipt_shapes_require_recovery_proof(tmp_path, receipt):
    path = tmp_path / "coordination/seat-cycles/orchestrator.health.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nJob=\nMainPID=0\n",
            stderr="",
        )

    result = run_generation(tmp_path, runner=runner)
    assert result["aborted"] is True
    assert not any(call[2:4] == ["start", "--wait"] for call in calls)


@pytest.mark.parametrize(
    "mutation",
    [
        {"started_at": "2026-09-15T00:00:02+00:00", "finished_at": "2026-09-15T00:00:01+00:00"},
        {"started_at": "2026-09-15T00:00:00", "finished_at": "2026-09-15T00:00:01"},
        {"seat": 0, "returncode": 7, "error": None, "timeout_cleanup_proven": False},
        {"seat": 0, "returncode": 0, "error": "unexpected", "timeout_cleanup_proven": True},
    ],
)
def test_contradictory_success_receipts_require_recovery_proof(tmp_path, mutation):
    units = ["skfleet-atlas.service", "skfleet-seraph.service", "skfleet-niobe.service"]
    receipt = {
        "schema": "skfleet.seat-cycle-generation/v1",
        "started_at": "2026-09-15T00:00:00+00:00",
        "finished_at": "2026-09-15T00:00:01+00:00",
        "aborted": False,
        "failures": 0,
        "seats": [
            {
                "unit": unit,
                "returncode": 0,
                "error": None,
                "timeout_cleanup_proven": None,
            }
            for unit in units
        ],
    }
    seat = mutation.get("seat")
    if seat is None:
        receipt.update(mutation)
    else:
        changes = {key: value for key, value in mutation.items() if key != "seat"}
        receipt["seats"][seat].update(changes)
        receipt["failures"] = sum(row["returncode"] != 0 for row in receipt["seats"])
    path = tmp_path / "coordination/seat-cycles/orchestrator.health.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nJob=\nMainPID=0\n",
            stderr="",
        )

    result = run_generation(tmp_path, runner=runner)
    assert result["aborted"] is True
    assert not any(call[2:4] == ["start", "--wait"] for call in calls)


def test_receipt_failures_boolean_is_not_accepted_as_integer(tmp_path):
    receipt = {
        "schema": "skfleet.seat-cycle-generation/v1",
        "started_at": "2026-09-15T00:00:00+00:00",
        "finished_at": "2026-09-15T00:00:01+00:00",
        "aborted": False,
        "failures": True,
        "seats": [
            {
                "unit": "skfleet-atlas.service",
                "returncode": 7,
                "error": "systemctl_start_failed",
                "timeout_cleanup_proven": True,
            },
            {
                "unit": "skfleet-seraph.service",
                "returncode": 0,
                "error": None,
                "timeout_cleanup_proven": None,
            },
            {
                "unit": "skfleet-niobe.service",
                "returncode": 0,
                "error": None,
                "timeout_cleanup_proven": None,
            },
        ],
    }
    path = tmp_path / "coordination/seat-cycles/orchestrator.health.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    assert seat_cycle_orchestrator._recovery_required(tmp_path) is True


def test_receipt_fsync_failure_leaves_durable_fence_for_next_cycle(tmp_path, monkeypatch):
    original_append = seat_cycle_orchestrator._append_receipt

    def healthy_runner(command, **_kwargs):
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=inactive\nJob=\nMainPID=0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        seat_cycle_orchestrator,
        "_append_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        run_generation(tmp_path, runner=healthy_runner)
    marker = tmp_path / "coordination/seat-cycles/recovery-required"
    assert marker.is_file()

    monkeypatch.setattr(seat_cycle_orchestrator, "_append_receipt", original_append)
    calls = []

    def lingering_runner(command, **_kwargs):
        calls.append(command)
        unit = command[3]
        state = "active" if unit == "skfleet-seraph.service" else "inactive"
        return SimpleNamespace(
            returncode=0,
            stdout=f"LoadState=loaded\nActiveState={state}\nJob=\nMainPID=0\n",
            stderr="",
        )

    result = run_generation(tmp_path, runner=lingering_runner)
    assert result["aborted"] is True
    assert marker.is_file()
    assert not any(call[2:4] == ["start", "--wait"] for call in calls)


def test_concurrent_process_cannot_enter_or_clear_owner_fence(tmp_path):
    ctx = multiprocessing.get_context("fork")
    entered = ctx.Event()
    release = ctx.Event()
    owner_result = ctx.Queue()
    contender_result = ctx.Queue()

    def owner():
        def runner(command, **_kwargs):
            if command[2:4] == ["start", "--wait"] and command[-1] == "skfleet-atlas.service":
                entered.set()
                assert release.wait(10)
            if command[2] == "show":
                return SimpleNamespace(
                    returncode=0,
                    stdout="LoadState=loaded\nActiveState=inactive\nJob=\nMainPID=0\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        owner_result.put(run_generation(tmp_path, runner=runner))

    def contender():
        def runner(_command, **_kwargs):
            raise AssertionError("contender must not invoke systemd")

        contender_result.put(run_generation(tmp_path, runner=runner))

    owner_process = ctx.Process(target=owner)
    owner_process.start()
    assert entered.wait(10)
    marker = tmp_path / "coordination/seat-cycles/recovery-required"
    assert marker.is_file()

    contender_process = ctx.Process(target=contender)
    contender_process.start()
    contender_process.join(10)
    assert contender_process.exitcode == 0
    contender_receipt = contender_result.get(timeout=2)
    assert contender_receipt["aborted"] is True
    assert contender_receipt["recovery"] == "generation_lock_contended"
    assert marker.is_file(), "contender must not clear the owner recovery fence"

    release.set()
    owner_process.join(10)
    assert owner_process.exitcode == 0
    assert owner_result.get(timeout=2)["aborted"] is False
    assert not marker.exists()


def test_dead_lock_owner_releases_flock_but_leaves_recovery_fence(tmp_path):
    ctx = multiprocessing.get_context("fork")
    entered = ctx.Event()

    def crashing_owner():
        def runner(command, **_kwargs):
            if command[2:4] == ["start", "--wait"]:
                entered.set()
                os._exit(17)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        run_generation(tmp_path, runner=runner)

    process = ctx.Process(target=crashing_owner)
    process.start()
    assert entered.wait(10)
    process.join(10)
    assert process.exitcode == 17
    marker = tmp_path / "coordination/seat-cycles/recovery-required"
    assert marker.is_file()

    def recovered_runner(command, **_kwargs):
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=inactive\nJob=\nMainPID=0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_generation(tmp_path, runner=recovered_runner)
    assert result["aborted"] is False
    assert not marker.exists()


def test_recovery_cancels_queued_jobs_before_starting_new_generation(tmp_path):
    marker = tmp_path / "coordination/seat-cycles/recovery-required"
    marker.parent.mkdir(parents=True)
    marker.write_text("recovery-required\n", encoding="utf-8")
    jobs = {unit: "99" for unit in seat_cycle_orchestrator._GOVERNED_SERVICES}
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        unit = command[3]
        if command[2] == "stop":
            jobs[unit] = ""
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[2] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout=(f"LoadState=loaded\nActiveState=inactive\nJob={jobs[unit]}\nMainPID=0\n"),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_generation(tmp_path, runner=runner)
    assert result["aborted"] is False
    first_start = next(i for i, call in enumerate(calls) if call[2:4] == ["start", "--wait"])
    for unit in seat_cycle_orchestrator._GOVERNED_SERVICES:
        assert (
            next(i for i, call in enumerate(calls) if call[2] == "stop" and call[-1] == unit)
            < first_start
        )
    assert all(job == "" for job in jobs.values())


def test_main_returns_nonzero_for_aborted_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        seat_cycle_orchestrator,
        "run_generation",
        lambda _home: {"aborted": True},
    )
    monkeypatch.setattr("sys.argv", ["seat-cycle", "--home", str(tmp_path)])
    assert seat_cycle_orchestrator.main() == 1


def test_main_resolves_configured_sovereign_home(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "relocated"))
    monkeypatch.setattr(
        seat_cycle_orchestrator,
        "run_generation",
        lambda home: captured.append(home) or {"aborted": False},
    )
    monkeypatch.setattr("sys.argv", ["seat-cycle"])
    assert seat_cycle_orchestrator.main() == 0
    assert captured == [tmp_path / "relocated"]


def test_niobe_activation_selects_live_or_shadow(tmp_path, monkeypatch) -> None:
    """Only a valid existing activation selects the live Niobe service."""

    activation = tmp_path / "coordination/niobe-activation.json"
    activation.parent.mkdir(parents=True)
    activation.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.parse_activation",
        lambda *_args, **_kwargs: object(),
    )
    assert select_niobe_service(tmp_path) == "skfleet-niobe-live.service"

    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.parse_activation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("expired")),
    )
    assert select_niobe_service(tmp_path) == "skfleet-niobe.service"


def test_non_object_niobe_activation_roots_fail_closed_to_shadow(tmp_path, monkeypatch) -> None:
    """Valid JSON with the wrong root type cannot abort the whole generation."""

    activation = tmp_path / "coordination/niobe-activation.json"
    activation.parent.mkdir(parents=True)

    def parser(value, **_kwargs):
        assert isinstance(value, Mapping)

    monkeypatch.setattr("skcapstone.fleet.seat_cycle_orchestrator.parse_activation", parser)
    for value in ([], None, 1, "active"):
        activation.write_text(json.dumps(value), encoding="utf-8")
        assert select_niobe_service(tmp_path) == "skfleet-niobe.service"


def test_structurally_malformed_object_activation_fails_closed_to_shadow(
    tmp_path, monkeypatch
) -> None:
    """An object rejected with TypeError cannot abort Atlas-first execution."""

    activation = tmp_path / "coordination/niobe-activation.json"
    activation.parent.mkdir(parents=True)
    activation.write_text(json.dumps({"product_scope": 1}), encoding="utf-8")
    monkeypatch.setattr(
        "skcapstone.fleet.seat_cycle_orchestrator.parse_activation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("not iterable")),
    )

    assert select_niobe_service(tmp_path) == "skfleet-niobe.service"


def test_orchestrator_units_are_packaged_and_prevent_overlapping_generations() -> None:
    """Only the orchestrator recurs, five minutes after a generation ends."""

    root = Path(__file__).parents[1]
    service = (root / "systemd/skfleet-seat-cycle.service").read_text()
    timer = (root / "systemd/skfleet-seat-cycle.timer").read_text()
    assert "TimeoutStartSec=960" in service
    assert "--home" not in service
    assert "OnUnitInactiveSec=5min" in timer
    assert "OnUnitActiveSec" not in timer and "OnCalendar" not in timer
    for suffix in ("service", "timer"):
        assert (root / f"systemd/skfleet-seat-cycle.{suffix}").read_bytes() == (
            root / f"src/skcapstone/data/systemd/skfleet-seat-cycle.{suffix}"
        ).read_bytes()


def test_control_profile_requires_only_orchestrator_for_serialized_seats() -> None:
    """Competing Seraph and Niobe timers cannot be enabled by profile convergence."""

    source = (Path(__file__).parents[1] / "scripts/fleet/gen-profile-manifests.py").read_text()
    assert '"skfleet-seat-cycle.timer"' in source
    for timer in (
        "skfleet-seraph.timer",
        "skfleet-niobe.timer",
        "skfleet-niobe-live.timer",
    ):
        assert f'"{timer}"' in source
    assert "SERIALIZED_SEAT_MUST_NOT" in source
