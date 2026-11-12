"""Isolated native review fixtures for producer guards, without live admission."""

import hashlib

from skcapstone.card_store import CardCore, CardStore
from skcapstone.provisional_verdict import candidate_evidence
from tests.test_provisional_verdict_producer import COMMIT, REF, TREE, _candidate


def prepared_review(home):
    """Seed a synthetic independent source-only review using native test events."""
    source, review = "1234abcd", "abcd1234"
    producer = "pi-deepseek-builder-fixture-" + source
    store = CardStore(home)
    repository = "https://github.com/example/fixture.git"
    store.create(
        CardCore(
            id=source,
            title="[S] Source fixture",
            created_by=producer,
            initial_labels=["source-only"],
            meta={"repository": repository, "base_ref": "main", "base_revision": "e" * 40},
        )
    )
    store.append_event(source, "claim", producer, owner=producer, claim_revision="c" * 32)
    artifact = _candidate(home, source)
    store.append_event(
        source,
        "verdict",
        producer,
        verdict="PASS_FOR_REVIEW",
        **candidate_evidence(artifact, COMMIT, TREE, REF),
    )
    store.create(
        CardCore(
            id=review,
            title="[S][REVIEW] Independent source fixture",
            created_by="link",
            initial_labels=["source-only", "review", "seat-seraph", "parent-" + source],
            meta={
                "repository": repository,
                "link_source_card": source,
                "link_head_revision": COMMIT,
                "producer_identity": producer,
                "candidate_evidence_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            },
        )
    )
    return store, source, producer, artifact, review
