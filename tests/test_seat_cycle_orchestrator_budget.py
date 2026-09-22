"""Generation-budget regressions for the serialized seat orchestrator."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from skcapstone.fleet import seat_cycle_orchestrator


class FakeClock:
    """Provide a manually advanced monotonic clock."""

    def __init__(self) -> None:
        """Start the clock at zero seconds."""

        self.now = 0.0

    def __call__(self) -> float:
        """Return the current monotonic time."""

        return self.now

    def advance(self, seconds: float) -> None:
        """Advance the clock by ``seconds``."""

        self.now += seconds


def _inactive_result() -> SimpleNamespace:
    """Return an exact inactive systemd state result."""

    return SimpleNamespace(
        returncode=0,
        stdout="LoadState=loaded\nActiveState=inactive\nJob=\n",
        stderr="",
    )


def test_successful_final_seat_may_cross_generation_boundary(tmp_path: Path, monkeypatch) -> None:
    """Scheduler overhead after final admission cannot erase three successes."""

    clock = FakeClock()
    durations = iter((190.0, 200.0, 214.0))
    starts: list[str] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        """Model the measured 604-second generation and next automatic Atlas run."""

        if command[2:4] == ["start", "--wait"]:
            starts.append(command[-1])
            clock.advance(next(durations))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[2] == "show" and clock() >= 600:
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=loaded\nActiveState=active\nJob=99\n",
                stderr="",
            )
        return _inactive_result()

    result = seat_cycle_orchestrator.run_generation(tmp_path, runner=runner, clock=clock)

    assert starts == [
        "skfleet-atlas.service",
        "skfleet-seraph.service",
        "skfleet-niobe.service",
    ]
    assert clock() == 604.0
    assert result["aborted"] is False
    assert result["failures"] == 0
    receipt = json.loads(
        (tmp_path / "coordination/seat-cycles/orchestrator.health.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert receipt["aborted"] is False
    assert receipt["failures"] == 0

    monkeypatch.setattr(seat_cycle_orchestrator, "run_generation", lambda _home: result)
    monkeypatch.setattr("sys.argv", ["seat-cycle", "--home", str(tmp_path)])
    assert seat_cycle_orchestrator.main() == 0


def test_exhausted_budget_prevents_starting_another_seat(tmp_path: Path) -> None:
    """A generation at 600 seconds must not admit the next seat."""

    clock = FakeClock()
    starts: list[str] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        """Consume 300 seconds in each admitted seat."""

        if command[2:4] == ["start", "--wait"]:
            starts.append(command[-1])
            clock.advance(300.0)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return _inactive_result()

    result = seat_cycle_orchestrator.run_generation(tmp_path, runner=runner, clock=clock)

    assert starts == ["skfleet-atlas.service", "skfleet-seraph.service"]
    assert result["aborted"] is True
    assert result["recovery"] == "generation_budget_exhausted"


def test_timeout_with_proven_cleanup_remains_failed_closed(tmp_path: Path) -> None:
    """Cleanup proof permits continuation but cannot turn a timeout into success."""

    starts: list[str] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        """Time out Atlas, prove cleanup, and allow later seats to finish."""

        if command[2:4] == ["start", "--wait"]:
            starts.append(command[-1])
            if command[-1] == "skfleet-atlas.service":
                raise subprocess.TimeoutExpired(command, 310)
        return _inactive_result()

    result = seat_cycle_orchestrator.run_generation(tmp_path, runner=runner)

    assert len(starts) == 3
    assert result["aborted"] is True
    assert result["failures"] == 1
    assert result["seats"][0]["returncode"] == 124
    assert result["seats"][0]["timeout_cleanup_proven"] is True


def test_nonzero_result_with_proven_cleanup_remains_failed_closed(tmp_path: Path) -> None:
    """A nonzero seat result cannot produce a successful generation receipt."""

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        """Return one cleanly stopped nonzero result followed by successes."""

        if command[2:4] == ["start", "--wait"]:
            return SimpleNamespace(
                returncode=7 if command[-1] == "skfleet-atlas.service" else 0,
                stdout="",
                stderr="",
            )
        return _inactive_result()

    result = seat_cycle_orchestrator.run_generation(tmp_path, runner=runner)

    assert len(result["seats"]) == 3
    assert result["aborted"] is True
    assert result["failures"] == 1
