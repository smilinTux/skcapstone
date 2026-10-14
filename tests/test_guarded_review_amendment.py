"""Review opening retains the dispatcher's exact operator-amended source pin."""

import json

import pytest

from skcapstone import source_ref_amendment as amendment
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_guarded_review_work import prepared
from tests.test_provisional_verdict_producer import _run


@pytest.mark.parametrize("mutation", [None, "worker", "repository", "revision", "unreachable"])
def test_review_opener_proves_operator_ref_amendment(tmp_path, monkeypatch, mutation):
    """Only a fetchable operator ref for unchanged source bytes can open review."""
    store, card, owner, _, args = prepared(tmp_path)
    writer = owner if mutation == "worker" else "lumina-nor"
    ref = "preserve/w7-exact"
    store.append_event(card, "link", writer, link_key="base_ref", link_value=ref)
    if mutation in {"repository", "revision"}:
        key = "repository" if mutation == "repository" else "base_revision"
        value = "https://github.com/other/repo.git" if key == "repository" else "f" * 40
        store.append_event(card, "link", writer, link_key=key, link_value=value)
    args[args.index("--expected-source-revision") + 1] = (
        LiveCardStoreGateway(tmp_path).read_card(card).revision
        if mutation != "repository"
        else "0" * 64
    )
    probes = []

    def prove(repository, ref, revision):
        probes.append((repository, ref, revision))
        if mutation == "unreachable":
            raise ValueError("amended ref is unreachable")

    monkeypatch.setattr(amendment, "prove_ref", prove)
    before = store._read_events(card)
    result = _run(tmp_path, *args)
    assert store._read_events(card) == before
    if mutation:
        assert result.exit_code != 0, result.output
        assert len(store.list_card_ids()) == 1
    else:
        assert result.exit_code == 0, result.output
        review = store.fold(json.loads(result.output)["review_card_id"])
        assert review.links["base_ref"] == ref
        assert review.links["base_revision"] == "e" * 40
        assert probes == [("https://github.com/org/repo.git", ref, "e" * 40)]
