from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
from multiprocessing.synchronize import Event
from pathlib import Path
from uuid import UUID

import pytest

from skcapstone.seat_cycle_guard import SeatCycleGuard


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _holder(
    state: str,
    ready: Event,
    release: Event,
) -> None:
    guard = SeatCycleGuard(Path(state), "link")

    def operation(_cycle_id: str) -> None:
        ready.set()
        release.wait(10)

    guard.run(operation)


def _crasher(state: str) -> None:
    guard = SeatCycleGuard(Path(state), "mero")
    guard.run(lambda _cycle_id: os._exit(17))


def test_overlap_is_nonblocking_noop_and_does_not_run_operation(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_holder, args=(str(tmp_path), ready, release))
    process.start()
    assert ready.wait(5)

    calls: list[str] = []
    result = SeatCycleGuard(tmp_path, "link").run(lambda cycle_id: calls.append(cycle_id))
    assert result.ran is False
    assert calls == []
    assert result.live_cycle_id

    release.set()
    process.join(5)
    assert process.exitcode == 0
    events = _events(tmp_path / "link.cycle.receipts.jsonl")
    assert [event["event"] for event in events] == [
        "start",
        "overlap_noop",
        "finish",
    ]
    assert events[1]["live_cycle_id"] == events[0]["cycle_id"]
    assert events[1]["owner_evidence_verified"] is True


def test_crash_is_marked_abandoned_only_by_later_exact_generation_check(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    crashed = ctx.Process(target=_crasher, args=(str(tmp_path),))
    crashed.start()
    crashed.join(5)
    assert crashed.exitcode == 17

    result = SeatCycleGuard(tmp_path, "mero").run(lambda _cycle_id: "census")
    assert result.ran and result.value == "census"
    events = _events(tmp_path / "mero.cycle.receipts.jsonl")
    assert [event["event"] for event in events] == [
        "start",
        "abandoned",
        "start",
        "finish",
    ]
    assert events[1]["cycle_id"] == events[0]["cycle_id"]
    assert events[1]["reason"] == "exact_process_generation_not_live"


def test_stale_receipt_with_reused_pid_is_rejected_by_generation(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-a\n", encoding="ascii")
    (proc / str(os.getpid())).mkdir()
    # fields 3 through 22 after comm: put process start ticks at index 19.
    tail = ["S"] + ["0"] * 18 + ["222"]
    (proc / str(os.getpid()) / "stat").write_text(
        f"{os.getpid()} (python worker) {' '.join(tail)}\n", encoding="utf-8"
    )
    old = {
        "event": "start",
        "at": "2000-01-01T00:00:00+00:00",
        "cycle_id": "old",
        "seat": "link",
        "process_generation": {
            "pid": os.getpid(),
            "boot_id": "boot-a",
            "start_ticks": 111,
        },
    }
    receipts = tmp_path / "link.cycle.receipts.jsonl"
    receipts.write_text(json.dumps(old) + "\n", encoding="utf-8")

    guard = SeatCycleGuard(
        tmp_path,
        "link",
        proc_root=proc,
        id_factory=lambda: UUID("00000000-0000-0000-0000-000000000001"),
    )
    assert guard.run(lambda _cycle_id: "assigned").ran
    events = _events(receipts)
    assert events[1]["event"] == "abandoned"
    assert events[1]["cycle_id"] == "old"


def test_healthy_long_inference_is_not_stale_despite_old_receipt(tmp_path: Path) -> None:
    guard = SeatCycleGuard(tmp_path, "mero", clock=lambda: "1900-01-01T00:00:00+00:00")
    generation = guard.proc_root / str(os.getpid()) / "stat"
    assert generation.exists()

    with guard.lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        process = guard._generation_is_live
        current = guard._read_lock_owner(lock_file)
        assert current == {}
        from skcapstone.seat_cycle_guard import ProcessGeneration

        gen = ProcessGeneration.current()
        owner = {
            "cycle_id": "long-inference",
            "seat": "mero",
            "process_generation": {
                "pid": gen.pid,
                "boot_id": gen.boot_id,
                "start_ticks": gen.start_ticks,
            },
        }
        guard._write_lock_owner(lock_file, owner)
        guard._append_receipt({"event": "start", "at": guard.clock(), **owner})

        calls: list[str] = []
        result = SeatCycleGuard(tmp_path, "mero").run(lambda cycle_id: calls.append(cycle_id))
        assert not result.ran
        assert calls == []
        assert result.live_cycle_id == "long-inference"
        assert process(owner["process_generation"])
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    events = _events(tmp_path / "mero.cycle.receipts.jsonl")
    assert [event["event"] for event in events] == ["start", "overlap_noop"]
    assert not any(event["event"] == "abandoned" for event in events)


def test_receipts_are_append_only_and_every_existing_line_is_parsed(tmp_path: Path) -> None:
    guard = SeatCycleGuard(tmp_path, "link")
    guard.run(lambda _cycle_id: None)
    with guard.receipt_path.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")

    with pytest.raises(json.JSONDecodeError):
        guard.run(
            lambda _cycle_id: pytest.fail("must not run after malformed CardStore-style receipt")
        )


def test_seats_have_independent_locks(tmp_path: Path) -> None:
    link = SeatCycleGuard(tmp_path, "link")
    mero = SeatCycleGuard(tmp_path, "mero")
    assert link.lock_path != mero.lock_path
    assert link.run(lambda _cycle_id: "link").value == "link"
    assert mero.run(lambda _cycle_id: "mero").value == "mero"
