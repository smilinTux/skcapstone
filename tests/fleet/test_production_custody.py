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
from tests.fleet.test_source_bundle import publish, source  # noqa: F401
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
def review_candidate(source, monkeypatch):  # noqa: F811
    """Real typed native claim and transferred Git bundle, stopped remote unit."""
    from skcapstone.fleet import production_builder, production_review_custody
    from skcapstone.fleet.production_review_finish import once

    source["store"].append_event(source["card"], "add_label", "operator", label="sk-s")
    source["store"].append_event(source["card"], "verdict", source["owner"], **source["outcome"])
    manifest = publish(source)
    policy = {"authority_host": "control"}
    production = {
        "host": "worker",
        "authority": "control",
        "family": "zai",
        "model": "qualified-model",
        "gateway_backend": "zai",
        "policy_sha256": production_builder.digest(policy),
    }
    request = {
        **source["request"],
        "base_ref": "main",
        "schema": "skfleet.builder-dispatch/v1",
        "node": "node-worker",
        "request_id": "d" * 64,
        "production": production,
        "labels": ["sk-s", "source-only"],
    }
    status = {
        "schema": "skfleet.builder-dispatch-status/v1",
        "node": "node-worker",
        "card_id": source["card"],
        "owner": source["owner"],
        "claim_revision": source["claim"],
        "state": "awaiting-review",
        "exit_code": 0,
        "claim_released": False,
        "request_id": request["request_id"],
        "attempt": 2,
        "unit": production_builder.unit_name(request, 2),
        "invocation": "e" * 32,
        "production": production,
        "source_artifact": manifest,
        "writer": {"role": "sknoded", "node": "node-worker"},
        "route_preflight": {"requested_identity": "qualified-model", "provider": "zai"},
    }
    root = source["home"] / "fleet"
    request_path = root / "dispatch/node-worker" / (source["card"] + ".json")
    status_path = root / "status/node-worker/dispatch" / (source["card"] + ".json")
    once(request_path, request)
    once(status_path, status)
    monkeypatch.setattr(production_review_custody, "unit_terminal", lambda *a, **k: {})
    source.update(
        policy=policy,
        status=status,
        status_path=status_path,
        dispatch=request,
        request_path=request_path,
    )
    return source


def candidate_allowed(value, process=None):
    """Exercise the exact native eligibility helper used by the fleet opener."""
    from skcapstone.fleet.production_custody import reviewable_source_candidate

    events = value["store"]._read_events(value["card"])
    outcome = next(row for row in reversed(events) if row.get("action") == "verdict")
    return reviewable_source_candidate(
        value["home"],
        value["card"],
        outcome["ts"],
        (
            value["owner"],
            str(value["shared"]),
            value["outcome"]["candidate_sha256"],
            value["head"],
            value["tree"],
            value["outcome"]["candidate_ref"],
        ),
        policy=value["policy"],
        process_check=process or (lambda card: {"sessions": [], "units": []}),
    )


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_exact_production_candidate_retains_claim_and_enters_existing_opener(
    review_candidate, tmp_path
):
    from tests.test_skfleet_provisional_opener import OpenerHarness

    value = review_candidate
    before = value["store"]._read_events(value["card"])
    assert candidate_allowed(value)
    harness = OpenerHarness(tmp_path / "opener")
    harness.outcome(value["card"])
    harness.states[value["card"]] = "claimed"
    outcome = next(row for row in reversed(before) if row.get("action") == "verdict")
    harness.events[value["card"]] = before
    harness.outcomes[value["card"]] = (outcome["ts"], "PASS_FOR_REVIEW")
    harness.ns.update(
        CARDS=str(value["home"] / "cards"),
        Path=Path,
        PRODUCTION_POLICY=value["policy"],
        HOST="control",
        _card_process_snapshot=lambda card: {"sessions": [], "units": []},
    )
    selected = harness.ns["_eligible_provisional_reviews"](1)
    assert len(selected) == 1
    assert selected[0][0] == value["card"]
    assert selected[0][-3:] == (value["head"], value["tree"], value["outcome"]["candidate_ref"])
    for state in ("complete", "void", "ambiguous"):
        harness.states[value["card"]] = state
        assert harness.ns["_eligible_provisional_reviews"](1) == []
    harness.states[value["card"]] = "claimed"
    harness.ns["PRODUCTION_POLICY"] = None
    assert harness.ns["_eligible_provisional_reviews"](1) == []
    harness.ns["PRODUCTION_POLICY"] = value["policy"]
    harness.ns["HOST"] = "other"
    assert harness.ns["_eligible_provisional_reviews"](1) == []
    harness.ns["HOST"] = "control"
    harness.cards = value["home"] / "cards"
    assert harness.open(1) == 1
    assert harness.open(1) == 0
    assert len(harness.calls) == 1
    # Existing active-review exclusion remains authoritative even for valid custody.
    harness.ns["_reviews_by_parent"] = lambda: {value["card"]: {"1234abcd"}}
    harness.states["1234abcd"] = "claimed"
    assert harness.ns["_eligible_provisional_reviews"](1) == []
    assert value["store"]._read_events(value["card"]) == before


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "claim",
        "request",
        "request-source",
        "policy",
        "running",
        "unknown-process",
        "live-process",
        "unknown-unit",
        "hold",
        "done",
        "archived",
        "ordinary",
        "outcome",
        "stale-outcome",
        "bundle",
        "evidence",
        "manifest-claim",
        "manifest-tree",
        "writer",
        "request-labels",
    ],
)
def test_production_opener_rejects_inexact_or_unavailable_custody(
    review_candidate, monkeypatch, mutation
):
    from skcapstone.fleet import production_review_custody

    value = review_candidate
    store, card = value["store"], value["card"]
    process = None
    if mutation in {"owner", "claim", "request", "running", "writer"}:
        key, replacement = {
            "owner": ("owner", "other"),
            "claim": ("claim_revision", "f" * 32),
            "request": ("request_id", "f" * 64),
            "running": ("state", "running"),
            "writer": ("writer", {"role": "model", "node": "node-worker"}),
        }[mutation]
        value["status"][key] = replacement
        value["status_path"].write_text(json.dumps(value["status"]))
    elif mutation == "request-source":
        value["dispatch"]["base_revision"] = "f" * 40
        value["request_path"].write_text(json.dumps(value["dispatch"]))
    elif mutation == "request-labels":
        value["dispatch"]["labels"] = ["source-only"]
        value["request_path"].write_text(json.dumps(value["dispatch"]))
    elif mutation == "policy":
        value["policy"]["authority_host"] = "other"
    elif mutation in {"unknown-process", "live-process"}:

        def process(card):
            return {} if mutation == "unknown-process" else {"sessions": ["live"], "units": []}

    elif mutation == "unknown-unit":

        def unavailable(*args, **kwargs):
            raise ValueError("exact managed worker death unproven")

        monkeypatch.setattr(production_review_custody, "unit_terminal", unavailable)
    elif mutation == "hold":
        store.append_event(card, "add_label", "operator", label="hold")
    elif mutation == "done":
        store.append_event(card, "move", "operator", column="done")
    elif mutation == "archived":
        store.append_event(card, "archive", "operator")
    elif mutation == "ordinary":
        store.append_event(card, "remove_label", "operator", label="source-only")
    elif mutation == "outcome":
        store.append_event(card, "verdict", value["owner"], verdict="BLOCKED")
    elif mutation == "stale-outcome":
        store.append_event(card, "claim", value["owner"], owner=value["owner"])
    else:
        manifest_path = Path(value["status"]["source_artifact"]["manifest"])
        manifest = json.loads(manifest_path.read_text())
        if mutation in {"bundle", "evidence"}:
            key, suffix = (
                ("bundle_sha256", ".bundle")
                if mutation == "bundle"
                else ("evidence_sha256", ".md")
            )
            (manifest_path.parent / (manifest[key] + suffix)).write_bytes(b"changed")
        else:
            manifest["claim_revision" if mutation == "manifest-claim" else "tree"] = "f" * (
                32 if mutation == "manifest-claim" else 40
            )
            manifest_path.write_text(json.dumps(manifest))
    assert not candidate_allowed(value, process)


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
