"""Retire only exact released, unacknowledged review admissions."""

import json
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_admission as admission
from skcapstone.fleet import review_dispatch
from skcapstone.fleet import review_retire as retire
from skcapstone.fleet.paths import FleetPaths


@pytest.fixture
def pending(tmp_path, monkeypatch):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    card, node, owner, claim = "0a10fcae", "node-chiap03", "pi-review-0a10fcae", "b" * 32
    core = dict(
        title="[REVIEW] exact",
        description="exact source",
        acceptance_criteria=["bytes"],
        labels=["review"],
        dependencies=[],
        links={},
    )
    source = {"head": "c" * 40}
    request = dict(
        schema="skfleet.builder-dispatch/v2",
        work_kind="review",
        card_id=card,
        node=node,
        request_id="a" * 64,
        reviewer=owner,
        source=source,
        criteria_sha256=retire.production_builder.digest(core),
        production={"host": "chiap03", "family": "codex"},
    )
    unit = "skfleet-worker-codex-" + card + ".service"
    binding = dict(
        card_id=card,
        owner=owner,
        claim_revision=claim,
        request_id=request["request_id"],
        work_kind="review",
    )
    intent = dict(
        schema="skfleet.resource-admission/v1",
        host="chiap03",
        unit=unit,
        binding=binding,
        argv_sha256="d" * 64,
    )
    identity = admission._reservation_id(intent)
    execution = dict(
        host="chiap03",
        node=node,
        unit=unit,
        invocation="",
        request_id=request["request_id"],
        request_sha256=retire.production_builder.digest(request),
        admission_id=identity,
        admission_sha256=admission._digest(intent),
        work_kind="review",
    )
    status = dict(
        schema="skfleet.builder-dispatch-status/v1",
        work_kind="review",
        card_id=card,
        node=node,
        request_id=request["request_id"],
        owner=owner,
        claim_revision=claim,
        state="admission-pending",
        execution=execution,
        admission=intent,
    )
    request_path = retire.dispatch.request_path(paths, node, card)
    status_path = retire.dispatch.status_path(paths, node, card)
    for p, value in [(request_path, request), (status_path, status)]:
        retire.source_bundle._once(p, json.dumps(value).encode())
    events = [
        dict(
            action="remote_review_offer",
            request_id=request["request_id"],
            request_sha256=retire.production_builder.digest(request),
        ),
        dict(
            action="release_claim",
            writer="reviewer",
            released_owner=owner,
            expected_claim_revision=claim,
        ),
    ]
    current = SimpleNamespace(owner=None, archived=False, meta={}, labels=["review"])
    current.model_dump = lambda **kw: core

    class Store:
        def __init__(self, home):
            pass

        def fold(self, card):
            return current

        def _read_events(self, card):
            return events

        def append_event(self, card, action, writer, **kw):
            events.append(dict(action=action, writer=writer, **kw))

    monkeypatch.setattr(retire, "CardStore", Store)
    monkeypatch.setattr(retire, "card_mutation_lock", lambda *a: nullcontext())
    monkeypatch.setattr(retire, "governed_review_assignment_ready", lambda c: True)
    monkeypatch.setattr(retire, "review_state_revision", lambda c: "f" * 64)
    monkeypatch.setattr(retire.production_builder, "policy", lambda: {"authority_host": "chiap08"})
    monkeypatch.setattr(retire.production_builder, "node_binding", lambda *a: {"host": "chiap03"})
    monkeypatch.setattr(retire.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(review_dispatch, "_source", lambda *a: source)
    calls = []
    state = dict(
        LoadState="loaded",
        ActiveState="failed",
        SubState="failed",
        MainPID="0",
        ControlPID="0",
        InvocationID="e" * 32,
    )

    def run(argv, **kw):
        calls.append(argv)
        assert argv[:5] == [
            "ssh",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oConnectTimeout=5",
            "chiap03",
        ]
        output = "\n".join(k + "=" + v for k, v in state.items()) if "systemctl" in argv else ""
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(retire.subprocess, "run", run)

    def invoke(**kw):
        status_path.write_text(json.dumps(status))
        return retire.retire_prestart(
            paths,
            home,
            node,
            card,
            request_sha256=retire.source_bundle._sha(request_path.read_bytes()),
            status_sha256=retire.source_bundle._sha(status_path.read_bytes()),
            card_sha256="f" * 64,
            previous_owner=owner,
            previous_claim_revision=claim,
            actor="niobe",
            reason="unacknowledged admission",
            **kw,
        )

    return SimpleNamespace(**locals())


def test_pending_retirement_archives_status_and_is_recognized(pending):
    p = pending
    assert p.invoke()["state"] == "qualified-check-only"
    assert p.request_path.exists() and p.status_path.exists()
    result = p.invoke(apply=True)
    assert result["state"] == "retired-prestart"
    assert not p.request_path.exists() and not p.status_path.exists()
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}
    receipt = retire._receipt_path(p.home, p.card, p.request["request_id"])
    (receipt.parent / "status.json").write_text("{}")
    assert retire.retired_offers(p.home, p.card, p.events) == set()


@pytest.mark.parametrize(
    "case",
    [
        "invocation",
        "running",
        "launch",
        "owner",
        "claim",
        "missing-release",
        "active",
        "pid",
        "host",
        "unit",
        "request",
        "packet",
        "source",
        "criteria",
        "hold",
        "unreachable",
        "session",
    ],
)
def test_pending_refuses_without_all_proofs(pending, monkeypatch, case):
    p = pending
    if case == "invocation":
        p.execution["invocation"] = "1" * 32
    elif case == "running":
        p.status["state"] = "running"
    elif case == "launch":
        p.events.append(
            dict(
                action="review_assignment_launch",
                recommendation_id=p.request["request_id"],
                launched=True,
            )
        )
    elif case == "owner":
        p.current.owner = p.owner
    elif case == "claim":
        p.status["claim_revision"] = "2" * 32
    elif case == "missing-release":
        p.events.pop()
    elif case == "active":
        p.state["ActiveState"] = "active"
    elif case == "pid":
        p.state["MainPID"] = "123"
    elif case == "host":
        p.execution["host"] = "chiap04"
    elif case == "unit":
        p.execution["unit"] = "skfleet-worker-codex-other.service"
    elif case == "request":
        p.execution["request_sha256"] = "0" * 64
    elif case == "packet":
        p.status["review_packet"] = {}
    elif case == "source":
        monkeypatch.setattr(review_dispatch, "_source", lambda *a: {})
    elif case == "criteria":
        p.core["title"] = "changed"
    elif case == "hold":
        p.current.labels.append("hold")
    elif case == "unreachable":
        monkeypatch.setattr(
            retire.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=255, stdout="")
        )
    elif case == "session":
        original = retire.subprocess.run
        monkeypatch.setattr(
            retire.subprocess,
            "run",
            lambda argv, **kw: (
                SimpleNamespace(returncode=0, stdout="pi-" + p.card)
                if "tmux" in argv
                else original(argv, **kw)
            ),
        )
    with pytest.raises((ValueError, subprocess.SubprocessError)):
        p.invoke(apply=True)
    assert p.request_path.exists() and p.status_path.exists()
    assert not any(e["action"] == "remote_review_prestart_retire" for e in p.events)


@pytest.mark.parametrize("start", [False, True])
def test_pending_keeps_exact_reservation_and_start_evidence(pending, start):
    p = pending
    directory = p.home / "fleet/resource-admission/chiap03" / p.identity
    retire.source_bundle._once(directory / "intent.json", json.dumps(p.intent).encode())
    if start:
        retire.source_bundle._once(
            directory / "start.json",
            json.dumps(
                dict(
                    schema="skfleet.resource-start/v1",
                    reservation_id=p.identity,
                    binding=p.binding,
                    argv_sha256=p.intent["argv_sha256"],
                )
            ).encode(),
        )
    assert p.invoke(apply=True)["state"] == "retired-prestart"
    assert (directory / "intent.json").exists()
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}


@pytest.mark.parametrize("receipt", ["observed.json", "terminal.json", "start.json"])
def test_pending_refuses_additional_or_invalid_reservation_custody(pending, receipt):
    p = pending
    directory = p.home / "fleet/resource-admission/chiap03" / p.identity
    retire.source_bundle._once(directory / "intent.json", json.dumps(p.intent).encode())
    retire.source_bundle._once(directory / receipt, b"{}")
    with pytest.raises(ValueError):
        p.invoke(apply=True)
    assert p.request_path.exists() and p.status_path.exists()


def test_pending_sweep_retires_released_admission_without_worker_exit(pending):
    p = pending
    assert retire.recover_failed_generations(p.paths, p.home) == [
        {"card": p.card, "node": p.node, "state": "retired-prestart"}
    ]
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}
    assert retire.recover_failed_generations(p.paths, p.home) == []


def test_pending_sweep_never_releases_an_owned_claim(pending):
    p = pending
    p.current.owner = p.owner
    p.current.meta["_claim_revision"] = p.claim
    assert retire.recover_failed_generations(p.paths, p.home)[0]["state"] == "refused"
    assert p.current.owner == p.owner
    assert p.request_path.exists() and p.status_path.exists()


@pytest.mark.parametrize("target", ["status", "request"])
def test_pending_refuses_pointer_changes_during_host_probe(pending, monkeypatch, target):
    p = pending
    original = retire.subprocess.run

    def run(argv, **kw):
        result = original(argv, **kw)
        if "tmux" in argv:
            (p.status_path if target == "status" else p.request_path).write_text("{}")
        return result

    monkeypatch.setattr(retire.subprocess, "run", run)
    with pytest.raises(ValueError, match="changed"):
        p.invoke(apply=True)
    assert not any(e["action"] == "remote_review_prestart_retire" for e in p.events)


def test_pending_allows_explicit_unsuccessful_launch_observation(pending):
    p = pending
    p.events.append(
        dict(
            action="review_assignment_launch",
            recommendation_id=p.request["request_id"],
            launched=False,
        )
    )
    assert p.invoke(apply=True)["state"] == "retired-prestart"
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}


@pytest.mark.parametrize(
    "state",
    [
        dict(LoadState="not-found", ActiveState="inactive", SubState="dead"),
        dict(LoadState="loaded", ActiveState="inactive", SubState="dead"),
    ],
)
def test_pending_accepts_stopped_or_collected_reserved_unit(pending, state):
    pending.state.update(state)
    assert pending.invoke(apply=True)["state"] == "retired-prestart"


@pytest.mark.parametrize(
    "case",
    [
        "status-hash",
        "request-hash",
        "claim-revision",
        "exit-receipt",
        "duplicate-release",
        "acknowledgment",
        "malformed-intent",
        "timeout",
    ],
)
def test_pending_additional_custody_and_identity_refusals(pending, monkeypatch, case):
    p = pending
    if case in {"status-hash", "request-hash"}:
        kwargs = dict(
            request_sha256=retire.source_bundle._sha(p.request_path.read_bytes()),
            status_sha256=retire.source_bundle._sha(p.status_path.read_bytes()),
            card_sha256="f" * 64,
            previous_owner=p.owner,
            previous_claim_revision=p.claim,
            actor="niobe",
            reason="test",
            apply=True,
        )
        kwargs["status_sha256" if case == "status-hash" else "request_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="hash changed"):
            retire.retire_prestart(p.paths, p.home, p.node, p.card, **kwargs)
        return
    if case == "claim-revision":
        p.current.meta["_claim_revision"] = "3" * 32
    elif case == "exit-receipt":
        path = p.home / "evidence/production-review-exits" / (p.card + "-" + p.claim + ".json")
        retire.source_bundle._once(path, b"{}")
    elif case == "duplicate-release":
        p.events.append(dict(p.events[-1]))
    elif case == "acknowledgment":
        p.status["acknowledgment"] = {}
    elif case == "malformed-intent":
        p.intent["binding"] = None
    elif case == "timeout":

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("ssh", 10)

        monkeypatch.setattr(retire.subprocess, "run", timeout)
    with pytest.raises((ValueError, subprocess.SubprocessError)):
        p.invoke(apply=True)
    assert p.request_path.exists() and p.status_path.exists()
    assert not any(e["action"] == "remote_review_prestart_retire" for e in p.events)
