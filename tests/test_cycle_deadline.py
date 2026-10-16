"""CycleDeadline: soft phase deadlines, resume cursors, budget warnings."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.fleet.cycle_deadline import CycleDeadline


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(tmp_path: Path, clock: Clock, lines: list[str], **kwargs) -> CycleDeadline:
    return CycleDeadline(
        kwargs.pop("budget_s", 100.0),
        clock=clock,
        emit=lines.append,
        host="h",
        state_dir=str(tmp_path),
        hard_timeout_s=kwargs.pop("hard_timeout_s", 120.0),
        final_reserve_s=kwargs.pop("final_reserve_s", 20.0),
        fractions=kwargs.pop("fractions", {"p": 0.5, "q": 1.0}),
        **kwargs,
    )


def test_budget_never_reaches_into_final_reserve(tmp_path: Path) -> None:
    clock = Clock()
    deadline = make(tmp_path, clock, [], budget_s=500.0, hard_timeout_s=270, final_reserve_s=20)
    assert deadline.budget_s == 250
    clock.now += 100
    assert deadline.remaining() == 150
    assert deadline.hard_remaining() == 170


def test_stop_is_false_with_time_and_logs_once_when_past_phase_share(tmp_path: Path) -> None:
    clock, lines = Clock(), []
    deadline = make(tmp_path, clock, lines)
    assert deadline.stop("p", 3, 7) is False and lines == []
    clock.now += 50
    assert deadline.stop("p", 3, 7) is True
    assert deadline.stop("p", 4, 6) is True
    assert len(lines) == 1
    assert lines[0].startswith("PHASE_DEADLINE|h|p|done=3|remaining=7|elapsed_s=50.0|limit_s=50.0")
    # A later phase keeps its own, larger share.
    assert deadline.stop("q", 0, 1) is False


def test_min_done_guarantees_progress(tmp_path: Path) -> None:
    clock, lines = Clock(), []
    deadline = make(tmp_path, clock, lines)
    clock.now += 90
    assert deadline.stop("p", 0, 5, min_done=2) is False
    assert deadline.stop("p", 1, 4, min_done=2) is False
    assert deadline.stop("p", 2, 3, min_done=2) is True


def test_report_logs_caller_owned_deadline_once(tmp_path: Path) -> None:
    lines: list[str] = []
    deadline = make(tmp_path, Clock(), lines)
    deadline.report("q", 4, 2)
    deadline.report("q", 5, 1)
    assert len(lines) == 1 and "|q|done=4|remaining=2" in lines[0]


def test_mark_warns_when_one_phase_exceeds_the_budget_share(tmp_path: Path) -> None:
    clock, lines = Clock(), []
    deadline = make(tmp_path, clock, lines, warn_share=0.4)
    clock.now += 39
    deadline.mark("fast")
    clock.now += 41
    assert deadline.mark("slow") == 41
    assert [line.split("|")[2] for line in lines] == ["slow"]
    assert lines[0].startswith("PHASE_BUDGET_WARN|h|slow|phase_s=41.0|share=0.41|warn_share=0.40")
    assert deadline.timings == [("fast", 39.0), ("slow", 41.0)]


def test_warn_share_is_configurable_from_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_PHASE_BUDGET_WARN_SHARE", "0.1")
    monkeypatch.setenv("SKFLEET_DISPATCH_TIMEOUT_S", "300")
    clock, lines = Clock(), []
    deadline = CycleDeadline.from_environment(250.0, clock=clock, emit=lines.append, host="h")
    assert deadline.warn_share == 0.1 and deadline.hard_timeout_s == 300
    clock.now += 30
    deadline.mark("x")
    assert lines and lines[0].startswith("PHASE_BUDGET_WARN|h|x")


def test_emitter_failure_never_stops_the_cycle(tmp_path: Path) -> None:
    def broken(_line: str) -> None:
        raise OSError("disk full")

    clock = Clock()
    deadline = CycleDeadline(10, clock=clock, emit=broken, fractions={"p": 0.0})
    assert deadline.stop("p", 0) is True
    deadline.mark("p")


def test_cursor_round_trip_clears_and_survives_corruption(tmp_path: Path) -> None:
    deadline = make(tmp_path, Clock(), [])
    assert deadline.read_cursor("c") is None
    deadline.write_cursor("c", "card9")
    deadline.write_cursor("d", "x")
    assert deadline.read_cursor("c") == "card9"
    deadline.write_cursor("c", None)
    assert json.loads((tmp_path / "cycle-cursors.json").read_text()) == {"d": "x"}
    (tmp_path / "cycle-cursors.json").write_text("{not json")
    assert deadline.read_cursor("d") is None


def test_dry_run_does_not_persist_cursors(tmp_path: Path) -> None:
    deadline = make(tmp_path, Clock(), [], persist=False)
    deadline.write_cursor("c", "x")
    assert not (tmp_path / "cycle-cursors.json").exists()


def test_rotate_from_starts_at_cursor_and_ignores_unknown() -> None:
    items = ["a", "b", "c", "d"]
    assert CycleDeadline.rotate_from(items, "c") == ["c", "d", "a", "b"]
    assert CycleDeadline.rotate_from(items, None) == items
    assert CycleDeadline.rotate_from(items, "gone") == items
    rows = [(1, "x"), (2, "y")]
    assert CycleDeadline.rotate_from(rows, "y", key=lambda r: r[1]) == [(2, "y"), (1, "x")]


def test_phase_cutoff_resumes_next_cycle_so_nothing_starves(tmp_path: Path) -> None:
    """A phase that always gets cut still walks the whole list across cycles."""
    items = [f"card{i:02d}" for i in range(10)]
    seen: list[str] = []
    for _ in range(4):
        clock, lines = Clock(), []
        deadline = make(tmp_path, clock, lines, fractions={"p": 0.5})
        order = deadline.rotate_from(items, deadline.read_cursor("p"))
        stopped_at = None
        for index, item in enumerate(order):
            if deadline.stop("p", index, len(order) - index, min_done=1) or index == 3:
                stopped_at = item
                break
            seen.append(item)
            clock.now += 20  # three items fit in a 50 s share, the 4th trips it
        deadline.write_cursor("p", stopped_at)
    assert set(seen) == set(items)


def test_full_cycle_with_time_runs_everything_in_original_order_and_clears_cursor(
    tmp_path: Path,
) -> None:
    clock, lines, ran = Clock(), [], []
    deadline = make(tmp_path, clock, lines, fractions={"s": 0.5})
    deadline.write_cursor("s", None)
    steps = [(n, (lambda n=n: ran.append(n))) for n in "abc"]
    assert deadline.run_steps("s", steps) == 3
    assert ran == ["a", "b", "c"] and lines == []
    assert deadline.read_cursor("s") is None


def test_run_steps_skips_late_sweeps_then_resumes_at_cursor(tmp_path: Path) -> None:
    ran: list[str] = []

    def step(name: str, clock: Clock):
        def run() -> None:
            ran.append(name)
            clock.now += 30  # each sweep eats 30 s of a 50 s share

        return (name, run)

    clock, lines = Clock(), []
    first = make(tmp_path, clock, lines, fractions={"s": 0.5})
    steps = [step(n, clock) for n in ("a", "b", "c")]
    assert first.run_steps("s", steps) == 2  # a, b run; c is past the share
    assert ran == ["a", "b"]
    assert first.read_cursor("s") == "c"
    assert any(line.startswith("PHASE_DEADLINE|h|s|done=2|remaining=1") for line in lines)
    assert any(line.startswith("PHASE_DEADLINE_SKIPPED|h|s|next=c") for line in lines)

    ran.clear()
    clock2, lines2 = Clock(), []
    second = make(tmp_path, clock2, lines2, fractions={"s": 0.5})
    steps2 = [step(n, clock2) for n in ("a", "b", "c")]
    assert second.run_steps("s", steps2) == 2  # c first, then a; b waits
    assert ran == ["c", "a"]
    assert second.read_cursor("s") == "b"


def test_run_steps_always_runs_at_least_one_step(tmp_path: Path) -> None:
    clock = Clock()
    clock.now += 0  # deadline fraction 0 means "already past"
    deadline = make(tmp_path, clock, [], fractions={"s": 0.0})
    ran: list[str] = []
    assert (
        deadline.run_steps("s", [("a", lambda: ran.append("a")), ("b", lambda: ran.append("b"))])
        == 1
    )
    assert ran == ["a"] and deadline.read_cursor("s") == "b"


def test_rotator_wires_every_phase_to_the_single_cycle_deadline() -> None:
    source = (Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py").read_text()
    assert source.count("CYCLE=CycleDeadline.from_environment(") == 1
    for needle in (
        'CYCLE.run_steps("startup_sweeps"',
        'CYCLE.run_steps("review_sweeps"',
        'CYCLE.stop("pool_fold"',
        'CYCLE.stop("pool_unblocks"',
        'CYCLE.stop("remote_review_offers"',
        'CYCLE.stop("builder_offers"',
        'CYCLE.stop("lane_selection"',
        'CYCLE.report("lane_launch"',
        '_cycle.report("review_observation"',
        "_cycle.mark(phase)",
    ):
        assert needle in source, needle


def test_tiny_timeout_still_leaves_a_positive_budget_and_never_blocks(tmp_path: Path) -> None:
    deadline = make(tmp_path, Clock(), [], budget_s=250.0, hard_timeout_s=0.2, final_reserve_s=20)
    assert 0 < deadline.budget_s <= 0.1
    assert deadline.run_steps("s", [("a", lambda: None)]) == 1
