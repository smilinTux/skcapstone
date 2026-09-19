"""Governed ZIOWK01 builder dispatch integration."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch, sknoded, store


@pytest.fixture(autouse=True)
def _clear_process_registry(monkeypatch):
    builder_dispatch._PROCESSES.clear()
    monkeypatch.setenv("SKFLEET_PI", "/test/bin/pi")
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: _folded())
    yield
    builder_dispatch._PROCESSES.clear()


def _node(paths, operator, noded41) -> None:
    store.write_spec(
        paths,
        "node",
        "node-ziowk01",
        {"role": "builder-standby", "actuate": True, "cordoned": False},
        writer=operator,
        labels={"host": "ziowk01"},
    )
    sknoded.run_once(paths, "node-ziowk01")


def _card() -> dict:
    return {
        "id": "24b00003",
        "meta": {
            "repository": "https://github.com/smilinTux/skcapstone.git",
            "base_ref": "main",
            "base_revision": "9cc415465d6bacc22b51b09a3c861c61f0823d45",
        },
    }


def _folded(**values) -> SimpleNamespace:
    defaults = {
        "id": "24b00003",
        "owner": None,
        "meta": dict(_card()["meta"]),
        "labels": ["sk-m", "source-only"],
        "status": SimpleNamespace(value="doing"),
        "links": {},
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_niobe_places_one_generic_medium_card(paths, operator, noded41) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=writer,
        now=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    assert request["node"] == "node-ziowk01"
    assert request["provider"] == "skgateway"
    assert request["logical_route"] == "sk-m"
    assert store.read_placement(paths, "job", "24b00003")["node"] == "node-ziowk01"
    repeated = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    assert repeated == request


def test_niobe_admits_four_distinct_requests_and_denies_fifth(paths, operator, noded41) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    requests = []
    for number in range(1, 6):
        core = _card() | {"id": f"24b0000{number}"}
        requests.append(
            builder_dispatch.offer(
                paths,
                core,
                ["sk-m", "source-only"],
                writer=writer,
            )
        )

    assert all(request is not None for request in requests[:4])
    assert requests[4] is None
    assert builder_dispatch._node_load(paths, "node-ziowk01") == 4
    assert (
        builder_dispatch.offer(
            paths,
            _card() | {"id": "24b00001"},
            ["sk-m", "source-only"],
            writer=writer,
        )
        == requests[0]
    )

    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        requests[0],
        "completed",
    )
    assert builder_dispatch._node_load(paths, "node-ziowk01") == 3
    assert (
        builder_dispatch.offer(
            paths,
            _card() | {"id": "24b00005"},
            ["sk-m", "source-only"],
            writer=writer,
        )
        is not None
    )


def test_consumer_reconciles_live_first_and_fills_four_slots(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    requests = [
        builder_dispatch.offer(
            paths, _card() | {"id": f"24b0000{number}"}, ["sk-m", "source-only"], writer=writer
        )
        for number in range(1, 5)
    ]
    folded = {request["card_id"]: _folded(id=request["card_id"]) for request in requests}
    claims = []
    launches = []

    def claim(_self, owner, card_id):
        claims.append(card_id)
        folded[card_id].owner = owner
        folded[card_id].meta = dict(_card()["meta"], _claim_revision=f"claim-{card_id}")

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda _self, card_id: folded[card_id])
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(builder_dispatch, "_proc_start_ticks", lambda pid: str(pid))

    def launch(_command, workspace):
        launches.append(workspace)
        return SimpleNamespace(pid=100 + len(launches), poll=lambda: None)

    builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=launch,
        materializer=lambda _request, workspace: workspace,
    )
    assert len(launches) == 4
    assert len(set(launches)) == 4
    assert claims == [request["card_id"] for request in requests]
    builder_dispatch._PROCESSES.clear()  # daemon restart: PID birth tokens remain exact
    builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=launch,
        materializer=lambda _request, workspace: workspace,
    )
    assert len(launches) == len(claims) == 4
    for request in requests:
        status = builder_dispatch._load(
            builder_dispatch.status_path(paths, "node-ziowk01", request["card_id"])
        )
        assert status["state"] == "running"
        assert status["liveness"] == "live"


def test_live_first_request_does_not_hide_queued_second(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    first, second = [
        builder_dispatch.offer(
            paths, _card() | {"id": card_id}, ["sk-m", "source-only"], writer=writer
        )
        for card_id in ("29a6f24e", "6fe4d373")
    ]
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        first,
        "running",
        owner="pi-builder-standby-node-ziowk01-29a6f24e",
        claim_revision="first-claim",
        pid=123,
        pid_start_ticks="123",
        attempt=1,
    )
    folded = {
        first["card_id"]: _folded(
            id=first["card_id"],
            owner="pi-builder-standby-node-ziowk01-29a6f24e",
            meta=dict(_card()["meta"], _claim_revision="first-claim"),
        ),
        second["card_id"]: _folded(id=second["card_id"]),
    }
    claims = []

    def claim(_self, owner, card_id):
        claims.append(card_id)
        folded[card_id].owner = owner
        folded[card_id].meta = dict(_card()["meta"], _claim_revision="second-claim")

    monkeypatch.setattr(builder_dispatch, "_proc_start_ticks", lambda pid: str(pid))
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda _self, card_id: folded[card_id])
    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: SimpleNamespace(pid=124, poll=lambda: None),
        materializer=lambda _request, workspace: workspace,
    )
    assert result["request_id"] == second["request_id"]
    assert claims == [second["card_id"]]
    assert (
        builder_dispatch._load(
            builder_dispatch.status_path(paths, "node-ziowk01", first["card_id"])
        )["liveness"]
        == "live"
    )


def test_changed_request_preserves_uncertain_live_generation(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "running",
        owner="prior-owner",
        claim_revision="prior-revision",
        pid=123,
        pid_start_ticks="123",
        attempt=1,
    )
    changed = dict(request, request_id="changed-generation")
    builder_dispatch.atomic_write_text(
        builder_dispatch.request_path(paths, "node-ziowk01", request["card_id"]),
        json.dumps(changed) + "\n",
    )
    monkeypatch.setattr(
        builder_dispatch.Board,
        "release_claim",
        lambda *_args, **_kwargs: pytest.fail("released"),
    )
    monkeypatch.setattr(
        builder_dispatch.Board, "claim_task", lambda *_args: pytest.fail("claimed")
    )
    assert (
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=lambda *_args: pytest.fail("launched"),
            materializer=lambda *_args: pytest.fail("materialized"),
        )
        is None
    )
    status = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", request["card_id"])
    )
    assert status["request_id"] == request["request_id"]
    assert status["owner"] == "prior-owner"


def test_consumer_keeps_fifth_waiting_until_a_slot_closes(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    requests = [
        builder_dispatch.offer(
            paths, _card() | {"id": f"24b0000{number}"}, ["sk-m", "source-only"], writer=writer
        )
        for number in range(1, 5)
    ]
    fifth = dict(requests[-1], card_id="24b00005", request_id="fifth-request")
    builder_dispatch.atomic_write_text(
        builder_dispatch.request_path(paths, "node-ziowk01", fifth["card_id"]),
        json.dumps(fifth) + "\n",
    )
    folded = {request["card_id"]: _folded(id=request["card_id"]) for request in [*requests, fifth]}
    processes = []
    claims = []

    def claim(_self, owner, card_id):
        claims.append(card_id)
        folded[card_id].owner = owner
        folded[card_id].meta = dict(_card()["meta"], _claim_revision=f"claim-{card_id}")

    def launch(_command, _workspace):
        process = SimpleNamespace(pid=100 + len(processes), poll=lambda: None)
        processes.append(process)
        return process

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda _self, card_id: folded[card_id])
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(builder_dispatch, "_proc_start_ticks", lambda pid: str(pid))
    builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=launch,
        materializer=lambda _request, workspace: workspace,
    )
    assert len(processes) == 4
    assert fifth["card_id"] not in claims
    builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=launch,
        materializer=lambda _request, workspace: workspace,
    )
    assert len(processes) == 4
    processes[0].poll = lambda: 0
    folded[requests[0]["card_id"]].status = SimpleNamespace(value="done")
    monkeypatch.setattr(builder_dispatch, "_send_status", lambda *_args: True)
    builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=launch,
        materializer=lambda _request, workspace: workspace,
    )
    assert len(processes) == 5
    assert claims[-1] == fifth["card_id"]


def test_expired_unclaimed_offer_is_terminal_without_claim(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        builder_dispatch.Board, "claim_task", lambda *_args: pytest.fail("claimed")
    )
    for _ in range(2):
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=lambda *_args: pytest.fail("launched"),
            materializer=lambda *_args: pytest.fail("materialized"),
        )
    status = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", request["card_id"])
    )
    assert status["state"] == "blocked"
    assert status["error"] == "unclaimed offer expired"
    assert status["attempt"] == 0


def test_offer_rejects_wrong_scheduler_and_lane_pins(paths) -> None:
    wrong = store.Writer(role="scheduler", node="atlas", identity="")
    try:
        builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=wrong)
    except builder_dispatch.BuilderDispatchError as exc:
        assert "Niobe" in str(exc)
    else:
        raise AssertionError("non-Niobe scheduler was accepted")
    assert not builder_dispatch.eligible(_card(), ["sk-m", "source-only", "codex-only"])


@pytest.mark.parametrize("route", ["sk-s", "sk-m", "sk-l", "sk-xl"])
def test_provider_neutral_logical_routes_are_eligible(route) -> None:
    assert builder_dispatch.eligible(_card(), [route, "source-only"])


def test_ambiguous_or_direct_provider_routes_are_rejected() -> None:
    assert not builder_dispatch.eligible(_card(), ["sk-s", "sk-m", "source-only"])
    assert not builder_dispatch.eligible(_card(), ["sk-l", "source-only", "glm-only"])


def test_worker_command_is_pi_through_gateway_only(monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross-boundary")
    command = builder_dispatch.worker_command(
        _card() | {"card_id": "24b00003", "logical_route": "sk-s"},
        "owner",
        "rev",
        "/tmp/work",
    )
    assert command[:2] == ["/usr/bin/env", "-i"]
    assert "--provider" in command
    assert command[command.index("--provider") + 1] == "skgateway"
    assert command[command.index("--model") + 1] == "sk-s"
    assert "--no-approve" in command
    assert "--approve" not in command
    assert command[command.index("--tools") + 1] == "read,bash,edit,write,grep,find,ls"
    assert "--extension" in command
    assert "codex" not in " ".join(command).lower()
    assert "glm" not in " ".join(command).lower()
    assert "kimi" not in " ".join(command).lower()
    assert "qwen" not in " ".join(command).lower()
    assert "openai.com" not in " ".join(command).lower()
    assert "must-not-cross-boundary" not in " ".join(command)
    assert not any(part.startswith("AWS_SECRET_ACCESS_KEY=") for part in command)
    assert "SKFLEET_CLAIM_REVISION=rev" in command
    assert f"SKCAPSTONE_HOME={builder_dispatch.Path.home() / '.skcapstone'}" in command


def test_worker_command_uses_bundled_guard_in_isolated_environment(monkeypatch) -> None:
    monkeypatch.delenv("SKFLEET_PI_CARDSTORE_GUARD", raising=False)
    monkeypatch.setattr(builder_dispatch.shutil, "which", lambda *_args: None)
    command = builder_dispatch.worker_command(
        _card() | {"card_id": "24b00003", "logical_route": "sk-l"},
        "owner",
        "rev",
        "/tmp/work",
    )

    assert command[command.index("--extension") + 1] == str(builder_dispatch._BUNDLED_GUARD)
    assert builder_dispatch._BUNDLED_GUARD.is_file()


def test_worker_command_rejects_missing_configured_guard(monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_PI_CARDSTORE_GUARD", "/missing/pi-cardstore-guard.mjs")

    with pytest.raises(builder_dispatch.BuilderDispatchError, match="does not exist"):
        builder_dispatch.worker_command(
            _card() | {"card_id": "24b00003", "logical_route": "sk-m"},
            "owner",
            "rev",
            "/tmp/work",
        )


def test_post_offer_card_amendment_blocks_materialization_and_claim(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    amended = _folded()
    amended.meta["base_revision"] = "a" * 40
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: amended)
    monkeypatch.setattr(
        builder_dispatch.Board,
        "claim_task",
        lambda *_args: pytest.fail("amended card was claimed"),
    )
    assert (
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            materializer=lambda *_args: pytest.fail("amended source was materialized"),
        )
        is None
    )
    status = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", "24b00003")
    )
    assert status["state"] == "blocked"
    assert status["attempt"] == 0
    assert "changed after dispatch" in status["error"]
    assert sknoded.run_once(paths, "node-ziowk01")["heartbeat"] is True


@pytest.mark.parametrize("mismatch_fold", [1, 2, 4])
def test_transient_mismatch_refolds_before_launch(
    paths, operator, noded41, monkeypatch, tmp_path, mismatch_fold
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()
    folds = 0
    launches = []

    def fold(_self, _card_id):
        nonlocal folds
        folds += 1
        if folds == mismatch_fold:
            return _folded(meta=dict(folded.meta, base_revision="a" * 40))
        return folded

    def claim(_self, owner, _card_id):
        folded.owner = owner
        folded.meta["_claim_revision"] = "exact-generation"

    monkeypatch.setattr(builder_dispatch.CardStore, "fold", fold)
    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        materializer=lambda _request, workspace: workspace,
        launcher=lambda *_args: (launches.append(1) or SimpleNamespace(pid=43, poll=lambda: None)),
    )
    assert result["state"] == "running"
    assert result["attempt"] == 1
    assert launches == [1]


def test_durable_mismatch_after_materialization_preserves_attempt_and_claim(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()

    def materialize(_request, workspace):
        folded.meta["base_revision"] = "a" * 40
        return workspace

    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(
        builder_dispatch.Board, "claim_task", lambda *_args: pytest.fail("mismatch claimed")
    )
    assert (
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            materializer=materialize,
        )
        is None
    )
    status = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", "24b00003")
    )
    assert status["state"] == "blocked"
    assert status["attempt"] == 0
    assert status["claim_released"] is False


def test_reoffer_after_source_amendment_mints_a_new_bound_request(
    paths, operator, noded41
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    first = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    amended = _card()
    amended["meta"]["base_ref"] = "release"
    second = builder_dispatch.offer(paths, amended, ["sk-m", "source-only"], writer=writer)

    assert second["request_id"] != first["request_id"]
    assert second["base_ref"] == "release"
    assert (
        builder_dispatch._load(builder_dispatch.request_path(paths, "node-ziowk01", "24b00003"))
        == second
    )


@pytest.mark.parametrize("release_result", [True, False])
def test_changed_offer_releases_prior_exact_claim_without_launch(
    paths, operator, noded41, monkeypatch, tmp_path, release_result
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    first = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        first,
        "blocked",
        owner="prior-owner",
        claim_revision="prior-revision",
        claim_released=False,
        attempt=0,
    )
    amended = _card()
    amended["meta"]["base_ref"] = "release"
    second = builder_dispatch.offer(paths, amended, ["sk-m", "source-only"], writer=writer)
    folded = _folded(
        owner="prior-owner", meta=dict(amended["meta"], _claim_revision="prior-revision")
    )
    releases = []

    def release(_self, owner, card_id, **kwargs):
        releases.append((owner, card_id, kwargs["expected_claim_revision"]))
        if release_result:
            folded.owner = None
        return release_result

    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch.Board, "release_claim", release)
    monkeypatch.setattr(
        builder_dispatch.Board,
        "claim_task",
        lambda *_args: pytest.fail("replacement offer was claimed"),
    )
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("replacement offer launched"),
        materializer=lambda *_args: pytest.fail("replacement offer materialized"),
    )

    assert releases == [("prior-owner", "24b00003", "prior-revision")]
    assert result["request_id"] == second["request_id"]
    assert result["state"] == "blocked"
    assert result["claim_released"] is release_result


def test_amendment_during_claim_releases_generation_without_launch(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()
    releases = []

    def claim(_self, owner, _card_id):
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="amended-generation")
        folded.labels.append("codex-only")

    def release(_self, owner, card_id, **kwargs):
        releases.append((owner, card_id, kwargs["expected_claim_revision"]))
        folded.owner = None
        return True

    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.Board, "release_claim", release)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    assert (
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=lambda *_args: pytest.fail("amended generation launched"),
            materializer=lambda _request, workspace: workspace,
        )
        is None
    )

    status = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", request["card_id"])
    )
    assert releases == [
        (
            "pi-builder-standby-node-ziowk01-24b00003",
            "24b00003",
            "amended-generation",
        )
    ]
    assert status["state"] == "blocked"
    assert status["claim_released"] is True
    assert status["attempt"] == 0


def test_duplicate_node_daemons_share_one_request_generation(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()
    entered = Event()
    resume = Event()
    guard = Lock()
    calls = {"materialize": 0, "claim": 0, "launch": 0}

    def materialize(_request, workspace):
        with guard:
            calls["materialize"] += 1
        entered.set()
        assert resume.wait(2)
        return workspace

    def claim(_self, owner, _card_id):
        with guard:
            calls["claim"] += 1
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="one-generation")

    def launch(_command, _workspace):
        with guard:
            calls["launch"] += 1
        return SimpleNamespace(pid=os.getpid(), poll=lambda: None)

    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            builder_dispatch.consume_one,
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=launch,
            materializer=materialize,
        )
        assert entered.wait(2)
        second = pool.submit(
            builder_dispatch.consume_one,
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=launch,
            materializer=materialize,
        )
        time.sleep(0.05)
        resume.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert calls == {"materialize": 1, "claim": 1, "launch": 1}
    assert {result["state"] for result in results} == {"running"}
    assert {result["claim_revision"] for result in results} == {"one-generation"}


def test_consumer_claims_exact_generation_and_reports_running(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    folded = _folded(links={"verdict": "PASS"})

    def claim(_self, owner, card_id):
        assert card_id == "24b00003"
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="claim-1")

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda _self, _card: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *args, **kwargs: True)
    calls = []
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda command, cwd: (
            calls.append((command, cwd)) or SimpleNamespace(pid=42, poll=lambda: None)
        ),
        materializer=lambda _request, workspace: workspace,
    )
    assert result["request_id"] == request["request_id"]
    assert result["claim_revision"] == "claim-1"
    assert result["state"] == "running"
    assert calls[0][0][calls[0][0].index("--provider") + 1] == "skgateway"
    again = builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01")
    assert again["state"] == "running"
    assert again["liveness"] == "live"


def test_freeze_blocks_offer_and_consume_before_side_effects(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    assert request is not None
    store.set_frozen(paths, True, writer=operator, reason="test")
    assert builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer) is None
    monkeypatch.setattr(
        builder_dispatch,
        "materialize_source",
        lambda *_args: pytest.fail("frozen consumer materialized source"),
    )
    assert builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01") is None


def test_freeze_during_materialization_prevents_claim_and_remains_retryable(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    store.set_frozen(paths, False, writer=operator, reason="test setup")
    folded = _folded()
    claims = []

    def materialize_then_freeze(_request, workspace):
        store.set_frozen(paths, True, writer=operator, reason="synchronized test")
        return workspace

    monkeypatch.setattr(
        builder_dispatch.Board,
        "claim_task",
        lambda *_args: claims.append("unexpected"),
    )
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("frozen request launched"),
        materializer=materialize_then_freeze,
    )
    assert result["state"] == "frozen"
    assert result["attempt"] == 0
    assert claims == []
    store.set_frozen(paths, False, writer=operator, reason="resume test")

    def claim(_self, owner, _card_id):
        claims.append(owner)
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="claim-after-freeze")

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    resumed = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: SimpleNamespace(pid=46, poll=lambda: None),
        materializer=lambda _request, workspace: workspace,
    )
    assert resumed["state"] == "running"
    assert resumed["attempt"] == 1
    assert len(claims) == 1


def test_freeze_after_claim_releases_generation_and_prevents_launch(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()
    claims = []
    releases = []

    def claim(_self, owner, _card_id):
        claims.append(owner)
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision=f"claim-{len(claims)}")
        if len(claims) == 1:
            store.set_frozen(paths, True, writer=operator, reason="synchronized test")

    def release(_self, owner, _card_id, **kwargs):
        releases.append((owner, kwargs["expected_claim_revision"]))
        folded.owner = None
        folded.meta = dict(_card()["meta"])
        return True

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.Board, "release_claim", release)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    launches = []
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: launches.append(True),
        materializer=lambda _request, workspace: workspace,
    )
    assert result["state"] == "frozen"
    assert result["claim_released"] is True
    assert releases == [(claims[0], "claim-1")]
    assert launches == []
    store.set_frozen(paths, False, writer=operator, reason="resume test")
    resumed = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: SimpleNamespace(pid=47, poll=lambda: None),
        materializer=lambda _request, workspace: workspace,
    )
    assert resumed["state"] == "running"
    assert resumed["attempt"] == 1
    assert len(claims) == 2


def test_freeze_winning_atomic_exclusion_prevents_process_creation(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    store.set_frozen(paths, False, writer=operator, reason="test setup")
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()
    releases = []

    def claim(_self, owner, _card_id):
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="atomic-claim")

    def release(_self, owner, _card_id, **kwargs):
        releases.append((owner, kwargs["expected_claim_revision"]))
        folded.owner = None
        return True

    @contextmanager
    def freeze_wins(_paths):
        payload = json.loads(paths.freeze_path().read_text(encoding="utf-8"))
        payload["frozen"] = True
        store._dump(paths.freeze_path(), payload)
        yield

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.Board, "release_claim", release)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(builder_dispatch.store, "actuation_exclusion", freeze_wins)
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("frozen process created"),
        materializer=lambda _request, workspace: workspace,
    )
    assert result["state"] == "frozen"
    assert result["attempt"] == 0
    assert releases == [(result["owner"], "atomic-claim")]


def test_live_old_worker_refreshes_and_cannot_be_reaped(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()

    def claim(_self, owner, _card_id):
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="claim-live")

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    process = SimpleNamespace(pid=42, poll=lambda: None)
    status = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: process,
        materializer=lambda _request, workspace: workspace,
    )
    status["heartbeat_at"] = "2026-09-10T00:00:00Z"
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "running",
        **{
            key: value
            for key, value in status.items()
            if key not in {"schema", "request_id", "card_id", "node", "state", "heartbeat_at"}
        },
    )
    assert not builder_dispatch.recover_stale(
        paths,
        tmp_path,
        "node-ziowk01",
        "24b00003",
        now=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    refreshed = builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01")
    assert refreshed["state"] == "running"
    assert refreshed["liveness"] == "live"


def test_dead_worker_recovery_releases_only_matching_generation(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    status = builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "running",
        owner="owner",
        claim_revision="claim-dead",
        pid=999999999,
        pid_start_ticks="123",
        attempt=1,
    )
    status["heartbeat_at"] = "2026-09-10T00:00:00Z"
    builder_dispatch.atomic_write_text(
        builder_dispatch.status_path(paths, "node-ziowk01", "24b00003"),
        builder_dispatch.json.dumps(status, sort_keys=True) + "\n",
    )
    folded = SimpleNamespace(owner="owner", meta={"_claim_revision": "claim-dead"})
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    releases = []

    def release(_self, owner, card_id, **kwargs):
        releases.append((owner, card_id, kwargs))
        return True

    monkeypatch.setattr(builder_dispatch.Board, "release_claim", release)
    assert builder_dispatch.recover_stale(
        paths,
        tmp_path,
        "node-ziowk01",
        "24b00003",
        now=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    assert releases == [
        (
            "owner",
            "24b00003",
            {"actor": "niobe", "expected_claim_revision": "claim-dead"},
        )
    ]
    assert (
        builder_dispatch._load(builder_dispatch.status_path(paths, "node-ziowk01", "24b00003"))[
            "state"
        ]
        == "stale"
    )


def test_terminal_request_does_not_starve_next_request(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    first = _card() | {"id": "10000001"}
    first_request = builder_dispatch.offer(paths, first, ["sk-m", "source-only"], writer=writer)
    builder_dispatch._write_status(paths, "node-ziowk01", first_request, "completed")
    second = _card() | {"id": "20000002"}
    second_request = builder_dispatch.offer(paths, second, ["sk-m", "source-only"], writer=writer)
    folded = _folded(id="20000002")

    def claim(_self, owner, card_id):
        assert card_id == "20000002"
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="claim-next")

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: SimpleNamespace(pid=43, poll=lambda: None),
        materializer=lambda _request, workspace: workspace,
    )
    assert result["request_id"] == second_request["request_id"]


def test_reconstruction_failure_records_retry_and_continues_queue(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    first = _card() | {"id": "10000001"}
    first_request = builder_dispatch.offer(paths, first, ["sk-m", "source-only"], writer=writer)
    second = _card() | {"id": "20000002"}
    second_request = builder_dispatch.offer(paths, second, ["sk-m", "source-only"], writer=writer)
    folded = {"10000001": _folded(id="10000001"), "20000002": _folded(id="20000002")}

    def claim(_self, owner, card_id):
        folded[card_id].owner = owner
        folded[card_id].meta = dict(_card()["meta"], _claim_revision="claim-next")

    def materialize(request, workspace):
        if request["card_id"] == "10000001":
            raise builder_dispatch.BuilderDispatchError("exact source reconstruction failed")
        return workspace

    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda _self, card_id: folded[card_id])
    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: SimpleNamespace(pid=43, poll=lambda: None),
        materializer=materialize,
    )

    first_status = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", first_request["card_id"])
    )
    assert first_status["request_id"] == first_request["request_id"]
    assert first_status["state"] == "failed"
    assert first_status["attempt"] == 1
    assert first_status["retryable"] is True
    assert first_status["claim_released"] is False
    assert first_status["error"] == "exact source reconstruction failed"
    assert result["request_id"] == second_request["request_id"]
    assert result["state"] == "running"

    builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01", materializer=materialize)
    exhausted = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", first_request["card_id"])
    )
    assert exhausted["attempt"] == 2
    assert exhausted["retryable"] is False


def test_unexpected_materializer_failure_still_escapes(paths, operator, noded41, tmp_path) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )

    with pytest.raises(RuntimeError, match="unexpected materializer failure"):
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            materializer=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("unexpected materializer failure")
            ),
        )


def test_launch_failure_releases_exact_claim_and_retries_once(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()
    claims = []

    def claim(_self, owner, _card_id):
        claims.append(owner)
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision=f"claim-{len(claims)}")

    def release(_self, owner, _card_id, **kwargs):
        assert kwargs["expected_claim_revision"] == folded.meta["_claim_revision"]
        assert owner == folded.owner
        folded.owner = None
        folded.meta = dict(_card()["meta"])
        return True

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.Board, "release_claim", release)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(builder_dispatch, "_send_status", lambda *_args: True)
    with pytest.raises(RuntimeError, match="launch failed"):
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=lambda *_args: (_ for _ in ()).throw(RuntimeError("launch failed")),
            materializer=lambda _request, workspace: workspace,
        )
    failed = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", "24b00003")
    )
    assert failed["state"] == "failed"
    assert failed["claim_released"] is True
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: SimpleNamespace(pid=44, poll=lambda: None),
        materializer=lambda _request, workspace: workspace,
    )
    assert result["attempt"] == 2
    assert result["claim_revision"] == "claim-2"
    process = builder_dispatch._PROCESSES[result["request_id"]]
    process.poll = lambda: 1
    builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01")
    # A launch that never produced a verdict is infrastructure, not a finding
    # about the card, so the spent attempts buy a fresh generation rather than
    # a permanent refusal.
    reoffer = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    assert reoffer is not None and reoffer["generation"] == 1


def test_supervisor_records_completion_and_sends_mail(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded(links={"verdict": "PASS"})

    def claim(_self, owner, _card_id):
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="claim-complete")

    process = SimpleNamespace(pid=45, poll=lambda: None)
    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    sent = []
    monkeypatch.setattr(
        builder_dispatch, "_send_status", lambda owner, request, state: sent.append(state) or True
    )
    builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: process,
        materializer=lambda _request, workspace: workspace,
    )
    process.poll = lambda: 0
    folded.status = SimpleNamespace(value="done")
    result = builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01")
    assert result["state"] == "completed"
    assert result["exit_code"] == 0
    assert result["mail_sent"] is True
    assert result["completion"]["verdict"] == "PASS"
    assert sent == ["completed"]


def test_decline_reason_names_exhausted_attempts(paths, operator, noded41) -> None:
    """A card whose retries are spent must say so instead of a silent None."""
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "failed",
        attempt=builder_dispatch.MAX_ATTEMPTS,
        completion={"verdict": "blocked", "detail": "the worker reached a finding"},
    )
    assert builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer) is None
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason is not None
    assert "node-ziowk01" in reason
    assert "failed" in reason
    assert "worked=True" in reason


def test_decline_reason_names_missing_ready_builder(paths) -> None:
    """An empty or role-less registry is a named decline, not silence."""
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason == "no-ready-builder"


def test_decline_reason_names_ineligible_and_invalid_source(paths, operator, noded41) -> None:
    _node(paths, operator, noded41)
    assert builder_dispatch.decline_reason(paths, _card(), ["source-only"]) == "ineligible"
    bad = _card()
    bad["meta"] = dict(bad["meta"], repository="http://github.com/smilinTux/skcapstone.git")
    reason = builder_dispatch.decline_reason(paths, bad, ["sk-m", "source-only"])
    assert reason is not None
    assert reason.startswith("invalid-source")


def test_decline_reason_is_none_when_offer_would_place(paths, operator, noded41) -> None:
    """decline_reason must never contradict offer(): offerable means None."""
    _node(paths, operator, noded41)
    assert builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"]) is None
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    assert request is not None
    assert builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"]) is None


def test_unworked_terminal_status_is_forgiven_with_a_fresh_generation(
    paths, operator, noded41
) -> None:
    """An offer that expired unconsumed proves nothing, so it must not park the card."""
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "blocked",
        attempt=0,
        claim_released=False,
        error="unclaimed offer expired",
    )
    assert builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"]) is None
    reoffer = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    assert reoffer is not None
    assert reoffer["generation"] == 1
    assert reoffer["request_id"] != request["request_id"]
    assert reoffer["lease_expires_at"] >= request["lease_expires_at"]


def test_generation_zero_keeps_the_established_request_identity(paths, operator, noded41) -> None:
    """Forgiveness must not re-address every live request and orphan its worker."""
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    assert request["generation"] == 0
    legacy_identity = json.dumps(
        [
            "24b00003",
            "node-ziowk01",
            _card()["meta"]["repository"],
            _card()["meta"]["base_ref"],
            _card()["meta"]["base_revision"],
            ["sk-m", "source-only"],
        ],
        separators=(",", ":"),
    )
    assert request["request_id"] == hashlib.sha256(legacy_identity.encode()).hexdigest()


def test_worker_verdict_charges_the_attempt_budget_and_parks_at_the_ceiling(
    paths, operator, noded41
) -> None:
    """A worker that actually reached a verdict twice has spent the card's attempts."""
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "failed",
        attempt=builder_dispatch.MAX_ATTEMPTS,
        completion={"verdict": "blocked", "detail": "the card cannot be built"},
    )
    assert builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer) is None
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason is not None and reason.startswith("parked:")


def test_unworked_forgiveness_is_bounded_so_an_unbuildable_card_stops(
    paths, operator, noded41
) -> None:
    """A card that never reaches a verdict still stops, at the generation ceiling."""
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    for generation in range(1, builder_dispatch.MAX_GENERATIONS + 2):
        builder_dispatch._write_status(
            paths,
            "node-ziowk01",
            request,
            "failed",
            attempt=builder_dispatch.MAX_ATTEMPTS,
            error="exact source reconstruction failed",
        )
        request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
        if request is None:
            break
        assert request["generation"] == generation
    assert request is None
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason is not None and reason.startswith("parked:")


def test_released_prior_claim_is_not_released_again_and_the_card_launches(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    """A forgiven generation whose prior claim is already released must reach a worker."""
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "blocked",
        attempt=1,
        owner="pi-builder-standby-node-ziowk01-24b00003",
        claim_revision="claim-old",
        claim_released=True,
    )
    reoffer = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    assert reoffer is not None and reoffer["generation"] == 1
    folded = _folded()

    def claim(_self, owner, card_id):
        folded.owner = owner
        folded.meta = dict(_card()["meta"], _claim_revision="claim-new")

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        builder_dispatch,
        "_release_exact",
        lambda *_args, **_kwargs: pytest.fail("released an already-released claim"),
    )
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: SimpleNamespace(pid=71, poll=lambda: None),
        materializer=lambda _request, workspace: workspace,
    )
    assert result is not None and result["state"] == "running"
    assert result["request_id"] == reoffer["request_id"]


def test_a_voided_card_parks_but_an_infrastructure_block_does_not(
    paths, operator, noded41
) -> None:
    """Both are blocked at attempt 0; only one of them is about the card."""
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "blocked",
        attempt=0,
        error="unclaimable: card 24b00003 was voided",
        unclaimable_reason="voided",
    )
    assert builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer) is None
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "blocked",
        attempt=0,
        error="unclaimed offer expired",
    )
    assert (
        builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer) is not None
    )
