"""Support intake selects bounded mandates and verifies real artifact bytes."""

from __future__ import annotations

import copy
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import crew_contract
from skcapstone.fleet import crew_requests as requests
from skcapstone.fleet.crew_store import CrewStore


def git(cwd, *args):
    """Use only a temporary local repository with no hooks or remote calls."""
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


@pytest.fixture
def scenario(paths, tmp_path, monkeypatch):
    """Construct actual parent custody, source Git and a sealed mandate."""
    monkeypatch.setattr(crew_contract.socket, "gethostname", lambda: "fixture-host")
    workspace = tmp_path / "source"
    workspace.mkdir()
    git(workspace, "init", "--quiet")
    git(workspace, "config", "user.name", "Fixture")
    git(workspace, "config", "user.email", "fixture@example.invalid")
    git(workspace, "remote", "add", "origin", "https://example.invalid/fixture.git")
    (workspace / "source.txt").write_text("source\n")
    git(workspace, "add", "source.txt")
    git(workspace, "commit", "--quiet", "-m", "fixture")
    source = {
        "repository": "https://example.invalid/fixture.git",
        "base_ref": "main",
        "base_revision": git(workspace, "rev-parse", "HEAD"),
    }
    home = tmp_path / "coord"
    home.mkdir()
    cards = CardStore(home)
    cards.create(
        CardCore(
            id="aaaa1111",
            title="[M] Parent",
            description="Source only",
            acceptance_criteria=["Exact results"],
            initial_owner="test-owner",
            initial_claim_revision="parent-1",
            initial_labels=["source-only"],
            meta=source,
        )
    )
    template = {
        "request_id": "lookup-1",
        "title": "[S] Find approved fixture",
        "objective": "Return exact approved fixture or blocker",
        "criteria": ["Exact bytes and source"],
        "allowed_paths": [],
        "verification_commands": ["Hash authorized fixture"],
    }
    packet = {
        "schema": "skfleet.crew/v1",
        "crew_id": "fixture-crew",
        "parent_id": "aaaa1111",
        "parent_claim_revision": "parent-1",
        "coordinator_node": "fixture-host",
        "owner_paths": ["src"],
        "max_active_helpers": 2,
        "max_helpers": 4,
        "slots": [
            {
                "slot_id": "lookup",
                "role": "support",
                "trigger": "support-request",
                "packet": template,
                "resource": {
                    "resource_id": "fixture",
                    "version": "v1",
                    "policy_sha256": "c" * 64,
                    "context_sha256": "d" * 64,
                },
            }
        ],
    }
    manifest = requests.register(paths, home, "test-owner", packet)
    return SimpleNamespace(
        paths=paths,
        home=home,
        cards=cards,
        packet=packet,
        manifest=manifest,
        source=source,
        workspace=workspace,
    )


def support(**updates):
    """Build exact typed intake, never commands or freeform template scope."""
    packet = {
        "schema": requests.SUPPORT_SCHEMA,
        "crew_id": "fixture-crew",
        "request_id": "need-1",
        "slot_id": "lookup",
        "requester_card_id": "aaaa1111",
        "requester_claim_revision": "parent-1",
        "evidence_sha256": "e" * 64,
        "reason": "Need the approved fixture",
    }
    packet.update(updates)
    return packet


def assign(s):
    """Simulate supported controller output on an isolated real CardStore."""
    row = requests.submit_support(s.paths, s.home, "test-owner", support())
    meta = {
        **s.source,
        "helper_parent_id": "aaaa1111",
        "helper_parent_claim_revision": "parent-1",
        "helper_parent_contract_sha256": s.manifest["parent_contract_sha256"],
    }
    s.cards.create(
        CardCore(
            id="bbbb2222",
            title="[S] Fixture helper",
            description="Return bytes",
            initial_owner="test-helper",
            initial_claim_revision="helper-1",
            initial_labels=["source-only", "owner-helper", "parent-aaaa1111"],
            meta=meta,
        )
    )
    with CrewStore(s.paths).lock() as store:
        row.update(state="assigned", helper_id="bbbb2222", cwd=str(s.workspace))
        store.save_request(row)
    return row


def receipt(s, **updates):
    """Build a receipt for real regular bytes in the temporary source checkout."""
    artifact = s.workspace / "result.txt"
    artifact.write_text("approved fixture evidence\n")
    packet = {
        "schema": requests.RECEIPT_SCHEMA,
        "crew_id": "fixture-crew",
        "request_id": "need-1",
        "helper_id": "bbbb2222",
        "claim_revision": "helper-1",
        "cwd": str(s.workspace),
        "artifacts": [
            {"path": "result.txt", "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        ],
        "tests": [{"command": "sha256sum result.txt", "outcome": "PASS reported"}],
    }
    packet.update(updates)
    return packet


def test_absent_status_is_read_only(paths):
    assert requests.status(paths, "absent") == {
        "crew_id": "absent",
        "manifest": None,
        "requests": [],
    }
    assert not paths.root.exists()


def test_request_replay_and_resource_consolidation(scenario):
    s = scenario
    first = requests.submit_support(s.paths, s.home, "test-owner", support())
    assert first["state"] == "requested"
    assert requests.submit_support(s.paths, s.home, "test-owner", support()) == first
    alias = requests.submit_support(
        s.paths, s.home, "test-owner", support(request_id="need-2", evidence_sha256="f" * 64)
    )
    assert alias["canonical_request_id"] == "need-1" and alias["state"] == "alias"
    with pytest.raises(ValueError, match="conflict"):
        requests.submit_support(s.paths, s.home, "test-owner", support(reason="changed"))


def test_concurrent_resource_requests_have_one_canonical(scenario):
    s = scenario

    def submit(i):
        return requests.submit_support(
            s.paths, s.home, "test-owner", support(request_id=f"need-{i}")
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(submit, range(8)))
    assert sum(row["canonical_request_id"] == row["request_id"] for row in rows) == 1
    assert len({row["canonical_request_id"] for row in rows}) == 1


def test_nonresource_dedup_requires_same_slot_and_evidence(scenario):
    s = scenario
    packet = copy.deepcopy(s.packet)
    packet["crew_id"] = "nonresource"
    packet["owner_paths"] = ["nonresource/owner.py"]
    packet["slots"][0].pop("resource")
    requests.register(s.paths, s.home, "test-owner", packet)
    first = requests.submit_support(s.paths, s.home, "test-owner", support(crew_id="nonresource"))
    alias = requests.submit_support(
        s.paths, s.home, "test-owner", support(crew_id="nonresource", request_id="again")
    )
    new = requests.submit_support(
        s.paths,
        s.home,
        "test-owner",
        support(crew_id="nonresource", request_id="new", evidence_sha256="a" * 64),
    )
    assert alias["canonical_request_id"] == first["request_id"]
    assert new["canonical_request_id"] == "new"


def test_same_resource_different_authorized_slot_does_not_collapse(scenario):
    s = scenario
    packet = copy.deepcopy(s.packet)
    packet["crew_id"] = "two-slots"
    packet["owner_paths"] = ["two_slots/owner.py"]
    second = copy.deepcopy(packet["slots"][0])
    second["slot_id"] = "verify-resource"
    second["role"] = "verification"
    second["packet"]["request_id"] = "verify-resource-template"
    second["packet"]["objective"] = "Independently check exact resource compatibility"
    packet["slots"].append(second)
    requests.register(s.paths, s.home, "test-owner", packet)
    first = requests.submit_support(s.paths, s.home, "test-owner", support(crew_id="two-slots"))
    second_row = requests.submit_support(
        s.paths,
        s.home,
        "test-owner",
        support(crew_id="two-slots", request_id="verify-need", slot_id="verify-resource"),
    )
    assert first["canonical_request_id"] == "need-1"
    assert second_row["canonical_request_id"] == "verify-need"
    repeated = requests.submit_support(
        s.paths,
        s.home,
        "test-owner",
        support(
            crew_id="two-slots",
            request_id="repeat",
            slot_id="verify-resource",
            evidence_sha256="a" * 64,
        ),
    )
    assert repeated["canonical_request_id"] == "verify-need"


@pytest.mark.parametrize(
    "updates",
    [
        {"command": "install something"},
        {"slot_id": "missing"},
        {"requester_claim_revision": "old"},
        {"evidence_sha256": 17},
        {"requester_card_id": "cccc3333"},
    ],
)
def test_intake_rejects_expansion_and_stale_identity(scenario, updates):
    s = scenario
    with pytest.raises(ValueError):
        requests.submit_support(s.paths, s.home, "test-owner", support(**updates))


def test_wrong_owner_and_changed_parent_refused(scenario):
    s = scenario
    with pytest.raises(ValueError, match="claimant"):
        requests.submit_support(s.paths, s.home, "outsider", support())
    with pytest.raises(ValueError):
        requests.register(s.paths, s.home, "outsider", s.packet)
    changed = copy.deepcopy(s.packet)
    changed["slots"][0]["packet"]["objective"] = "different scope"
    with pytest.raises(ValueError, match="conflict"):
        requests.register(s.paths, s.home, "test-owner", changed)


def test_current_crew_helper_can_request_support_but_not_expand(scenario):
    s = scenario
    assign(s)
    row = requests.submit_support(
        s.paths,
        s.home,
        "test-helper",
        support(
            request_id="helper-need",
            requester_card_id="bbbb2222",
            requester_claim_revision="helper-1",
        ),
    )
    assert row["canonical_request_id"] == "need-1"
    with pytest.raises(ValueError, match="claimant"):
        requests.submit_support(
            s.paths,
            s.home,
            "test-helper",
            support(
                request_id="stale", requester_card_id="bbbb2222", requester_claim_revision="old"
            ),
        )


def test_actual_receipt_and_idempotent_replay(scenario):
    s = scenario
    assign(s)
    payload = receipt(s)
    row = requests.record_receipt(s.paths, s.home, "test-helper", payload)
    assert row["state"] == "delivered" and len(row["receipt_sha256"]) == 64
    assert requests.record_receipt(s.paths, s.home, "test-helper", payload) == row
    assert s.cards.fold("aaaa1111").status.value == "doing"
    (s.workspace / "result.txt").write_text("changed after delivery")
    with pytest.raises(ValueError, match="artifact bytes"):
        requests.record_receipt(s.paths, s.home, "test-helper", payload)


@pytest.mark.parametrize("tamper", ["bytes", "symlink", "traversal", "claim", "actor", "origin"])
def test_receipt_rejects_tamper(scenario, tamper, tmp_path):
    s = scenario
    assign(s)
    payload = receipt(s)
    actor = "test-helper"
    if tamper == "bytes":
        (s.workspace / "result.txt").write_text("changed")
    elif tamper == "symlink":
        outside = tmp_path / "outside.txt"
        outside.write_bytes((s.workspace / "result.txt").read_bytes())
        (s.workspace / "result.txt").unlink()
        (s.workspace / "result.txt").symlink_to(outside)
    elif tamper == "traversal":
        payload["artifacts"][0]["path"] = "../outside.txt"
    elif tamper == "claim":
        payload["claim_revision"] = "old"
    elif tamper == "actor":
        actor = "test-owner"
    else:
        git(s.workspace, "remote", "set-url", "origin", "https://example.invalid/other.git")
    with pytest.raises((ValueError, OSError)):
        requests.record_receipt(s.paths, s.home, actor, payload)
    assert requests.status(s.paths, "fixture-crew")["requests"][0]["state"] == "assigned"


def test_exact_blocker_and_conflicting_result(scenario):
    s = scenario
    assign(s)
    payload = receipt(s)
    payload.pop("artifacts")
    payload.pop("tests")
    payload["blocker"] = "Approved fixture digest is unavailable; no download authority."
    row = requests.record_receipt(s.paths, s.home, "test-helper", payload)
    assert row["state"] == "blocked"
    payload["blocker"] = "different blocker"
    with pytest.raises(ValueError, match="conflict"):
        requests.record_receipt(s.paths, s.home, "test-helper", payload)
