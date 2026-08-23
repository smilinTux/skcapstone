"""Tests for SendAuthority (P4, card c6a87139): the only object able to
send, gated on an explicit arm, an armer identity, no-self-approval, and a
wired transport. Any one missing condition refuses instead of sending.
"""

from __future__ import annotations

import pytest

from skcapstone import send_authority as sa


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """The wired transport is a module global; a leaked dispatcher would
    poison unrelated tests, same discipline as agent_run's execute seam."""
    sa.set_send_dispatcher(None)
    yield
    sa.set_send_dispatcher(None)


def _draft(prepared_by="lumina"):
    return {"subject": "GMKtec RMA escalation", "body": "...", "prepared_by": prepared_by}


def test_unarmed_authority_refuses():
    authority = sa.SendAuthority(armed=False, armed_by="chef")
    with pytest.raises(sa.SendAuthorityError, match="not armed"):
        authority.send(_draft())


def test_armed_but_no_armer_identity_refuses():
    authority = sa.SendAuthority(armed=True, armed_by="")
    with pytest.raises(sa.SendAuthorityError, match="armer identity"):
        authority.send(_draft())


def test_self_approval_refuses():
    """The armer must not be the same identity that prepared the draft."""
    authority = sa.SendAuthority(armed=True, armed_by="lumina")
    with pytest.raises(sa.SendAuthorityError, match="self-approval"):
        authority.send(_draft(prepared_by="lumina"))


def test_armed_and_attributed_but_no_transport_wired_refuses():
    authority = sa.SendAuthority(armed=True, armed_by="chef")
    with pytest.raises(sa.SendAuthorityError, match="no transport wired"):
        authority.send(_draft(prepared_by="lumina"))


def test_fully_satisfied_send_calls_the_wired_transport():
    calls = []
    sa.set_send_dispatcher(lambda d: calls.append(d) or {"sent": True})
    authority = sa.SendAuthority(armed=True, armed_by="chef")

    result = authority.send(_draft(prepared_by="lumina"))

    assert result == {"sent": True}
    assert calls == [_draft(prepared_by="lumina")]


def test_prepared_by_can_be_overridden_explicitly():
    sa.set_send_dispatcher(lambda d: {"sent": True})
    authority = sa.SendAuthority(armed=True, armed_by="chef")
    # draft carries no prepared_by; caller supplies it explicitly
    authority.send({"subject": "x"}, prepared_by="lumina")  # different from armer: ok

    with pytest.raises(sa.SendAuthorityError, match="self-approval"):
        authority.send({"subject": "x"}, prepared_by="chef")  # same as armer: refused


def test_send_dispatch_available_toggle():
    assert sa.send_dispatch_available() is False
    sa.set_send_dispatcher(lambda d: d)
    assert sa.send_dispatch_available() is True
    sa.set_send_dispatcher(None)
    assert sa.send_dispatch_available() is False


def test_no_default_arms_true():
    """There is no default constructor argument that arms an authority: the
    caller must say armed=True out loud."""
    with pytest.raises(TypeError):
        sa.SendAuthority(armed_by="chef")  # missing required armed=


def test_transport_failure_propagates_unmodified():
    def _boom(_draft):
        raise ConnectionError("smtp down")

    sa.set_send_dispatcher(_boom)
    authority = sa.SendAuthority(armed=True, armed_by="chef")
    with pytest.raises(ConnectionError, match="smtp down"):
        authority.send(_draft(prepared_by="lumina"))


def test_order_of_checks_reports_first_violation():
    """Not armed wins over every other violation, including self-approval."""
    authority = sa.SendAuthority(armed=False, armed_by="lumina")
    with pytest.raises(sa.SendAuthorityError, match="not armed"):
        authority.send(_draft(prepared_by="lumina"))
