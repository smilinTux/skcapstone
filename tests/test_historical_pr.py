"""Exact historical association repair preserves custody and acceptance evidence."""

import json

import pytest

from skcapstone.card import CardEventLog
from skcapstone.card_store import CardCore, CardStore
from skcapstone.historical_pr import (
    LINEAGE_KEY,
    archive_historical_pr,
    history_revision,
    raw_card_revision,
)
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome
from tests.test_guarded_completion import prepared
from tests.test_provisional_verdict_producer import _run


def fixture(home):
    """Construct a synthetic migrated repository with its original PR and verdict."""
    store, card, owner, _ = prepared(home)
    links = {
        "repository": "https://forge.example/team/project.git",
        "pr": "https://github.com/team/project/pull/78",
        "commit": "a" * 40,
        "candidate_evidence_sha256": "e" * 64,
        "verdict": "PASS_FOR_REVIEW",
    }
    for key, value in links.items():
        store.append_event(card, "link", owner, link_key=key, link_value=value)
    guards = dict(
        expected_card_revision=raw_card_revision(store.fold(card)),
        expected_history_revision=history_revision(store, card),
        expected_claim_revision="c" * 32,
        expected_pr=links["pr"],
        expected_repository=links["repository"],
        transition_id="f" * 64,
    )
    return store, card, owner, guards


def test_repairs_only_active_association_and_replays_exactly(tmp_path):
    store, card, owner, guards = fixture(tmp_path)
    before = store.fold(card).model_dump(mode="json")
    history = store._read_events(card)
    outcome = _latest_outcome(store, card)
    result = archive_historical_pr(tmp_path, card, owner, **guards)
    assert archive_historical_pr(tmp_path, card, owner, **guards) == result
    after = CardStore(tmp_path).fold(card).model_dump(mode="json")
    receipt = json.loads(after["links"].pop(LINEAGE_KEY))
    assert receipt["pr"] == before["links"]["pr"]
    assert receipt["before_history_revision"] == guards["expected_history_revision"]
    assert after["links"].pop("pr") == ""
    before["links"].pop("pr")
    after.pop("updated_at")
    before.pop("updated_at")
    assert after == before
    assert _latest_outcome(CardStore(tmp_path), card) == outcome
    assert store._read_events(card)[: len(history)] == history
    assert len(store._read_events(card)) == len(history) + 2
    snapshot = LiveCardStoreGateway(tmp_path).read_card(card)
    assert snapshot.number is None
    assert snapshot.head_sha == "a" * 40
    assert snapshot.verdict == "PASS_FOR_REVIEW"


@pytest.mark.parametrize("kind", ["native", "overlay"])
@pytest.mark.parametrize("step", [1, 2])
def test_interrupted_write_recovers_without_duplicate_lineage(tmp_path, monkeypatch, kind, step):
    store, card, owner, guards = fixture(tmp_path)
    baseline = store._read_events(card)
    cls, method = (CardStore, "append_event") if kind == "native" else (CardEventLog, "append")
    original = getattr(cls, method)
    count = 0

    def interrupted(self, *args, **kwargs):
        nonlocal count
        result = original(self, *args, **kwargs)
        count += 1
        if count == step:
            raise ValueError("synthetic lost acknowledgement")
        return result

    monkeypatch.setattr(cls, method, interrupted)
    with pytest.raises(ValueError, match="lost acknowledgement"):
        archive_historical_pr(tmp_path, card, owner, **guards)
    monkeypatch.setattr(cls, method, original)
    first = archive_historical_pr(tmp_path, card, owner, **guards)
    assert archive_historical_pr(tmp_path, card, owner, **guards) == first
    assert len(store._read_events(card)) == len(baseline) + 2


@pytest.mark.parametrize("after_first", [False, True])
@pytest.mark.parametrize(
    "change",
    ["digest", "history", "claim", "owner", "lineage", "outcome", "pr", "repository", "request"],
)
def test_stale_state_is_rejected_without_another_write(tmp_path, after_first, change):
    store, card, owner, guards = fixture(tmp_path)
    if after_first:
        archive_historical_pr(tmp_path, card, owner, **guards)
    if change == "digest":
        guards["expected_card_revision"] = "0" * 64
    elif change == "history":
        store.append_event(card, "note", owner, note="new provenance with unchanged fold")
    elif change == "claim":
        store.append_event(card, "claim", owner, owner=owner, claim_revision="d" * 32)
    elif change == "owner":
        store.append_event(card, "unassign", owner)
    elif change == "lineage":
        store.append_event(card, "link", owner, link_key=LINEAGE_KEY, link_value="other")
    elif change == "outcome":
        store.append_event(card, "verdict", owner, verdict="FAIL")
    elif change in {"pr", "repository"}:
        store.append_event(card, "link", owner, link_key=change, link_value="other")
    else:
        guards["expected_pr"] += "0"
    before = store._read_events(card)
    with pytest.raises(ValueError):
        archive_historical_pr(tmp_path, card, owner, **guards)
    assert store._read_events(card) == before


@pytest.mark.parametrize(
    "pr,extra",
    [
        ("https://forge.example/team/project/pull/78", {}),
        ("https://github.com/team/project/pull/78?bad=1", {}),
        ("78", {}),
        ("https://github.com/team/project/pull/78", {"head": "b" * 40}),
        ("https://github.com/team/project/pull/78", {LINEAGE_KEY: "existing"}),
    ],
)
def test_other_errors_or_matching_associations_are_not_repaired(tmp_path, pr, extra):
    store, card, owner, guards = fixture(tmp_path)
    for key, value in dict(pr=pr, **extra).items():
        store.append_event(card, "link", owner, link_key=key, link_value=value)
    guards.update(
        expected_card_revision=raw_card_revision(store.fold(card)),
        expected_history_revision=history_revision(store, card),
        expected_pr=pr,
    )
    before = store._read_events(card)
    with pytest.raises(ValueError):
        archive_historical_pr(tmp_path, card, owner, **guards)
    assert store._read_events(card) == before


def test_cli_requires_all_guards_and_authorized_mutation(tmp_path, monkeypatch):
    store, card, owner, guards = fixture(tmp_path)
    args = ["archive-historical-pr", card, "--agent", owner]
    assert _run(tmp_path, *args).exit_code != 0
    for key, value in guards.items():
        args.extend(["--" + key.replace("_", "-"), value])
    from skcapstone import jarvis_emergency

    calls = []
    monkeypatch.setattr(jarvis_emergency, "authorize_coord_mutation", lambda *a: calls.append(a))
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["card_id"] == card
    assert calls[0][0] == owner


def test_intervening_outcome_during_projection_is_not_acknowledged(tmp_path, monkeypatch):
    store, card, owner, guards = fixture(tmp_path)
    original = CardEventLog.append

    def raced(self, event):
        store.append_event(card, "verdict", owner, verdict="FAIL")
        return original(self, event)

    monkeypatch.setattr(CardEventLog, "append", raced)
    with pytest.raises(ValueError, match="changed during projection"):
        archive_historical_pr(tmp_path, card, owner, **guards)
    assert _latest_outcome(CardStore(tmp_path), card)["verdict"] == "FAIL"


def test_cli_denied_actor_cannot_mutate(tmp_path, monkeypatch):
    store, card, owner, guards = fixture(tmp_path)
    from skcapstone import jarvis_emergency

    def denied(*args):
        raise ValueError("denied actor")

    monkeypatch.setattr(jarvis_emergency, "authorize_coord_mutation", denied)
    before = store._read_events(card)
    args = ["archive-historical-pr", card, "--agent", owner]
    for key, value in guards.items():
        args.extend(["--" + key.replace("_", "-"), value])
    assert _run(tmp_path, *args).exit_code != 0
    assert store._read_events(card) == before


def test_partial_native_repair_rejects_changed_claim_before_resume(tmp_path, monkeypatch):
    store, card, owner, guards = fixture(tmp_path)
    original = CardEventLog.append

    def interrupted(*args):
        raise ValueError("before first projection")

    monkeypatch.setattr(CardEventLog, "append", interrupted)
    with pytest.raises(ValueError, match="before first projection"):
        archive_historical_pr(tmp_path, card, owner, **guards)
    monkeypatch.setattr(CardEventLog, "append", original)
    store.append_event(card, "claim", owner, owner=owner, claim_revision="d" * 32)
    before = store._read_events(card)
    with pytest.raises(ValueError, match="claim changed"):
        archive_historical_pr(tmp_path, card, owner, **guards)
    assert store._read_events(card) == before
    assert store.fold(card).links["pr"] == guards["expected_pr"]


@pytest.mark.parametrize(
    "repository",
    [
        "",
        "team/project",
        "http://forge.example/team/project",
        "https://forge.example",
        "https://forge.example/a/b?q=1",
    ],
)
def test_unbound_or_invalid_repository_is_not_a_repair_target(tmp_path, repository):
    store, card, owner, guards = fixture(tmp_path)
    store.append_event(card, "link", owner, link_key="repository", link_value=repository)
    guards.update(
        expected_repository=repository,
        expected_card_revision=raw_card_revision(store.fold(card)),
        expected_history_revision=history_revision(store, card),
    )
    before = store._read_events(card)
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        archive_historical_pr(tmp_path, card, owner, **guards)
    assert store._read_events(card) == before


@pytest.mark.parametrize(
    "meta",
    [
        {"pr": "https://github.com/team/project/pull/78"},
        {"repository": "https://other.example/team/project.git"},
        {"head": "b" * 40},
    ],
)
def test_creation_metadata_cannot_be_rewritten_or_bypassed(tmp_path, meta):
    card, owner = "metadata1", "pi-metadata"
    store = CardStore(tmp_path)
    store.create(CardCore(id=card, title="Metadata fixture", meta=meta))
    store.append_event(card, "claim", owner, owner=owner, claim_revision="c" * 32)
    repository, pr = (
        "https://forge.example/team/project.git",
        "https://github.com/team/project/pull/78",
    )
    for key, value in {"repository": repository, "pr": pr, "commit": "a" * 40}.items():
        store.append_event(card, "link", owner, link_key=key, link_value=value)
    before = store._read_events(card)
    with pytest.raises(ValueError):
        archive_historical_pr(
            tmp_path,
            card,
            owner,
            expected_card_revision=raw_card_revision(store.fold(card)),
            expected_history_revision=history_revision(store, card),
            expected_claim_revision="c" * 32,
            expected_pr=pr,
            expected_repository=repository,
            transition_id="f" * 64,
        )
    assert store._read_events(card) == before
