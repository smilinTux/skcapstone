"""A producer publishes exact commit, branch and candidate through native guards."""

import hashlib
import json

from skcapstone.card_store import CardStore
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_provisional_verdict_producer import COMMIT, REF, TREE, _candidate, _run, _seed


def test_claim_bound_producer_handoff_preserves_completion_links(tmp_path):
    """Exercise the real CLI sequence without changing or completing its claim."""
    card, owner, claim = "1234abcd", "pi-deepseek-builder-node-fixture", "c" * 32
    _seed(tmp_path, card)
    store = CardStore(tmp_path)
    store.append_event(card, "claim", owner, owner=owner, claim_revision=claim)
    for key, value in [("commit_sha", COMMIT), ("branch", "skcapstone:fix/candidate")]:
        revision = LiveCardStoreGateway(tmp_path).read_card(card).revision
        result = _run(
            tmp_path,
            "link",
            card,
            key,
            value,
            "--agent",
            owner,
            "--expected-source-revision",
            revision,
            "--expected-claim-revision",
            claim,
            "--transition-id",
            hashlib.sha256(key.encode()).hexdigest(),
            "--json",
        )
        assert result.exit_code == 0, result.output
        receipt = json.loads(result.output)
        assert receipt["card_id"] == card
        current = LiveCardStoreGateway(tmp_path).read_card(card).revision
        assert receipt["source_revision"] == current
        assert store.fold(card).links[key] == value
    candidate = _candidate(tmp_path, card)
    candidate.chmod(0o600)
    revision = LiveCardStoreGateway(tmp_path).read_card(card).revision
    result = _run(
        tmp_path,
        "verdict",
        card,
        "PASS_FOR_REVIEW",
        "--candidate",
        str(candidate),
        "--commit",
        COMMIT,
        "--tree",
        TREE,
        "--ref",
        REF,
        "--agent",
        owner,
        "--expected-source-revision",
        revision,
        "--expected-claim-revision",
        claim,
        "--expected-candidate-sha256",
        hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "--transition-id",
        "d" * 64,
    )
    assert result.exit_code == 0, result.output
    row = store.fold(card)
    assert row.owner == owner and row.meta["_claim_revision"] == claim
    assert row.links["commit_sha"] == COMMIT
    events = store._read_events(card)
    assert len([event for event in events if event["action"] == "verdict"]) == 1
    assert not any(event["action"] in {"complete", "release_claim"} for event in events)
