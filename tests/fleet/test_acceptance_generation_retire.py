"""An operator retires exact unusable generations without writing reviewer outcomes."""

import socket
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from skcapstone.fleet import acceptance_generation_retire as recovery
from skcapstone.fleet import production_builder as builder
from skcapstone.fleet import review_retire
from skcapstone.fleet.source_custody import CustodyState
from tests.fleet.test_review_supersession import (
    CARD,
    CLAIM,
    NODE,
    OWNER,
    REQUEST_ID,
    _write,
    world,  # noqa: F401
)


class Row(SimpleNamespace):
    def model_dump(self, **kwargs):
        return {**vars(self), "status": self.status.value}


@pytest.fixture
def candidate(world, monkeypatch):  # noqa: F811
    source = Row(id="aabbccdd", owner=None, meta={}, status=SimpleNamespace(value="review"))
    row = Row(
        id=CARD,
        owner=None,
        archived=False,
        status=SimpleNamespace(value="review"),
        labels=["review", "source-only"],
        meta={"link_source_card": source.id, "link_head_revision": "a" * 40},
    )
    world.store.card = row
    monkeypatch.setattr(world.store, "fold", lambda card: source if card == source.id else row)
    from skcoord import card_store

    monkeypatch.setattr(card_store, "CardStore", lambda home: world.store)
    monkeypatch.setattr(card_store, "card_mutation_lock", lambda *a: nullcontext())
    monkeypatch.setattr(
        builder, "policy", lambda: {"authority_host": socket.gethostname().split(".")[0].lower()}
    )
    monkeypatch.setattr(
        recovery,
        "inspect_custody",
        lambda *a, **k: SimpleNamespace(state=CustodyState.RELEASED_CLEAN),
    )
    outcomes = {
        CARD: {},
        source.id: {
            "action": "verdict",
            "writer": "producer",
            "candidate_commit": "a" * 40,
            "candidate_tree": "b" * 40,
            "candidate_sha256": "c" * 64,
        },
    }
    monkeypatch.setattr(recovery, "_latest_outcome", lambda store, card: outcomes[card])
    monkeypatch.setattr(
        review_retire, "unit_terminal", lambda *a, **k: {"ActiveState": "inactive"}
    )
    request = dict(
        world.request,
        reviewer=OWNER,
        source={
            "card": source.id,
            "owner": "producer",
            "head": "a" * 40,
            "tree": "b" * 40,
            "evidence_sha256": "c" * 64,
        },
    )
    execution = {
        "node": NODE,
        "host": "worker",
        "unit": "review.service",
        "invocation": "d" * 32,
        "request_id": REQUEST_ID,
        "request_sha256": builder.digest(request),
    }
    status = dict(
        request_id=REQUEST_ID,
        card_id=CARD,
        work_kind="review",
        state="running",
        owner=OWNER,
        claim_revision=CLAIM,
        execution=execution,
    )
    terminal = dict(
        schema="skfleet.production-review-exit/v1",
        card=CARD,
        owner=OWNER,
        claim_revision=CLAIM,
        exit_code=0,
        execution=execution,
        source_head="a" * 40,
        **{k: execution[k] for k in ("host", "unit", "invocation")},
    )
    for event in world.store.events:
        if event["action"] == "remote_review_offer":
            event["request_sha256"] = builder.digest(request)
        if event["action"] == "review_assignment_launch":
            event.update(writer=OWNER, execution=execution)
    rp = review_retire.dispatch.request_path(world.paths, NODE, CARD)
    sp = review_retire.dispatch.status_path(world.paths, NODE, CARD)
    ep = world.home / "evidence/production-review-exits" / f"{CARD}-{CLAIM}.json"
    _write(rp, request)
    _write(sp, status)
    _write(ep, terminal)
    context = world.home / "evidence/production-acceptance" / f"{CARD}-{CLAIM}" / "context.json"
    context.unlink()
    context.parent.rmdir()
    expected = {
        "card_sha256": builder.digest(row.model_dump()),
        "source_sha256": builder.digest(source.model_dump()),
        "request_sha256": review_retire.source_bundle._sha(rp.read_bytes()),
        "status_sha256": review_retire.source_bundle._sha(sp.read_bytes()),
        "exit_sha256": review_retire.source_bundle._sha(ep.read_bytes()),
    }
    return SimpleNamespace(
        **vars(world),
        expected=expected,
        row=row,
        source=source,
        outcomes=outcomes,
        rp=rp,
        sp=sp,
        ep=ep,
        context=context,
    )


def run(c, apply=False):
    return recovery.retire(
        c.home,
        CARD,
        CLAIM,
        OWNER,
        c.expected,
        actor="lumina-nor",
        reason="exact released pending generation",
        apply=apply,
    )


def test_checked_retirement_preserves_no_verdict_and_replays(candidate):
    c = candidate
    before = list(c.store.events)
    assert run(c) == "eligible"
    assert c.store.events == before and c.rp.exists()
    assert run(c, True) == "superseded"
    assert not c.rp.exists() and not c.sp.exists()
    assert REQUEST_ID in review_retire.retired_offers(c.home, CARD, c.store.events)
    # All writes belong to the operator, and none is a reviewer verdict/completion.
    assert [(e["action"], e["writer"]) for e in c.store.events[len(before) :]] == [
        ("remote_review_supersede", "lumina-nor")
    ]
    assert run(c, True) == "already-retired"


@pytest.mark.parametrize(
    "mutation", ["hash", "claim", "outcome", "acceptance", "exit", "live", "hold", "terminal"]
)
def test_changed_or_acceptable_generation_refuses(candidate, monkeypatch, mutation):
    c = candidate
    before = list(c.store.events)
    if mutation == "hash":
        c.expected["status_sha256"] = "0" * 64
    elif mutation == "claim":
        monkeypatch.setattr(
            recovery,
            "inspect_custody",
            lambda *a, **k: SimpleNamespace(state=CustodyState.CHANGED),
        )
    elif mutation == "outcome":
        c.outcomes[CARD] = {"action": "link", "link_key": "verdict", "link_value": "PASS"}
    elif mutation == "acceptance":
        _write(c.context, {})
    elif mutation == "exit":
        _write(c.ep, {})
    elif mutation == "live":
        c.row.owner = OWNER
    elif mutation == "hold":
        c.row.labels.append("hold")
    elif mutation == "terminal":
        monkeypatch.setattr(
            review_retire,
            "unit_terminal",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("running")),
        )
    with pytest.raises(ValueError):
        run(c, True)
    assert c.store.events == before and c.rp.exists() and c.sp.exists()


def test_obsolete_source_review_can_retire_without_erasing_pass(candidate):
    c = candidate
    c.outcomes[CARD] = {"action": "link", "link_key": "verdict", "link_value": "PASS"}
    c.outcomes[c.source.id]["candidate_commit"] = "f" * 40
    assert run(c, True) == "superseded"
    assert c.outcomes[CARD]["link_value"] == "PASS"


@pytest.mark.parametrize("crash_at", ["event", "cleanup"])
def test_crash_recovers_without_second_retirement(candidate, monkeypatch, crash_at):
    c = candidate
    target = c.store if crash_at == "event" else review_retire
    name = "append_event" if crash_at == "event" else "_remove_pointer"
    original = getattr(target, name)

    def crash(*args, **kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(target, name, crash)
    with pytest.raises(RuntimeError, match="crash"):
        run(c, True)
    monkeypatch.setattr(target, name, original)
    assert run(c, True) in {"superseded", "already-retired"}
    assert len([e for e in c.store.events if e["action"] == "remote_review_supersede"]) == 1
    assert not c.rp.exists() and not c.sp.exists()


def test_replay_preserves_new_request(candidate):
    c = candidate
    assert run(c, True) == "superseded"
    _write(c.rp, {"request_id": "new"})
    with pytest.raises(ValueError, match="new generation"):
        run(c, True)
    assert c.rp.exists()


def test_coord_command_preserves_operator_identity_and_check_default(tmp_path, monkeypatch):
    import click
    from click.testing import CliRunner

    from skcapstone import jarvis_emergency
    from skcapstone.cli.coord_review_work import register_coord_review_work

    coord = click.Group()
    register_coord_review_work(coord)
    path = tmp_path / "expected.json"
    _write(path, {})
    calls, authorities = [], []

    def fake_retire(*args, **kwargs):
        calls.append(kwargs)
        return "eligible"

    monkeypatch.setattr(recovery, "retire", fake_retire)
    monkeypatch.setattr(
        jarvis_emergency, "authorize_coord_mutation", lambda *args: authorities.append(args)
    )
    argv = [
        "review-generation-retire",
        CARD,
        "--owner",
        OWNER,
        "--claim",
        CLAIM,
        "--expected",
        str(path),
        "--agent",
        "lumina-nor",
        "--reason",
        "exact proof",
    ]
    assert CliRunner().invoke(coord, argv).exit_code == 0
    assert not calls[-1]["apply"] and not authorities
    result = CliRunner().invoke(coord, argv + ["--apply"])
    assert result.exit_code == 0, result.output
    assert calls[-1]["apply"] and calls[-1]["actor"] == "lumina-nor"
    assert authorities[-1][0] == "lumina-nor"


def test_imported_review_is_preserved_without_granting_acceptance(candidate):
    c = candidate
    artifact = c.context.parent / "imported-review" / "REVIEW-DECISION.json"
    _write(artifact, {"verdict": "FAIL"})
    c.outcomes[CARD] = {"action": "link", "link_key": "verdict", "link_value": "FAIL"}
    c.outcomes[c.source.id]["candidate_commit"] = "f" * 40
    before = artifact.read_bytes()
    assert run(c, True) == "superseded"
    assert artifact.read_bytes() == before


@pytest.mark.parametrize(
    "name",
    [
        "context.json",
        "finish-intent.json",
        "negative-disposition.json",
        "step-00.intent.json",
        "unknown.json",
    ],
)
def test_retirement_still_refuses_transaction_artifacts(candidate, name):
    c = candidate
    _write(c.context.parent / name, {})
    with pytest.raises(ValueError, match="acceptance custody"):
        run(c, True)
    assert c.rp.exists() and c.sp.exists()
