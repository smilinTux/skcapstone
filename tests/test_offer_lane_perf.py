"""Offer and lane-selection throughput (2026-10-14: 98 ready cards, 0 launched).

The cycle spent builder offers 84.5 s and lane selection 57.2 s, leaving lane_launch
9.6 s of a 250 s budget. These tests pin the fixes: bounded offer phases, overlapped
source fetches, memoised network proofs, backoff for repeat failures, a deadline-aware
profile harvest, and failed-claim records that settle once.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone import source_ref_amendment as amendment
from skcapstone.fleet import builder_dispatch
from skcapstone.fleet import builder_offer_memory as memory
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import profile_requalification as requal
from skcapstone.fleet.cycle_deadline import PHASE_FRACTIONS, CycleDeadline
from skcapstone.fleet.source_prefetch import SourcePrefetch, predict_requalifications

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


def test_rotator_wires_the_budgets_and_the_accelerator() -> None:
    source = ROTATE.read_text()
    assert 'CYCLE.stop("builder_offers", _builder_offered)' in source
    assert "deadline=CYCLE)" in source  # harvest_completed honours the cycle deadline
    assert '"SKFLEET_BUILDER_OFFER_SECONDS", "45"' in source
    assert "for _candidate in tuple(_builder_candidates)[:MAX_CANDIDATE_SCAN]:" in source
    assert "_requal_prefetch.prepare if _requal_prefetch else _profile_workspace" in source
    # The accelerator is installed before any phase can validate a recipe.
    assert source.index("recipe_accel.install()") < source.index("def sh(*a)")


# -- source prefetch ----------------------------------------------------------------
def _core(card_id: str, **extra) -> dict:
    return {"id": card_id, "title": card_id, **extra}


def test_prefetch_replays_results_and_errors_at_the_call_site() -> None:
    calls: list[str] = []

    def prepare(core, labels):
        calls.append(core["id"])
        if core["id"] == "bad00001":
            raise builder_dispatch.BuilderDispatchError("exact source reconstruction failed")
        return Path("/ws") / core["id"]

    prefetch = SourcePrefetch(prepare, workers=2)
    cards = [_core("aaaa0001"), _core("bad00001"), _core("cccc0003")]
    for core in cards:
        assert prefetch.submit(core, ["l"]) is True
    assert prefetch.submit(cards[0], ["l"]) is False  # one fetch per card
    assert prefetch.prepare(cards[0], ["l"]) == Path("/ws/aaaa0001")
    with pytest.raises(builder_dispatch.BuilderDispatchError, match="reconstruction failed"):
        prefetch.prepare(cards[1], ["l"])
    assert prefetch.prepare(cards[2], ["l"]) == Path("/ws/cccc0003")
    assert sorted(calls) == ["aaaa0001", "bad00001", "cccc0003"]  # nothing ran twice


def test_prefetch_never_answers_a_changed_core_from_stale_input() -> None:
    seen = []

    def prepare(core, labels):
        seen.append((core["id"], core.get("links", {}).get("base_ref")))
        return core.get("links", {}).get("base_ref")

    prefetch = SourcePrefetch(prepare)
    prefetch.submit(_core("aaaa0001", links={"base_ref": "old"}), ["l"])
    # The loop reaches the card with an amended ref: it must run inline with that.
    assert prefetch.prepare(_core("aaaa0001", links={"base_ref": "new"}), ["l"]) == "new"
    # An unsubmitted card also runs inline.
    assert prefetch.prepare(_core("dddd0004", links={"base_ref": "x"}), ["l"]) == "x"
    assert ("aaaa0001", "new") in seen and ("dddd0004", "x") in seen


def test_prefetch_overlaps_fetches_timing() -> None:
    """Twelve 0.2 s fetches: 2.4 s back to back, well under 1.2 s overlapped."""
    delay, count = 0.2, 12

    def prepare(core, labels):
        time.sleep(delay)
        return core["id"]

    cards = [_core("%08x" % index) for index in range(count)]

    started = time.perf_counter()
    for core in cards:
        prepare(core, [])
    sequential = time.perf_counter() - started

    prefetch = SourcePrefetch(prepare, workers=4)
    started = time.perf_counter()
    for core in cards:
        prefetch.submit(core, [])
    results = [prefetch.prepare(core, []) for core in cards]
    overlapped = time.perf_counter() - started

    assert results == [core["id"] for core in cards]  # loop order is preserved
    assert sequential >= count * delay
    assert overlapped < sequential / 2.5, (overlapped, sequential)


def test_prefetch_threads_do_not_block_interpreter_exit() -> None:
    gate = threading.Event()
    prefetch = SourcePrefetch(lambda core, labels: gate.wait(30), workers=2)
    prefetch.submit(_core("aaaa0001"), [])
    time.sleep(0.05)
    assert prefetch.pending() == 1
    daemons = [t for t in threading.enumerate() if t.name == "source-prefetch"]
    assert daemons and all(t.daemon for t in daemons)
    gate.set()


def test_predict_requalifications_keeps_loop_order_and_limit() -> None:
    rows = [(0, 0, "c%07d" % i, {"title": "t%d" % i}, ["source-only"]) for i in range(10)]
    needs = {"c0000001", "c0000003", "c0000004", "c0000008", "c0000009"}
    merged_for = []

    def folded(core):
        merged_for.append(core["id"])
        return None if core["id"] == "c0000003" else dict(core, labels=["folded"])

    chosen = predict_requalifications(
        rows,
        needs_requalification=lambda core, labels: core["id"] in needs,
        folded_core=folded,
        limit=2,
    )
    # c0000003 defers before touching source (owned/not offerable): skipped, not counted.
    assert [core["id"] for core, _ in chosen] == ["c0000001", "c0000004"]
    assert chosen[0][1] == ["source-only"] and chosen[0][0]["labels"] == ["folded"]
    assert merged_for == ["c0000001", "c0000003", "c0000004"]


def test_predict_requalifications_survives_probe_errors() -> None:
    rows = [(0, 0, "aaaa0001", {}, []), (0, 0, "bbbb0002", {}, [])]

    def needs(core, labels):
        if core["id"] == "aaaa0001":
            raise OSError("profile unreadable")
        return True

    chosen = predict_requalifications(
        rows, needs_requalification=needs, folded_core=lambda core: core, limit=5
    )
    assert [core["id"] for core, _ in chosen] == ["bbbb0002"]


def test_requalification_core_is_the_inline_merge_it_replaced() -> None:
    card = SimpleNamespace(labels={"b", "a"}, links={"base_ref": "amended", "extra": "1"})
    core = {"id": "aaaa0001", "links": {"base_ref": "old", "keep": "x"}, "meta": {"m": 1}}
    expected = dict(
        core,
        labels=sorted(getattr(card, "labels", None) or []),
        links=dict(core.get("links") or {}, **(getattr(card, "links", None) or {})),
    )
    assert requal.requalification_core(core, card) == expected
    bare = SimpleNamespace()
    assert requal.requalification_core({"id": "x"}, bare) == {"id": "x", "labels": [], "links": {}}


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


# -- deadline-aware harvest -------------------------------------------------------------
@pytest.fixture
def harvest_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fleet = home / "fleet"
    fleet.mkdir(parents=True, mode=0o700)
    plan.private_dir(fleet / "profile-requalifications", create=True)
    plans = fleet / "test-plans"
    plan.private_dir(plans, create=True)
    for index in range(8):
        plan.write_once(
            plans / ("p%02d.json" % index),
            {
                "profile_requalification": True,
                "binding": {"source_card": "%08x" % index},
            },
        )
    monkeypatch.setattr(requal.socket, "gethostname", lambda: "chiap08")
    return home


def _count_loaded_plans(monkeypatch, clock=None, step=0.0):
    loaded: list[str] = []

    def load_plan(home, binding, **kwargs):
        loaded.append(binding["source_card"])
        if clock is not None:
            clock.now += step
        raise OSError("sealed plan unreadable")  # skipped by harvest, like a gone workspace

    monkeypatch.setattr(requal.plan, "load_plan", load_plan)
    return loaded


def test_harvest_scans_every_plan_without_a_deadline(harvest_home, monkeypatch) -> None:
    loaded = _count_loaded_plans(monkeypatch)
    assert requal.harvest_completed(harvest_home, {"authority_host": "chiap08"}) == "idle"
    assert loaded == ["%08x" % index for index in range(8)]


def test_harvest_with_time_to_spare_scans_everything_and_clears_the_cursor(
    harvest_home, monkeypatch
) -> None:
    clock = Clock()
    cycle = CycleDeadline(
        250.0, clock=clock, host="chiap08", state_dir=str(harvest_home / "fleet"), persist=True
    )
    cycle.write_cursor("profile_harvest", "p05.json")
    loaded = _count_loaded_plans(monkeypatch, clock, step=0.01)
    requal.harvest_completed(harvest_home, {"authority_host": "chiap08"}, deadline=cycle)
    assert sorted(loaded) == ["%08x" % index for index in range(8)]
    assert loaded[0] == "00000005"  # resumed at the cursor, wrapped around
    assert cycle.read_cursor("profile_harvest") is None


def test_harvest_stops_at_its_share_and_resumes_where_it_stopped(
    harvest_home, monkeypatch
) -> None:
    clock, lines = Clock(), []
    cycle = CycleDeadline(
        100.0,
        clock=clock,
        emit=lines.append,
        host="chiap08",
        state_dir=str(harvest_home / "fleet"),
        persist=True,
        fractions={"profile_harvest": 0.5},
        hard_timeout_s=120.0,
    )
    loaded = _count_loaded_plans(monkeypatch, clock, step=20.0)  # 3 plans fit in 50 s
    requal.harvest_completed(harvest_home, {"authority_host": "chiap08"}, deadline=cycle)
    assert loaded == ["00000000", "00000001", "00000002"]
    assert cycle.read_cursor("profile_harvest") == "p03.json"
    assert any(line.startswith("PHASE_DEADLINE|chiap08|profile_harvest|") for line in lines)

    # Next cycle starts at the first plan not reached, so the tail is not starved.
    clock2 = Clock()
    cycle2 = CycleDeadline(
        100.0,
        clock=clock2,
        host="chiap08",
        state_dir=str(harvest_home / "fleet"),
        persist=True,
        fractions={"profile_harvest": 0.5},
        hard_timeout_s=120.0,
    )
    loaded = _count_loaded_plans(monkeypatch, clock2, step=20.0)
    requal.harvest_completed(harvest_home, {"authority_host": "chiap08"}, deadline=cycle2)
    assert loaded[0] == "00000003"


def test_harvest_always_scans_at_least_one_plan_even_when_late(harvest_home, monkeypatch) -> None:
    clock = Clock()
    clock.now = 500.0
    cycle = CycleDeadline(
        100.0,
        clock=clock,
        started=0.0,
        host="chiap08",
        state_dir=str(harvest_home / "fleet"),
        persist=False,
        fractions={"profile_harvest": 0.5},
        hard_timeout_s=120.0,
    )
    loaded = _count_loaded_plans(monkeypatch)
    requal.harvest_completed(harvest_home, {"authority_host": "chiap08"}, deadline=cycle)
    assert loaded == ["00000000"]


# -- failed claim records settle once ---------------------------------------------------
def _failed_job(root: Path, card: str, revision: str = "rev-1") -> Path:
    path = root / (card * 8 + ".job.json")
    plan.write_once(
        path,
        {
            "schema": "skfleet.profile-requalification/v1",
            "card": card * 8,
            "owner": "niobe-requal-" + card * 8,
            "claim_revision": revision,
        },
    )
    plan.write_once(path.with_name(path.stem + ".failed.json"), {"reason": "x"})
    return path


def test_failed_records_that_no_longer_hold_their_claim_are_folded_once(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    paths = [_failed_job(root, card) for card in "abcd"]
    monkeypatch.setattr(requal, "CardStore", lambda home: object())
    calls: list[str] = []

    def release(home, skc, actor, card_id, owner, revision, store=None, **kwargs):
        calls.append(card_id)
        return None if card_id[0] in "abc" else True  # d still holds its claim

    monkeypatch.setattr(requal, "_release_exact_claim", release)

    first = requal._release_failed_claims(tmp_path, "skc", "niobe", root)
    assert first == ["dddddddd=released"]
    assert calls == ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"]
    assert [requal._settled_marker(p).exists() for p in paths] == [True, True, True, False]

    calls.clear()
    monkeypatch.setattr(requal, "_release_exact_claim", lambda *a, **k: calls.append(a[3]) or None)
    assert requal._release_failed_claims(tmp_path, "skc", "niobe", root) == []
    assert calls == ["dddddddd"]  # settled records are no longer re-read
    assert requal._settled_marker(paths[3]).exists()
    assert oct(os.stat(requal._settled_marker(paths[0])).st_mode & 0o777) == "0o600"


def test_a_refused_release_is_retried_not_settled(tmp_path, monkeypatch) -> None:
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    path = _failed_job(root, "e")
    monkeypatch.setattr(requal, "CardStore", lambda home: object())
    monkeypatch.setattr(requal, "_release_exact_claim", lambda *a, **k: False)
    assert requal._release_failed_claims(tmp_path, "skc", "niobe", root) == [
        "eeeeeeee=release-refused"
    ]
    assert not requal._settled_marker(path).exists()
    assert json.loads(plan.read_private(path))["card"] == "eeeeeeee"
