"""Supersession preserves custody proofs without requiring current criteria."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import review_dispatch
from skcapstone.fleet import review_retire as retire

from . import test_review_admission_retire

pending = test_review_admission_retire.pending


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
    p.exit_path.chmod(0o600)
    return p


def test_failed_production_exit_is_terminal_and_superseded(running):
    p = running
    result = retire.recover_failed_generations(p.paths, p.home)
    assert result[0]["state"] == "retired", result
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


def test_exact_systemd_failure_without_exit_can_supersede(running):
    p = running
    p.exit_path.unlink()
    p.state.update(Result="oom-kill", ExecMainStatus="143")
    result = retire.recover_failed_generations(p.paths, p.home)
    assert result[0]["state"] == "retired", result
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}


def test_no_terminal_proof_retains_running_generation(running):
    p = running
    p.exit_path.unlink()
    p.state.update(ActiveState="inactive", SubState="dead", Result="success", ExecMainStatus="0")
    assert retire.recover_failed_generations(p.paths, p.home)[0]["state"] == "refused"
    assert p.status_path.exists() and p.request_path.exists()


def test_pending_supersession_snapshot_tamper_is_not_retired(pending):
    p = pending
    retire.recover_failed_generations(p.paths, p.home)
    directory = retire._receipt_path(p.home, p.card, p.request["request_id"]).parent
    (directory / "superseded-card.json").write_text("{}")
    assert retire.retired_offers(p.home, p.card, p.events) == set()


@pytest.mark.parametrize("failure", ["claim-changed", "release-failed", "active"])
def test_running_refusal_never_releases_or_retires_wrong_claim(running, monkeypatch, failure):
    p = running
    p.events.pop()
    p.current.owner = p.owner
    p.current.meta["_claim_revision"] = "c" * 32 if failure == "claim-changed" else p.claim
    p.current.status = "doing"
    releases = []

    def release(*a, **kw):
        releases.append(kw)
        return False

    monkeypatch.setattr("skcapstone.coordination.Board.release_claim", release)
    if failure == "active":
        p.state["MainPID"] = "91"
    assert retire.recover_failed_generations(p.paths, p.home)[0]["state"] == "refused"
    assert len(releases) == (1 if failure == "release-failed" else 0)
    assert p.request_path.exists() and p.status_path.exists()


@pytest.mark.parametrize("expired", [True, False])
def test_unused_offer_sweep_expires_only_exact_unlaunched_offer(pending, expired):
    p = pending
    p.status_path.unlink()
    p.events.pop()
    p.request["lease_expires_at"] = "2020-01-01T00:00:00Z" if expired else "2099-01-01T00:00:00Z"
    p.request_path.write_text(json.dumps(p.request))
    p.events[0].update(
        ts="2019-12-31T23:00:00Z", request_sha256=retire.production_builder.digest(p.request)
    )
    p.state.update(LoadState="not-found", ActiveState="inactive", SubState="dead", InvocationID="")
    p.core["links"]["worker_died"] = "older claim"
    result = retire.recover_failed_generations(p.paths, p.home)
    if expired:
        assert result[0]["state"] == "retired-prestart", result
        assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}
    else:
        assert result[0]["state"] == "refused"
        assert p.request_path.exists()


@pytest.mark.parametrize("mutation", ["pointer", "card", "offer", "launch"])
def test_running_rejects_changed_generation_before_release(running, monkeypatch, mutation):
    p = running
    p.current.owner = p.owner
    p.current.meta["_claim_revision"] = p.claim
    p.current.status = "doing"
    p.events.pop()

    def no_release(*a, **kw):
        pytest.fail("must prove exact generation before release")

    monkeypatch.setattr("skcapstone.coordination.Board.release_claim", no_release)
    if mutation == "offer":
        p.events[0]["request_sha256"] = "f" * 64
    elif mutation == "launch":
        p.events[1]["launched"] = False
    else:
        original = retire.unit_terminal

        def query(*a, **kw):
            result = original(*a, **kw)
            if mutation == "pointer":
                p.status_path.write_text("{}")
            else:
                monkeypatch.setattr(retire, "review_state_revision", lambda *a: "0" * 64)
            return result

        monkeypatch.setattr(retire, "unit_terminal", query)
    assert retire.recover_failed_generations(p.paths, p.home)[0]["state"] == "refused"
    assert p.request_path.exists()


@pytest.mark.parametrize("column", ["done", "blocked", "doing"])
def test_released_terminal_or_unexpected_column_is_not_reopened(running, column):
    p = running
    p.current.status = column
    assert retire.recover_failed_generations(p.paths, p.home)[0]["state"] == "refused"
    assert p.current.status == column
    assert p.request_path.exists()
