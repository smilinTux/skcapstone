"""Tests for :mod:`skdashboard.queue_authz`.

All PDP checks are exercised via an injected ``decide_fn`` so these tests
never touch a live capauth PDP or ``~/.skcapstone`` registry.
"""

from __future__ import annotations

import pytest

from skdashboard.queue_authz import authorize_queue, capability_for


# --------------------------------------------------------------------------- #
# capability_for
# --------------------------------------------------------------------------- #
def test_capability_for_execute():
    assert capability_for("execute") == "agentrun.execute"


@pytest.mark.parametrize("mode", ["propose", "dry-run", "queue", "", "bogus"])
def test_capability_for_non_execute(mode):
    assert capability_for(mode) == "agentrun.queue"


# --------------------------------------------------------------------------- #
# token mode
# --------------------------------------------------------------------------- #
def test_token_mode_allows_on_match(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "token")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    result = authorize_queue(token="sekrit", resource="card-1", mode="propose")
    assert result["ok"] is True
    assert result["via"] == "token"


def test_token_mode_denies_on_mismatch(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "token")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    result = authorize_queue(token="wrong", resource="card-1", mode="propose")
    assert result["ok"] is False
    assert result["via"] == "token"


def test_token_mode_denies_when_no_token_presented(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "token")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    result = authorize_queue(token=None, resource="card-1", mode="propose")
    assert result["ok"] is False
    assert result["via"] == "token"


def test_token_mode_denies_when_secret_unset(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "token")
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)
    result = authorize_queue(token="anything", resource="card-1", mode="propose")
    assert result["ok"] is False
    assert result["via"] == "token"
    assert "not configured" in result["reason"]


def test_token_mode_denies_when_secret_empty(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "token")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "")
    result = authorize_queue(token="", resource="card-1", mode="propose")
    assert result["ok"] is False
    assert result["via"] == "token"


def test_default_authz_mode_is_token_when_unset(monkeypatch):
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    result = authorize_queue(token="sekrit", resource="card-1", mode="propose")
    assert result["via"] == "token"
    assert result["ok"] is True


def test_unrecognized_authz_mode_falls_back_to_token(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "bogus-mode")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    result = authorize_queue(token="sekrit", resource="card-1", mode="propose")
    assert result["via"] == "token"
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# pdp mode
# --------------------------------------------------------------------------- #
def test_pdp_mode_allows_when_decide_fn_allows(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")

    def allow_fn(*, capability, resource, actor):
        assert capability == "agentrun.queue"
        assert resource == "card-1"
        assert actor == "lumina"
        return True

    result = authorize_queue(
        token=None, resource="card-1", mode="propose", actor="lumina", decide_fn=allow_fn
    )
    assert result == {"ok": True, "reason": "pdp ok", "via": "pdp", "obligations": []}


def test_pdp_mode_allows_with_dict_result(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")

    def allow_fn(*, capability, resource, actor):
        return {"allow": True}

    result = authorize_queue(
        token=None, resource="card-1", mode="propose", actor="lumina", decide_fn=allow_fn
    )
    assert result["ok"] is True
    assert result["via"] == "pdp"


def test_pdp_mode_surfaces_obligations_from_dict_result(monkeypatch):
    """The PDP AUDIT obligation must survive the authz gate instead of being
    discarded (Unified Consent Plane Phase 2, card 90d23f56): a consumer that
    wants to persist a consent.granted event reads this key."""
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")
    audit = {"kind": "audit", "data": {"event": "authz.decide", "decision": "allow"}}

    def allow_fn(*, capability, resource, actor):
        return {"allow": True, "obligations": [audit]}

    result = authorize_queue(
        token=None, resource="card-1", mode="propose", actor="lumina", decide_fn=allow_fn
    )
    assert result["ok"] is True
    assert result["obligations"] == [audit]


def test_token_mode_obligations_empty_no_pdp_call(monkeypatch):
    """Token-only mode never calls the PDP, so there is no audit obligation
    to surface - the key is present but empty, never missing."""
    monkeypatch.setenv("SKAI_AUTHZ", "token")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    result = authorize_queue(token="sekrit", resource="card-1", mode="propose")
    assert result["obligations"] == []


def test_pdp_mode_denies_when_decide_fn_denies(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")

    def deny_fn(*, capability, resource, actor):
        return False

    result = authorize_queue(
        token=None, resource="card-1", mode="propose", actor="lumina", decide_fn=deny_fn
    )
    assert result["ok"] is False
    assert result["via"] == "pdp"


def test_pdp_mode_denies_fail_closed_when_decide_fn_raises(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")

    def raising_fn(*, capability, resource, actor):
        raise RuntimeError("pdp unreachable")

    result = authorize_queue(
        token=None, resource="card-1", mode="propose", actor="lumina", decide_fn=raising_fn
    )
    assert result["ok"] is False
    assert result["via"] == "pdp"
    assert "raised" in result["reason"]


def test_pdp_mode_execute_capability_requested(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")
    seen = {}

    def capture_fn(*, capability, resource, actor):
        seen["capability"] = capability
        return False

    authorize_queue(
        token=None, resource="card-1", mode="execute", actor="lumina", decide_fn=capture_fn
    )
    assert seen["capability"] == "agentrun.execute"


def test_pdp_mode_deny_reason_mentions_verified_for_execute(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")

    def deny_fn(*, capability, resource, actor):
        return False

    result = authorize_queue(
        token=None, resource="card-1", mode="execute", actor="lumina", decide_fn=deny_fn
    )
    assert result["ok"] is False
    assert "verified" in result["reason"]


def test_pdp_mode_uses_default_decide_fn_when_none_and_capauth_import_fails(monkeypatch):
    # No capauth PDP is set up in this test environment; the default decide_fn
    # must fail closed (deny) rather than raise, whether that is because the
    # import fails or because the subject/resource facts are unknown.
    monkeypatch.setenv("SKAI_AUTHZ", "pdp")
    result = authorize_queue(token=None, resource="card-1", mode="propose", actor="lumina")
    assert result["ok"] is False
    assert result["via"] == "pdp"


# --------------------------------------------------------------------------- #
# both mode
# --------------------------------------------------------------------------- #
def test_both_mode_allows_when_token_and_pdp_allow(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "both")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")

    def allow_fn(*, capability, resource, actor):
        return True

    result = authorize_queue(
        token="sekrit", resource="card-1", mode="propose", actor="lumina", decide_fn=allow_fn
    )
    assert result["ok"] is True
    assert result["via"] == "both"


def test_both_mode_denies_when_token_fails(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "both")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")

    def allow_fn(*, capability, resource, actor):
        return True

    result = authorize_queue(
        token="wrong", resource="card-1", mode="propose", actor="lumina", decide_fn=allow_fn
    )
    assert result["ok"] is False
    assert result["via"] == "both"


def test_both_mode_denies_when_pdp_fails(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "both")
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")

    def deny_fn(*, capability, resource, actor):
        return False

    result = authorize_queue(
        token="sekrit", resource="card-1", mode="propose", actor="lumina", decide_fn=deny_fn
    )
    assert result["ok"] is False
    assert result["via"] == "both"


def test_both_mode_denies_when_both_fail(monkeypatch):
    monkeypatch.setenv("SKAI_AUTHZ", "both")
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)

    def deny_fn(*, capability, resource, actor):
        return False

    result = authorize_queue(
        token=None, resource="card-1", mode="propose", actor="lumina", decide_fn=deny_fn
    )
    assert result["ok"] is False
    assert result["via"] == "both"
    assert "token" in result["reason"]
    assert "pdp" in result["reason"]
