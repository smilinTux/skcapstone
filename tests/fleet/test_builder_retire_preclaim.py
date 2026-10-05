"""Preclaim retirement preserves source and refuses hidden execution custody."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_retire as retire
from skcapstone.fleet import production_admission as admission
from tests.fleet.test_builder_retire import case as case
from tests.fleet.test_builder_retire import run, write


@pytest.fixture
def preclaim(case, monkeypatch):
    c = case
    c.request.update(
        offered_at="2026-01-01T00:00:00Z", production={"host": "worker", "family": "glm"}
    )
    c.status = dict(
        schema="skfleet.builder-dispatch-status/v1",
        card_id=c.request["card_id"],
        request_id=c.request["request_id"],
        node=c.request["node"],
        production=c.request["production"],
        state="failed",
        attempt=2,
        error="exact source reconstruction failed",
    )
    write(c.req, json.dumps(c.request).encode())
    write(c.sts, json.dumps(c.status).encode())
    c.kwargs.update(
        request_sha256=retire.sha(c.req.read_bytes()), status_sha256=retire.sha(c.sts.read_bytes())
    )
    c.workspace = c.paths.root / "workspaces/pi-glm-builder-node-worker-1234abcd"
    write(c.workspace / ".git/index", b"exact base metadata")
    write(c.workspace / "source.py", b"public synthetic source")
    c.events = []
    c.state = dict(
        LoadState="not-found",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        ControlPID="0",
        InvocationID="",
    )
    c.history = ""
    c.source = {"head": c.request["base_revision"], "tree": "e" * 40}
    monkeypatch.setattr(retire.CardStore, "_read_events", lambda *_: c.events)
    monkeypatch.setattr(admission, "unit_state", lambda *_args, **_kwargs: c.state)
    monkeypatch.setattr(
        retire.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=c.history, stderr=""),
    )
    monkeypatch.setattr(retire.source_bundle, "_inspect", lambda *_: c.source)
    c.kwargs["probe"] = lambda host, payload: retire.node_check(
        payload, paths=c.paths, home=c.home, locked=True
    )
    return c


def test_preclaim_check_apply_and_replay_preserve_exact_source(preclaim):
    c = preclaim
    before = c.req.read_bytes(), c.sts.read_bytes(), retire.inventory(c.workspace)
    assert run(c)["state"] == "qualified-check-only"
    assert not (c.home / "evidence").exists()
    assert run(c, apply=True)["state"] == "retired"
    assert not c.req.exists()
    assert (c.sts.read_bytes(), retire.inventory(c.workspace)) == before[1:]
    root = retire.directory(c.home, c.request["card_id"], c.request["request_id"])
    assert (root / "request.json").read_bytes() == before[0]
    assert (root / "status.json").read_bytes() == before[1]
    assert retire.archive_inventory(root / "workspace.tar.gz") == before[2]
    assert json.loads((root / "retirement.json").read_text())["proof"]["preclaim"] is True
    assert run(c, apply=True)["state"] == "already-retired"


@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "unit",
        "pid",
        "invocation",
        "journal",
        "reservation",
        "source",
        "partial-claim",
        "other-error",
        "retryable",
        "host",
    ],
)
def test_preclaim_execution_or_changed_source_refuses_before_preservation(preclaim, change):
    c = preclaim
    if change == "claim":
        c.events.append({"action": "claim", "ts": c.request["offered_at"]})
    elif change == "unit":
        c.state.update(LoadState="loaded", ActiveState="active", MainPID="123")
    elif change == "invocation":
        c.state["InvocationID"] = "f" * 32
    elif change == "pid":
        c.status["pid"] = 123
    elif change == "journal":
        c.history = '{"USER_UNIT":"previous native launch"}'
    elif change == "reservation":
        path = c.home / "fleet/resource-admission/worker" / ("f" * 64) / "intent.json"
        write(path, json.dumps({"binding": {"request_id": c.request["request_id"]}}).encode())
    elif change == "source":
        c.source["head"] = "f" * 40
    elif change == "partial-claim":
        c.status["claim_revision"] = "f" * 32
    elif change == "other-error":
        c.status["error"] = "unknown execution failure"
    elif change == "retryable":
        c.status["attempt"] = 1
    else:
        c.request["production"]["host"] = "another"
    write(c.req, json.dumps(c.request).encode())
    write(c.sts, json.dumps(c.status).encode())
    c.kwargs.update(
        request_sha256=retire.sha(c.req.read_bytes()), status_sha256=retire.sha(c.sts.read_bytes())
    )
    before = c.req.read_bytes(), c.sts.read_bytes(), retire.inventory(c.workspace)
    with pytest.raises(ValueError):
        run(c, apply=True)
    assert (c.req.read_bytes(), c.sts.read_bytes(), retire.inventory(c.workspace)) == before
    assert not (c.home / "evidence").exists()


def test_old_claim_does_not_fake_a_claim_for_this_offer(preclaim):
    c = preclaim
    c.events.append({"action": "claim", "ts": "2025-12-31T00:00:00Z"})
    assert run(c)["state"] == "qualified-check-only"
