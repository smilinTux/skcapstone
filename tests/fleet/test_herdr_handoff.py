"""Exact Herdr handoffs with temporary Git, board and fleet state only."""

from __future__ import annotations

import copy
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from skcoord.card_store import CardCore, CardStore, explicit_creation_request_digest

from skcapstone.fleet import herdr_handoff as handoff
from skcapstone.fleet import herdr_handoff_validation as validation
from skcapstone.fleet.paths import FleetPaths
from skcapstone.fleet.store import Writer, set_frozen


class FakeTransport:
    """Return qualified observations without invoking Herdr or a worker."""

    def __init__(self, workspace):
        self.sends = []
        self.delivery = "submitted"
        self.failure = None
        self.context_data = {
            "host": "fixture-host",
            "socket_path": "/tmp/fixture.sock",
            "socket_dev": 1,
            "socket_inode": 2,
        }
        self.agent = {
            "pane_id": "w1:p2",
            "workspace_id": "w1",
            "tab_id": "w1:t1",
            "terminal_id": "term-2",
            "agent_name": "helper-test",
            "agent_kind": "pi",
            "cwd": str(workspace),
            "state": "idle",
            "revision": 1,
            "state_change_seq": 10,
            "agent_session": None,
        }

    def context(self):
        return dict(self.context_data)

    def inspect(self, target):
        assert target == "w1:p2"
        return dict(self.agent)

    def prompt(self, target, text):
        self.sends.append((target, text))
        if self.failure:
            raise self.failure
        return {"delivery": self.delivery, "agent": dict(self.agent)}


def git(workspace, *args):
    return subprocess.check_output(
        ["git", "-C", str(workspace), *args], stderr=subprocess.PIPE, text=True
    ).strip()


def build_scenario(tmp_path):
    """Public fixture helper for real CLI tests; no monkeypatched authority."""
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    git(workspace, "init", "--quiet")
    git(workspace, "config", "user.name", "Fixture")
    git(workspace, "config", "user.email", "fixture@example.invalid")
    git(workspace, "remote", "add", "origin", "https://example.invalid/project.git")
    (workspace / "code.py").write_text("value = 1\n")
    git(workspace, "add", "code.py")
    git(workspace, "commit", "--quiet", "-m", "fixture")
    source = {
        "repository": "https://example.invalid/project.git",
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
            description="No external actions",
            acceptance_criteria=["Verified integrated output"],
            initial_owner="jarvis",
            initial_claim_revision="parent-1",
            initial_labels=["source-only"],
            meta=source,
        )
    )
    parent = cards.fold("aaaa1111")
    digest = explicit_creation_request_digest(
        {
            "title": parent.title,
            "description": parent.description,
            "acceptance_criteria": list(parent.acceptance_criteria),
        }
    )
    meta = {
        **source,
        "helper_parent_id": parent.id,
        "helper_parent_claim_revision": "parent-1",
        "helper_parent_contract_sha256": digest,
        "helper_allowed_paths": ["code.py"],
        "helper_objective": "Verify the selected source",
        "helper_verification": ["pytest -q"],
    }
    cards.create(
        CardCore(
            id="bbbb2222",
            title="[S] Source helper",
            acceptance_criteria=["Report exact output"],
            initial_owner="pi-helper-test",
            initial_claim_revision="helper-1",
            meta=meta,
            initial_labels=["source-only", "owner-helper", "parent-aaaa1111"],
        )
    )
    paths = FleetPaths(tmp_path / "fleet")
    set_frozen(paths, False, writer=Writer(role="operator", node="fixture", identity="casey"))
    transport = FakeTransport(workspace)
    packet = {
        "assignment_id": "assignment-1",
        "helper_id": "bbbb2222",
        "claim_revision": "helper-1",
        "target": {
            key: transport.agent[key] for key in ("pane_id", "agent_name", "agent_kind", "cwd")
        },
        "route": "sk-s",
        "expected_output": meta["helper_objective"],
    }
    manager = handoff.HandoffManager(paths, home, "jarvis", transport)
    return SimpleNamespace(
        manager=manager,
        paths=paths,
        home=home,
        cards=cards,
        transport=transport,
        packet=packet,
        workspace=workspace,
    )


@pytest.fixture
def scenario(tmp_path):
    return build_scenario(tmp_path)


def pickup(run):
    row = run.manager.deliver(run.packet)
    run.transport.agent.update(state="working", state_change_seq=11)
    return {
        "assignment_id": "assignment-1",
        "packet_sha256": row["packet_sha256"],
        "helper_id": "bbbb2222",
        "claim_revision": "helper-1",
        "base_revision": row["contract"]["base_revision"],
        "cwd": str(run.workspace),
    }


def result(run, payload):
    data = b"exact result\n"
    (run.workspace / "result.txt").write_bytes(data)
    return {
        **payload,
        "artifacts": [{"path": "result.txt", "sha256": hashlib.sha256(data).hexdigest()}],
        "tests": [{"command": "pytest -q", "outcome": "1 passed (worker report)"}],
    }


def test_deliver_pickup_result_preserves_card_ownership(scenario):
    run = scenario
    before = [run.cards.fold(card).model_dump() for card in ("aaaa1111", "bbbb2222")]
    payload = pickup(run)
    assert run.manager.receipt("assignment-1", "pickup", payload)["state"] == "acknowledged"
    assert (
        run.manager.receipt("assignment-1", "result", result(run, payload))["state"] == "delivered"
    )
    assert [run.cards.fold(card).model_dump() for card in ("aaaa1111", "bbbb2222")] == before
    assert len(run.transport.sends) == 1


def test_concurrent_delivery_and_restart_never_resend(scenario):
    run = scenario
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: run.manager.deliver(run.packet), range(2)))
    assert rows[0] == rows[1]
    fresh = handoff.HandoffManager(run.paths, run.home, "jarvis", run.transport)
    fresh.deliver(run.packet)
    assert len(run.transport.sends) == 1


@pytest.mark.parametrize("crash", [False, True])
def test_ambiguous_prompt_reconciles_same_intent_without_resend(scenario, crash):
    run = scenario
    run.transport.failure = KeyboardInterrupt() if crash else TimeoutError()
    if crash:
        with pytest.raises(KeyboardInterrupt):
            run.manager.deliver(run.packet)
    else:
        assert run.manager.deliver(run.packet)["state"] == "delivery-unknown"
    row = run.manager.deliver(run.packet)
    assert row["state"] == ("send-intent" if crash else "delivery-unknown")
    assert len(run.transport.sends) == 1


@pytest.mark.parametrize("change", ["packet", "destination", "assignment"])
def test_conflicting_replay_refused(scenario, change):
    run = scenario
    run.manager.deliver(run.packet)
    packet = copy.deepcopy(run.packet)
    if change == "packet":
        packet["route"] = "sk-m"
    elif change == "destination":
        packet["target"]["pane_id"] = "w1:p3"
    else:
        packet["assignment_id"] = "assignment-2"
    with pytest.raises(handoff.HandoffError):
        run.manager.deliver(packet)
    assert len(run.transport.sends) == 1


@pytest.mark.parametrize("pause", ["frozen", "unprovisioned"])
def test_paused_or_unprovisioned_fleet_sends_nothing(scenario, pause):
    run = scenario
    if pause == "frozen":
        set_frozen(
            run.paths, True, writer=Writer(role="operator", node="fixture", identity="casey")
        )
    else:
        run.paths.freeze_path().unlink()
    with pytest.raises(handoff.HandoffError, match=pause):
        run.manager.deliver(run.packet)
    assert not run.transport.sends


@pytest.mark.parametrize("actor", ["pi-helper-test", "other"])
def test_only_parent_owner_can_deliver(scenario, actor):
    run = scenario
    run.manager.actor = actor
    with pytest.raises(handoff.HandoffError, match="owner"):
        run.manager.deliver(run.packet)
    assert not run.transport.sends


def test_existing_launch_authority_denial_prevents_delivery(scenario, monkeypatch):
    def denied(*args):
        raise ValueError("launch authority denied")

    monkeypatch.setattr(validation, "authorize_coord_mutation", denied)
    with pytest.raises(ValueError, match="authority denied"):
        scenario.manager.deliver(scenario.packet)
    assert not scenario.transport.sends


@pytest.mark.parametrize(
    "key,value",
    [
        ("state", "blocked"),
        ("state", "unknown"),
        ("cwd", "/tmp/foreign"),
        ("agent_name", "different"),
    ],
)
def test_unavailable_or_wrong_destination_prevents_delivery(scenario, key, value):
    scenario.transport.agent[key] = value
    with pytest.raises(handoff.HandoffError):
        scenario.manager.deliver(scenario.packet)
    assert not scenario.transport.sends


@pytest.mark.parametrize("kind", ["socket", "pane", "session"])
def test_changed_destination_does_not_retarget(scenario, kind):
    run = scenario
    pickup(run)
    if kind == "socket":
        run.transport.context_data["socket_inode"] = 999
    elif kind == "pane":
        run.transport.agent["pane_id"] = "w2:p2"
    else:
        run.transport.agent["agent_session"] = "replacement"
    with pytest.raises(handoff.HandoffError):
        run.manager.observe("assignment-1")
    assert len(run.transport.sends) == 1


@pytest.mark.parametrize("state,seq", [("working", 10), ("unknown", 11), ("idle", 11)])
def test_echo_or_uncorroborated_activity_is_not_pickup(scenario, state, seq):
    payload = pickup(scenario)
    scenario.transport.agent.update(state=state, state_change_seq=seq, revision=999)
    with pytest.raises(handoff.HandoffError, match="activity"):
        scenario.manager.receipt("assignment-1", "pickup", payload)
    with pytest.raises(handoff.HandoffError, match="fields"):
        scenario.manager.receipt(
            "assignment-1", "pickup", {"echo": scenario.transport.sends[0][1]}
        )


def test_fast_done_receipt_and_missing_optional_hooks_are_supported(scenario):
    payload = pickup(scenario)
    scenario.transport.agent.update(state="done", state_change_seq=12)
    assert scenario.manager.receipt("assignment-1", "pickup", payload)["state"] == "acknowledged"


@pytest.mark.parametrize("field", ["claim_revision", "base_revision", "packet_sha256", "cwd"])
def test_wrong_receipt_identity_refused(scenario, field):
    payload = pickup(scenario)
    payload[field] = "wrong"
    with pytest.raises(handoff.HandoffError, match="exact assignment"):
        scenario.manager.receipt("assignment-1", "pickup", payload)


@pytest.mark.parametrize("card", ["aaaa1111", "bbbb2222"])
def test_new_claim_generation_blocks_further_transitions(scenario, card):
    run = scenario
    pickup(run)
    old = run.cards.fold(card)
    run.cards.append_event(card, "claim", old.owner, owner=old.owner, claim_revision="new")
    with pytest.raises(handoff.HandoffError, match="generation"):
        run.manager.observe("assignment-1")


@pytest.mark.parametrize(
    "failure", ["missing", "hash", "leaf-symlink", "parent-symlink", "traversal"]
)
def test_result_custody_requires_exact_inside_regular_artifacts(scenario, tmp_path, failure):
    run = scenario
    payload = pickup(run)
    run.manager.receipt("assignment-1", "pickup", payload)
    report = result(run, payload)
    target = run.workspace / "result.txt"
    if failure == "missing":
        target.unlink()
    elif failure == "hash":
        target.write_text("changed")
    elif failure == "leaf-symlink":
        target.unlink()
        target.symlink_to(run.workspace / "code.py")
    elif failure == "parent-symlink":
        (run.workspace / "linked").symlink_to(tmp_path, target_is_directory=True)
        report["artifacts"][0]["path"] = "linked/foreign.txt"
    else:
        report["artifacts"][0]["path"] = "../outside.txt"
    with pytest.raises((handoff.HandoffError, OSError)):
        run.manager.receipt("assignment-1", "result", report)
    assert "result" not in run.manager.observe("assignment-1")


def test_conflicting_result_preserves_prior_custody(scenario):
    run = scenario
    payload = pickup(run)
    run.manager.receipt("assignment-1", "pickup", payload)
    report = result(run, payload)
    row = run.manager.receipt("assignment-1", "result", report)
    report["tests"][0]["outcome"] = "different"
    with pytest.raises(handoff.HandoffError, match="conflicts"):
        run.manager.receipt("assignment-1", "result", report)
    assert run.manager.observe("assignment-1")["result"] == row["result"]


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b"x" * 32769, b"not json"])
def test_strict_bounded_json(tmp_path, data):
    path = tmp_path / "packet.json"
    path.write_bytes(data)
    with pytest.raises(handoff.HandoffError):
        handoff.load_json(path)


@pytest.mark.parametrize(
    "field", ["title", "description", "acceptance_criteria", "labels", "source", "paths"]
)
def test_contract_changes_under_same_claim_refuse_transition(scenario, monkeypatch, field):
    run = scenario
    pickup(run)
    original = CardStore.fold

    def changed(cards, card_id):
        card = original(cards, card_id)
        if card_id == ("bbbb2222" if field == "paths" else "aaaa1111"):
            card = card.model_copy(deep=True)
            if field == "source":
                card.meta["repository"] = "https://example.invalid/other.git"
            elif field == "paths":
                card.meta["helper_allowed_paths"] = ["new.py"]
            elif field == "acceptance_criteria":
                card.acceptance_criteria = ["New requirement"]
            elif field == "labels":
                card.labels = []
            else:
                setattr(card, field, "Changed contract")
        return card

    monkeypatch.setattr(CardStore, "fold", changed)
    with pytest.raises(handoff.HandoffError):
        run.manager.observe("assignment-1")


@pytest.mark.parametrize("key", ["agent_name", "terminal_id", "agent_session"])
def test_replaced_occupant_during_submit_stays_unknown(scenario, monkeypatch, key):
    run = scenario
    original = run.transport.prompt

    def replaced(target, text):
        response = original(target, text)
        response["agent"][key] = "replacement"
        return response

    monkeypatch.setattr(run.transport, "prompt", replaced)
    assert run.manager.deliver(run.packet)["state"] == "delivery-unknown"
    assert run.manager.deliver(run.packet)["state"] == "delivery-unknown"
    assert len(run.transport.sends) == 1


def test_final_prompt_bound_checked_before_intent_or_send(scenario, monkeypatch):
    run = scenario
    original = CardStore.fold

    def oversized(cards, card_id):
        card = original(cards, card_id)
        if card_id == "bbbb2222":
            card = card.model_copy(deep=True)
            card.description = "x" * 32768
        return card

    monkeypatch.setattr(CardStore, "fold", oversized)
    with pytest.raises(handoff.HandoffError, match="prompt"):
        run.manager.deliver(run.packet)
    assert not run.transport.sends
    assert not run.manager._path("assignment-1").exists()


def test_unrelated_metadata_never_enters_prompt(scenario, monkeypatch):
    original = CardStore.fold

    def unrelated(cards, card_id):
        card = original(cards, card_id).model_copy(deep=True)
        card.meta["unrelated_secret"] = "EXCLUDED-METADATA-SENTINEL"
        return card

    monkeypatch.setattr(CardStore, "fold", unrelated)
    scenario.manager.deliver(scenario.packet)
    assert "EXCLUDED-METADATA-SENTINEL" not in scenario.transport.sends[0][1]


@pytest.mark.parametrize(
    "field,value",
    [("route", "sk-xl"), ("agent_kind", "claude"), ("helper_coordinator_host", "another-host")],
)
def test_explicit_helper_pins_are_respected(scenario, monkeypatch, field, value):
    original = CardStore.fold

    def restricted(cards, card_id):
        card = original(cards, card_id).model_copy(deep=True)
        if card_id == "bbbb2222":
            card.meta[field] = value
        return card

    monkeypatch.setattr(CardStore, "fold", restricted)
    with pytest.raises(handoff.HandoffError):
        scenario.manager.deliver(scenario.packet)
    assert not scenario.transport.sends


def test_oversized_result_refused_before_journal_update(scenario):
    payload = pickup(scenario)
    scenario.manager.receipt("assignment-1", "pickup", payload)
    report = result(scenario, payload)
    report["tests"] = [{"command": "x" * 4096, "outcome": "y" * 4096}] * 5
    with pytest.raises(handoff.HandoffError, match="bounded"):
        scenario.manager.receipt("assignment-1", "result", report)
    assert "result" not in scenario.manager.observe("assignment-1")


def test_replaced_file_during_descriptor_read_is_rejected(tmp_path, monkeypatch):
    target, replacement = tmp_path / "artifact", tmp_path / "replacement"
    target.write_text("original")
    replacement.write_text("changed")
    original, calls = validation.os.fstat, []

    def replaced(fd):
        info = original(fd)
        calls.append(fd)
        if len(calls) == 2:
            replacement.replace(target)
        return info

    monkeypatch.setattr(validation.os, "fstat", replaced)
    with pytest.raises(handoff.HandoffError, match="changed"):
        validation._regular(target)
