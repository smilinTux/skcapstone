"""Runtime integration, saturation, replay and live authority regression tests."""

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli import main
from skcapstone.skrsi_experiment_controller import ExperimentController
from skcapstone.skrsi_handoffs import (
    FIRST_WAVE_HANDOFFS,
    HandoffError,
    HandoffRuntime,
    RetryBeforeEffectError,
)
from skcapstone.skrsi_registry import canonical_json
from skcapstone.skrsi_runtime import SKRSIRuntime
from tests.test_link_review_work import _home_with_source, _item
from tests.test_skrsi_evaluator import cohort, gates, policy
from tests.test_skrsi_registry import target


def executor(tmp_path, **changes):
    contracts = {k: replace(v, **changes) for k, v in FIRST_WAVE_HANDOFFS.items()}
    return HandoffRuntime(tmp_path / "handoffs.sqlite3", contracts=contracts)


def invoke(runtime, key="work", operation=lambda: {"ok": True}, **changes):
    kwargs = dict(
        authorize=lambda: True,
        quality=lambda: True,
        authority=lambda: "revision-1",
        expected_revision="revision-1",
    )
    kwargs.update(changes)
    return runtime.execute("evidence-to-review", key, "a" * 64, operation, **kwargs)


def facade(runtime, **changes):
    kwargs = dict(authorize=lambda: True, quality=lambda: True, authority=lambda: "revision-1")
    kwargs.update(changes)
    return SKRSIRuntime(runtime, **kwargs)


def test_concurrent_replay_and_restart_execute_once(tmp_path):
    runtime = executor(tmp_path)
    calls = []

    def work():
        calls.append(1)
        return {"claim": "one", "reviewer": "seraph"}

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: invoke(runtime, operation=work), range(24)))
    assert calls == [1]
    assert all(r == {"claim": "one", "reviewer": "seraph"} for r in results)
    assert invoke(executor(tmp_path), operation=work) == results[0]
    assert calls == [1]
    receipt = runtime.read("evidence-to-review", "work")
    assert receipt["status"] == "success"
    assert receipt["contract"]["notification_only"] is True


def test_timeout_retains_saturated_slot_and_late_result_cannot_replace_receipt(tmp_path):
    runtime = executor(tmp_path, queue_bound=1, timeout_seconds=0.1)
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append(1)
        started.set()
        release.wait(3)
        return {"late": True}

    before = time.monotonic()
    with pytest.raises(HandoffError, match="timeout"):
        invoke(runtime, operation=work)
    assert started.is_set() and time.monotonic() - before < 1
    receipt = runtime.read("evidence-to-review", "work")
    with pytest.raises(HandoffError, match="saturated"):
        invoke(runtime, key="other")
    with pytest.raises(HandoffError, match="timeout"):
        invoke(runtime, operation=work)
    release.set()
    assert calls == [1]
    assert runtime.read("evidence-to-review", "work") == receipt
    assert receipt["notification"] == {
        "recipient": "mero",
        "mode": "notification-only",
        "required": True,
    }
    assert receipt["recovery_owner"] == "link"


@pytest.mark.parametrize(
    "gate,value,message",
    [
        ("authorize", False, "authorization-denied"),
        ("quality", False, "quality-denied"),
        ("authority", "old", "stale-authority"),
    ],
)
def test_bad_gates_never_execute_even_at_saturation(tmp_path, gate, value, message):
    runtime = executor(tmp_path, queue_bound=1)
    calls = []
    with pytest.raises(HandoffError, match=message):
        invoke(runtime, operation=lambda: calls.append(1), **{gate: lambda: value})
    assert not calls


def test_stale_after_execution_is_not_published_and_replay_is_gated(tmp_path):
    runtime = executor(tmp_path)
    revision = ["revision-1"]

    def work():
        revision[0] = "old"
        return {"ok": True}

    with pytest.raises(HandoffError, match="stale-authority"):
        invoke(runtime, operation=work, authority=lambda: revision[0])
    invoke(runtime, key="second")
    with pytest.raises(HandoffError):
        invoke(runtime, key="second", authorize=lambda: False)


def test_only_certified_no_effect_errors_retry_with_backoff_and_fresh_gates(tmp_path):
    runtime = executor(tmp_path, backoff_seconds=0.02)
    times = []

    def work():
        times.append(time.monotonic())
        if len(times) < 3:
            raise RetryBeforeEffectError()
        return {"ok": True}

    assert invoke(runtime, operation=work) == {"ok": True}
    assert len(times) == 3
    assert times[1] - times[0] >= 0.02 and times[2] - times[1] >= 0.04
    calls = []

    def uncertain():
        calls.append(1)
        raise OSError("a potentially sensitive message is not persisted")

    with pytest.raises(HandoffError, match="OSError"):
        invoke(runtime, key="uncertain", operation=uncertain)
    assert calls == [1]
    assert "sensitive" not in json.dumps(runtime.read("evidence-to-review", "uncertain"))


def test_terminal_receipt_is_immutable_and_input_collision_rejected(tmp_path):
    runtime = executor(tmp_path)
    invoke(runtime)
    with (
        sqlite3.connect(runtime.path) as db,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        db.execute("UPDATE handoffs SET receipt='{}'")
    with pytest.raises(HandoffError, match="collision"):
        runtime.execute(
            "evidence-to-review",
            "work",
            "b" * 64,
            lambda: {},
            authorize=lambda: True,
            quality=lambda: True,
            authority=lambda: "revision-1",
            expected_revision="revision-1",
        )


def test_all_runtime_boundaries_execute_real_components_and_emit_receipts(tmp_path):
    runtime = executor(tmp_path)
    api = facade(runtime)
    value = target()
    now = datetime.now(timezone.utc).isoformat()
    for source in ["cardstore", "skfleet", "skmail"]:
        envelope = {
            "source": source,
            "natural_key": "one",
            "event_id": "one",
            "cursor": "1",
            "occurred_at": now,
            "recorded_at": now,
            "metadata": {"count": 1},
        }
        collected = api.collect(source, [envelope], value, revision="revision-1")
        assert collected["accepted"] == 1
    evaluation = api.evaluate(
        cohort("before", (1.0, 1.0, 1.0, 1.0)),
        cohort("after", (9.0, 9.0, 9.0, 9.0)),
        policy(),
        gates(quality=False),
        evaluator="atlas",
        independent_reviewer="seraph",
        revision="revision-1",
    )
    assert evaluation["proposed_decision"] == "REJECT"
    store = CardStore(tmp_path / "authority")
    store.home.mkdir()
    store.create(CardCore(id="exp00001", title="Experiment"))
    api.transition(
        store,
        "exp00001",
        "hypothesis",
        agent="atlas",
        revision=1,
        transition_id="hypothesis",
        evaluation=evaluation,
        authority_revision="revision-1",
    )
    assert len(store._read_events("exp00001")) == 2
    projection = api.project(
        store, "exp00001", revision="revision-1", expected_experiment_revision=1
    )
    digest = hashlib.sha256(canonical_json(projection)).hexdigest()
    assert api.deliver("query", digest, lambda: projection, revision="revision-1") == projection
    assert api.canary_review(evaluation, revision="revision-1")["proposed_decision"] == "REJECT"
    home = _home_with_source(tmp_path)
    result = api.review(home, _item(home), "4" * 64, revision="revision-1")
    assert result["handoff_state"] == "materialized"
    assert result["launch_authority"] is False
    assert CardStore(home).fold(result["review_card_id"]).owner is None
    with sqlite3.connect(runtime.path) as db:
        boundaries = {
            r[0] for r in db.execute("SELECT boundary FROM handoffs WHERE receipt IS NOT NULL")
        }
    assert boundaries == set(FIRST_WAVE_HANDOFFS)
    ExperimentController(store, agent="atlas").transition(
        "exp00001",
        "experiment",
        revision=2,
        transition_id="experiment",
        evidence={"sha256": "b" * 64},
    )
    with pytest.raises(HandoffError):
        api.project(store, "exp00001", revision="revision-1", expected_experiment_revision=1)


def test_real_link_materialization_concurrent_replay_and_reviewer_collision(tmp_path):
    runtime = executor(tmp_path)
    api = facade(runtime)
    home = _home_with_source(tmp_path)
    item = _item(home)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: api.review(home, item, "4" * 64, revision="revision-1"), range(16))
        )
    assert len({r["review_card_id"] for r in results}) == 1
    store = CardStore(home)
    assert len(store.list_cards()) == 2
    review = results[0]["review_card_id"]
    assert (
        len(
            [
                e
                for e in store._read_events(review)
                if e["action"] == "review_assignment_recommendation"
            ]
        )
        == 1
    )
    other = {
        **item,
        "reviewer_candidates": [
            {"identity": "different", "name": "Seraph", "seat": "seraph", "eligible": True}
        ],
    }
    with pytest.raises(HandoffError, match="collision"):
        api.review(home, other, "4" * 64, revision="revision-1")
    assert store.fold(review).owner is None
    store.append_event(review, "claim", "seraph", owner="seraph")
    with pytest.raises(HandoffError):
        api.review(home, item, "4" * 64, revision="revision-1")
    assert len([e for e in store._read_events(review) if e["action"] == "claim"]) == 1


def test_link_materialization_waits_for_bounded_sqlite_contention(tmp_path):
    runtime = executor(tmp_path)
    api = facade(runtime)
    home = _home_with_source(tmp_path)
    item = _item(home)
    locked = threading.Event()

    def hold_lock():
        with sqlite3.connect(runtime.path) as db:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(0.15)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert locked.wait(1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: api.review(home, item, "4" * 64, revision="revision-1"), range(16))
        )
    thread.join()
    assert len({result["review_card_id"] for result in results}) == 1
    store = CardStore(home)
    review = results[0]["review_card_id"]
    assert len(store.list_cards()) == 2
    assert (
        sum(
            event["action"] == "review_assignment_recommendation"
            for event in store._read_events(review)
        )
        == 1
    )
    assert store.fold(review).owner is None


def test_handoff_contention_fails_closed_after_bounded_wait(tmp_path, monkeypatch):
    runtime = executor(tmp_path)
    monkeypatch.setattr("skcapstone.skrsi_handoffs._SQLITE_BUSY_TIMEOUT_MS", 25)
    with sqlite3.connect(runtime.path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            runtime.execute(
                "evidence-to-review",
                "source01+revision-1",
                "4" * 64,
                lambda: {"unexpected": True},
                authorize=lambda: True,
                quality=lambda: True,
                authority=lambda: "revision-1",
                expected_revision="revision-1",
            )
        assert time.monotonic() - started < 0.5
    assert runtime.read("evidence-to-review", "source01+revision-1") is None


def test_consumers_enforce_contract_limits_and_bad_projection(tmp_path):
    runtime = executor(tmp_path, queue_bound=1)
    api = facade(runtime)
    with pytest.raises(HandoffError, match="queue bound"):
        api.collect("cardstore", [{}, {}], target(), revision="revision-1")
    with pytest.raises(HandoffError):
        api.deliver("bad", "a" * 64, lambda: {"count": 1}, revision="revision-1")
    with pytest.raises(HandoffError):
        api.canary_review({"content_hash": "a" * 64}, revision="revision-1")


def test_cli_requires_claim_and_machine_quality_evidence_and_uses_runtime(tmp_path):
    store = CardStore(tmp_path / "authority")
    store.home.mkdir()
    store.create(CardCore(id="source01", title="SKRSI metadata task"))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"operation": "register", "target": target().to_dict()}))
    args = [
        "skrsi",
        "run",
        "--home",
        str(store.home),
        "--state-dir",
        str(tmp_path / "runtime"),
        "--card",
        "source01",
        "--agent",
        "atlas",
        "--request",
        str(request),
    ]
    assert CliRunner().invoke(main, args).exit_code != 0
    store.append_event("source01", "claim", "atlas", owner="atlas")
    store.append_event("source01", "move", "atlas", column="doing")
    store.append_event(
        "source01",
        "link",
        "atlas",
        link_key="runtime_input_sha256",
        link_value=hashlib.sha256(request.read_bytes()).hexdigest(),
    )
    assert CliRunner().invoke(main, args).exit_code != 0
    store.append_event("source01", "link", "atlas", link_key="quality_gate", link_value="PASS")
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == "review-latency"
    with sqlite3.connect(tmp_path / "runtime/handoffs.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"notification_only": False},
        {"timeout_seconds": float("nan")},
        {"queue_bound": True},
        {"consumer": "a,b"},
    ],
)
def test_invalid_contract_cannot_introduce_hidden_gate_or_unbounded_execution(change):
    with pytest.raises(ValueError):
        replace(FIRST_WAVE_HANDOFFS["evidence-to-review"], **change)


def test_authority_loss_during_backoff_prevents_second_attempt(tmp_path):
    runtime = executor(tmp_path)
    authorized = [True]
    calls = []

    def work():
        calls.append(1)
        authorized[0] = False
        raise RetryBeforeEffectError()

    with pytest.raises(HandoffError, match="authorization-denied"):
        invoke(runtime, operation=work, authorize=lambda: authorized[0])
    assert calls == [1]
    assert runtime.notifications()[0]["notification"]["mode"] == "notification-only"


def test_expired_target_cannot_be_replayed(tmp_path, monkeypatch):
    from datetime import timedelta

    import skcapstone.skrsi_runtime as module

    runtime = executor(tmp_path)
    api = facade(runtime)
    value = target()
    api.register(value, revision="revision-1")

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(days=30)

    monkeypatch.setattr(module, "datetime", Later)
    with pytest.raises(HandoffError):
        api.register(value, revision="revision-1")


def test_independent_runtime_instances_share_single_flight(tmp_path):
    first, second = executor(tmp_path), executor(tmp_path)
    calls = []

    def work():
        calls.append(1)
        return {"claim": "only-one"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda i: invoke(first if i % 2 else second, operation=work), range(16))
        )
    assert calls == [1]
    assert all(value == {"claim": "only-one"} for value in results)
