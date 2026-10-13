"""Direct builder recovery must preserve the exact producer's finished claim."""

from unittest.mock import Mock

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import builder_dispatch


@pytest.mark.parametrize("verdict", ("PASS", "PASS_FOR_REVIEW", "PASS_FOR_REREVIEW"))
def test_direct_builder_release_refuses_finished_claim(tmp_path, monkeypatch, verdict):
    """The in-process Board path may not bypass the CLI's PASS guard."""
    card = "deadbeef"
    owner = "pi-deepseek-builder-node-chiap02-deadbeef"
    store = CardStore(tmp_path)
    store.create(CardCore(id=card, kind="task", title="finished builder", created_by="test"))
    store.append_event(card, "claim", owner, owner=owner)
    claim = store.fold(card).meta["_claim_revision"]
    store.append_event(card, "verdict", owner, verdict=verdict)
    board = Mock()
    monkeypatch.setattr(builder_dispatch, "Board", lambda _home: board)
    assert builder_dispatch._release_exact(tmp_path, card, owner, claim, actor="niobe") is False
    board._release_claim_locked.assert_not_called()
    assert store.fold(card).owner == owner


def test_direct_builder_release_keeps_exact_cas_for_unfinished_claim(tmp_path, monkeypatch):
    """An eligible unfinished claim still uses the original revision fence."""
    card = "deadbeef"
    owner = "pi-deepseek-builder-node-chiap02-deadbeef"
    store = CardStore(tmp_path)
    store.create(CardCore(id=card, kind="task", title="unfinished builder", created_by="test"))
    store.append_event(card, "claim", owner, owner=owner)
    claim = store.fold(card).meta["_claim_revision"]
    board = Mock()
    board._release_claim_locked.return_value = True
    monkeypatch.setattr(builder_dispatch, "Board", lambda _home: board)
    assert builder_dispatch._release_exact(tmp_path, card, owner, claim, actor="niobe") is True
    board._release_claim_locked.assert_called_once_with(
        owner, card, actor="niobe", expected_claim_revision=claim
    )


def test_direct_release_holds_card_lock_across_guard_and_mutation(tmp_path, monkeypatch):
    """Another local writer cannot slip a PASS between inspection and release."""
    from concurrent.futures import ThreadPoolExecutor

    from skcoord.card_store import card_mutation_lock

    from skcapstone import release_guard

    card = "deadbeef"
    owner = "pi-glm-builder-node-chiap02-deadbeef"
    store = CardStore(tmp_path)
    store.create(CardCore(id=card, kind="task", title="lock probe", created_by="test"))
    store.append_event(card, "claim", owner, owner=owner)
    claim = store.fold(card).meta["_claim_revision"]
    observed = []

    def competing_writer():
        with pytest.raises(TimeoutError):
            with card_mutation_lock(tmp_path, card, timeout_seconds=0.02):
                pytest.fail("another writer acquired the card lock")

    def assert_locked(stage):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(competing_writer).result(timeout=2)
        observed.append(stage)

    def guard(*_args):
        assert_locked("guard")

    def release(*_args, **_kwargs):
        assert_locked("release")
        return True

    board = Mock()
    board._release_claim_locked.side_effect = release
    monkeypatch.setattr(builder_dispatch, "Board", lambda _home: board)
    monkeypatch.setattr(release_guard, "require_release_allowed", guard)
    assert builder_dispatch._release_exact(tmp_path, card, owner, claim, actor="niobe") is True
    assert observed == ["guard", "release"]
