"""Governed ZIOWK01 builder dispatch integration."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch, sknoded, store


@pytest.fixture(autouse=True)
def _clear_process_registry():
    builder_dispatch._PROCESSES.clear()
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
    assert store.read_placement(paths, "job", "24b00003")["node"] == "node-ziowk01"
    repeated = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    assert repeated == request


def test_offer_rejects_wrong_scheduler_and_lane_pins(paths) -> None:
    wrong = store.Writer(role="scheduler", node="atlas", identity="")
    try:
        builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=wrong)
    except builder_dispatch.BuilderDispatchError as exc:
        assert "Niobe" in str(exc)
    else:
        raise AssertionError("non-Niobe scheduler was accepted")
    assert not builder_dispatch.eligible(_card(), ["sk-m", "source-only", "codex-only"])


def test_worker_command_is_pi_through_gateway_only() -> None:
    command = builder_dispatch.worker_command(
        _card() | {"card_id": "24b00003"}, "owner", "rev", "/tmp/work"
    )
    assert "--provider" in command
    assert command[command.index("--provider") + 1] == "skgateway"
    assert "codex" not in " ".join(command).lower()
    assert "openai.com" not in " ".join(command).lower()
    assert "SKFLEET_CLAIM_REVISION=rev" in command


def test_consumer_claims_exact_generation_and_reports_running(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    _node(paths, operator, noded41)
    writer = store.Writer(role="scheduler", node="niobe", identity="")
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=writer)
    folded = SimpleNamespace(
        owner=None, meta={}, status=SimpleNamespace(value="doing"), links={"verdict": "PASS"}
    )

    def claim(_self, owner, card_id):
        assert card_id == "24b00003"
        folded.owner = owner
        folded.meta = {"_claim_revision": "claim-1"}

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", claim)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda _self, _card: folded)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *args, **kwargs: True)
    calls = []
    result = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda command, cwd: calls.append((command, cwd))
        or SimpleNamespace(pid=42, poll=lambda: None),
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
    folded = SimpleNamespace(owner=None, meta={})
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
        folded.meta = {"_claim_revision": "claim-after-freeze"}

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
    folded = SimpleNamespace(owner=None, meta={})
    claims = []
    releases = []

    def claim(_self, owner, _card_id):
        claims.append(owner)
        folded.owner = owner
        folded.meta = {"_claim_revision": f"claim-{len(claims)}"}
        if len(claims) == 1:
            store.set_frozen(paths, True, writer=operator, reason="synchronized test")

    def release(_self, owner, _card_id, **kwargs):
        releases.append((owner, kwargs["expected_claim_revision"]))
        folded.owner = None
        folded.meta = {}
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
    folded = SimpleNamespace(owner=None, meta={}, status=SimpleNamespace(value="doing"))

    def claim(_self, owner, _card_id):
        folded.owner = owner
        folded.meta = {"_claim_revision": "claim-live"}

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
    builder_dispatch._write_status(paths, "node-ziowk01", request, "running", **{
        key: value for key, value in status.items()
        if key not in {"schema", "request_id", "card_id", "node", "state", "heartbeat_at"}
    })
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
    assert builder_dispatch._load(
        builder_dispatch.status_path(paths, "node-ziowk01", "24b00003")
    )["state"] == "stale"


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
    folded = SimpleNamespace(owner=None, meta={})

    def claim(_self, owner, card_id):
        assert card_id == "20000002"
        folded.owner = owner
        folded.meta = {"_claim_revision": "claim-next"}

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
    folded = SimpleNamespace(owner=None, meta={}, status=SimpleNamespace(value="doing"))
    claims = []

    def claim(_self, owner, _card_id):
        claims.append(owner)
        folded.owner = owner
        folded.meta = {"_claim_revision": f"claim-{len(claims)}"}

    def release(_self, owner, _card_id, **kwargs):
        assert kwargs["expected_claim_revision"] == folded.meta["_claim_revision"]
        assert owner == folded.owner
        folded.owner = None
        folded.meta = {}
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
    folded = SimpleNamespace(
        owner=None, meta={}, status=SimpleNamespace(value="doing"), links={"verdict": "PASS"}
    )

    def claim(_self, owner, _card_id):
        folded.owner = owner
        folded.meta = {"_claim_revision": "claim-complete"}

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
