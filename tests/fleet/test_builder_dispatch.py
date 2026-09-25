"""Governed ZIOWK01 builder dispatch integration."""

from __future__ import annotations

import json
import os
import subprocess
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
    # Runtime observation is separately tested with synthetic service/cgroup
    # readers. No integration fixture may query this machine's systemd.
    monkeypatch.setattr(
        builder_dispatch.builder_execution,
        "observe",
        lambda *_args, **_kwargs: builder_dispatch.builder_execution.ExecutionObservation(None),
    )
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


def _execution_observation(monkeypatch, alive, exit_code=None):
    monkeypatch.setattr(
        builder_dispatch.builder_execution,
        "observe",
        lambda *_args, **_kwargs: builder_dispatch.builder_execution.ExecutionObservation(
            alive, exit_code
        ),
    )


@pytest.fixture
def clean_attempt(paths, operator, noded41, monkeypatch, tmp_path):
    """Run the lifecycle against real isolated Git, without any live service."""
    _node(paths, operator, noded41)
    seed = tmp_path / "seed"
    seed.mkdir()

    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    git(seed, "init", "--quiet")
    git(seed, "config", "user.name", "Synthetic Worker")
    git(seed, "config", "user.email", "worker@example.invalid")
    (seed / "code.py").write_text("initial = True\n")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "base")
    core = _card()
    core["meta"]["base_revision"] = git(seed, "rev-parse", "HEAD")
    request = builder_dispatch.offer(
        paths,
        core,
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded(meta=dict(core["meta"]))
    claims, releases, launches = [], [], []

    def claim(_self, owner, _card_id):
        claims.append(owner)
        folded.owner = owner
        folded.meta = dict(core["meta"], _claim_revision=f"claim-{len(claims)}")

    def release(_self, owner, card_id, **kwargs):
        assert owner == folded.owner
        assert kwargs["expected_claim_revision"] == folded.meta["_claim_revision"]
        # Custody must be durable before the original writer can be retired.
        assert list((paths.root / "recovery" / request["request_id"]).glob("*.json"))
        releases.append((owner, kwargs["expected_claim_revision"]))
        folded.owner = None
        folded.meta = dict(core["meta"])
        return True

    def materialize(_request, workspace):
        workspace.parent.mkdir(parents=True, exist_ok=True)
        git(seed, "clone", "--quiet", "--no-local", str(seed), str(workspace))
        git(workspace, "remote", "set-url", "origin", request["repository"])
        return workspace

    def launch(command, workspace):
        process = SimpleNamespace(pid=100 + len(launches), poll=lambda: None)
        launches.append((workspace, process))
        return process

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.Board, "release_claim", release)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(builder_dispatch, "_send_status", lambda *_args: True)
    status = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=launch,
        materializer=materialize,
    )
    workspace, process = launches[0]
    git(workspace, "config", "user.name", "Synthetic Worker")
    git(workspace, "config", "user.email", "worker@example.invalid")
    (workspace / "code.py").write_text("completed_progress = True\n")
    git(workspace, "commit", "--quiet", "-am", "preserved progress")
    return SimpleNamespace(
        request=request,
        status=status,
        workspace=workspace,
        process=process,
        folded=folded,
        claims=claims,
        releases=releases,
        launches=launches,
        launch=launch,
        git=git,
    )


def test_confirmed_dead_attempt_preserves_commit_and_restarts_in_fresh_workspace(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
):
    attempt = clean_attempt
    original_head = attempt.git(attempt.workspace, "rev-parse", "HEAD")
    attempt.process.poll = lambda: 1
    _execution_observation(monkeypatch, False, 1)
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=attempt.launch,
        materializer=lambda *_args: pytest.fail("continuation reset to original base"),
    )
    assert result["attempt"] == 2 and result["claim_revision"] == "claim-2"
    assert len(attempt.releases) == 1 and len(attempt.claims) == 2
    resumed = attempt.launches[1][0]
    assert resumed != attempt.workspace
    assert result["owner"].endswith("-attempt-2")
    assert attempt.git(resumed, "rev-parse", "HEAD") == original_head
    assert attempt.git(attempt.workspace, "rev-parse", "HEAD") == original_head
    assert attempt.git(attempt.workspace, "status", "--porcelain") == ""


@pytest.mark.parametrize("proof", [None, True, "foreign"])
def test_unproven_whole_execution_never_releases_or_retries(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
    proof,
):
    attempt = clean_attempt
    attempt.process.poll = lambda: 1
    _execution_observation(monkeypatch, proof if proof != "foreign" else False, 1)
    if proof == "foreign":
        attempt.status["execution_host"] = "another-host"
        builder_dispatch.atomic_write_text(
            builder_dispatch.status_path(paths, "node-ziowk01", attempt.request["card_id"]),
            json.dumps(attempt.status) + "\n",
        )
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("relaunched"),
    )
    assert result["state"] == "running"
    assert attempt.releases == [] and len(attempt.claims) == 1


def test_dead_dirty_attempt_is_preserved_without_claim_release(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
):
    attempt = clean_attempt
    (attempt.workspace / "unfinished.py").write_text("unfinished = True\n")
    attempt.process.poll = lambda: 1
    _execution_observation(monkeypatch, False, 1)
    result = builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01")
    assert result["state"] == "custody-held"
    assert attempt.releases == [] and len(attempt.claims) == 1
    assert (attempt.workspace / "unfinished.py").read_text() == "unfinished = True\n"


def test_dead_old_generation_cannot_release_a_replacement_claim(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
):
    attempt = clean_attempt
    attempt.process.poll = lambda: 1
    _execution_observation(monkeypatch, False, 1)
    attempt.folded.meta["_claim_revision"] = "replacement-claim"
    result = builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01")
    assert result["state"] == "blocked" and result["claim_released"] is False
    assert attempt.releases == []


def test_confirmed_recovery_is_bounded_to_configured_attempt_limit(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
):
    attempt = clean_attempt
    _execution_observation(monkeypatch, False, 1)
    attempt.process.poll = lambda: 1
    builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01", launcher=attempt.launch)
    attempt.launches[1][1].poll = lambda: 1
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("third attempt"),
    )
    assert result["state"] == "failed" and result["attempt"] == builder_dispatch.MAX_ATTEMPTS
    assert len(attempt.claims) == len(attempt.releases) == 2


def test_prior_checkpoint_claim_mismatch_never_reconstructs_or_claims(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
):
    attempt = clean_attempt
    attempt.process.poll = lambda: 1
    _execution_observation(monkeypatch, False, 1)
    failed = builder_dispatch._reconcile_running(
        paths,
        tmp_path,
        "node-ziowk01",
        attempt.request,
        attempt.status,
    )
    failed["checkpoint"]["claim_revision"] = "unrelated-claim"
    builder_dispatch.atomic_write_text(
        builder_dispatch.status_path(paths, "node-ziowk01", attempt.request["card_id"]),
        json.dumps(failed) + "\n",
    )
    builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("relaunched"),
    )
    assert len(attempt.claims) == 1
    assert not (paths.root / "workspaces" / (attempt.status["owner"] + "-attempt-2")).exists()


def test_recovery_rechecks_extinction_after_sealing_before_release(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
):
    attempt = clean_attempt
    attempt.process.poll = lambda: 1
    observations = iter([False, True])
    monkeypatch.setattr(
        builder_dispatch.builder_execution,
        "observe",
        lambda *_args, **_kwargs: builder_dispatch.builder_execution.ExecutionObservation(
            next(observations), 1
        ),
    )
    result = builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01")
    assert result["state"] == "custody-held"
    assert result["checkpoint"]["head"] == attempt.git(attempt.workspace, "rev-parse", "HEAD")
    assert attempt.releases == []


def test_paused_stale_recovery_never_probes_or_releases(
    clean_attempt,
    paths,
    tmp_path,
    monkeypatch,
    operator,
):
    attempt = clean_attempt
    store.set_frozen(paths, True, writer=operator, reason="user pause")
    monkeypatch.setattr(
        builder_dispatch, "_process_state", lambda *_args: pytest.fail("probed while paused")
    )
    assert not builder_dispatch.recover_stale(
        paths,
        tmp_path,
        "node-ziowk01",
        attempt.request["card_id"],
        now=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    assert attempt.releases == []


def test_crash_after_launch_before_running_status_preserves_discoverable_intent(
    paths,
    operator,
    noded41,
    monkeypatch,
    tmp_path,
):
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity=""),
    )
    folded = _folded()
    launches = []

    def claim(_self, owner, _card):
        folded.owner = owner
        folded.meta = dict(_card_meta, _claim_revision="crash-claim")

    _card_meta = dict(_card()["meta"])
    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_args, **_kwargs: True)
    original_write = builder_dispatch._write_status

    def interrupted_write(paths, node, request, state, **values):
        if state == "running":
            raise OSError("synthetic interrupted status write")
        return original_write(paths, node, request, state, **values)

    monkeypatch.setattr(builder_dispatch, "_write_status", interrupted_write)
    with pytest.raises(OSError, match="interrupted status"):
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=lambda *_args: (
                launches.append(True) or SimpleNamespace(pid=42, poll=lambda: None)
            ),
            materializer=lambda _request, workspace: workspace,
        )
    intent = builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", request["card_id"])
    )
    assert intent["state"] == "launching" and intent["execution_unit"]
    assert intent["workspace"] and intent["claim_revision"] == "crash-claim"
    builder_dispatch._PROCESSES.clear()
    monkeypatch.setattr(builder_dispatch, "_write_status", original_write)
    _execution_observation(monkeypatch, True)
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("duplicate launched"),
    )
    assert result["state"] == "running" and result["liveness"] == "live"
    assert len(launches) == 1


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
    builder_dispatch._PROCESSES.clear()  # daemon restart: exact unit remains observable
    _execution_observation(monkeypatch, True)
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
        **builder_dispatch.builder_execution.identity(first, "first-claim"),
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
    _execution_observation(monkeypatch, True)
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
        )["owner"]
        == "prior-owner"
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
    _execution_observation(monkeypatch, False, 0)
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


@pytest.mark.parametrize(
    ("restriction", "expected"),
    [
        ("kimi", False),
        ("kimi-only", False),
        ("kimi-suitable", False),
        ("kimi-lane", False),
        (None, True),
    ],
)
def test_kimi_restrictions_are_ineligible_for_neutral_builder(restriction, expected) -> None:
    labels = ["sk-m", "source-only"]
    if restriction is not None:
        labels.append(restriction)
    assert builder_dispatch.eligible(_card(), labels) is expected


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
        launcher=lambda *_args: launches.append(1) or SimpleNamespace(pid=43, poll=lambda: None),
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


@pytest.mark.parametrize("state", ["running", "launching", "custody-held"])
@pytest.mark.parametrize("same_generation", [True, False])
def test_changed_offer_cannot_replace_active_execution_custody(
    paths,
    operator,
    noded41,
    state,
    same_generation,
):
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    prior_request = request if same_generation else dict(request, request_id="older-execution")
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        prior_request,
        state,
        owner="prior-owner",
        claim_revision="prior-revision",
        workspace="/preserved/source",
    )
    request_path = builder_dispatch.request_path(paths, "node-ziowk01", request["card_id"])
    status_path = builder_dispatch.status_path(paths, "node-ziowk01", request["card_id"])
    before = (request_path.read_bytes(), status_path.read_bytes())
    amended = _card()
    amended["meta"]["base_ref"] = "amended"
    reason = builder_dispatch.decline_reason(paths, amended, ["sk-m", "source-only"])
    assert reason == f"superseded-binding-{state}: node=node-ziowk01"
    assert builder_dispatch.offer(paths, amended, ["sk-m", "source-only"], writer=writer) is None
    assert (request_path.read_bytes(), status_path.read_bytes()) == before
    assert builder_dispatch.request_holds_card(
        request,
        builder_dispatch._load(status_path),
        datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("state", ["launching", "custody-held"])
def test_new_execution_states_reserve_capacity_and_preserve_idempotent_offer(
    paths,
    operator,
    noded41,
    state,
):
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    for number in range(builder_dispatch.BUILDER_CAPACITY):
        core = _card() | {"id": f"24b000{number:02d}"}
        request = builder_dispatch.offer(paths, core, ["sk-m", "source-only"], writer=writer)
        builder_dispatch._write_status(paths, "node-ziowk01", request, state, owner="retained")
        assert (
            builder_dispatch.offer(paths, core, ["sk-m", "source-only"], writer=writer) == request
        )
    fifth = _card() | {"id": "24b00009"}
    assert builder_dispatch._node_load(paths, "node-ziowk01") == builder_dispatch.BUILDER_CAPACITY
    assert builder_dispatch.decline_reason(paths, fifth, ["sk-m", "source-only"]).startswith(
        "builders-at-capacity:"
    )
    assert builder_dispatch.offer(paths, fifth, ["sk-m", "source-only"], writer=writer) is None


@pytest.mark.parametrize("release_result", [True, False])
def test_changed_offer_preserves_prior_claim_without_execution_proof(
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

    assert releases == []
    assert result["request_id"] == first["request_id"] != second["request_id"]
    assert result["state"] == "blocked"
    assert result["claim_released"] is False


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


def test_legacy_pid_recovery_does_not_release_without_execution_proof(
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
    assert not builder_dispatch.recover_stale(
        paths,
        tmp_path,
        "node-ziowk01",
        "24b00003",
        now=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    assert releases == []
    assert (
        builder_dispatch._load(builder_dispatch.status_path(paths, "node-ziowk01", "24b00003"))[
            "state"
        ]
        == "running"
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


def test_ambiguous_launch_failure_holds_exact_claim_without_retry(
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
    assert failed["state"] == "launching"
    assert failed["claim_released"] is False
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_args: pytest.fail("uncertain unit relaunched"),
        materializer=lambda _request, workspace: workspace,
    )
    assert result["attempt"] == 1
    assert result["claim_revision"] == "claim-1"
    assert result["liveness"] == "unknown"
    assert len(claims) == 1


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
    _execution_observation(monkeypatch, False, 0)
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
    )
    assert builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer) is None
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason is not None
    assert "node-ziowk01" in reason
    assert "failed" in reason


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


def _links_only_card() -> dict:
    card = _card()
    meta = card.pop("meta")
    card["links"] = dict(meta)
    return card


def test_links_only_source_binding_dispatches(paths, operator, noded41) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    request = builder_dispatch.offer(
        paths,
        _links_only_card(),
        ["sk-m", "source-only"],
        writer=writer,
        now=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    assert request["repository"] == "https://github.com/smilinTux/skcapstone.git"
    assert request["base_ref"] == "main"
    assert request["base_revision"] == "9cc415465d6bacc22b51b09a3c861c61f0823d45"


def test_dual_matching_sources_coalesce(paths, operator, noded41) -> None:
    _node(paths, operator, noded41)
    card = _card()
    card["links"] = dict(card["meta"])
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    request = builder_dispatch.offer(paths, card, ["sk-m", "source-only"], writer=writer)
    assert request is not None
    assert request["repository"] == card["meta"]["repository"]
    assert request["base_ref"] == card["meta"]["base_ref"]
    assert request["base_revision"] == card["meta"]["base_revision"]


@pytest.mark.parametrize(
    "field",
    ["repository", "base_ref", "base_revision"],
)
def test_conflicting_sources_fail_closed(paths, operator, noded41, field) -> None:
    _node(paths, operator, noded41)
    card = _card()
    card["links"] = dict(card["meta"])
    if field == "repository":
        card["links"]["repository"] = "https://github.com/smilinTux/skcoord.git"
    elif field == "base_ref":
        card["links"]["base_ref"] = "develop"
    else:
        card["links"]["base_revision"] = "f" * 40
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    with pytest.raises(builder_dispatch.BuilderDispatchError) as exc_info:
        builder_dispatch.offer(paths, card, ["sk-m", "source-only"], writer=writer)
    assert str(exc_info.value) == f"source binding conflict: {field}"


def test_decline_reason_invalid_source_conflict(paths, operator, noded41) -> None:
    _node(paths, operator, noded41)
    card = _card()
    card["links"] = dict(card["meta"])
    card["links"]["repository"] = "https://github.com/smilinTux/skcoord.git"
    reason = builder_dispatch.decline_reason(paths, card, ["sk-m", "source-only"])
    assert reason is not None
    assert reason.startswith("invalid-source")
    assert "source binding conflict: repository" in reason


@pytest.mark.parametrize("container", ["meta", "links"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", 123),
        ("base_ref", 123),
        ("base_revision", int("1" * 40)),
    ],
)
def test_non_string_source_binding_is_rejected(
    paths, operator, noded41, container, field, value
) -> None:
    _node(paths, operator, noded41)
    card = _card()
    card.setdefault("links", {})
    card[container][field] = value
    reason = builder_dispatch.decline_reason(paths, card, ["sk-m", "source-only"])
    assert reason == f"invalid-source: {field} must be a string"


def test_link_amendment_after_offer_blocks_materialization_and_claim(
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
    amended.links["repository"] = "https://github.com/smilinTux/skcoord.git"
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
    assert status["error"] == "source binding conflict: repository"


def test_legacy_link_review_work_workspace_link_shape_dispatches(paths, operator, noded41) -> None:
    _node(paths, operator, noded41)
    card = _card()
    meta = card.pop("meta")
    card["links"] = {
        "repository": meta["repository"],
        "base_ref": meta["base_ref"],
        "base_revision": meta["base_revision"],
    }
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    request = builder_dispatch.offer(paths, card, ["sk-m", "source-only"], writer=writer)
    assert request is not None
    assert request["repository"] == meta["repository"]
    assert request["base_ref"] == meta["base_ref"]
    assert request["base_revision"] == meta["base_revision"]


def test_link_source_card_and_head_revision_are_not_aliased(paths, operator, noded41) -> None:
    """link_source_card / link_head_revision must never fill the binding fields."""
    _node(paths, operator, noded41)
    card = _card()
    meta = card["meta"]
    card["links"] = {
        "link_source_card": "abc123",
        "link_head_revision": "0000000000000000000000000000000000000009",
    }
    # Without the canonical link keys, meta must still drive the binding.
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    request = builder_dispatch.offer(paths, card, ["sk-m", "source-only"], writer=writer)
    assert request is not None
    assert request["repository"] == meta["repository"]
    assert request["base_ref"] == meta["base_ref"]
    assert request["base_revision"] == meta["base_revision"]
