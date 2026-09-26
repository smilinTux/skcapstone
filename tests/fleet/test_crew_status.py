"""Bounded read-only crew status over isolated local stores."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import crew_requests
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.crew_store import CrewStore
from skcapstone.fleet.paths import FleetPaths


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "fixture-node")
    home = tmp_path / "coord"
    home.mkdir()
    paths = FleetPaths(home / "fleet")
    cards = CardStore(home)
    source = {
        "repository": "https://example.invalid/repo.git",
        "base_ref": "main",
        "base_revision": "a" * 40,
    }
    cards.create(
        CardCore(
            id="aabb0011",
            title="[M] Parent",
            description="Source only",
            acceptance_criteria=["Exact status"],
            initial_owner="jarvis",
            initial_claim_revision="parent-1",
            initial_labels=["source-only"],
            meta=source,
        )
    )
    packet = {
        "schema": "skfleet.crew/v1",
        "crew_id": "crew-test",
        "parent_id": "aabb0011",
        "parent_claim_revision": "parent-1",
        "coordinator_node": "fixture-node",
        "owner_paths": ["src/owner.py"],
        "max_active_helpers": 2,
        "max_helpers": 4,
        "slots": [
            {
                "slot_id": "produce",
                "role": "technical",
                "trigger": "initial",
                "packet": {
                    "request_id": "producer-template",
                    "title": "[S] Produce candidate",
                    "objective": "Return exact candidate",
                    "criteria": ["Exact evidence"],
                    "allowed_paths": ["src/helper.py"],
                    "verification_commands": ["pytest -q"],
                    "logical_route": "sk-deepseek-m",
                },
            }
        ],
        "provider_pair": {
            "schema": "skfleet.provider-pair/v1",
            "state": "independent_review_unavailable",
            "snapshot_revision": "b" * 64,
            "producer": {
                "provider_family": "deepseek",
                "logical_route": "sk-deepseek-m",
            },
            "reviewer": None,
        },
    }
    manifest = crew_requests.register(paths, home, "jarvis", packet)
    request = {
        "schema": "skfleet.crew-support/v1",
        "crew_id": "crew-test",
        "request_id": "produce-1",
        "slot_id": "produce",
        "requester_card_id": "aabb0011",
        "requester_claim_revision": "parent-1",
        "evidence_sha256": "c" * 64,
        "reason": "Authorized initial crew assignment",
    }
    row = crew_requests.submit_support(paths, home, "jarvis", request)
    helper_id = "bbcc0022"
    cards.create(
        CardCore(
            id=helper_id,
            title="[S] Producer",
            description="Bounded helper",
            acceptance_criteria=["Exact evidence"],
            initial_owner="helper",
            initial_claim_revision="helper-1",
            initial_labels=[
                "source-only",
                "owner-helper",
                "parent-aabb0011",
                "sk-deepseek-m",
            ],
            meta={
                **source,
                "helper_parent_id": "aabb0011",
                "helper_parent_claim_revision": "parent-1",
                "helper_parent_contract_sha256": manifest["parent_contract_sha256"],
                "logical_route": "sk-deepseek-m",
            },
        )
    )
    with CrewStore(paths).lock() as ledger:
        row.update(
            state="assigned",
            helper_id=helper_id,
            helper_packet=packet["slots"][0]["packet"],
        )
        ledger.save_request(row)
    receipt = {
        "schema": "skfleet.herdr-delivery-receipt/v1",
        "parent_id": "aabb0011",
        "helper_id": helper_id,
        "helper_generation": "helper-1",
        "claim_revision": "helper-1",
        "host": "fixture-node",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "pane_id": "w1:p1",
        "agent_identity": {
            "agent_name": "helper",
            "agent_kind": "pi",
            "terminal_id": "term-1",
            "agent_session": None,
        },
        "delivery_token_sha256": "d" * 64,
        "delivery_state": "delivered",
        "timestamps": {"reserved": "2026-09-26T00:00:00Z"},
        "terminal_evidence": None,
    }
    journal = {
        "schema": 1,
        "packet": {"helper_id": helper_id, "claim_revision": "helper-1"},
        "contract": {
            "parent_id": "aabb0011",
            "crew_binding": {
                "crew_id": "crew-test",
                "request_id": "produce-1",
                "manifest_sha256": manifest["manifest_sha256"],
                "provider_pair": packet["provider_pair"],
                "producer_candidate": None,
            },
        },
        "delivery_receipt": receipt,
    }
    directory = paths.root / "herdr-handoffs"
    directory.mkdir(parents=True)
    delivery_path = directory / "assignment-1.json"
    delivery_path.write_text(json.dumps(journal))
    return SimpleNamespace(
        home=home,
        paths=paths,
        cards=cards,
        packet=packet,
        manifest=manifest,
        row=row,
        journal=journal,
        delivery_path=delivery_path,
    )


def invoke(scenario, *args):
    return CliRunner().invoke(
        fleet,
        ["crew", "status", *args, "--home", str(scenario.home)],
    )


def test_exact_crew_json_is_versioned_deterministic_and_redacted(scenario):
    first = invoke(scenario, "--crew", "crew-test", "--json")
    second = invoke(scenario, "--crew", "crew-test", "--json")
    assert first.exit_code == second.exit_code == 0
    assert first.output == second.output
    result = json.loads(first.output)
    assert result["schema"] == "skfleet.crew-status/v1"
    assert result["parent_id"] == "aabb0011"
    assert result["parent_owner"] == "jarvis"
    assert result["review_availability"] == "independent_review_unavailable"
    assert result["helpers"][0] == {
        "candidate_custody": None,
        "cleanup_eligible": False,
        "delivery_state": "delivered",
        "helper_owner": "helper",
        "helper_generation": "helper-1",
        "helper_id": "bbcc0022",
        "host": "fixture-node",
        "logical_route": "sk-deepseek-m",
        "pane_id": "w1:p1",
        "request_id": "produce-1",
        "request_state": "assigned",
        "role": "technical",
        "slot_id": "produce",
        "tab_id": "w1:t1",
        "terminal_state": None,
        "workspace_id": "w1",
    }
    text = first.output
    assert "Return exact candidate" not in text
    assert "src/helper.py" not in text
    assert "delivery_token" not in text


def test_parent_filter_and_human_projection_share_values(scenario):
    result = invoke(scenario, "--parent", "aabb0011")
    assert result.exit_code == 0
    assert "crew crew-test parent aabb0011 generation parent-1" in result.output
    assert "produce technical helper=bbcc0022 owner=helper generation=helper-1" in result.output
    assert "route=sk-deepseek-m" in result.output
    assert "host=fixture-node workspace=w1 tab=w1:t1 pane=w1:p1" in result.output


def test_status_does_not_write_any_store(scenario):
    before = {
        path: (_digest(path), path.stat().st_mtime_ns)
        for path in scenario.home.rglob("*")
        if path.is_file()
    }
    assert invoke(scenario, "--crew", "crew-test", "--json").exit_code == 0
    after = {
        path: (_digest(path), path.stat().st_mtime_ns)
        for path in scenario.home.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    "args,message",
    [
        ([], "select exactly one"),
        (["--crew", "missing"], "crew generation is missing"),
        (["--parent", "aabb0011", "--crew", "crew-test"], "select exactly one"),
    ],
)
def test_missing_or_ambiguous_selection_is_nonzero(scenario, args, message):
    result = invoke(scenario, *args, "--json")
    assert result.exit_code == 1
    assert message in result.output


@pytest.mark.parametrize("damage", ["unknown-key", "claim", "manifest", "missing-delivery"])
def test_malformed_conflicting_or_incomplete_state_is_nonzero(scenario, damage):
    if damage == "unknown-key":
        scenario.journal["raw_prompt"] = "secret"
        scenario.delivery_path.write_text(json.dumps(scenario.journal))
    elif damage == "claim":
        scenario.journal["delivery_receipt"]["claim_revision"] = "other"
        scenario.delivery_path.write_text(json.dumps(scenario.journal))
    elif damage == "manifest":
        scenario.journal["contract"]["crew_binding"]["manifest_sha256"] = "e" * 64
        scenario.delivery_path.write_text(json.dumps(scenario.journal))
    else:
        scenario.delivery_path.unlink()
    result = invoke(scenario, "--crew", "crew-test", "--json")
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "raw_prompt" not in result.output


def test_terminal_cleanup_eligibility_and_candidate_custody(scenario):
    candidate = {
        "ref": "refs/heads/work/candidate",
        "commit": "1" * 40,
        "tree": "2" * 40,
        "helper_id": "bbcc0022",
        "claim_revision": "helper-1",
        "evidence_sha256": "3" * 64,
    }
    with CrewStore(scenario.paths).lock() as ledger:
        row = ledger.get_request("crew-test", "produce-1")
        row.update(
            state="delivered",
            producer_candidate=candidate,
            receipt_sha256="3" * 64,
            receipt={"claim_revision": "helper-1"},
        )
        ledger.save_request(row)
    scenario.journal["delivery_receipt"].update(
        delivery_state="terminal",
        terminal_evidence={
            "state": "done",
            "state_change_seq": 12,
            "process_group_alive": False,
        },
    )
    scenario.delivery_path.write_text(json.dumps(scenario.journal))
    result = json.loads(invoke(scenario, "--crew", "crew-test", "--json").output)
    helper = result["helpers"][0]
    assert helper["cleanup_eligible"] is True
    assert helper["terminal_state"] == "done"
    assert helper["candidate_custody"] == candidate


def test_duplicate_helper_and_row_bound_fail_closed(scenario):
    with CrewStore(scenario.paths).lock() as ledger:
        original = ledger.get_request("crew-test", "produce-1")
        duplicate = copy.deepcopy(original)
        duplicate.update(
            request_id="duplicate",
            packet={**duplicate["packet"], "request_id": "duplicate"},
            packet_sha256="f" * 64,
            dedup_key="e" * 64,
            canonical_request_id="duplicate",
        )
        ledger.save_request(duplicate)
    result = invoke(scenario, "--crew", "crew-test", "--json")
    assert result.exit_code == 1
    assert "duplicate helper custody" in result.output

    with CrewStore(scenario.paths).lock() as ledger:
        for index in range(32):
            row = copy.deepcopy(scenario.row)
            request_id = f"extra-{index}"
            row.update(
                request_id=request_id,
                packet={**row["packet"], "request_id": request_id},
                packet_sha256=f"{index:064x}",
                dedup_key=f"{index + 100:064x}",
                canonical_request_id=request_id,
                helper_id=f"{index:08x}",
            )
            ledger.save_request(row)
    result = invoke(scenario, "--crew", "crew-test", "--json")
    assert result.exit_code == 1
    assert "row bound exceeded" in result.output
