"""Running reviewer supersession regression tests for card 5e7a3f11."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from skcoord.card_store import CardCore, CardStore

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"


def load_module():
    spec = importlib.util.spec_from_file_location("worker_wrapper_supersession", SCRIPT)
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


def review(head: str, *, superseded_by: str = ""):
    return SimpleNamespace(
        labels=["review", "seat-seraph"],
        owner="pi-seraph-chiap08-3a11f071",
        meta={
            "_claim_revision": "generation-1",
            "link_source_card": "a9b4266c",
            "link_head_revision": head,
        },
        links={"superseded_by": superseded_by} if superseded_by else {},
    )


def source(head: str):
    return SimpleNamespace(links={"candidate": head})


def install_store(module, cards):
    class Store:
        def __init__(self, _home):
            pass

        def fold(self, card_id):
            return cards.get(card_id)

    module.CardStore = Store


class OneCheckStop:
    def __init__(self):
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > 1


def test_two_repairs_supersede_old_review_but_preserve_current_review() -> None:
    module = load_module()
    old = "1" * 40
    first_repair = "2" * 40
    current = "3" * 40
    cards = {
        "3a11f071": review(old, superseded_by=f"a9b4266c candidate {first_repair}"),
        "a9b4266c": source(current),
    }
    install_store(module, cards)

    evidence = module.review_supersession(args())
    assert evidence["reviewed_head"] == old
    assert evidence["current_head"] == current

    cards["3a11f071"] = review(current)
    assert module.review_supersession(args()) is None


def test_source_head_mismatch_supersedes_review_without_manual_link() -> None:
    module = load_module()
    cards = {
        "3a11f071": review("1" * 40),
        "a9b4266c": source("2" * 40),
    }
    install_store(module, cards)

    evidence = module.review_supersession(args())

    assert evidence["reviewed_head"] == "1" * 40
    assert evidence["current_head"] == "2" * 40
    assert evidence["superseded_by"] == "source-head-mismatch"


def test_stale_claim_cannot_stop_newer_review_generation(monkeypatch) -> None:
    module = load_module()
    cards = {
        "3a11f071": review("1" * 40, superseded_by="candidate " + "2" * 40),
        "a9b4266c": source("2" * 40),
    }
    cards["3a11f071"].meta["_claim_revision"] = "generation-2"
    install_store(module, cards)
    killed = []
    monkeypatch.setattr(module.os, "killpg", lambda *values: killed.append(values))

    child = SimpleNamespace(pid=123, poll=lambda: None)
    stop = OneCheckStop()
    module.monitor_review_supersession(args(), child, stop)

    assert killed == []


def test_exact_superseded_generation_stops_only_its_process_group(monkeypatch) -> None:
    module = load_module()
    cards = {
        "3a11f071": review("1" * 40, superseded_by="candidate " + "2" * 40),
        "a9b4266c": source("2" * 40),
    }
    install_store(module, cards)
    killed = []
    monkeypatch.setattr(module.os, "killpg", lambda *values: killed.append(values))

    child = SimpleNamespace(pid=123, poll=lambda: None)
    stop = SimpleNamespace(wait=lambda _seconds: False)
    values = args()
    module.monitor_review_supersession(values, child, stop)

    assert killed == [(123, module.signal.SIGTERM), (123, module.signal.SIGKILL)]
    assert values.review_supersession["claim_revision"] == "generation-1"


def test_superseded_review_releases_only_exact_claim(monkeypatch) -> None:
    module = load_module()
    calls = []

    class Board:
        def __init__(self, home):
            calls.append(("home", home))

        def release_claim(self, owner, card, *, actor, expected_claim_revision):
            calls.append((owner, card, actor, expected_claim_revision))
            return True

    monkeypatch.setattr("skcoord.coordination.Board", Board)
    values = args()
    values.review_supersession = {"current_head": "2" * 40}

    module.release_superseded_review_claim(values)

    assert calls[-1] == (
        values.owner,
        values.card,
        values.owner,
        "generation-1",
    )


def test_superseded_review_keeps_custody_when_exact_release_fails(monkeypatch) -> None:
    module = load_module()

    class Board:
        def __init__(self, _home):
            pass

        def release_claim(self, *_args, **_kwargs):
            return False

    class Store:
        def __init__(self, _home):
            pass

        def fold(self, _card):
            return SimpleNamespace(owner=args().owner, meta={"_claim_revision": "other"})

    monkeypatch.setattr("skcoord.coordination.Board", Board)
    monkeypatch.setattr(module, "CardStore", Store)
    values = args()
    values.review_supersession = {"current_head": "2" * 40}

    with pytest.raises(RuntimeError, match="exact claim was not released"):
        module.release_superseded_review_claim(values)


def test_terminal_snapshot_release_is_not_repeated(monkeypatch) -> None:
    module = load_module()
    values = args()
    values.review_supersession = {"current_head": "2" * 40}
    calls = []
    monkeypatch.setattr(module, "publish_terminal_capacity", lambda *_args: True)
    monkeypatch.setattr(
        module,
        "release_superseded_review_claim",
        lambda *_args: calls.append("release") or True,
    )

    assert module.finalize_terminal_capacity(values, None) is True
    assert calls == ["release"]


def test_snapshot_failure_falls_back_to_exact_claim_release(monkeypatch, capsys) -> None:
    module = load_module()
    values = args()
    values.review_supersession = {"current_head": "2" * 40}
    calls = []

    def fail_publication(*_args):
        raise ValueError("bad snapshot")

    monkeypatch.setattr(module, "publish_terminal_capacity", fail_publication)
    monkeypatch.setattr(
        module,
        "release_superseded_review_claim",
        lambda *_args: calls.append("release") or True,
    )

    assert module.finalize_terminal_capacity(values, None) is True
    assert calls == ["release"]
    assert "bad snapshot" in capsys.readouterr().err


def test_failed_exact_release_preserves_owner_projection(monkeypatch) -> None:
    module = load_module()
    values = args()
    values.review_supersession = {"current_head": "2" * 40}
    idled = []
    monkeypatch.setattr(
        module,
        "finalize_terminal_capacity",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("release failed")),
    )
    monkeypatch.setattr(module, "idle_owner_projection", idled.append)

    with pytest.raises(RuntimeError, match="release failed"):
        module.finalize_worker_exit(values, None)
    assert idled == []


def test_board_lock_timeout_becomes_durable_exact_generation_retry(
    monkeypatch, tmp_path, capsys
) -> None:
    module = load_module()
    values = args()
    values.review_supersession = {"current_head": "2" * 40}
    values.evidence_dir = tmp_path
    values.live_snapshot = None
    monkeypatch.setattr(
        module,
        "release_superseded_review_claim",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("board lock")),
    )

    assert module.finalize_terminal_capacity(values, None) is False
    retry = next((tmp_path / "worker-finalization-retry").glob("*.json"))
    row = json.loads(retry.read_text(encoding="utf-8"))
    assert (row["card_id"], row["owner"], row["claim_revision"]) == (
        values.card,
        values.owner,
        values.claim_revision,
    )
    assert "exact-generation retry" in capsys.readouterr().err


def test_pending_finalization_retries_only_exact_generation(monkeypatch, tmp_path) -> None:
    module = load_module()
    values = args()
    values.evidence_dir = tmp_path
    values.live_snapshot = None
    request = module.record_finalization_retry(values, TimeoutError("board lock"))
    calls = []

    class Board:
        def __init__(self, _home):
            pass

        def release_claim(self, owner, card, *, actor, expected_claim_revision):
            calls.append((owner, card, actor, expected_claim_revision))
            return True

    monkeypatch.setattr("skcoord.coordination.Board", Board)

    assert module.reconcile_finalization_retries(tmp_path) == 1
    assert calls == [(values.owner, values.card, values.owner, values.claim_revision)]
    receipt = request.with_suffix(".reconciled.json")
    assert json.loads(receipt.read_text(encoding="utf-8"))["request_sha256"]
    assert module.reconcile_finalization_retries(tmp_path) == 0
    assert len(calls) == 1


def test_partial_publication_release_is_idempotent(monkeypatch) -> None:
    module = load_module()

    class Board:
        def __init__(self, _home):
            pass

        def release_claim(self, *_args, **_kwargs):
            raise ValueError("revision already released")

    class Store:
        def __init__(self, _home):
            pass

        def fold(self, _card):
            return SimpleNamespace(owner=None, meta={})

        def _read_events(self, _card):
            return [
                {
                    "action": "release_claim",
                    "released_owner": args().owner,
                    "expected_claim_revision": args().claim_revision,
                }
            ]

    monkeypatch.setattr("skcoord.coordination.Board", Board)
    monkeypatch.setattr(module, "CardStore", Store)
    values = args()
    values.review_supersession = {"current_head": "2" * 40}

    assert module.release_superseded_review_claim(values) is True


def test_wrapper_never_rewrites_owner_projection(monkeypatch, tmp_path) -> None:
    module = load_module()
    monkeypatch.setattr(module.Path, "home", classmethod(lambda _cls: tmp_path))
    agents = tmp_path / ".skcapstone" / "coordination" / "agents"
    agents.mkdir(parents=True)
    path = agents / f"{args().owner}.json"
    path.write_text(
        '{"agent":"pi-seraph-chiap08-3a11f071","state":"active",'
        '"current_task":"new-card","claimed_tasks":["new-card","sibling"]}',
        encoding="utf-8",
    )

    before = path.read_bytes()
    module.idle_owner_projection(args().owner, args().card, args().claim_revision)
    assert path.read_bytes() == before


def test_superseded_main_releases_once_retires_capacity_and_exits_75(
    monkeypatch, tmp_path
) -> None:
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
    evidence = {
        "card_id": values.card,
        "claim_revision": values.claim_revision,
        "owner": values.owner,
        "current_head": "2" * 40,
    }
    monkeypatch.setattr(module.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(module, "parse_args", lambda: values)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "preflight_mailbox", lambda _args: True)
    monkeypatch.setattr(module, "emit_work_mail", lambda *_args: None)
    monkeypatch.setattr(module, "review_supersession", lambda _args: evidence)
    monkeypatch.setattr(module, "terminal_local_evidence", lambda child: child.poll() is not None)

    assert module.main() == 75
    assert store.fold(values.card).owner is None
    assert json.loads(snapshot.read_text(encoding="utf-8"))["cards"] == []
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
