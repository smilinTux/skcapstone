"""Retained builder custody must prove native operator ref amendments."""

import pytest

from skcapstone import source_ref_amendment as amendment
from skcapstone.fleet import builder_retry as retry
from tests.test_source_ref_amendment import CARD, REF, binding  # noqa: F401


def attempt(store):
    """Bind a real native claim to an already-stopped production attempt."""
    owner = f"pi-glm-builder-node-worker-{CARD}"
    for label in ["sk-m", "source-only"]:
        store.append_event(CARD, "add_label", "lumina-nor", label=label)
    claim = store.append_event(CARD, "claim", owner, owner=owner)
    store.append_event(CARD, "move", owner, column="doing")
    card = store.fold(CARD)
    request = {
        "schema": "skfleet.builder-dispatch/v1",
        "card_id": CARD,
        "request_id": "a" * 64,
        "node": "node-worker",
        "labels": sorted(card.labels),
        "repository": card.meta["repository"],
        "base_ref": REF,
        "base_revision": card.meta["base_revision"],
        "production": {"host": "worker"},
    }
    status = {
        **request,
        "schema": "skfleet.builder-dispatch-status/v1",
        "state": "awaiting-evidence",
        "owner": owner,
        "claim_revision": claim["event_id"],
        "attempt": 1,
        "invocation": "d" * 32,
        "unit": retry.builder.production_builder.unit_name(request, 1),
        "claim_released": False,
    }
    return request, status


def test_retained_claim_accepts_exact_native_operator_amendment(binding, tmp_path):  # noqa: F811
    store, link, probes, _ = binding
    link()
    request, status = attempt(store)
    before = store._read_events(CARD)
    _, card = retry.check_custody(tmp_path, request, status)
    assert card.owner == status["owner"]
    assert probes == [(request["repository"], REF, request["base_revision"])]
    assert store._read_events(CARD) == before


@pytest.mark.parametrize("bad", ["worker", "revision", "repository", "request-ref", "unreachable"])
def test_amended_retry_refusals(binding, tmp_path, monkeypatch, bad):  # noqa: F811
    store, link, _, _ = binding
    extra = {"base_revision": "b" * 40} if bad == "revision" else {}
    if bad == "repository":
        extra = {"repository": "https://other.test/repo.git"}
    link("pi-glm-worker" if bad == "worker" else "lumina-nor", **extra)
    request, status = attempt(store)
    if bad == "request-ref":
        request["base_ref"] = "wrong"
    if bad == "unreachable":

        def unavailable(*args):
            raise ValueError("source amended ref fetch or ancestry proof failed")

        monkeypatch.setattr(amendment, "prove_ref", unavailable)
    before = store._read_events(CARD)
    with pytest.raises(ValueError):
        retry.check_custody(tmp_path, request, status)
    assert store._read_events(CARD) == before
