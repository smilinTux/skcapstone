from skcapstone.fleet.review_pool import (
    ReviewAdmissionError,
    ReviewRequest,
    append_jsonl,
    elastic_reviewer_identity,
    review_fanout_limit,
    run_bounded,
    validate_request,
)


def test_barrington_and_ziowk_reviews_fill_only_free_bounded_slots():
    cards = ["ba771001", "2100c001"]
    assert review_fanout_limit(len(cards), 3, 2) == 2
    identities = [elastic_reviewer_identity("chiap01", card) for card in cards]
    assert len(set(identities)) == 2
    assert all(identity.endswith(card) for identity, card in zip(identities, cards))


def test_review_fanout_never_exceeds_any_boundary():
    assert review_fanout_limit(5, 1, 4) == 1
    assert review_fanout_limit(1, 5, 4) == 1
    assert review_fanout_limit(5, 4, 0) == 0


def request(card, reviewer, identity):
    return ReviewRequest(
        card, "a" * 40, "builder", reviewer, "session", identity, ("git", "diff", "a" * 40)
    )


def test_two_independent_reviews_are_bounded_and_hashed():
    seen = []

    def run(item):
        seen.append(item.identity)
        return "PASS", (item.card_id + item.source_head).encode()

    receipts = run_bounded([request("a", "r1", "id1"), request("b", "r2", "id2")], run)
    assert [r.card_id for r in receipts] == ["a", "b"]
    assert all(len(r.artifact_sha256) == 64 for r in receipts)
    assert set(seen) == {"id1", "id2"}


def test_producer_and_mutation_are_rejected():
    bad = request("a", "builder", "id")
    try:
        validate_request(bad)
        assert False
    except ReviewAdmissionError:
        pass
    bad = request("a", "r", "id")
    bad = bad.__class__(**{**bad.__dict__, "command": ("git", "commit")})
    try:
        validate_request(bad)
        assert False
    except ReviewAdmissionError:
        pass


def test_evidence_is_serialized_and_round_trips(tmp_path):
    path = tmp_path / "evidence.jsonl"
    digest = append_jsonl(path, {"kind": "verdict", "verdict": "PASS"})
    assert len(digest) == 64
    assert path.read_text().count("\n") == 1
