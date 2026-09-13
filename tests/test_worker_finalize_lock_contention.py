"""Bounded, idempotent board-lock contention repair for card 47cf2443.

Card c3236a0f completed its child successfully and emitted completion mail,
but the wrapper exited failure when ``release_superseded_review_claim``
raised ``TimeoutError`` from the shared board mutation lock. These tests pin
the bounded retry contract: transient contention never changes the child
outcome, retries stay idempotent, and persistent contention still fails
truthfully. Successor card a49d8603 pins the projection contract on top: a
failed release keeps the owner projection active for fenced reconciliation,
while only a successful release may idle it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from skcoord.card_store import CardCore, CardStore
from skcoord.coordination import Board

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"


def load_module():
    spec = importlib.util.spec_from_file_location("worker_wrapper_lock_contention", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def args():
    return SimpleNamespace(
        card="3a11f071",
        owner="pi-seraph-chiap08-3a11f071",
        claim_revision="generation-1",
        host="chiap08",
        lane="codex",
        session="codex-auto-3a11f071",
    )


def make_flaky_board(failures: int):
    """Return a Board stand-in that times out ``failures`` times, then delegates."""
    state = {"calls": []}

    class FlakyBoard(Board):
        def __init__(self, home):
            super().__init__(home)

        def release_claim(self, owner, task_id, *, actor, expected_claim_revision):
            state["calls"].append((owner, task_id, actor, expected_claim_revision))
            if len(state["calls"]) <= failures:
                raise TimeoutError("timed out acquiring board mutation lock")
            return super().release_claim(
                owner,
                task_id,
                actor=actor,
                expected_claim_revision=expected_claim_revision,
            )

    return FlakyBoard, state


@pytest.fixture
def claimed_board(tmp_path, monkeypatch):
    """Install a real claimed card and agent under an isolated board home."""
    module = load_module()
    home = tmp_path / ".skcapstone"
    home.mkdir()
    store = CardStore(home)
    values = args()
    store.create(
        CardCore(
            id=values.card,
            title="obsolete review",
            initial_owner=values.owner,
            initial_claim_revision=values.claim_revision,
        )
    )
    agents = home / "coordination" / "agents"
    agents.mkdir(parents=True)
    (agents / f"{values.owner}.json").write_text(
        json.dumps(
            {
                "agent": values.owner,
                "state": "active",
                "current_task": values.card,
                "claimed_tasks": [values.card],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module.Path, "home", classmethod(lambda _cls: tmp_path))
    return module, values, home


def test_release_retries_transient_board_lock_timeout(claimed_board, monkeypatch) -> None:
    module, values, _home = claimed_board
    flaky, state = make_flaky_board(failures=2)
    monkeypatch.setattr("skcoord.coordination.Board", flaky)
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    values.review_supersession = {"current_head": "2" * 40}

    assert module.release_superseded_review_claim(values) is True

    expected = (values.owner, values.card, values.owner, values.claim_revision)
    assert state["calls"] == [expected, expected, expected]
    assert len(sleeps) == 2
    assert all(wait == module.LOCK_RELEASE_RETRY_BACKOFF_SECONDS for wait in sleeps)


def test_release_does_not_retry_non_timeout_failures(monkeypatch) -> None:
    module = load_module()

    class ConflictBoard:
        def __init__(self, _home):
            pass

        def release_claim(self, *_args, **_kwargs):
            raise ValueError("claim revision conflict")

    class Store:
        def __init__(self, _home):
            pass

        def fold(self, _card):
            return SimpleNamespace(owner=args().owner, meta={"_claim_revision": "other"})

    monkeypatch.setattr("skcoord.coordination.Board", ConflictBoard)
    monkeypatch.setattr(module, "CardStore", Store)
    values = args()
    values.review_supersession = {"current_head": "2" * 40}

    with pytest.raises(RuntimeError, match="exact claim was not released"):
        module.release_superseded_review_claim(values)


def test_release_fails_truthfully_after_bounded_lock_timeouts(claimed_board, monkeypatch) -> None:
    module, values, _home = claimed_board
    flaky, state = make_flaky_board(failures=module.LOCK_RELEASE_ATTEMPTS + 1)
    monkeypatch.setattr("skcoord.coordination.Board", flaky)
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    values = args()
    values.review_supersession = {"current_head": "2" * 40}

    with pytest.raises(RuntimeError, match="exact claim was not released") as excinfo:
        module.release_superseded_review_claim(values)

    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert len(state["calls"]) == module.LOCK_RELEASE_ATTEMPTS
    assert len(sleeps) == module.LOCK_RELEASE_ATTEMPTS - 1


def _terminal_board_setup(tmp_path):
    """Install a claimed card, active projection, and live snapshot."""
    module = load_module()
    home = tmp_path / ".skcapstone"
    home.mkdir()
    store = CardStore(home)
    store.create(
        CardCore(
            id="3a11f071",
            title="obsolete review",
            initial_owner=args().owner,
            initial_claim_revision=args().claim_revision,
        )
    )
    agents = home / "coordination" / "agents"
    agents.mkdir(parents=True)
    projection = agents / f"{args().owner}.json"
    projection.write_text(
        json.dumps(
            {
                "agent": args().owner,
                "state": "active",
                "current_task": args().card,
                "claimed_tasks": [args().card],
            }
        ),
        encoding="utf-8",
    )
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text(
        json.dumps(
            {
                "host": "chiap08",
                "cards": ["3a11f071"],
                "workers": [
                    {
                        "card_id": "3a11f071",
                        "owner": args().owner,
                        "claim_revision": args().claim_revision,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    values = argparse.Namespace(
        **vars(args()),
        model="fake",
        stdout=tmp_path / "stdout.log",
        live_snapshot=snapshot,
        worker_executable="",
        startup_timeout=1.0,
        command=[sys.executable, "-c", "pass"],
        evidence_dir=tmp_path / "evidence",
    )
    return module, store, projection, values


def _patch_finalize_dependencies(module, monkeypatch, tmp_path, values, evidence):
    monkeypatch.setattr(module.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(module, "parse_args", lambda: values)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "preflight_mailbox", lambda _args: True)
    monkeypatch.setattr(module, "emit_work_mail", lambda *_args: None)
    monkeypatch.setattr(module, "review_supersession", lambda _args: evidence)
    monkeypatch.setattr(module, "terminal_local_evidence", lambda child: child.poll() is not None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    idle_calls = []
    monkeypatch.setattr(
        module, "idle_owner_projection", lambda *call, **_kw: idle_calls.append(call)
    )
    return idle_calls


def _run_main_with_flaky_release(monkeypatch, tmp_path, failures, evidence):
    module, store, projection, values = _terminal_board_setup(tmp_path)
    flaky, state = make_flaky_board(failures=failures)
    monkeypatch.setattr("skcoord.coordination.Board", flaky)
    idle_calls = _patch_finalize_dependencies(module, monkeypatch, tmp_path, values, evidence)

    result = module.main()

    assert store.fold(values.card).owner is None
    folded_projection = json.loads(projection.read_text(encoding="utf-8"))
    assert folded_projection["current_task"] is None
    assert folded_projection["claimed_tasks"] == []
    releases = [
        event
        for event in store._read_events(values.card)
        if event.get("action") == "release_claim"
        and event.get("expected_claim_revision") == values.claim_revision
    ]
    assert len(releases) == 1
    return result, state, idle_calls


def test_successful_child_exit_survives_transient_lock_timeout(monkeypatch, tmp_path) -> None:
    result, state, _idle_calls = _run_main_with_flaky_release(
        monkeypatch, tmp_path, failures=1, evidence=None
    )

    assert result == 0
    assert len(state["calls"]) == 2


def test_superseded_terminal_verdict_survives_transient_lock_timeout(
    monkeypatch, tmp_path
) -> None:
    evidence = {
        "card_id": args().card,
        "claim_revision": args().claim_revision,
        "owner": args().owner,
        "current_head": "2" * 40,
    }
    result, state, _idle_calls = _run_main_with_flaky_release(
        monkeypatch, tmp_path, failures=1, evidence=evidence
    )

    assert result == 75
    assert len(state["calls"]) == 2


def test_successful_terminal_release_idles_owner_projection(monkeypatch, tmp_path) -> None:
    result, _state, idle_calls = _run_main_with_flaky_release(
        monkeypatch, tmp_path, failures=0, evidence=None
    )

    assert result == 0
    assert len(idle_calls) == 1
    assert idle_calls[0] == (args().owner, args().card, args().claim_revision)


def test_persistent_release_failure_keeps_owner_projection_active(monkeypatch, tmp_path) -> None:
    module, store, projection, values = _terminal_board_setup(tmp_path)
    flaky, state = make_flaky_board(failures=module.LOCK_RELEASE_ATTEMPTS + 1)
    monkeypatch.setattr("skcoord.coordination.Board", flaky)
    idle_calls = _patch_finalize_dependencies(module, monkeypatch, tmp_path, values, None)

    with pytest.raises(RuntimeError, match="exact claim was not released") as excinfo:
        module.main()

    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert len(state["calls"]) == module.LOCK_RELEASE_ATTEMPTS
    # The failed release must not idle the projection: fenced reconciliation
    # still needs to see the owner and the unreleased claim.
    assert idle_calls == []
    assert store.fold(values.card).owner == values.owner
    folded_projection = json.loads(projection.read_text(encoding="utf-8"))
    assert folded_projection["current_task"] == values.card
    assert folded_projection["claimed_tasks"] == [values.card]
    releases = [
        event
        for event in store._read_events(values.card)
        if event.get("action") == "release_claim"
        and event.get("expected_claim_revision") == values.claim_revision
    ]
    assert releases == []
