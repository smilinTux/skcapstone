"""Real node-loop and temporary CardStore integration, without worker launch."""

from __future__ import annotations

import copy
import json
import socket

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import crew_controller as controller
from skcapstone.fleet import crew_requests, sknoded, store
from skcapstone.fleet.crew_store import CrewStore
from skcapstone.fleet.paths import FleetPaths


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    """Create an owned parent and provision only an isolated fleet kill switch."""
    monkeypatch.setattr(socket, "gethostname", lambda: "fixture-node")
    home = tmp_path / "coord"
    home.mkdir()
    paths = FleetPaths(home / "fleet")
    cards = CardStore(home)
    cards.create(
        CardCore(
            id="aabb0011",
            title="[M] Deliver boundary",
            description="No external actions",
            acceptance_criteria=["Integrate verified output"],
            initial_owner="jarvis",
            initial_claim_revision="parent-generation",
            initial_labels=["source-only"],
            meta={
                "repository": "https://example.invalid/repo.git",
                "base_ref": "main",
                "base_revision": "a" * 40,
            },
        )
    )
    operator = store.Writer(role="operator", node="fixture-node", identity="fixture")
    store.set_frozen(paths, False, writer=operator)
    packet = {
        "schema": "skfleet.crew/v1",
        "crew_id": "crew-test",
        "parent_id": "aabb0011",
        "parent_claim_revision": "parent-generation",
        "coordinator_node": "fixture-node",
        "owner_paths": ["src/owner.py"],
        "max_active_helpers": 2,
        "max_helpers": 4,
        "slots": [
            {
                "slot_id": "verify",
                "role": "verification",
                "trigger": "initial",
                "packet": {
                    "request_id": "verify-template",
                    "title": "[S] Verify boundary",
                    "objective": "Return exact isolated findings",
                    "criteria": ["Exact evidence"],
                    "allowed_paths": [],
                    "verification_commands": ["pytest -q tests/test_boundary.py"],
                },
            }
        ],
    }
    return home, paths, cards, operator, packet


def register(scenario):
    """Persist a real owner mandate through the public operation."""
    home, paths, _, _, packet = scenario
    return crew_requests.register(paths, home, "jarvis", packet)


def cycle(scenario):
    """Use the production reconciliation function with explicit fixture roots."""
    home, paths, *_ = scenario
    return controller.reconcile_crews(paths, home, "fixture-node")


def test_real_node_hook_assigns_once_for_thirty_unchanged_cycles(scenario, monkeypatch):
    home, paths, cards, _, _ = scenario
    register(scenario)
    monkeypatch.setattr(sknoded, "node_capacity", lambda: {"cores": 4, "ram_gb": 8, "disk_gb": 40})
    monkeypatch.setattr(sknoded, "node_inventory", lambda: {})
    before = cards.fold("aabb0011").model_dump(mode="json")
    first = sknoded.run_once(paths, "fixture-node")["crew"]
    assert len(first["created"]) == 1
    for _ in range(30):
        assert sknoded.run_once(paths, "fixture-node")["crew"]["created"] == []
    assert len(cards.list_cards()) == 2
    helper = cards.fold(first["created"][0])
    assert helper.owner is None
    assert helper.meta["helper_parent_id"] == "aabb0011"
    assert cards.fold("aabb0011").model_dump(mode="json") == before


def test_no_manifest_is_no_store_write(scenario):
    _, paths, *_ = scenario
    assert cycle(scenario) is None
    assert not (paths.root / "crews").exists()


def test_frozen_means_zero_automatic_intake_or_creation(scenario):
    _, paths, cards, operator, packet = scenario
    register(scenario)
    store.set_frozen(paths, True, writer=operator)
    assert cycle(scenario)["created"] == []
    assert len(cards.list_cards()) == 1
    assert crew_requests.status(paths, packet["crew_id"])["requests"] == []


def test_wrong_node_does_not_consume(scenario):
    home, paths, cards, *_ = scenario
    register(scenario)
    assert controller.reconcile_crews(paths, home, "other-node") is None
    assert len(cards.list_cards()) == 1


def test_explicit_small_request_does_not_wait_for_stall(scenario):
    home, paths, cards, _, packet = scenario
    packet["slots"][0]["trigger"] = "support-request"
    register(scenario)
    crew_requests.submit_support(
        paths,
        home,
        "jarvis",
        {
            "schema": "skfleet.crew-support/v1",
            "crew_id": "crew-test",
            "request_id": "quick",
            "slot_id": "verify",
            "requester_card_id": "aabb0011",
            "requester_claim_revision": "parent-generation",
            "evidence_sha256": "b" * 64,
            "reason": "Independent two-minute useful verification",
        },
    )
    assert len(cycle(scenario)["created"]) == 1
    assert len(cards.list_cards()) == 2


def test_crash_after_create_replays_same_child(scenario, monkeypatch):
    _, _, cards, *_ = scenario
    register(scenario)
    original = CrewStore.save_request
    failed = False

    def interrupted(self, row):
        nonlocal failed
        if row["state"] == "assigned" and not failed:
            failed = True
            raise OSError("simulated crash after helper creation")
        return original(self, row)

    monkeypatch.setattr(CrewStore, "save_request", interrupted)
    assert cycle(scenario)["crews"][0]["state"] == "held"
    assert len(cards.list_cards()) == 2
    assert len(cycle(scenario)["created"]) == 1
    assert len(cards.list_cards()) == 2
    assert cycle(scenario)["created"] == []


def test_concurrency_limit_parks_independent_requests(scenario):
    _, _, cards, _, packet = scenario
    packet["max_active_helpers"] = 1
    second = copy.deepcopy(packet["slots"][0])
    second["slot_id"] = "diagnose"
    second["packet"]["request_id"] = "diagnose-template"
    packet["slots"].append(second)
    register(scenario)
    assert len(cycle(scenario)["created"]) == 1
    assert cycle(scenario)["created"] == []
    assert len(cards.list_cards()) == 2


def test_missing_dependency_parks_before_helper_creation(scenario):
    _, _, cards, _, packet = scenario
    previous = cards.fold("aabb0011")
    cards.create(
        CardCore(
            id="ccdd0033",
            title="[M] Waiting parent",
            description="Bounded source",
            initial_owner="jarvis",
            initial_claim_revision="parent-generation",
            acceptance_criteria=["Verified output"],
            initial_labels=["source-only"],
            dependencies=["ffff2222"],
            meta={key: previous.meta[key] for key in ("repository", "base_ref", "base_revision")},
        )
    )
    packet["parent_id"] = "ccdd0033"
    register(scenario)
    result = cycle(scenario)
    assert result["created"] == []
    assert result["crews"][0]["state"] == "dependencies"
    assert len(cards.list_cards()) == 2


def test_actual_cli_register_reconcile_status(scenario, tmp_path):
    from click.testing import CliRunner

    from skcapstone.fleet.cli import fleet

    home, _, cards, _, packet = scenario
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(packet))
    runner = CliRunner()
    registered = runner.invoke(
        fleet,
        ["crew", "register", "--home", str(home), "--agent", "jarvis", "--packet", str(source)],
    )
    assert registered.exit_code == 0, registered.output
    reconciled = runner.invoke(fleet, ["crew", "reconcile", "--home", str(home)])
    assert reconciled.exit_code == 0, reconciled.output
    assert len(json.loads(reconciled.output)["created"]) == 1
    readback = runner.invoke(fleet, ["crew", "status", "crew-test", "--home", str(home)])
    assert readback.exit_code == 0, readback.output
    requests = json.loads(readback.output)["requests"]
    assert len(requests) == 1 and requests[0]["state"] == "assigned"
    assert cards.fold(requests[0]["helper_id"]).owner is None


def test_fqdn_coordinator_matches_registration(scenario, monkeypatch):
    home, paths, _, _, packet = scenario
    monkeypatch.setattr(socket, "gethostname", lambda: "Fixture.Example")
    packet["coordinator_node"] = "fixture.example"
    register(scenario)
    result = controller.reconcile_crews(paths, home, "fixture.example")
    assert len(result["created"]) == 1


@pytest.mark.parametrize("contents", ["{", '{"packet":{"crew_id":"broken"}}'])
def test_corrupt_manifest_does_not_starve_valid_crew(scenario, contents):
    _, paths, _, _, _ = scenario
    register(scenario)
    directory = CrewStore(paths).directory
    (directory / "manifest-broken.json").write_text(contents)
    result = cycle(scenario)
    assert len(result["created"]) == 1
    assert any(row["state"] == "held" for row in result["crews"])


def test_malformed_request_does_not_stop_heartbeat(scenario, monkeypatch):
    _, paths, _, _, _ = scenario
    register(scenario)
    monkeypatch.setattr(sknoded, "node_capacity", lambda: {"cores": 4, "ram_gb": 8, "disk_gb": 40})
    monkeypatch.setattr(sknoded, "node_inventory", lambda: {})
    ledger = CrewStore(paths)
    (ledger.directory / ledger._request_name("crew-test", "broken")).write_text("{")
    result = sknoded.run_once(paths, "fixture-node")
    assert result["heartbeat"] is True
    assert result["crew"]["crews"][0]["state"] == "held"


def test_too_long_derived_objective_refused_before_registration(scenario):
    _, paths, _, _, packet = scenario
    packet["slots"][0]["packet"]["objective"] = "a" * 4096
    with pytest.raises(ValueError):
        register(scenario)
    assert CrewStore(paths).get_manifest("crew-test") is None


def test_sealed_resource_and_delivery_contract_reach_helper(scenario):
    _, _, cards, _, packet = scenario
    packet["slots"][0]["resource"] = {
        "resource_id": "fixture-v1",
        "version": "1",
        "policy_sha256": "a" * 64,
        "context_sha256": "b" * 64,
    }
    register(scenario)
    helper = cards.fold(cycle(scenario)["created"][0])
    objective = helper.meta["helper_objective"]
    assert '"resource_id": "fixture-v1"' in objective
    assert '"crew_id": "crew-test"' in objective
    assert "Reuse matching already-authorized resources first" in objective
    assert "skfleet crew receipt" in objective


def test_invalid_mandate_is_reported_without_auto_intake(scenario, monkeypatch):
    _, _, cards, *_ = scenario
    register(scenario)

    def changed(*_):
        raise ValueError("parent generation changed")

    monkeypatch.setattr(controller, "validate_mandate", changed)
    result = cycle(scenario)
    assert result["created"] == []
    assert result["crews"][0]["reason"] == "parent generation changed"
    assert len(cards.list_cards()) == 1


def test_held_prefix_cannot_starve_later_crew_across_fresh_controller_calls(scenario):
    _, paths, cards, _, packet = scenario
    packet["crew_id"] = "zz-valid"
    register(scenario)
    ledger = CrewStore(paths)
    with ledger.lock():
        for index in range(32):
            ledger.put_manifest({"packet": {"crew_id": f"held-{index:02d}"}})
    first = cycle(scenario)
    assert first["created"] == []
    assert len(first["crews"]) == 32
    assert all(row["state"] == "held" for row in first["crews"])
    second = cycle(scenario)  # Constructs a fresh CrewStore, as after daemon restart.
    assert len(second["created"]) == 1
    assert second["crews"][0]["crew_id"] == "zz-valid"
    assert len(second["crews"]) == 32
    assert len(cards.list_cards()) == 2


def test_budget_stop_advances_only_through_considered_crews(scenario):
    _, _, cards, _, packet = scenario
    for index in range(9):
        packet["crew_id"] = f"crew-{index:02d}"
        packet["owner_paths"] = [f"src/owner-{index:02d}.py"]
        register(scenario)
    first = cycle(scenario)
    assert len(first["created"]) == 8
    assert [row["crew_id"] for row in first["crews"]] == [f"crew-{i:02d}" for i in range(8)]
    second = cycle(scenario)
    assert second["crews"][0]["crew_id"] == "crew-08"
    assert len(second["created"]) == 1
    assert len(cards.list_cards()) == 10


def test_pause_preserves_cursor_and_resume_continues_after_held_prefix(scenario):
    _, paths, _, operator, packet = scenario
    packet["crew_id"] = "zz-valid"
    register(scenario)
    ledger = CrewStore(paths)
    with ledger.lock():
        for index in range(32):
            ledger.put_manifest({"packet": {"crew_id": f"held-{index:02d}"}})
    assert cycle(scenario)["created"] == []
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in ledger.directory.iterdir()
    }
    store.set_frozen(paths, True, writer=operator)
    assert cycle(scenario)["created"] == []
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in ledger.directory.iterdir()
    } == before
    store.set_frozen(paths, False, writer=operator)
    assert len(cycle(scenario)["created"]) == 1
