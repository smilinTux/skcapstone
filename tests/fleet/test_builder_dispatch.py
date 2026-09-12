"""Governed ZIOWK01 builder dispatch integration."""

from __future__ import annotations

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
    with pytest.raises(builder_dispatch.BuilderDispatchError, match="changed after dispatch"):
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            materializer=lambda *_args: pytest.fail("amended source was materialized"),
        )


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
    with pytest.raises(builder_dispatch.BuilderDispatchError, match="changed after dispatch"):
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=lambda *_args: pytest.fail("amended generation launched"),
            materializer=lambda _request, workspace: workspace,
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
    assert (
        builder_dispatch.offer(
            paths,
            _card(),
            ["sk-m", "source-only"],
            writer=store.Writer(role="scheduler", node="niobe", identity=""),
        )
        is None
    )


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
