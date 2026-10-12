"""Supersession preserves custody proofs without requiring current criteria."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import review_dispatch, review_retire as retire
from .test_review_admission_retire import pending  # noqa: F401


def test_pending_drift_supersedes_without_current_source(pending, monkeypatch):
    p = pending
    p.core["links"]["worker_died"] = "exact old claim"
    monkeypatch.setattr(
        review_dispatch,
        "_source",
        lambda *a: (_ for _ in ()).throw(ValueError("original source custody unavailable")),
    )
    result = retire.recover_failed_generations(p.paths, p.home)
    assert result[0]["state"] == "retired-prestart"
    receipt = json.loads(retire._receipt_path(p.home, p.card, p.request["request_id"]).read_text())
    assert receipt["disposition"] == "superseded"
    assert receipt["criteria_offered"] != receipt["criteria_current"]
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}
    assert not any(e["action"] == "verdict" for e in p.events)


@pytest.fixture
def running(pending, monkeypatch):
    p = pending
    p.execution["invocation"] = "e" * 32
    p.status["state"] = "running"
    p.status_path.write_text(json.dumps(p.status))
    p.events.insert(
        1,
        dict(
            action="review_assignment_launch",
            recommendation_id=p.request["request_id"],
            claim_revision=p.claim,
            execution=dict(p.execution),
            writer=p.owner,
            launched=True,
        ),
    )
    p.state.update(Result="exit-code", ExecMainStatus="1")
    p.exit = dict(
        schema="skfleet.production-review-exit/v1",
        card=p.card,
        owner=p.owner,
        claim_revision=p.claim,
        host="chiap03",
        unit=p.unit,
        invocation=p.execution["invocation"],
        execution=dict(p.execution),
        exit_code=75,
        source_head=p.source["head"],
        lane="codex",
    )
    p.exit_path = p.home / "evidence/production-review-exits" / (p.card + "-" + p.claim + ".json")
    p.exit_path.parent.mkdir(parents=True)
    p.exit_path.write_text(json.dumps(p.exit))
    return p


def test_failed_production_exit_is_terminal_and_superseded(running):
    p = running
    result = retire.recover_failed_generations(p.paths, p.home)
    assert result[0]["state"] == "retired"
    receipt = json.loads(retire._receipt_path(p.home, p.card, p.request["request_id"]).read_text())
    assert receipt["exit_source"] == "production-review-exit"
    assert receipt["disposition"] == "superseded"
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}
    (
        retire._receipt_path(p.home, p.card, p.request["request_id"]).parent
        / "production-review-exit.json"
    ).write_text("{}")
    assert retire.retired_offers(p.home, p.card, p.events) == set()


@pytest.mark.parametrize(
    "bad",
    [
        "success",
        "claim",
        "host",
        "invocation",
        "request",
        "head",
        "active",
        "unreachable",
        "packet",
        "terminal",
        "context",
    ],
)
def test_running_refuses_uncertain_or_completed_custody(running, monkeypatch, bad):
    p = running
    if bad == "success":
        p.exit["exit_code"] = 0
    elif bad == "claim":
        p.exit["claim_revision"] = "a" * 32
    elif bad == "host":
        p.exit["host"] = "chiap09"
    elif bad == "invocation":
        p.exit["invocation"] = "a" * 32
    elif bad == "request":
        p.exit["execution"]["request_id"] = "f" * 64
    elif bad == "head":
        p.exit["source_head"] = "a" * 40
    elif bad == "active":
        p.state["ActiveState"] = "active"
    elif bad == "unreachable":
        monkeypatch.setattr(
            retire.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=255, stdout="")
        )
    elif bad == "context":
        directory = p.home / "evidence/production-acceptance" / (p.card + "-" + p.claim)
        directory.mkdir(parents=True)
        (directory / "context.json").write_text("{}")
    else:
        p.status["review_packet" if bad == "packet" else "terminal"] = {}
    p.exit_path.write_text(json.dumps(p.exit))
    p.status_path.write_text(json.dumps(p.status))
    retire.recover_failed_generations(p.paths, p.home)
    assert p.status_path.exists() and p.request_path.exists()
    assert not any(e["action"] == "remote_review_retire" for e in p.events)


def test_running_all_proofs_precede_exact_claim_release(running, monkeypatch):
    p = running
    p.current.owner = p.owner
    p.current.meta["_claim_revision"] = p.claim
    p.current.status = "doing"
    p.events.pop()
    calls = []

    def release(self, owner, card, **kw):
        assert any("systemctl" in argv for argv in p.calls)
        assert kw == dict(actor=p.owner, expected_claim_revision=p.claim, abandon_reason="error")
        calls.append(card)
        p.current.owner = None
        p.current.meta = {}
        p.current.status = "review"
        p.events.append(
            dict(action="release_claim", released_owner=owner, expected_claim_revision=p.claim)
        )

    monkeypatch.setattr("skcapstone.coordination.Board.release_claim", release)
    assert retire.recover_failed_generations(p.paths, p.home)[0]["state"] == "retired"
    assert calls == [p.card]
