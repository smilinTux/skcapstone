"""An operator retry is exact, bounded and does not manufacture an outcome."""

import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch as builder
from skcapstone.fleet import builder_retry as retry
from skcapstone.fleet import source_bundle, store
from tests.fleet.test_builder_dispatch import _card, _folded
from tests.fleet.test_production_builder import production_setup as production_setup
from tests.fleet.test_source_bundle import git
from tests.fleet.test_source_bundle import source as source


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    from skcapstone.fleet.paths import FleetPaths

    paths = FleetPaths(tmp_path / "fleet")
    request = {
        "schema": "skfleet.builder-dispatch/v1",
        "card_id": "1234abcd",
        "request_id": "a" * 64,
        "node": "node-worker",
        "labels": ["sk-m", "source-only"],
        "repository": "https://github.com/example/source.git",
        "base_ref": "main",
        "base_revision": "b" * 40,
        "production": {"host": "worker"},
    }
    status = {
        **request,
        "schema": "skfleet.builder-dispatch-status/v1",
        "state": "awaiting-evidence",
        "owner": "pi-deepseek-builder-node-worker-1234abcd",
        "claim_revision": "c" * 32,
        "attempt": 1,
        "invocation": "d" * 32,
        "unit": builder.production_builder.unit_name(request, 1),
        "claim_released": False,
    }
    card = SimpleNamespace(
        id=request["card_id"],
        owner=status["owner"],
        archived=False,
        status=SimpleNamespace(value="doing"),
        labels=request["labels"],
        links={},
        meta={k: request[k] for k in ("repository", "base_ref", "base_revision")},
    )
    card.meta["_claim_revision"] = status["claim_revision"]
    monkeypatch.setattr(retry.CardStore, "fold", lambda *args: card)
    monkeypatch.setattr(retry, "_latest_outcome", lambda *args: {})
    monkeypatch.setattr(builder, "_process_state", lambda value: (False, 143))
    monkeypatch.setattr(source_bundle, "inspect_clean_base", lambda *args: {"head": "b" * 40})
    monkeypatch.setattr(retry.socket, "gethostname", lambda: "worker")
    return SimpleNamespace(paths=paths, home=tmp_path, request=request, status=status, card=card)


def test_exact_stopped_unchanged_attempt_is_qualified(attempt):
    a = attempt
    proof = retry.check_attempt(a.paths, a.home, a.request, a.status)
    assert proof["claim_revision"] == a.status["claim_revision"]
    assert proof["attempt"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "live",
        "unknown",
        "candidate",
        "artifact",
        "dirty",
        "review",
        "attempts",
        "invocation",
    ],
)
def test_unsafe_retry_is_refused(attempt, monkeypatch, change):
    a = attempt
    if change == "claim":
        a.card.meta["_claim_revision"] = "e" * 32
    elif change in {"live", "unknown"}:
        monkeypatch.setattr(
            builder, "_process_state", lambda value: (True if change == "live" else None, None)
        )
    elif change == "candidate":
        monkeypatch.setattr(
            retry,
            "_latest_outcome",
            lambda *args: {"action": "verdict", "verdict": "PASS_FOR_REVIEW"},
        )
    elif change == "artifact":
        root = source_bundle._root(a.home, a.request["card_id"])
        root.mkdir(parents=True)
        (root / "candidate.json").write_text("{}")
    elif change == "dirty":
        monkeypatch.setattr(
            source_bundle,
            "inspect_clean_base",
            lambda *args: (_ for _ in ()).throw(ValueError("dirty")),
        )
    elif change == "review":
        a.status["state"] = "awaiting-review"
    elif change == "attempts":
        a.status["attempt"] = builder.MAX_ATTEMPTS
    else:
        a.status["invocation"] = None
    with pytest.raises(ValueError):
        retry.check_attempt(a.paths, a.home, a.request, a.status)


def test_one_use_authorization_cannot_match_another_attempt(attempt):
    a = attempt
    grant = {"schema": retry.SCHEMA, "binding": retry.binding(a.request, a.status)}
    grant["id"] = hashlib.sha256(retry.encoded(grant)).hexdigest()
    a.request["operator_retry"] = grant
    assert retry.pending(a.request, a.status)
    changed = deepcopy(a.status)
    changed["attempt"] += 1
    assert not retry.pending(a.request, changed)
    changed = deepcopy(a.status)
    changed["operator_retry_consumed"] = grant["id"]
    assert not retry.pending(a.request, changed)


def test_authorization_is_readonly_by_default_and_replay_refuses(attempt, monkeypatch):
    a = attempt
    monkeypatch.setattr(builder.production_builder, "policy", lambda: {"authority_host": "worker"})
    monkeypatch.setattr(store, "is_frozen", lambda paths: True)
    request_path = builder.request_path(a.paths, "node-worker", a.request["card_id"])
    status_path = builder.status_path(a.paths, "node-worker", a.request["card_id"])
    for path, row in ((request_path, a.request), (status_path, a.status)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row))
    monkeypatch.setattr(builder, "_validated_status", lambda *args: a.status)
    evidence = a.home / "failure.txt"
    evidence.write_text("Gateway failure, no implementation.")
    evidence.chmod(0o600)
    kwargs = dict(
        request_id=a.request["request_id"],
        claim=a.status["claim_revision"],
        invocation=a.status["invocation"],
        actor="jarvis",
        reason="gateway failure",
        evidence=evidence,
        probe=lambda host, payload: {"binding": payload["binding"]},
    )
    before = request_path.read_bytes()
    assert (
        retry.authorize(a.paths, a.home, "node-worker", a.request["card_id"], **kwargs)["state"]
        == "qualified-check-only"
    )
    assert request_path.read_bytes() == before
    result = retry.authorize(
        a.paths, a.home, "node-worker", a.request["card_id"], apply=True, **kwargs
    )
    assert result["state"] == "authorized-no-launch"
    assert a.card.meta["_claim_revision"] == a.status["claim_revision"]
    with pytest.raises(ValueError, match="already authorized"):
        retry.authorize(a.paths, a.home, "node-worker", a.request["card_id"], apply=True, **kwargs)


@pytest.fixture
def ready_retry(paths, production_setup, monkeypatch, tmp_path):
    p = production_setup
    request = builder.offer(paths, _card(), ["sk-m", "source-only"], writer=p.writer)
    owner = f"pi-{request['production']['family']}-builder-node-worker-{request['card_id']}"
    status = builder._write_status(
        paths,
        "node-worker",
        request,
        "awaiting-evidence",
        owner=owner,
        claim_revision="a" * 32,
        attempt=1,
        unit=builder.production_builder.unit_name(request, 1),
        invocation="b" * 32,
        claim_released=False,
        exit_code=143,
    )
    card = _folded(owner=owner, archived=False)
    card.meta["_claim_revision"] = "a" * 32
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: card)
    monkeypatch.setattr(retry, "_latest_outcome", lambda *args: {})
    monkeypatch.setattr(retry, "card_mutation_lock", lambda *args: nullcontext())
    monkeypatch.setattr(builder.production_builder.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(
        builder,
        "_process_state",
        lambda status: (False, 143) if status.get("attempt") == 1 else (None, None),
    )
    monkeypatch.setattr(
        source_bundle, "inspect_clean_base", lambda *args: {"head": request["base_revision"]}
    )
    monkeypatch.setattr(builder, "startup_hello", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        builder.Board, "claim_task", lambda *args: pytest.fail("must retain existing claim")
    )
    monkeypatch.setattr(
        builder,
        "_release_exact",
        lambda *args, **kwargs: pytest.fail("must retain existing claim"),
    )
    grant = {"schema": retry.SCHEMA, "binding": retry.binding(request, status), "actor": "jarvis"}
    grant["id"] = hashlib.sha256(retry.encoded(grant)).hexdigest()
    request["operator_retry"] = grant
    builder.request_path(paths, "node-worker", request["card_id"]).write_text(json.dumps(request))
    (paths.root / "workspaces" / status["owner"]).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(paths=paths, home=tmp_path, request=request, status=status, card=card)


def test_consumer_launches_once_with_original_claim_and_fresh_unit(ready_retry):
    a = ready_retry
    launches = []

    def launch(command, workspace):
        current = builder._load(builder.status_path(a.paths, "node-worker", a.request["card_id"]))
        assert current["operator_retry_consumed"] == a.request["operator_retry"]["id"]
        assert current["claim_revision"] == a.status["claim_revision"]
        assert current["attempt"] == 2
        launches.append(command)
        return SimpleNamespace(pid=12345678, poll=lambda: None)

    for _ in range(2):
        builder.consume_one(
            a.paths, a.home, "node-worker", launcher=launch, materializer=lambda *args: None
        )
    assert len(launches) == 1
    argv = launches[0]
    assert "--unit=" + builder.production_builder.unit_name(a.request, 2) in argv
    assert not {"--resume", "--continue"} & set(argv)
    assert argv[argv.index("--model") + 1] == a.request["production"]["model"]


@pytest.mark.parametrize("refusal", ["expired", "policy", "materialize", "source"])
def test_prelaunch_refusal_preserves_exact_status_and_claim(ready_retry, monkeypatch, refusal):
    a = ready_retry
    if refusal == "expired":
        a.request["lease_expires_at"] = "2000-01-01T00:00:00Z"
        builder.request_path(a.paths, "node-worker", a.request["card_id"]).write_text(
            json.dumps(a.request)
        )
    elif refusal == "policy":
        monkeypatch.setattr(
            builder.production_builder,
            "validate_request",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("policy changed")),
        )
    elif refusal == "source":

        def changed_source(coordination_home, request, *, retained_claim=False):
            assert retained_claim is True
            raise builder.BuilderDispatchError("source changed")

        monkeypatch.setattr(
            builder,
            "_ensure_request_matches_current_card",
            changed_source,
        )
    path = builder.status_path(a.paths, "node-worker", a.request["card_id"])
    before = path.read_bytes()

    def materialize(*args):
        if refusal == "materialize":
            raise builder.BuilderDispatchError("workspace unavailable")

    builder.consume_one(
        a.paths,
        a.home,
        "node-worker",
        launcher=lambda *args: pytest.fail("must not launch"),
        materializer=materialize,
    )
    assert path.read_bytes() == before
    assert a.card.meta["_claim_revision"] == a.status["claim_revision"]


def test_new_candidate_after_authorization_uses_review_path(ready_retry, monkeypatch):
    from skcapstone.fleet import production_exit

    a = ready_retry
    monkeypatch.setattr(
        retry, "_latest_outcome", lambda *args: {"action": "verdict", "verdict": "PASS_FOR_REVIEW"}
    )
    monkeypatch.setattr(production_exit, "release_blocked", lambda *args: None)
    monkeypatch.setattr(
        source_bundle, "publish_source", lambda *args, **kwargs: {"manifest_sha256": "f" * 64}
    )
    result = builder.consume_one(
        a.paths,
        a.home,
        "node-worker",
        launcher=lambda *args: pytest.fail("candidate must not replay"),
    )
    assert result["state"] == "awaiting-review"
    assert result["owner"] == a.status["owner"]
    assert result["claim_revision"] == a.status["claim_revision"]


def test_launch_exception_consumes_once_and_keeps_exact_custody(ready_retry):
    a = ready_retry
    calls = []

    def broken_launch(*args):
        calls.append(True)
        raise OSError("launcher unavailable")

    for _ in range(2):
        builder.consume_one(
            a.paths, a.home, "node-worker", launcher=broken_launch, materializer=lambda *args: None
        )
    assert len(calls) == 1
    row = builder._load(builder.status_path(a.paths, "node-worker", a.request["card_id"]))
    assert row["attempt"] == 2 and row["owner"] == a.status["owner"]
    assert row["claim_revision"] == a.status["claim_revision"]
    assert row["route_preflight"]["requested_identity"] == a.request["production"]["model"]


def test_grant_hash_detects_changed_operator_evidence(attempt):
    a = attempt
    grant = {
        "schema": retry.SCHEMA,
        "binding": retry.binding(a.request, a.status),
        "reason": "gateway failure",
    }
    grant["id"] = hashlib.sha256(retry.encoded(grant)).hexdigest()
    a.request["operator_retry"] = grant
    assert retry.pending(a.request, a.status)
    grant["reason"] = "different incident"
    assert not retry.pending(a.request, a.status)


def test_clean_base_is_verified_inside_real_readonly_git_sandbox(source):
    from skcapstone.fleet import source_bundle as real

    workspace = source["workspace"]
    base = source["base"]
    with pytest.raises(ValueError):
        real.inspect_clean_base(workspace, base)
    git(workspace, "checkout", "--detach", base)
    assert real.inspect_clean_base(workspace, base) == {"head": base}
    (workspace / "base.txt").write_text("changed")
    with pytest.raises(ValueError):
        real.inspect_clean_base(workspace, base)


def test_freeze_wins_during_retry_without_releasing_claim(ready_retry, operator):
    a = ready_retry
    path = builder.status_path(a.paths, "node-worker", a.request["card_id"])
    before = path.read_bytes()

    def materialize(*args):
        store.set_frozen(a.paths, True, writer=operator, reason="operator maintenance")

    builder.consume_one(
        a.paths,
        a.home,
        "node-worker",
        launcher=lambda *args: pytest.fail("freeze won"),
        materializer=materialize,
    )
    assert path.read_bytes() == before
    assert a.card.meta["_claim_revision"] == a.status["claim_revision"]


def test_native_cli_defaults_to_check_only(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from skcapstone.fleet.cli import fleet

    evidence = tmp_path / "failure.txt"
    evidence.write_text("Observed gateway failure.")
    calls = []

    def authorize(*args, **kwargs):
        calls.append(kwargs)
        return {"state": "qualified-check-only"}

    monkeypatch.setattr(retry, "authorize", authorize)
    result = CliRunner().invoke(
        fleet,
        [
            "builder-retry",
            "1234abcd",
            "--node",
            "node-worker",
            "--request-id",
            "a" * 64,
            "--claim",
            "b" * 32,
            "--invocation",
            "c" * 32,
            "--agent",
            "jarvis",
            "--reason",
            "gateway failure",
            "--evidence",
            str(evidence),
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["apply"] is False
