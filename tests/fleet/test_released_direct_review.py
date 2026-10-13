"""An exact released modern producer can enter review without a new claim."""

import json
from pathlib import Path

import pytest

from skcapstone.fleet import production_acceptance as acceptance
from skcapstone.fleet import production_review_custody
from skcapstone.fleet.production_receipts import persist_production_snapshot
from skcapstone.fleet.production_review_finish import once
from skcapstone.guarded_review_work import open_guarded_review
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.fleet.test_production_receipts import snapshot
from tests.fleet.test_source_bundle import publish, source  # noqa: F401


@pytest.fixture
def released_direct(source, monkeypatch):  # noqa: F811
    value = source
    home, store, card, owner = (value[k] for k in ("home", "store", "card", "owner"))
    monkeypatch.setattr(acceptance.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(production_review_custody, "unit_terminal", lambda *a, **k: {})
    route = {
        "provider": "skgateway",
        "logical_route": "sk-m",
        "capacity_domains": ["deepseek"],
        "model_or_bucket": "gateway-current",
        "production_snapshot": persist_production_snapshot(home, snapshot()),
    }
    store.append_event(
        card,
        "production_assignment_launch",
        owner,
        schema="skfleet.production-assignment-launch/v1",
        worker=owner,
        claim_revision=value["claim"],
        launched=True,
        route_identity=route,
        source_binding={
            "repository": value["remote"],
            "base_revision": value["base"],
            "base_ref": "main",
        },
    )
    # Source fixture's earlier outcome remains history; this exact latest one follows launch.
    store.append_event(card, "verdict", owner, **value["outcome"])
    artifact = publish(value)
    manifest = json.loads(Path(artifact["manifest"]).read_text())
    receipt = {
        "schema": "skfleet.direct-seat/v1",
        "card": card,
        "owner": owner,
        "claim_revision": value["claim"],
        "host": "worker",
        "lane": "glm",
        "pid": 123,
        "unit": f"skfleet-worker-glm-{card}.service",
        "invocation": "e" * 32,
        "completion_state": "awaiting-review",
        "exit_code": 0,
        "route_schema": "skfleet.runtime-route/v1",
        **{
            k: route[k]
            for k in ("provider", "logical_route", "capacity_domains", "model_or_bucket")
        },
        "source_disposition": {
            "state": "awaiting-review",
            "process_terminal": True,
            "claim_released": False,
            "source_artifact": artifact,
        },
    }
    path = home / "fleet/direct-seats" / f"{owner}.json"
    once(path, receipt)
    prior = LiveCardStoreGateway(home).read_card(card).revision
    store.append_event(
        card,
        "release_claim",
        "historical-reaper",
        released_owner=owner,
        expected_claim_revision=value["claim"],
        abandon_reason="error",
    )
    value.update(manifest=manifest, receipt=receipt, receipt_path=path, prior_revision=prior)
    return value


def open_review(value):
    return open_guarded_review(
        value["home"],
        value["card"],
        value["owner"],
        expected_source_revision=LiveCardStoreGateway(value["home"])
        .read_card(value["card"])
        .revision,
        expected_claim_revision=value["claim"],
    )


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_released_direct_terminal_remains_readable(released_direct):
    v = released_direct
    terminal = acceptance._producer_terminal(v["home"], v["store"].fold(v["card"]), v["manifest"])
    assert terminal["owner"] == v["owner"]
    assert terminal["receipt_kind"] == "direct-seat"


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_opens_one_review_without_restoring_claim_or_changing_source(released_direct):
    v = released_direct
    before = v["store"]._read_events(v["card"])
    result = open_review(v)
    assert result.launchable
    assert open_review(v).created is False
    review = v["store"].fold(result.review_card_id)
    assert review.meta["source_revision"] == v["prior_revision"]
    assert v["store"].fold(v["card"]).owner is None
    assert v["store"]._read_events(v["card"]) == before
    assert json.loads(v["receipt_path"].read_text()) == v["receipt"]


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "mutation", ["claim", "outcome", "hold", "bundle", "receipt", "route", "live"]
)
def test_released_terminal_refuses_changed_proof(released_direct, monkeypatch, mutation):
    v = released_direct
    store, card = v["store"], v["card"]
    if mutation == "claim":
        store.append_event(card, "claim", "new-worker", owner="new-worker")
    elif mutation == "outcome":
        store.append_event(card, "verdict", v["owner"], verdict="BLOCKED")
    elif mutation == "hold":
        store.append_event(card, "add_label", "lumina-nor", label="hold")
    elif mutation == "bundle":
        (
            v["home"]
            / "evidence/work"
            / card
            / "source-bundles"
            / (v["manifest"]["bundle_sha256"] + ".bundle")
        ).write_bytes(b"changed")
    elif mutation in {"receipt", "route"}:
        receipt = dict(v["receipt"])
        receipt["exit_code" if mutation == "receipt" else "model_or_bucket"] = (
            1 if mutation == "receipt" else "different"
        )
        v["receipt_path"].write_text(json.dumps(receipt))
    else:

        def refuse(*a, **k):
            raise ValueError("unit still live or unknown")

        monkeypatch.setattr(production_review_custody, "unit_terminal", refuse)
    before = store._read_events(card)
    with pytest.raises((ValueError, acceptance.ReviewEvidenceError)):
        acceptance._producer_terminal(v["home"], store.fold(card), v["manifest"])
    assert store._read_events(card) == before
