"""Remote reviewers prove direct producer death on the producer's host."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_acceptance as acceptance
from skcapstone.fleet import production_review_custody as custody
from skcapstone.fleet.review_dispatch import _source
from tests.fleet.test_released_direct_review import (  # noqa: F401
    open_review,
    released_direct,
    source,
)

UNIT_TERMINAL = custody.unit_terminal


def remote_probe(monkeypatch, state):
    """Keep real terminal validation, replacing only the remote transport."""
    calls = []
    original = subprocess.run
    monkeypatch.setattr(acceptance.socket, "gethostname", lambda: "reviewer")
    monkeypatch.setattr(custody, "unit_terminal", UNIT_TERMINAL)

    def run(command, **kwargs):
        if command[0] != "ssh":
            return original(command, **kwargs)
        calls.append(command)
        if state == "unreachable":
            return SimpleNamespace(returncode=255, stdout="", stderr="connection refused")
        if state == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        values = dict(
            LoadState="loaded",
            ActiveState="inactive",
            SubState="dead",
            MainPID="0",
            ControlPID="0",
            InvocationID="e" * 32,
            Result="success",
            ExecMainStatus="0",
        )
        if state == "absent":
            values.update(LoadState="not-found", InvocationID="")
        elif state == "active":
            values.update(ActiveState="active", SubState="running", MainPID="42")
        elif state == "unknown":
            values = {}
        elif state == "different-invocation":
            values["InvocationID"] = "f" * 32
        return SimpleNamespace(
            returncode=0, stdout="\n".join(f"{k}={v}" for k, v in values.items()), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize("state", ["stopped", "absent"])
def test_remote_review_resolves_released_direct_producer(
    released_direct,  # noqa: F811
    monkeypatch,
    state,
):
    v = released_direct
    review = open_review(v)
    before = v["store"]._read_events(v["card"])
    calls = remote_probe(monkeypatch, state)
    binding = _source(v["home"], v["store"].fold(review.review_card_id))
    assert binding["owner"] == v["owner"]
    assert binding["claim"] == v["claim"]
    assert calls
    assert all(
        c[:5]
        == [
            "ssh",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oConnectTimeout=5",
            "worker",
        ]
        for c in calls
    )
    assert v["store"]._read_events(v["card"]) == before
    assert v["store"].fold(v["card"]).owner is None


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "state", ["active", "unknown", "unreachable", "timeout", "different-invocation"]
)
def test_remote_review_refuses_unproved_producer_death(
    released_direct,  # noqa: F811
    monkeypatch,
    state,
):
    v = released_direct
    review = open_review(v)
    before = v["store"]._read_events(v["card"])
    calls = remote_probe(monkeypatch, state)
    with pytest.raises((ValueError, subprocess.SubprocessError)):
        _source(v["home"], v["store"].fold(review.review_card_id))
    assert calls
    assert v["store"]._read_events(v["card"]) == before


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "mutation", ["legacy", "wrong-host", "missing-invocation", "boolean-exit"]
)
def test_remote_terminal_requires_modern_exact_identity(
    released_direct,  # noqa: F811
    monkeypatch,
    mutation,
):
    v = released_direct
    receipt = v["receipt"]
    if mutation == "legacy":
        receipt.pop("schema")
    elif mutation == "wrong-host":
        receipt["host"] = "other-producer"
    elif mutation == "boolean-exit":
        receipt["exit_code"] = False
    else:
        receipt.pop("invocation")
    v["receipt_path"].write_text(json.dumps(receipt))
    calls = remote_probe(monkeypatch, "absent")
    with pytest.raises(ValueError):
        acceptance._producer_terminal(v["home"], v["store"].fold(v["card"]), v["manifest"])
    assert not calls
