"""Terminal retirement preserves source bytes and refuses stale authority."""

import json
import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_retire as retire
from skcapstone.fleet.paths import FleetPaths


def write(path, raw):
    """Create a private test input."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)


@pytest.fixture
def case(tmp_path, monkeypatch):
    """Bind one exact old request to an unclaimed card."""
    paths = FleetPaths(tmp_path / "fleet")
    request = dict(
        schema="skfleet.builder-dispatch/v1",
        card_id="1234abcd",
        request_id="a" * 64,
        node="node-worker",
        labels=["sk-m", "source-only"],
        repository="https://github.com/example/repo.git",
        base_ref="main",
        base_revision="b" * 40,
    )
    status = dict(
        schema="skfleet.builder-dispatch-status/v1",
        card_id="1234abcd",
        request_id="a" * 64,
        node="node-worker",
        state="blocked",
        attempt=1,
        owner="pi-builder-standby-node-worker-1234abcd",
        claim_revision="c" * 32,
        pid=12345,
        pid_start_ticks="999",
        production=None,
    )
    req = retire.builder.request_path(paths, "node-worker", "1234abcd")
    sts = retire.builder.status_path(paths, "node-worker", "1234abcd")
    write(req, json.dumps(request).encode())
    write(sts, json.dumps(status).encode())
    card = SimpleNamespace(
        owner=None,
        archived=False,
        status=SimpleNamespace(value="backlog"),
        labels=request["labels"],
        meta={},
        links={},
    )
    monkeypatch.setattr(retire.CardStore, "fold", lambda *a: card)
    monkeypatch.setattr(retire, "card_mutation_lock", lambda *a: nullcontext())
    monkeypatch.setattr(retire, "card_revision", lambda c: "d" * 64)
    monkeypatch.setattr(
        retire.builder,
        "_source",
        lambda c: tuple(request[k] for k in ("repository", "base_ref", "base_revision")),
    )
    monkeypatch.setattr(
        retire.builder.production_builder, "policy", lambda: {"authority_host": "worker"}
    )
    monkeypatch.setattr(
        retire.builder.production_builder, "node_binding", lambda *a: {"host": "worker"}
    )
    monkeypatch.setattr(retire.socket, "gethostname", lambda: "worker")
    proof = dict(
        request_sha256=retire.sha(req.read_bytes()),
        status_sha256=retire.sha(sts.read_bytes()),
        archive_sha256="f" * 64,
        inventory_sha256="e" * 64,
        process_dead=True,
    )
    kwargs = dict(
        request_sha256=proof["request_sha256"],
        status_sha256=proof["status_sha256"],
        card_sha256="d" * 64,
        actor="jarvis",
        reason="Resolved prior admission failure",
        probe=lambda host, payload: proof,
    )
    return SimpleNamespace(
        paths=paths,
        home=tmp_path,
        request=request,
        status=status,
        req=req,
        sts=sts,
        card=card,
        kwargs=kwargs,
        proof=proof,
    )


def run(c, **kw):
    """Invoke the controller with its exact caller guards."""
    return retire.retire(c.paths, c.home, "node-worker", "1234abcd", **(c.kwargs | kw))


def test_check_apply_replay_preserves_status_and_old_request(case):
    c = case
    original = c.req.read_bytes(), c.sts.read_bytes()
    assert run(c)["state"] == "qualified-check-only"
    assert c.req.read_bytes() == original[0]
    assert run(c, apply=True)["state"] == "retired"
    assert not c.req.exists()
    assert c.sts.read_bytes() == original[1]
    assert run(c, apply=True)["state"] == "already-retired"
    assert (
        retire.directory(c.home, "1234abcd", "a" * 64) / "request.json"
    ).read_bytes() == original[0]


@pytest.mark.parametrize(
    "change", ["request", "status", "claim", "card", "process", "custody", "review"]
)
def test_changed_guards_never_remove_offer(case, change):
    c = case
    if change in {"request", "status"}:
        (c.req if change == "request" else c.sts).write_bytes(b"{}")
    elif change == "claim":
        c.card.owner = "another-worker"
    elif change == "card":
        c.kwargs["card_sha256"] = "0" * 64
    elif change == "process":
        c.proof["process_dead"] = False
    elif change == "custody":
        del c.proof["archive_sha256"]
    else:
        c.status["state"] = "awaiting-review"
        write(c.sts, json.dumps(c.status).encode())
        c.kwargs["status_sha256"] = retire.sha(c.sts.read_bytes())
    with pytest.raises(ValueError):
        run(c, apply=True)
    assert c.req.exists()


def test_interrupted_receipt_recovers_and_new_generation_survives(case, monkeypatch):
    c = case
    unlink = retire.Path.unlink

    def fail(path, *a, **kw):
        if path == c.req:
            raise OSError("interrupted")
        return unlink(path, *a, **kw)

    monkeypatch.setattr(retire.Path, "unlink", fail)
    with pytest.raises(OSError, match="interrupted"):
        run(c, apply=True)
    monkeypatch.setattr(retire.Path, "unlink", unlink)
    assert run(c, apply=True)["state"] == "retired"
    write(c.req, b'{"request_id":"new"}')
    with pytest.raises(ValueError):
        run(c, apply=True)
    assert c.req.read_bytes() == b'{"request_id":"new"}'


def test_complete_workspace_archive_and_changed_replay(tmp_path):
    workspace = tmp_path / "workspace"
    write(workspace / ".git/index", b"staged bytes")
    write(workspace / "untracked", b"untracked bytes")
    write(workspace / "node_modules/ignored", b"ignored bytes")
    (workspace / "link").symlink_to("untracked")
    destination = tmp_path / "private"
    retire.private_directory(destination)
    proof = retire.preserve(workspace, destination, apply=True)
    assert proof == retire.preserve(workspace, destination, apply=True)
    assert (destination / "workspace.tar.gz").stat().st_mode & 0o777 == 0o600
    assert (workspace / ".git/index").read_bytes() == b"staged bytes"
    write(workspace / "untracked", b"changed")
    with pytest.raises(ValueError):
        retire.preserve(workspace, destination, apply=True)


def test_unknown_or_live_pid_rejected(monkeypatch):
    status = {"pid": os.getpid(), "pid_start_ticks": retire.builder._proc_start_ticks(os.getpid())}
    with pytest.raises(ValueError):
        retire.prove_dead(status)
    monkeypatch.setattr(retire.os, "kill", lambda *a: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(ValueError):
        retire.prove_dead(status)


@pytest.mark.parametrize("state,attempt", [("running", 1), ("completed", 1), ("failed", 1)])
def test_active_accepted_or_retryable_attempt_refused(case, state, attempt):
    c = case
    c.status.update(state=state, attempt=attempt)
    write(c.sts, json.dumps(c.status).encode())
    c.kwargs["status_sha256"] = retire.sha(c.sts.read_bytes())
    with pytest.raises(ValueError, match="terminal"):
        run(c, apply=True)
    assert c.req.exists()


def test_claim_changes_during_node_proof_are_rejected(case):
    c = case

    def drift(host, payload):
        c.card.owner = "new-worker"
        return c.proof

    with pytest.raises(ValueError, match="unclaimed"):
        run(c, apply=True, probe=drift)
    assert c.req.exists()


def test_same_host_uses_existing_lock_without_ssh(monkeypatch):
    monkeypatch.setattr(retire.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(retire, "node_check", lambda payload, **kw: kw)
    assert retire.remote_check("worker", {}) == {"locked": True}


def test_offer_exclusion_covers_preservation_and_pointer_removal(case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    c = case
    preserving, finish, offered = Event(), Event(), Event()

    def probe(host, payload):
        preserving.set()
        assert finish.wait(3)
        return c.proof

    def offer():
        with retire.builder._request_exclusion(c.paths.root / "dispatch" / ".production-offer"):
            assert not c.req.exists()
            write(c.req, b"new generation")
            offered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        retiring = pool.submit(run, c, apply=True, probe=probe)
        assert preserving.wait(3)
        offering = pool.submit(offer)
        assert not offered.wait(0.05)
        finish.set()
        assert retiring.result(timeout=3)["state"] == "retired"
        offering.result(timeout=3)
    assert c.req.read_bytes() == b"new generation"


def test_native_card_and_node_preservation_integration(case, monkeypatch):
    """Exercise real native card guards and full node custody through retirement."""
    from skcoord.card_store import CardCore, CardStore

    from tests.fleet.test_source_bundle import git

    c = case
    monkeypatch.undo()
    node_home = c.home / "node-home"
    node_paths = FleetPaths(node_home / "fleet")
    workspace = node_paths.root / "workspaces" / c.status["owner"]
    workspace.mkdir(parents=True)
    git(workspace, "init", "-q", "-b", "main")
    git(workspace, "config", "user.name", "Test")
    git(workspace, "config", "user.email", "test@example.invalid")
    write(workspace / "original", b"original")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "base")
    c.request["base_revision"] = git(workspace, "rev-parse", "HEAD")
    write(workspace / "original", b"staged")
    git(workspace, "add", ".")
    write(workspace / "original", b"unstaged")
    write(workspace / "untracked", b"untracked")
    c.status["pid"] = 2147483647
    for paths in (c.paths, node_paths):
        write(
            retire.builder.request_path(paths, "node-worker", "1234abcd"),
            json.dumps(c.request).encode(),
        )
        write(
            retire.builder.status_path(paths, "node-worker", "1234abcd"),
            json.dumps(c.status).encode(),
        )
    native = CardStore(c.home)
    native.create(
        CardCore(
            id="1234abcd",
            title="[M] Exact terminal source",
            created_by="test",
            initial_labels=c.request["labels"],
            meta={k: c.request[k] for k in ("repository", "base_ref", "base_revision")},
        )
    )
    original = native.fold("1234abcd").model_dump(mode="json")
    host = retire.socket.gethostname().split(".")[0].lower()
    monkeypatch.setattr(
        retire.builder.production_builder, "policy", lambda: {"authority_host": host}
    )
    monkeypatch.setattr(
        retire.builder.production_builder, "node_binding", lambda *a: {"host": host}
    )
    c.kwargs.update(
        card_sha256=retire.card_revision(native.fold("1234abcd")),
        request_sha256=retire.sha(c.req.read_bytes()),
        status_sha256=retire.sha(c.sts.read_bytes()),
        probe=lambda host, payload: retire.node_check(payload, paths=node_paths, home=node_home),
    )
    assert run(c)["state"] == "qualified-check-only"
    assert not (node_home / "evidence").exists()
    assert run(c, apply=True)["state"] == "retired"
    assert native.fold("1234abcd").model_dump(mode="json") == original
    assert (workspace / "original").read_bytes() == b"unstaged"
    assert run(c, apply=True)["state"] == "already-retired"


def test_ready_unclaimed_card_can_retire_terminal_offer(case, monkeypatch):
    c = case
    c.card.status.value = "ready"
    monkeypatch.setattr(
        retire.builder,
        "_source",
        lambda _core: tuple(c.request[key] for key in ("repository", "base_ref", "base_revision")),
    )
    retire.check_card(c.home, "1234abcd", c.request, "d" * 64)


def test_every_post_offer_claim_must_have_exact_release(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    offered = datetime(2026, 10, 8, 3, 30, tzinfo=timezone.utc)
    events = [
        {
            "action": "claim",
            "ts": "2026-10-08T03:31:00Z",
            "owner": "pi-glm-chiap08-52f7c2a5",
            "claim_revision": "a" * 32,
        },
        {
            "action": "release_claim",
            "ts": "2026-10-08T03:32:00Z",
            "released_owner": "pi-glm-chiap08-52f7c2a5",
            "expected_claim_revision": "a" * 32,
        },
        {
            "action": "claim",
            "ts": "2026-10-08T03:33:00Z",
            "owner": "pi-glm-chiap08-52f7c2a5",
            "claim_revision": "b" * 32,
        },
        {
            "action": "release_claim",
            "ts": "2026-10-08T03:34:00Z",
            "released_owner": "pi-glm-chiap08-52f7c2a5",
            "expected_claim_revision": "b" * 32,
        },
    ]
    monkeypatch.setattr(retire.CardStore, "_read_events", lambda *_args: events)
    request = {"card_id": "52f7c2a5", "offered_at": offered.isoformat()}

    assert retire.claims_released_since_offer(tmp_path, request) == 2
    events.pop()
    with pytest.raises(ValueError, match="post-offer claim lacks one exact release"):
        retire.claims_released_since_offer(tmp_path, request)
