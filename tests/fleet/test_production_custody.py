"""Ended production source custody survives legacy absence and TTL cleanup."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet.paths import FleetPaths
from skcapstone.fleet.production_receipts import persist_production_snapshot
from skcapstone.seat_runtime import append_production_launch_receipt
from tests.fleet.test_claim_expiry_reaper import _load, _observation
from tests.fleet.test_production_receipts import snapshot
from tests.test_skfleet_reaper_provenance import _reaper_fixture

CARD = "a6397c0d"
OWNER = "pi-deepseek-chiap08-" + CARD
CLAIM = "a" * 32
SOURCE = {
    "repository": "https://github.com/example/public.git",
    "base_ref": "main",
    "base_revision": "b" * 40,
}


@pytest.fixture
def custody(tmp_path):
    home = tmp_path / ".skcapstone"
    home.mkdir()
    store = CardStore(home)
    store.create(
        CardCore(
            id=CARD,
            title="[M] Source",
            created_by=OWNER,
            initial_labels=["source-only"],
            meta=SOURCE,
        )
    )
    store.append_event(CARD, "claim", OWNER, owner=OWNER, claim_revision=CLAIM)
    ref = persist_production_snapshot(home, snapshot())
    event = append_production_launch_receipt(
        home,
        CARD,
        actor=OWNER,
        claim_revision=CLAIM,
        launched=True,
        route_identity={
            "provider": "skgateway",
            "logical_route": "sk-m",
            "capacity_domains": ["deepseek"],
            "model_or_bucket": "gateway-current",
            "production_snapshot": ref,
        },
    )
    return home, store, event


@pytest.mark.parametrize("proposal", [False, True])
def test_absent_production_producer_is_not_declared_dead_or_released(tmp_path, custody, proposal):
    home, store, event = custody
    if proposal:
        store.append_event(CARD, "verdict", OWNER, verdict="PASS_FOR_REVIEW")
    ns, releases, messages = _reaper_fixture(
        tmp_path, card_id=CARD, owner=OWNER, claim_revision=CLAIM, launch_revision=CLAIM
    )
    ns["PRODUCTION_POLICY"] = {"authority_host": "chiap08"}
    ns["default_fleet_paths"] = lambda: FleetPaths(home / "fleet")
    assert ns["reap_dead_claims"]() == 0
    assert releases == []
    assert not list((tmp_path / "card_events").glob("*.jsonl"))


def test_ttl_cannot_release_exact_production_custody(tmp_path, custody):
    ns = _load(tmp_path)
    ns["PRODUCTION_POLICY"] = {"authority_host": "chiap08"}
    ns["default_fleet_paths"] = lambda: FleetPaths(custody[0] / "fleet")
    obs = _observation(OWNER, cid=CARD)
    obs = type(obs)(CARD, OWNER, CLAIM, obs.last_owner_event_at)
    calls = []
    count = ns["_expire_idle_claims"](
        [obs],
        runner=lambda argv: calls.append(argv) or SimpleNamespace(returncode=0),
        state=lambda card: "claimed",
        env={"SKFLEET_CLAIM_TTL_MODE": "enforce"},
        dry=False,
    )
    assert count == 0 and calls == []


def test_custody_guard_requires_native_exact_receipt_and_source(custody):
    from skcapstone.fleet.production_custody import retains_source_custody

    home, store, event = custody
    assert event["source_binding"] == SOURCE
    assert retains_source_custody(home, CARD, OWNER, CLAIM, fleet_paths=FleetPaths(home / "fleet"))
    assert not retains_source_custody(
        home, CARD, OWNER, "f" * 32, fleet_paths=FleetPaths(home / "fleet")
    )
    assert not retains_source_custody(
        home, CARD, "jarvis", CLAIM, fleet_paths=FleetPaths(home / "fleet")
    )
    store.append_event(CARD, "link", OWNER, link_key="base_revision", link_value="c" * 40)
    assert not retains_source_custody(
        home, CARD, OWNER, CLAIM, fleet_paths=FleetPaths(home / "fleet")
    )


def test_missing_or_corrupt_receipt_never_infers_custody_from_owner_name(custody):
    from skcapstone.fleet.production_custody import retains_source_custody

    home, store, event = custody
    path = Path(event["route_identity"]["production_snapshot"]["path"])
    path.write_text("{}")
    assert not retains_source_custody(
        home, CARD, OWNER, CLAIM, fleet_paths=FleetPaths(home / "fleet")
    )


@pytest.mark.parametrize(
    "mutation", ["missing-launch", "wrong-writer", "old-claim", "not-launched", "wrong-source"]
)
def test_native_receipt_must_prove_exact_launch(custody, monkeypatch, mutation):
    from skcapstone.fleet.production_custody import retains_source_custody

    home, store, event = custody
    original = CardStore._read_events

    def events(self, card):
        rows = [dict(row) for row in original(self, card)]
        for row in rows:
            if row.get("action") != "production_assignment_launch":
                continue
            if mutation == "missing-launch":
                row["action"] = "other"
            if mutation == "wrong-writer":
                row["writer"] = "jarvis"
            if mutation == "old-claim":
                row["claim_revision"] = "f" * 32
            if mutation == "not-launched":
                row["launched"] = False
            if mutation == "wrong-source":
                row["source_binding"] = {**SOURCE, "base_revision": "f" * 40}
        return rows

    monkeypatch.setattr(CardStore, "_read_events", events)
    assert not retains_source_custody(
        home, CARD, OWNER, CLAIM, fleet_paths=FleetPaths(home / "fleet")
    )


@pytest.mark.parametrize("relocated", [False, True])
def test_remote_generation_protects_pending_evidence_without_local_launch_event(
    custody, relocated
):
    from skcapstone.fleet.production_custody import retains_source_custody

    home, store, event = custody
    Path(event["route_identity"]["production_snapshot"]["path"]).unlink()
    request_id = "d" * 64
    production = {
        "host": "chiap03",
        "authority": "chiap08",
        "policy_sha256": "e" * 64,
        "model": "gateway-current",
        "gateway_backend": "deepseek",
    }
    request = {
        "schema": "skfleet.builder-dispatch/v1",
        "card_id": CARD,
        "node": "node-chiap03",
        "request_id": request_id,
        "production": production,
        **SOURCE,
    }
    status = {
        "schema": "skfleet.builder-dispatch-status/v1",
        "card_id": CARD,
        "node": "node-chiap03",
        "request_id": request_id,
        "production": production,
        "owner": OWNER,
        "claim_revision": CLAIM,
        "state": "awaiting-evidence",
        "attempt": 1,
        "unit": f"skfleet-builder-{CARD}-{request_id}-1.service",
        "route_preflight": {"requested_identity": "gateway-current", "provider": "deepseek"},
    }
    paths = FleetPaths(home.parent / "relocated-fleet" if relocated else home / "fleet")
    rp = paths.root / "dispatch/node-chiap03" / f"{CARD}.json"
    sp = paths.status_path("node-chiap03", "dispatch", CARD)
    for path, value in ((rp, request), (sp, status)):
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    assert retains_source_custody(home, CARD, OWNER, CLAIM, fleet_paths=paths)
    status["request_id"] = "f" * 64
    sp.write_text(json.dumps(status))
    assert not retains_source_custody(home, CARD, OWNER, CLAIM, fleet_paths=paths)
