"""Regressions for stale beats and atomic observer/worker claim separation."""

import ast
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore

from skcapstone.cli.coord_liveness import register_coord_liveness_command
from skcapstone.fleet import liveness_publication as publication
from skcapstone.fleet import worker_liveness_runtime as runtime
from skcapstone.fleet.worker_liveness import WorkerProjection, classify, reconcile
from tests.test_worker_liveness_runtime import observation

CARD = "deadbeef"
OWNER = "pi-seraph-chiap08-deadbeef"
CLAIM = "a" * 32
INVOCATION = "b" * 32
UNIT = f"skfleet-worker-glm-{CARD}.service"


def properties(**changes):
    """Render realistic systemd properties retained after process exit."""
    values = {
        "ExecMainPID": "123",
        "InvocationID": INVOCATION,
        "ActiveState": "active",
        "ControlGroup": f"/user.slice/{UNIT}",
        "WorkingDirectory": "/work/test",
        "ExecStart": (
            "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /bin/skfleet-worker-wrapper.py "
            f"--card {CARD} --owner {OWNER} --claim-revision {CLAIM} "
            "-- bash -lc 'child' ; ignore_errors=no ; }"
        ),
    }
    values.update(changes)
    return values


def result(values):
    """Return a synthetic systemctl result without operating real units."""
    return SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={v}" for k, v in values.items()))


def create_card(home):
    """Seed an isolated real CardStore with one exact claim."""
    store = CardStore(home)
    store.create(
        CardCore(
            id=CARD,
            title="[M] Liveness fixture",
            created_by="test",
            initial_owner=OWNER,
            initial_claim_revision=CLAIM,
        )
    )
    return store


def publish(home, **changes):
    """Exercise the real lock and event writer with synthetic host facts."""
    values = dict(
        card=CARD,
        owner=OWNER,
        claim=CLAIM,
        state="active",
        unit=UNIT,
        pid=123,
        invocation=INVOCATION,
        actor="niobe",
        runner=lambda _: result(properties()),
    )
    values.update(changes)
    return publication.publish_liveness(home, **values)


def test_reused_unit_old_and_new_beats_only_observe_current_wrapper(tmp_path, monkeypatch):
    create_card(tmp_path)
    monkeypatch.setattr(runtime, "workspace_custody", lambda _: ("repo", "a" * 40, "b" * 64))
    directory = tmp_path / "fleet/beats"
    directory.mkdir(parents=True)
    common = dict(
        card_id=CARD,
        unit=UNIT,
        pid=123,
        invocation_id=INVOCATION,
        session_id="current",
        beat_at=datetime.now(timezone.utc).isoformat(),
    )
    (directory / "old.json").write_text(
        json.dumps(dict(common, owner="old", claim_revision="c" * 32))
    )
    (directory / "current.json").write_text(
        json.dumps(dict(common, owner=OWNER, claim_revision=CLAIM))
    )
    rows = runtime.collect_observations(
        tmp_path, runner=lambda _: result(properties()), cgroup_root=tmp_path
    )
    assert [(row.owner, row.claim_generation, row.pid) for row in rows] == [(OWNER, CLAIM, 123)]


@pytest.mark.parametrize(
    "changes",
    [
        {"ExecMainPID": "456"},
        {"InvocationID": "c" * 32},
        {"ExecStart": properties()["ExecStart"].replace(OWNER, "old-owner")},
        {"ExecStart": properties()["ExecStart"].replace(CLAIM, "old-claim")},
        {"ExecStart": properties()["ExecStart"].replace(CARD, "feedface")},
        {"ExecStart": ""},
        {"InvocationID": ""},
        {"ActiveState": "inactive"},
    ],
)
def test_changed_process_or_wrapper_fails_closed_at_publication(tmp_path, changes):
    store = create_card(tmp_path)
    before = store._read_events(CARD)
    assert not publish(tmp_path, runner=lambda _: result(properties(**changes)))
    assert store._read_events(CARD) == before


@pytest.mark.parametrize("change", ["claim", "owner", "complete"])
def test_claim_changed_between_observation_and_publication_is_noop(tmp_path, change):
    store = create_card(tmp_path)
    if change == "claim":
        store.append_event(CARD, "claim", OWNER, owner=OWNER, claim_revision="c" * 32)
    elif change == "owner":
        store.append_event(CARD, "assign", "niobe", owner="other")
    else:
        store.append_event(CARD, "complete", OWNER)
    before = store._read_events(CARD)
    assert not publish(tmp_path)
    assert store._read_events(CARD) == before


def test_honest_actor_idempotence_and_native_cli(tmp_path, monkeypatch):
    store = create_card(tmp_path)
    assert publish(tmp_path)
    event = store._read_events(CARD)[-1]
    assert event["writer"] == "niobe"
    assert event["expected_owner"] == OWNER and event["expected_claim_revision"] == CLAIM
    assert not publish(tmp_path)
    assert store._read_events(CARD)[-1] == event
    original = publication.publish_liveness
    monkeypatch.setattr(
        publication,
        "publish_liveness",
        lambda *a, **kw: original(*a, **kw, runner=lambda _: result(properties())),
    )
    group = click.Group()
    register_coord_liveness_command(group)
    outcome = CliRunner().invoke(
        group,
        [
            "worker-liveness",
            CARD,
            "--owner",
            OWNER,
            "--expected-claim-revision",
            CLAIM,
            "--state",
            "active",
            "--unit",
            UNIT,
            "--pid",
            "123",
            "--invocation",
            INVOCATION,
            "--agent",
            "niobe",
            "--home",
            str(tmp_path),
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    assert "No liveness change" in outcome.output
    assert store._read_events(CARD)[-1] == event


def test_claim_mutation_cannot_interleave_with_liveness_publication(tmp_path):
    """A real second writer must wait until the guarded append releases its lock."""
    store = create_card(tmp_path)
    observing = threading.Event()
    competing = threading.Event()
    release = threading.Event()

    def runner(_):
        observing.set()
        assert release.wait(5)
        return result(properties())

    def replace_claim():
        competing.set()
        store.append_event(
            CARD, "claim", "replacement", owner="replacement", claim_revision="c" * 32
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        observation_write = pool.submit(publish, tmp_path, runner=runner)
        assert observing.wait(5)
        claim_write = pool.submit(replace_claim)
        assert competing.wait(5)
        assert not claim_write.done()
        release.set()
        assert observation_write.result(timeout=5)
        claim_write.result(timeout=5)
    events = store._read_events(CARD)
    assert [e["action"] for e in events[-2:]] == ["link", "claim"]
    assert not publish(tmp_path)


def test_stale_or_unmatched_projection_is_never_published(tmp_path):
    row = observation(tmp_path, current_claim_generation="new-generation")
    decision = classify(row, now=row.observed_at)
    projection = WorkerProjection(row.owner, row.card_id, row.claim_generation, "active")
    assert (
        reconcile([projection], {(row.owner, row.card_id, row.claim_generation): decision}) == ()
    )
    assert reconcile([projection], {}) == ()


def test_terminal_exact_invocation_can_publish(tmp_path):
    store = create_card(tmp_path)
    assert publish(
        tmp_path, state="terminal", runner=lambda _: result(properties(ActiveState="inactive"))
    )
    assert store.fold(CARD).links["worker_liveness"] == f"{OWNER}|{CLAIM}|terminal"


@pytest.mark.parametrize("field", ["pid", "invocation_id"])
def test_legacy_beat_missing_process_generation_is_not_adopted(tmp_path, field):
    create_card(tmp_path)
    directory = tmp_path / "fleet/beats"
    directory.mkdir(parents=True)
    beat = dict(
        card_id=CARD,
        owner=OWNER,
        claim_revision=CLAIM,
        unit=UNIT,
        pid=123,
        invocation_id=INVOCATION,
    )
    del beat[field]
    (directory / "legacy.json").write_text(json.dumps(beat))
    assert runtime.collect_observations(tmp_path, runner=lambda _: result(properties())) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"pid": None},
        {"pid": 0},
        {"pid": -1},
        {"pid": True},
        {"pid": "123"},
        {"pid": 123.5},
        {"invocation_id": None},
        {"invocation_id": ""},
        {"invocation_id": "b" * 31},
        {"invocation_id": "B" * 32},
        {"invocation_id": 123},
    ],
)
def test_unbound_beats_do_no_board_or_systemd_reads(tmp_path, monkeypatch, change):
    """Old or malformed process identities must fail before costly authority reads."""
    directory = tmp_path / "fleet/beats"
    directory.mkdir(parents=True)
    beat = dict(card_id=CARD, owner=OWNER, claim_revision=CLAIM, pid=123, invocation_id=INVOCATION)
    beat.update(change)
    (directory / "beat.json").write_text(json.dumps(beat))

    def forbidden(*_):
        pytest.fail("unbound beat reached board or systemd")

    monkeypatch.setattr(runtime, "_claim_revision", forbidden)
    assert runtime.collect_observations(tmp_path, runner=forbidden) == ()


def test_real_generated_beat_records_wrapper_parent_and_invocation(tmp_path):
    """Execute the actual generated shell prefix, without Pi or a systemd unit."""
    source = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    expressions = [
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and isinstance(node.left, ast.Constant)
        and str(node.left.value).startswith("beat()")
    ]
    assert len(expressions) == 1
    template = expressions[0].left.value.split("env SKAGENT=", 1)[0]
    template = template.replace("~/.skcapstone/fleet/beats", str(tmp_path))
    beat = tmp_path / "beat.json"
    prefix = template % (OWNER, CARD, CLAIM, "session", beat, beat, beat, "0.01")
    command = prefix + f"sleep 0.1; stop_beat; cat {beat}"
    result = subprocess.run(
        ["bash", "-c", command],
        text=True,
        capture_output=True,
        timeout=5,
        env={**os.environ, "INVOCATION_ID": INVOCATION},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pid"] == os.getpid()
    assert payload["invocation_id"] == INVOCATION
    assert payload["owner"] == OWNER and payload["claim_revision"] == CLAIM


def test_repeated_stale_and_current_observations_do_not_break_guarded_handoff(tmp_path):
    """Exercise installed native revision guards against the isolated fixture board."""
    from skcapstone.seraph_review_cardstore import LiveCardStoreGateway

    store = create_card(tmp_path)
    assert publish(tmp_path)
    gateway = LiveCardStoreGateway(tmp_path)
    baseline = gateway.read_card(CARD).revision
    for _ in range(5):
        assert not publish(tmp_path, owner="old", claim="c" * 32)
        assert not publish(tmp_path)
    assert gateway.read_card(CARD).revision == baseline
    command = [
        str(Path.home() / ".skenv/bin/skcapstone"),
        "coord",
        "link",
        CARD,
        "test_handoff",
        "synthetic-local-only",
        "--agent",
        OWNER,
        "--expected-source-revision",
        baseline,
        "--expected-claim-revision",
        CLAIM,
        "--transition-id",
        "f" * 64,
        "--home",
        str(tmp_path),
    ]
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    started, stopped = threading.Event(), threading.Event()

    def observe_during_handoff():
        while not stopped.is_set():
            assert not publish(tmp_path, owner="old", claim="c" * 32)
            assert not publish(tmp_path)
            started.set()
            stopped.wait(0.005)

    with ThreadPoolExecutor(max_workers=1) as pool:
        observations = pool.submit(observe_during_handoff)
        assert started.wait(5)
        try:
            outcome = subprocess.run(
                command, capture_output=True, text=True, timeout=15, env=environment
            )
        finally:
            stopped.set()
        observations.result(timeout=5)
    assert outcome.returncode == 0, outcome.stderr
    assert store.fold(CARD).links["test_handoff"] == "synthetic-local-only"
    assert store._read_events(CARD)[-1]["writer"] == OWNER
