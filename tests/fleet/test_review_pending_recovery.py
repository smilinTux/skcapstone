"""Guard automatic release by stale admission proofs before retirement."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from skcapstone.fleet import review_retire as retire

from . import test_review_admission_retire

pending = test_review_admission_retire.pending


@pytest.fixture
def owned(pending, monkeypatch):
    p = pending
    p.current.owner = p.owner
    p.current.meta["_claim_revision"] = p.claim
    p.current.status = "doing"
    p.events.pop()
    p.releases = []
    original = retire.subprocess.run

    def release(self, owner, card, **kwargs):
        assert any("systemctl" in cmd for cmd in p.calls)
        assert any("tmux" in cmd for cmd in p.calls)
        p.releases.append((owner, card, kwargs))
        p.current.owner = None
        p.current.meta = {}
        p.current.status = "backlog"
        p.events.append(
            dict(
                action="release_claim",
                writer=owner,
                released_owner=owner,
                expected_claim_revision=p.claim,
            )
        )
        return True

    def run(argv, **kwargs):
        if "move" in argv:
            assert p.releases
            assert argv[-5:] == ["move", p.card, "review", "--agent", "niobe"]
            p.current.status = "review"
            return SimpleNamespace(returncode=0)
        return original(argv, **kwargs)

    monkeypatch.setattr("skcapstone.coordination.Board.release_claim", release)
    monkeypatch.setattr(retire.subprocess, "run", run)
    return p


def test_stale_pending_proofs_release_exact_claim_then_retire(owned):
    p = owned
    assert retire.recover_failed_generations(p.paths, p.home) == [
        dict(card=p.card, node=p.node, state="retired-prestart")
    ]
    assert p.releases == [
        (
            p.owner,
            p.card,
            dict(actor=p.owner, expected_claim_revision=p.claim, abandon_reason="error"),
        )
    ]
    assert p.current.owner is None and p.current.status == "review"
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}


@pytest.mark.parametrize("heartbeat", [None, "", "bad", "2026-01-01T00:00:00", "fresh", "future"])
def test_fresh_or_unprovable_age_keeps_claim_and_pointers(owned, heartbeat):
    p = owned
    if heartbeat in {"fresh", "future"}:
        heartbeat = (
            datetime.now(timezone.utc) + timedelta(minutes=10 if heartbeat == "future" else -29)
        ).isoformat()
    p.status["heartbeat_at"] = heartbeat
    p.status_path.write_text(json.dumps(p.status))
    result = retire.recover_failed_generations(p.paths, p.home)[0]
    assert result["state"] == "refused"
    assert "heartbeat" in result["reason"]
    assert p.releases == [] and p.current.owner == p.owner
    assert p.status_path.exists() and p.request_path.exists()


@pytest.mark.parametrize("failure", ["active", "unreachable", "invocation", "inventory", "launch"])
def test_no_release_before_all_pending_proofs(owned, monkeypatch, failure):
    p = owned
    if failure == "active":
        p.state["ActiveState"] = "active"
    elif failure == "unreachable":
        monkeypatch.setattr(
            retire.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=255, stdout="")
        )
    elif failure == "invocation":
        p.execution["invocation"] = "1" * 32
    elif failure == "inventory":

        def refuse(*args, **kwargs):
            raise ValueError("inventory mismatch")

        monkeypatch.setattr(retire, "_admission_inventory", refuse)
    elif failure == "launch":
        p.events.append(
            dict(
                action="review_assignment_launch",
                recommendation_id=p.request["request_id"],
                launched=True,
            )
        )
    p.status_path.write_text(json.dumps(p.status))
    retire.recover_failed_generations(p.paths, p.home)
    assert p.releases == [] and p.current.owner == p.owner
    assert p.status_path.exists() and p.request_path.exists()


@pytest.mark.parametrize("seconds,accepted", [(1800, True), (1799, False), (-1, False)])
def test_thirty_minute_boundary(seconds, accepted):
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    status = {"heartbeat_at": (now - timedelta(seconds=seconds)).isoformat()}
    if accepted:
        retire.review_pending.require_stale(status, now=now)
    else:
        with pytest.raises(ValueError, match="heartbeat"):
            retire.review_pending.require_stale(status, now=now)


def test_released_backlog_without_release_proof_is_not_moved(pending):
    p = pending
    p.current.status = "backlog"
    p.events.pop()
    result = retire.recover_failed_generations(p.paths, p.home)
    assert result[0]["state"] == "refused"
    assert p.current.status == "backlog"
    assert not p.calls


def test_no_mutation_when_recovery_preflight_is_applied(owned):
    with pytest.raises(ValueError, match="check-only"):
        owned.invoke(check_recovery=True, apply=True)
    assert owned.releases == []


def test_failed_release_readback_never_retires(owned, monkeypatch):
    p = owned
    monkeypatch.setattr("skcapstone.coordination.Board.release_claim", lambda *a, **kw: False)
    result = retire.recover_failed_generations(p.paths, p.home)
    assert result[0]["state"] == "refused"
    assert "readback" in result[0]["reason"]
    assert p.current.owner == p.owner
    assert p.request_path.exists() and p.status_path.exists()


def test_fresh_released_backlog_is_not_moved(pending):
    p = pending
    p.current.status = "backlog"
    p.status["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    p.status_path.write_text(json.dumps(p.status))
    assert retire.recover_failed_generations(p.paths, p.home)[0]["state"] == "refused"
    assert p.current.status == "backlog" and not p.calls
