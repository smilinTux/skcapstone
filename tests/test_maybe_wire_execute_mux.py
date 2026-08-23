"""Tests for agent_run._maybe_wire_execute_mux (P4, card c6a87139).

A SEPARATE wiring step from _maybe_wire_execute_bridge (tests in
tests/test_execute_bridge_wiring.py, untouched by this card): proves it
wraps whatever _maybe_wire_execute_bridge left behind (a real dispatcher, or
None) into a mux, is idempotent, and that the resulting end-to-end wiring
via run_ai_runner_job actually routes an alert card to a draft instead of
dead-ending on the "sandbox unavailable" refusal.
"""

from __future__ import annotations

from unittest import mock

import pytest

from skcapstone import agent_run as ar
from skcapstone import alert_store


@pytest.fixture(autouse=True)
def _reset_execute_dispatcher():
    ar.set_execute_dispatcher(None)
    yield
    ar.set_execute_dispatcher(None)


def test_wires_a_mux_when_nothing_was_wired():
    assert ar.execute_dispatch_available() is False
    ar._maybe_wire_execute_mux()
    assert ar.execute_dispatch_available() is True
    assert getattr(ar._execute_dispatcher, "_is_execute_mux", False) is True


def test_idempotent_second_call_does_not_rewrap():
    ar._maybe_wire_execute_mux()
    first = ar._execute_dispatcher
    ar._maybe_wire_execute_mux()
    assert ar._execute_dispatcher is first


def test_wraps_an_existing_code_dispatcher_as_the_code_leg(tmp_path):
    from skcapstone.card_store import CardCore, CardStore

    CardStore(tmp_path).create(
        CardCore(id="t1", kind="task", title="fix it", initial_labels=["repo:skcapstone"])
    )
    code = mock.Mock(return_value={"summary": "code", "activity": [], "links": {}})
    ar.set_execute_dispatcher(code)

    ar._maybe_wire_execute_mux()

    assert ar._execute_dispatcher is not code  # wrapped, not replaced in place
    out = ar._execute_dispatcher({"card_id": "t1"})
    code.assert_called_once()
    assert out["summary"] == "code"


def test_end_to_end_alert_execute_drafts_instead_of_gating(tmp_path, monkeypatch):
    """Before this card: an alert card queued for execute, with
    SKAI_RUNNER_LIVE=1 and no code bridge wired, dead-ends at "execute gated
    (R1)" having done nothing. After: it routes to the comms executor and
    produces a draft."""
    import skcapstone

    monkeypatch.setattr(skcapstone, "SHARED_ROOT", str(tmp_path), raising=False)
    monkeypatch.delenv("SKAI_EXECUTE_BRIDGE", raising=False)
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")

    alert_store.raise_alert(tmp_path, "a1", "GMKtec RMA", options=["escalate"])
    ar.request_run(tmp_path, "alert-a1", "handle the alert", mode="execute")

    ar.run_ai_runner_job()

    run = ar.current_run(tmp_path, "alert-a1")
    assert run["state"] == ar.NEEDS_REVIEW
    assert run.get("last_error", "") == ""


def test_end_to_end_repo_labeled_execute_still_gates_without_bridge(tmp_path, monkeypatch):
    """A repo-labeled card with no code bridge wired still gets a clean,
    well-formed refusal (never a crash, never a dispatch) - the property
    this card must not break."""
    import skcapstone

    monkeypatch.setattr(skcapstone, "SHARED_ROOT", str(tmp_path), raising=False)
    monkeypatch.delenv("SKAI_EXECUTE_BRIDGE", raising=False)
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")

    from skcapstone.card_store import CardCore, CardStore

    CardStore(tmp_path).create(
        CardCore(id="t1", kind="task", title="fix it", initial_labels=["repo:skcapstone"])
    )
    ar.request_run(tmp_path, "t1", "make the change", mode="execute")

    ar.run_ai_runner_job()

    run = ar.current_run(tmp_path, "t1")
    assert run["state"] == ar.NEEDS_REVIEW


def test_the_idempotency_marker_is_identity_not_truthiness():
    """A truthy-but-not-True marker must NOT count as already-muxed.

    Regression control for the bug this file's wrapping test caught: the guard
    used to read `if getattr(d, "_is_execute_mux", False)`, and any object with a
    permissive __getattr__ satisfies that. `unittest.mock.Mock` auto-creates the
    attribute as a truthy child mock, so the wrap silently became a no-op against
    every mocked dispatcher. `build_execute_mux` stamps exactly True, so identity
    is what the marker means.

    Asserted from both sides so neither direction can regress alone.
    """
    truthy_not_true = mock.Mock(return_value={"summary": "x", "activity": [], "links": {}})
    assert getattr(truthy_not_true, "_is_execute_mux", False)  # truthy
    assert getattr(truthy_not_true, "_is_execute_mux", False) is not True  # but not True

    ar.set_execute_dispatcher(truthy_not_true)
    ar._maybe_wire_execute_mux()
    assert ar._execute_dispatcher is not truthy_not_true, (
        "a truthy-but-not-True marker was treated as already-muxed, so the code "
        "leg was never wired"
    )

    # Positive control: a real marker (exactly True) IS honoured, so the fix did
    # not simply disable idempotency.
    already = ar._execute_dispatcher
    assert getattr(already, "_is_execute_mux", False) is True
    ar._maybe_wire_execute_mux()
    assert ar._execute_dispatcher is already
