"""Offer and lane-selection throughput (2026-10-14: 98 ready cards, 0 launched).

The cycle spent builder offers 84.5 s and lane selection 57.2 s, leaving lane_launch
9.6 s of a 250 s budget. These tests pin the fixes: bounded offer phases,
memoised network proofs and backoff for repeat failures.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from skcapstone import source_ref_amendment as amendment
from skcapstone.fleet import builder_dispatch
from skcapstone.fleet import builder_offer_memory as memory
from skcapstone.fleet.cycle_deadline import PHASE_FRACTIONS, CycleDeadline

ROTATE = Path(__file__).resolve().parents[1] / "scripts/fleet/skfleet-rotate.py"


# -- phase budgets ----------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_offer_phases_leave_selection_and_launch_a_guaranteed_share() -> None:
    assert PHASE_FRACTIONS["remote_review_offers"] < PHASE_FRACTIONS["builder_offers"]
    assert PHASE_FRACTIONS["builder_offers"] <= 0.7
    assert PHASE_FRACTIONS["builder_offers"] < PHASE_FRACTIONS["profile_harvest"]
    assert PHASE_FRACTIONS["profile_harvest"] < PHASE_FRACTIONS["lane_selection"] < 1.0
    # 30% of a 250 s budget is 75 s for lane selection plus launch.
    assert (1.0 - PHASE_FRACTIONS["builder_offers"]) * 250 >= 75


def test_builder_offers_stop_at_their_share_with_the_production_budget() -> None:
    clock, lines = Clock(), []
    cycle = CycleDeadline(250.0, clock=clock, emit=lines.append, host="chiap08", persist=False)
    clock.now = 0.7 * 250 - 1
    assert cycle.stop("builder_offers", 3, 9) is False
    clock.now = 0.7 * 250 + 1
    assert cycle.stop("builder_offers", 4, 8) is True
    assert lines and lines[0].startswith("PHASE_DEADLINE|chiap08|builder_offers|done=4")
    # The launch phase keeps the rest of the budget.
    assert cycle.stop("lane_launch", 0, 1) is False


def test_rotator_wires_the_budgets() -> None:
    source = ROTATE.read_text()
    assert 'CYCLE.stop("builder_offers", _builder_offered)' in source
    assert '"SKFLEET_BUILDER_OFFER_SECONDS", "45"' in source
    assert "for _candidate in tuple(_builder_candidates)[:MAX_CANDIDATE_SCAN]:" in source


# -- amended-ref proof memo ----------------------------------------------------------
def test_amended_ref_proof_runs_once_per_ttl_and_replays_failure(monkeypatch) -> None:
    now = [0.0]
    runs: list[tuple] = []
    outcomes = {"ok": None, "bad": ValueError("source amended ref fetch or ancestry proof failed")}

    def prover(repository, ref, revision):
        runs.append((repository, ref, revision))
        failure = outcomes["bad" if ref == "refs/heads/bad" else "ok"]
        if failure:
            raise failure

    monkeypatch.setattr(amendment, "prove_ref", prover)
    monkeypatch.setattr(amendment, "_PROOFS", {})
    clock = lambda: now[0]  # noqa: E731
    for _ in range(5):
        amendment._prove_once("https://h/r.git", "refs/heads/ok", "a" * 40, clock=clock)
    assert len(runs) == 1
    for _ in range(3):
        with pytest.raises(ValueError, match="ancestry proof failed"):
            amendment._prove_once("https://h/r.git", "refs/heads/bad", "a" * 40, clock=clock)
    assert len(runs) == 2
    # A different revision is a different proof.
    amendment._prove_once("https://h/r.git", "refs/heads/ok", "b" * 40, clock=clock)
    assert len(runs) == 3
    # The TTL expires both successes and failures.
    now[0] = amendment.PROOF_TTL_SECONDS + 1
    amendment._prove_once("https://h/r.git", "refs/heads/ok", "a" * 40, clock=clock)
    with pytest.raises(ValueError):
        amendment._prove_once("https://h/r.git", "refs/heads/bad", "a" * 40, clock=clock)
    assert len(runs) == 5


def test_concurrent_proofs_of_one_ref_are_single_flight(monkeypatch) -> None:
    """The prefetch threads and the offer loop ask for the same proof at once."""
    runs = []

    def slow_prover(repository, ref, revision):
        runs.append(threading.current_thread().name)
        time.sleep(0.3)

    monkeypatch.setattr(amendment, "prove_ref", slow_prover)
    monkeypatch.setattr(amendment, "_PROOFS", {})
    monkeypatch.setattr(amendment, "_PROOF_LOCKS", {})
    threads = [
        threading.Thread(
            target=amendment._prove_once, args=("https://h/r.git", "refs/heads/x", "a" * 40)
        )
        for _ in range(4)
    ]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(runs) == 1
    assert time.perf_counter() - started < 0.9


def test_amended_ref_proof_survives_the_process_via_the_fleet_store(tmp_path, monkeypatch) -> None:
    """The dispatcher is a new process every cycle; a fresh proof is reused across them."""
    runs = []
    monkeypatch.setattr(amendment, "prove_ref", lambda *a: runs.append(a))
    wall = [1_000_000.0]
    monkeypatch.setattr(amendment.time, "time", lambda: wall[0])
    args = ("https://h/r.git", "refs/heads/x", "a" * 40)

    amendment._prove_once(*args, home=tmp_path)
    assert len(runs) == 1
    assert (tmp_path / "fleet/source-ref-proofs.json").is_file()

    monkeypatch.setattr(amendment, "_PROOFS", {})  # a new process
    amendment._prove_once(*args, home=tmp_path)
    assert len(runs) == 1

    monkeypatch.setattr(amendment, "_PROOFS", {})
    wall[0] += amendment.PROOF_SUCCESS_TTL_SECONDS + 1
    amendment._prove_once(*args, home=tmp_path)
    assert len(runs) == 2  # expired: proven again


def test_amended_ref_failure_is_remembered_briefly_across_processes(tmp_path, monkeypatch) -> None:
    runs = []

    def failing(*args):
        runs.append(args)
        raise ValueError("source amended ref proof unavailable")

    monkeypatch.setattr(amendment, "prove_ref", failing)
    wall = [2_000_000.0]
    monkeypatch.setattr(amendment.time, "time", lambda: wall[0])
    args = ("https://h/r.git", "refs/heads/x", "b" * 40)
    for _ in range(2):
        monkeypatch.setattr(amendment, "_PROOFS", {})
        with pytest.raises(ValueError, match="proof unavailable"):
            amendment._prove_once(*args, home=tmp_path)
    assert len(runs) == 1
    monkeypatch.setattr(amendment, "_PROOFS", {})
    wall[0] += amendment.PROOF_FAILURE_TTL_SECONDS + 1
    with pytest.raises(ValueError):
        amendment._prove_once(*args, home=tmp_path)
    assert len(runs) == 2  # the network may be back: retried soon


def test_amended_ref_proof_store_is_optional_and_corruption_tolerant(
    tmp_path, monkeypatch
) -> None:
    runs = []
    monkeypatch.setattr(amendment, "prove_ref", lambda *a: runs.append(a))
    monkeypatch.setattr(amendment, "_PROOFS", {})
    amendment._prove_once("https://h/r.git", "r", "c" * 40)  # no home: no file written
    assert not list(tmp_path.rglob("source-ref-proofs.json"))
    store = tmp_path / "fleet/source-ref-proofs.json"
    store.parent.mkdir(parents=True)
    for garbage in ("not json", "[]", '{"k": 5}', '{"k": [1]}'):
        store.write_text(garbage)
        monkeypatch.setattr(amendment, "_PROOFS", {})
        amendment._prove_once("https://h/r.git", "r", "d" * 40, home=tmp_path)
    assert len(runs) == 5  # garbage is never trusted
    assert json.loads(store.read_text())  # and is replaced by a valid file


def test_amended_ref_proof_is_keyed_by_the_prover(monkeypatch) -> None:
    monkeypatch.setattr(amendment, "_PROOFS", {})
    first, second = [], []
    monkeypatch.setattr(amendment, "prove_ref", lambda *a: first.append(a))
    amendment._prove_once("https://h/r.git", "r", "a" * 40)
    monkeypatch.setattr(amendment, "prove_ref", lambda *a: second.append(a))
    amendment._prove_once("https://h/r.git", "r", "a" * 40)
    assert len(first) == 1 and len(second) == 1


# -- source reconstruction timeout --------------------------------------------------
def test_materialize_source_times_out_instead_of_hanging(tmp_path, monkeypatch) -> None:
    timeouts = []

    def hang(command, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(builder_dispatch.subprocess, "run", hang)
    workspace = tmp_path / "ws" / "niobe-profile-requal-aaaa0001-aaaaaaaaaaaa"
    request = {
        "repository": "https://github.com/example/repo.git",
        "base_ref": "main",
        "base_revision": "a" * 40,
    }
    with pytest.raises(
        builder_dispatch.BuilderDispatchError, match="exact source reconstruction failed"
    ):
        builder_dispatch.materialize_source(request, workspace)
    assert timeouts == [builder_dispatch.SOURCE_MATERIALIZE_TIMEOUT_SECONDS]
    assert builder_dispatch.SOURCE_MATERIALIZE_TIMEOUT_SECONDS > 0
    assert not workspace.exists()
    assert not [p for p in workspace.parent.iterdir()]  # the temp checkout was removed


# -- offer memory backoff -------------------------------------------------------------
def test_costly_failures_back_off_30_60_120_minutes_then_hold(tmp_path) -> None:
    path = tmp_path / "memory.json"
    state = "ineligible:exact source reconstruction failed"
    windows = []
    now = 10_000.0
    for _ in range(5):
        memory.record(path, "dead0001", state, now=now)
        loaded = memory.load(path)
        windows.append(memory.dead_window(loaded, "dead0001"))
        assert memory.skip(loaded, "dead0001", now=now + windows[-1] - 1)
        assert not memory.skip(loaded, "dead0001", now=now + windows[-1] + 1)
        now += windows[-1] + 2
    assert windows == [1800, 3600, 7200, 7200, 7200]


def test_other_dead_outcomes_keep_the_flat_window_and_success_resets(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory.record(path, "dead0002", "ineligible:qualified Node environment changed", now=1000)
    memory.record(path, "dead0002", "ineligible:qualified Node environment changed", now=2000)
    assert memory.dead_window(memory.load(path), "dead0002") == memory.DEAD_SECONDS
    state = "ineligible:exact source reconstruction failed"
    memory.record(path, "dead0003", state, now=1000)
    memory.record(path, "dead0003", state, now=2000)
    assert memory.dead_window(memory.load(path), "dead0003") == 3600
    memory.record(path, "dead0003", "pending:chiap01", now=3000)
    memory.record(path, "dead0003", state, now=4000)
    assert memory.dead_window(memory.load(path), "dead0003") == 1800


def test_backoff_applies_to_amended_ref_proof_failures(tmp_path) -> None:
    path = tmp_path / "memory.json"
    state = "ineligible:source binding conflict: base_ref"
    memory.record(path, "dead0004", state, now=0)
    memory.record(path, "dead0004", state, now=100)
    loaded = memory.load(path)
    assert memory.skip(loaded, "dead0004", now=100 + 3599)
    assert not memory.skip(loaded, "dead0004", now=100 + 3601)


def test_legacy_memory_rows_without_a_failure_count_keep_working() -> None:
    legacy = {"old00001": {"ts": 1000.0, "state": "ineligible:exact source reconstruction failed"}}
    assert memory.dead_window(legacy, "old00001") == memory.DEAD_SECONDS
    assert memory.skip(legacy, "old00001", now=1000 + 100)
