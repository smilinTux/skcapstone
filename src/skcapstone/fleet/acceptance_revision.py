"""Exact legacy outcome-projection compatibility for sealed review acceptance."""

from skcoord.card_store import CardStore

from ..seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome, card_revision
from ..seraph_review_contracts import _digest
from .production_review_evidence import ReviewEvidenceError


def review_start_revision(home, item):
    """Return the live fence only when the sealed review facts are identical.

    Older snapshots hashed a legacy overlay of the same native PASS link.
    Reconstruct only that defined projection. The old digest must match using
    today's entire card and bindings; any other semantic change still refuses.
    This neither edits the context nor synthesizes a board event.
    """
    store = CardStore(home)
    card = store.fold(item["card"])
    snapshot = LiveCardStoreGateway(home).read_card(item["card"], _store=store)
    if snapshot.revision == item["revision"]:
        return snapshot.revision
    outcome = _latest_outcome(store, item["card"])
    if (
        card is None
        or card.owner != item["owner"]
        or card.meta.get("_claim_revision") != item["claim"]
        or outcome.get("action") != "link"
        or outcome.get("link_key") != "verdict"
        or outcome.get("link_value") != "PASS"
        or outcome.get("writer") != item["owner"]
        or outcome.get("expected_claim_revision") != item["claim"]
        or not outcome.get("event_id")
        or outcome not in store._read_events(item["card"])
    ):
        raise ReviewEvidenceError("sealed review revision projection is unproven")
    bindings = {
        key: getattr(snapshot, key)
        for key in (
            "repository",
            "number",
            "head_sha",
            "producer_identity",
            "reviewer_identity",
            "unresolved_review",
            "candidate_evidence_sha256",
        )
    }
    payload = {"card": card_revision(card), "bindings": bindings, "outcome": outcome}
    if _digest(payload) != snapshot.revision:
        raise ReviewEvidenceError("current review outcome projection differs")
    projection = {
        key: outcome[key] for key in ("ts", "writer", "seq", "action", "link_key", "link_value")
    }
    projection["origin"] = "legacy-overlay"
    if _digest(dict(payload, outcome=projection)) != item["revision"]:
        raise ReviewEvidenceError("sealed review facts changed beyond outcome projection")
    return snapshot.revision


def source_recipe_revision(home, item):
    """Accept only a source revision changed by its test-profile recipe link."""
    store = CardStore(home)
    card = store.fold(item["card"])
    snapshot = LiveCardStoreGateway(home).read_card(item["card"], _store=store)
    if (
        card is None
        or card.owner != item["owner"]
        or card.meta.get("_claim_revision") != item["claim"]
        or snapshot.revision == item["revision"]
        or "test_profile_recipe" not in card.links
    ):
        raise ReviewEvidenceError("source test recipe projection is unproven")
    links = dict(card.links)
    links.pop("test_profile_recipe")
    projected = card.model_copy(update={"links": links}, deep=True)
    bindings = {
        key: getattr(snapshot, key)
        for key in (
            "repository",
            "number",
            "head_sha",
            "producer_identity",
            "reviewer_identity",
            "unresolved_review",
            "candidate_evidence_sha256",
        )
    }
    payload = {
        "card": card_revision(projected),
        "bindings": bindings,
        "outcome": _latest_outcome(store, item["card"]),
    }
    if _digest(payload) != item["revision"]:
        raise ReviewEvidenceError("source changed beyond test profile recipe")
    return snapshot.revision
