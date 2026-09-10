"""Running reviewer supersession regression tests for card 5e7a3f11."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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

    assert killed == [(123, module.signal.SIGTERM)]
    assert values.review_supersession["claim_revision"] == "generation-1"
