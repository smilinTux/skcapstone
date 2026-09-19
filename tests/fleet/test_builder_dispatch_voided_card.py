"""A card voided mid-flight must skip, not kill the node worker.

Incident, ziowk01-wsl, 2026-09-18. Card 59553966 was voided and replaced by
59550966 as part of a legitimate source-binding repair. The sknoded node worker
still held the old card in its dispatch queue, called ``Board.claim_task``, and
``skcoord.coordination._claim_task`` raised a bare ``ValueError: Task 59553966
not found``. Nothing between that raise and ``main_loop``'s ``while True``
caught it, so ``sknoded.service`` exited 1 and lost 18h 15m of accumulated
process state. One voided card took down the entire builder.

Cards are voided and replaced at fleet scale, so this recurs on every rebind.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from skcoord.coordination import TaskUnclaimable

from skcapstone.fleet import builder_dispatch, sknoded, store


@pytest.fixture(autouse=True)
def _clear_process_registry(monkeypatch):
    builder_dispatch._PROCESSES.clear()
    monkeypatch.setenv("SKFLEET_PI", "/test/bin/pi")
    yield
    builder_dispatch._PROCESSES.clear()


def _card(card_id: str = "59553966") -> dict:
    return {
        "id": card_id,
        "meta": {
            "repository": "https://github.com/smilinTux/skcapstone.git",
            "base_ref": "main",
            "base_revision": "9cc415465d6bacc22b51b09a3c861c61f0823d45",
        },
    }


def _folded(card_id: str = "59553966", **values) -> SimpleNamespace:
    defaults = {
        "id": card_id,
        "owner": None,
        "meta": dict(_card(card_id)["meta"]),
        "labels": ["sk-m", "source-only"],
        "status": SimpleNamespace(value="doing"),
        "links": {},
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _queued(paths, operator, noded41) -> dict:
    """Place one real offer on the builder, exactly as Niobe does."""
    store.write_spec(
        paths,
        "node",
        "node-ziowk01",
        {"role": "builder-standby", "actuate": True, "cordoned": False},
        writer=operator,
        labels={"host": "ziowk01"},
    )
    sknoded.run_once(paths, "node-ziowk01")
    return builder_dispatch.offer(
        paths,
        _card(),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity="capauth:niobe"),
        now=datetime.now(timezone.utc),  # a LIVE lease; an expired one blocks first
    )


def _void_the_card(monkeypatch) -> None:
    """The card still folds (void sets archived, it is not deleted)..."""
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: _folded())

    def _claim(_self, _owner, card_id):
        # ...but it has dropped out of the claim projection, which is exactly
        # what the live crash reported.
        raise TaskUnclaimable(
            card_id, "voided", f"Task {card_id} was voided at 2026-09-18T11:04:00Z by chef"
        )

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", _claim)


def test_voided_card_is_skipped_with_a_logged_reason(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    request = _queued(paths, operator, noded41)
    _void_the_card(monkeypatch)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_a, **_k: True)

    status = builder_dispatch.consume_one(
        paths,
        tmp_path,
        "node-ziowk01",
        launcher=lambda *_a: pytest.fail("a voided card must never launch a worker"),
        materializer=lambda _request, workspace: workspace,
    )

    assert status is not None
    assert status["state"] == "blocked"
    assert status["request_id"] == request["request_id"]
    assert status["unclaimable_reason"] == "voided"
    assert "voided" in status["error"]


def test_the_skip_is_terminal_so_the_card_is_not_re_offered_forever(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    """The status file is the only thing that tells the dispatcher to stop.

    Nothing deletes the request file when a card is voided, so consume_one
    would otherwise re-read it every pass forever. Writing a TERMINAL status
    for that exact request generation is what makes offer() decline, frees the
    builder slot in _node_load, and gives decline_reason a real line to log.
    """
    _queued(paths, operator, noded41)
    _void_the_card(monkeypatch)
    monkeypatch.setattr(builder_dispatch, "startup_hello", lambda *_a, **_k: True)

    builder_dispatch.consume_one(paths, tmp_path, "node-ziowk01", materializer=lambda _r, w: w)
    # Second pass: the request file is still there, and must now be a no-op.
    assert (
        builder_dispatch.consume_one(
            paths,
            tmp_path,
            "node-ziowk01",
            launcher=lambda *_a: pytest.fail("re-launched a voided card"),
            materializer=lambda _r, w: w,
        )
        is None
    )
    assert builder_dispatch.request_path(paths, "node-ziowk01", "59553966").exists()
    assert builder_dispatch._node_load(paths, "node-ziowk01") == 0
    assert (
        builder_dispatch.offer(
            paths,
            _card(),
            ["sk-m", "source-only"],
            writer=store.Writer(role="scheduler", node="niobe", identity="capauth:niobe"),
        )
        is None
    )
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason is not None and "state=blocked" in reason


def test_contention_is_retried_rather_than_parked(
    paths, operator, noded41, monkeypatch, tmp_path
) -> None:
    """An unmet dependency clears on its own; parking it would strand the card."""
    _queued(paths, operator, noded41)
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: _folded())
    attempts = []

    def _claim(_self, _owner, card_id):
        attempts.append(card_id)
        raise TaskUnclaimable(card_id, "dependencies", f"Task {card_id} has incomplete deps: x")

    monkeypatch.setattr(builder_dispatch.Board, "claim_task", _claim)

    for _ in range(2):
        assert (
            builder_dispatch.consume_one(
                paths, tmp_path, "node-ziowk01", materializer=lambda _r, w: w
            )
            is None
        )
    assert attempts == ["59553966", "59553966"]
    assert not builder_dispatch.status_path(paths, "node-ziowk01", "59553966").exists()


def test_main_loop_survives_a_card_that_cannot_be_claimed(paths, monkeypatch) -> None:
    """The regression proper: the daemon must outlive one unusable card."""
    passes = []

    def _run_once(_paths, _node):
        passes.append(1)
        if len(passes) == 1:
            raise TaskUnclaimable("59553966", "voided", "Task 59553966 was voided")
        raise RuntimeError("stop after the second cycle")

    monkeypatch.setattr(sknoded, "run_once", _run_once)
    monkeypatch.setattr("skcapstone.fleet.converge.converge_once", lambda *_a: None)
    monkeypatch.setattr(sknoded.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="stop after the second cycle"):
        sknoded.main_loop(paths, "node-ziowk01", interval=0, actuation_interval=0)
    assert len(passes) == 2


def test_main_loop_still_dies_on_infrastructure_failure(paths, monkeypatch) -> None:
    """Fail closed: only a single unusable card degrades to a skip.

    A vanished or unreadable store, a permissions failure, or any unexpected
    error must still terminate the unit so systemd restarts it and the failure
    is visible, rather than turning every card on the node into a silent skip.
    """
    monkeypatch.setattr(
        sknoded, "run_once", lambda *_a: (_ for _ in ()).throw(OSError("cards/ is unreadable"))
    )
    monkeypatch.setattr("skcapstone.fleet.converge.converge_once", lambda *_a: None)
    monkeypatch.setattr(sknoded.time, "sleep", lambda _s: None)

    with pytest.raises(OSError, match="unreadable"):
        sknoded.main_loop(paths, "node-ziowk01", interval=0, actuation_interval=0)
