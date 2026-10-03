"""Candidate native CLI integration with an explicitly substituted test validator."""

import hashlib
import json
import multiprocessing
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from skcoord.card_store import CardCore

from skcapstone.fleet import production_review_finish as finish
from skcapstone.fleet.production_review_evidence import inspect_proposal
from tests.fleet.test_source_bundle import git, publish, source  # noqa: F401


@pytest.mark.parametrize("legacy", [False, True])
def test_native_state_reuses_only_current_locked_store(tmp_path, monkeypatch, legacy):
    """Snapshots match separate-store reads and refresh after either event lane."""
    from contextlib import contextmanager
    from copy import deepcopy

    import skcoord.card_store as cards

    home, card = tmp_path / "home", "c1460001"
    home.mkdir()
    store = cards.CardStore(home)
    store.create(CardCore(id=card, title="[M] Source", created_by="producer"))
    store.append_event(card, "claim", "producer", owner="producer")
    overlay, loads, locked = {}, [], []
    load = cards.load_legacy_mutations
    lock = finish.card_mutation_lock
    read = finish.LiveCardStoreGateway.read_card

    def load_current(path):
        loads.append(path)
        return {**load(path), **deepcopy(overlay)}

    @contextmanager
    def tracked_lock(*args):
        with lock(*args):
            locked.append(True)
            try:
                yield
            finally:
                locked.pop()

    def checked_read(self, card_id, *, _store=None):
        assert locked and _store is not None
        return read(self, card_id, _store=_store)

    monkeypatch.setattr(cards, "load_legacy_mutations", load_current)
    monkeypatch.setattr(finish, "card_mutation_lock", tracked_lock)
    monkeypatch.setattr(finish.LiveCardStoreGateway, "read_card", checked_read)
    first = finish.native_state(home, card)
    assert len(loads) == 1
    if legacy:
        overlay[card] = [
            {
                "action": "link",
                "link_key": "verdict",
                "link_value": "PASS",
                "writer": "producer",
                "seq": 1,
                "ts": "2099-01-01T00:00:00Z",
            }
        ]
    else:
        store.append_event(card, "link", "producer", link_key="verdict", link_value="PASS")
    loads.clear()
    second = finish.native_state(home, card)
    assert len(loads) == 1
    assert second["revision"] != first["revision"]
    assert second["completion_revision"] != first["completion_revision"]
    assert second["owner"] == first["owner"] == "producer"
    assert second["claim_revision"] == first["claim_revision"]
    # Replay the old call boundary: the gateway constructs a second fresh store.
    monkeypatch.setattr(
        finish.LiveCardStoreGateway,
        "read_card",
        lambda self, card_id, **kwargs: read(self, card_id),
    )
    loads.clear()
    assert finish.native_state(home, card) == second
    assert len(loads) == 2


@pytest.fixture
def pair(source, monkeypatch, tmp_path):  # noqa: F811
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "control")
    candidate_src = Path(__file__).resolve().parents[2] / "src"
    monkeypatch.setenv("PYTHONPATH", str(candidate_src))
    # Verify the child interpreter resolves the candidate before testing writes.
    import subprocess

    resolved = subprocess.check_output(
        [sys.executable, "-c", "import skcapstone; print(skcapstone.__file__)"],
        text=True,
    ).strip()
    assert Path(resolved).resolve() == candidate_src / "skcapstone/__init__.py"
    home, store = source["home"], source["store"]
    card, owner = "24b00002", "pi-seraph-control-24b00002"
    artifact = publish(source)
    from skcapstone.review_replacement import current_review_attempt

    card = current_review_attempt(home, source["card"], source["head"])
    source_revision = finish.native_state(home, source["card"])["revision"]
    meta = {
        **source["core"]["meta"],
        "repository": source["remote"],
        "base_revision": source["base"],
        "source_revision": source_revision,
    }
    store.create(
        CardCore(
            id=card,
            title="[REVIEW] exact source",
            created_by="controller",
            initial_labels=["source-only", "review", "parent-" + source["card"]],
            meta=meta,
        )
    )
    store.append_event(card, "claim", owner, owner=owner)
    claim = store.fold(card).meta["_claim_revision"]
    workspace = tmp_path / "review-work"
    git(tmp_path, "clone", "-q", str(source["workspace"]), str(workspace))
    git(workspace, "config", "user.name", "Independent Reviewer")
    git(workspace, "config", "user.email", "review@example.invalid")
    directory = workspace / "docs/evidence/agents" / card
    directory.mkdir(parents=True)
    report = directory / "COMPLETION-EVIDENCE.md"
    report.write_text("Independent source inspection. Model CI claims are not test proof.\n")
    report.chmod(0o600)
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    proposal = dict(
        schema="skfleet.source-review-decision/v1",
        card=card,
        parent_card=source["card"],
        source_head=source["head"],
        source_tree=source["tree"],
        reviewer_identity=owner,
        verdict="PASS",
        report_sha256=report_sha,
    )
    decision = directory / "REVIEW-DECISION.json"
    decision.write_text(json.dumps(proposal))
    decision.chmod(0o600)
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "independent review evidence")
    inspected = inspect_proposal(
        workspace,
        **{
            key: proposal[key]
            for key in ("card", "parent_card", "source_head", "source_tree", "reviewer_identity")
        },
    )
    applicability = dict(
        type="source-only-applicability",
        card_id=card,
        source_head=source["head"],
        reviewer=owner,
        evidence_digest=report_sha,
        governed_pr_ci=False,
    )
    for key, value in [
        ("evidence", str(report)),
        ("reviewer_evidence_sha256", report_sha),
        ("verdict", "PASS"),
        ("applicability_receipt", json.dumps(applicability)),
    ]:
        before = finish.native_state(home, card)
        finish.native_command(
            home,
            [
                "link",
                card,
                key,
                value,
                "--agent",
                owner,
                "--expected-source-revision",
                before["revision"],
                "--expected-claim-revision",
                claim,
                "--transition-id",
                hashlib.sha256(key.encode()).hexdigest(),
                "--json",
            ],
        )
    source_item = dict(
        card=source["card"],
        owner=source["owner"],
        claim=source["claim"],
        revision=source_revision,
        repository=source["remote"],
        head=source["head"],
        tree=source["tree"],
        ref=artifact["ref"],
        evidence_path=str(source["shared"]),
        evidence_sha256=artifact["evidence_sha256"],
    )
    review_item = dict(
        card=card,
        owner=owner,
        claim=claim,
        revision=finish.native_state(home, card)["revision"],
        evidence_path=str(report),
        evidence_sha256=report_sha,
        decision_path=str(decision),
        decision_sha256=inspected["decision_sha256"],
        proposal=proposal,
    )
    from tests.fleet.acceptance_proof_fixture import retained_proof

    binding, receipt = retained_proof(
        home,
        source_item,
        source["workspace"],
        finish._digest(store.fold(source["card"]).acceptance_criteria),
    )
    context = dict(
        source=source_item,
        review=review_item,
        controller="fleet-review-closer@control",
        test_binding=binding,
        source_workspace=str(source["workspace"]),
    )
    calls = []

    def validate(*args):
        calls.append(args)
        return dict(receipt)

    monkeypatch.setitem(
        sys.modules,
        "skcapstone.fleet.production_tests",
        SimpleNamespace(validate_test_receipt=validate),
    )
    return home, tmp_path / "finish", context, store, calls, receipt


def test_real_guarded_native_completion_review_before_source_and_replay_without_writes(pair):
    home, directory, context, store, calls, _ = pair
    guards = []
    result = finish.finish_pair(home, directory, context, guard=lambda: guards.append(True))
    assert result["accepted"] and result["controller"] == "fleet-review-closer@control"
    events = {}
    for role in ("source", "review"):
        card = context[role]["card"]
        row = store.fold(card)
        assert row.status.value == "done" and row.owner is None
        assert "_claim_revision" not in row.meta
        events[role] = store._read_events(card)
        assert len([e for e in events[role] if e["action"] == "complete"]) == 1
        assert not any(e["action"] == "release_claim" for e in events[role])
    review_complete = next(e for e in events["review"] if e["action"] == "complete")
    source_complete = next(e for e in events["source"] if e["action"] == "complete")
    assert review_complete["ts"] < source_complete["ts"]
    assert len(calls) > 2 and guards
    assert (
        finish.finish_pair(home, directory, context, guard=lambda: pytest.fail("no mutation"))
        == result
    )
    assert all(store._read_events(context[role]["card"]) == events[role] for role in events)


@pytest.mark.parametrize("lost", ["link", "review-complete", "source-complete"])
def test_crash_after_successful_native_write_recovers_exactly_once(pair, lost):
    home, directory, context, store, _, _ = pair
    crashed = []

    def command(home, args):
        result = finish.native_command(home, args)
        selected = (
            args[0]
            if args[0] == "link"
            else ("review-complete" if args[1] == context["review"]["card"] else "source-complete")
        )
        if selected == lost and not crashed:
            crashed.append(True)
            raise OSError("simulated lost successful CLI reply")
        return result

    with pytest.raises(OSError, match="lost"):
        finish.finish_pair(home, directory, context, guard=lambda: None, command=command)
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    for role in ("source", "review"):
        events = store._read_events(context[role]["card"])
        assert len([e for e in events if e["action"] == "complete"]) == 1


def test_sigkill_after_native_link_before_ack_resumes_in_fresh_controller(pair):
    home, directory, context, store, _, _ = pair
    finish.once(directory / "context.json", context)

    def killed_controller():
        def command(home, args):
            result = finish.native_command(home, args)
            if args[0] == "link":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        finish.finish_pair(
            home,
            directory,
            finish.read_json(directory / "context.json"),
            guard=lambda: None,
            command=command,
        )

    first = multiprocessing.get_context("fork").Process(target=killed_controller)
    first.start()
    first.join(30)
    if first.is_alive():
        first.kill()
        first.join(5)
        pytest.fail("first controller did not reach deliberate crash boundary")
    assert first.exitcode == -signal.SIGKILL
    assert (directory / "step-00.intent.json").is_file()
    assert not (directory / "step-00.ack.json").exists()
    assert native_revision(home, context["review"]["card"]) != context["review"]["revision"]

    def resumed_controller():
        finish.finish_pair(
            home, directory, finish.read_json(directory / "context.json"), guard=lambda: None
        )

    second = multiprocessing.get_context("fork").Process(target=resumed_controller)
    second.start()
    second.join(45)
    if second.is_alive():
        second.kill()
        second.join(5)
        pytest.fail("fresh controller did not reconcile durable native write")
    assert second.exitcode == 0
    assert finish.read_json(directory / "finished.json")["accepted"]
    for role in ("source", "review"):
        events = store._read_events(context[role]["card"])
        assert len([event for event in events if event["action"] == "complete"]) == 1
    review_events = store._read_events(context["review"]["card"])
    links = [event for event in review_events if event.get("link_key") == "test_acceptance"]
    assert len(links) == 1


def native_revision(home, card):
    return finish.native_state(home, card)["revision"]


@pytest.mark.parametrize("change", ["source", "claim", "report", "decision", "FAIL", "tests"])
def test_changed_identity_artifacts_or_missing_trusted_tests_never_complete(
    pair, change, monkeypatch
):
    home, directory, context, store, _, _ = pair
    if change == "source":
        store.append_event(context["source"]["card"], "add_label", "operator", label="changed")
    elif change == "claim":
        store.append_event(context["review"]["card"], "claim", "replacement", owner="replacement")
    elif change in {"report", "decision"}:
        key = "evidence_path" if change == "report" else "decision_path"
        Path(context["review"][key]).write_text("altered")
    elif change == "FAIL":
        context["review"]["proposal"]["verdict"] = "FAIL"
    else:

        def refuse(*args):
            raise ValueError("required native test receipt missing")

        monkeypatch.setitem(
            sys.modules,
            "skcapstone.fleet.production_tests",
            SimpleNamespace(validate_test_receipt=refuse),
        )
    with pytest.raises(ValueError):
        finish.finish_pair(home, directory, context, guard=lambda: None)
    assert not any(
        e["action"] == "complete"
        for role in ("source", "review")
        for e in store._read_events(context[role]["card"])
    )


def test_false_hosted_ci_events_cannot_be_downgraded_to_source_only_acceptance(pair):
    from skcapstone.review_verdict import _source_only_applicability

    home, _, context, _, _, _ = pair
    review = context["review"]
    assert _source_only_applicability(review["card"], home)
    finish.native_command(
        home,
        [
            "link",
            review["card"],
            "hosted_checks",
            "SUCCESS",
            "--agent",
            review["owner"],
            "--expected-source-revision",
            review["revision"],
            "--expected-claim-revision",
            review["claim"],
            "--transition-id",
            "f" * 64,
            "--json",
        ],
    )
    assert not _source_only_applicability(review["card"], home)
