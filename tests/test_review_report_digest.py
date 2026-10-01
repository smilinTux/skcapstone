"""Published and historical review report bindings share one conflict boundary."""
import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from skcapstone.seraph_review_contracts import ReviewPublicationError

CARD = "8b3ec3ea"
DIGEST = "d" * 64
ALIASES = ("review_evidence_sha256", "evidence_sha256", "reviewer_evidence_sha256")


def review(home, links, meta=None):
    """Build only temporary native review events, with exact report digest links."""
    store = CardStore(home)
    store.create(CardCore(id="1234abcd", title="[S] source", created_by="producer"))
    store.create(CardCore(
        id=CARD, title="[S][REVIEW] report binding", created_by="link",
        initial_labels=["review", "source-only", "parent-1234abcd"],
        meta={"link_source_card": "1234abcd", "link_head_revision": "a" * 40,
              "producer_identity": "producer", **(meta or {})},
    ))
    for key, value in links.items():
        store.append_event(CARD, "link", "reviewer", link_key=key, link_value=value)
    return store, LiveCardStoreGateway(home)


@pytest.mark.parametrize("keys", [(key,) for key in ALIASES] + [ALIASES])
def test_actual_published_key_and_legacy_aliases_resolve_same_digest(tmp_path, keys):
    _, gateway = review(tmp_path, dict.fromkeys(keys, DIGEST))
    assert gateway.read_card(CARD).evidence_sha256 == DIGEST


@pytest.mark.parametrize("old_key", ALIASES[1:])
@pytest.mark.parametrize("location", ["link", "meta"])
def test_published_digest_cannot_override_conflicting_historical_hash(tmp_path, old_key, location):
    links = {"review_evidence_sha256": DIGEST}
    meta = {}
    (links if location == "link" else meta)[old_key] = "e" * 64
    _, gateway = review(tmp_path, links, meta)
    with pytest.raises(ReviewPublicationError, match="card_binding_conflict"):
        gateway.read_card(CARD)


def test_new_report_digest_changes_exact_snapshot_revision(tmp_path):
    store, gateway = review(tmp_path, {"review_evidence_sha256": DIGEST})
    before = gateway.read_card(CARD)
    store.append_event(
        CARD, "link", "reviewer", link_key="review_evidence_sha256", link_value="e" * 64
    )
    after = gateway.read_card(CARD)
    assert after.evidence_sha256 == "e" * 64 and after.revision != before.revision
