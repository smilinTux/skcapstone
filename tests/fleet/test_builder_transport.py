"""Transport recovery binds the stopped Pi session, never a product verdict."""

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from skcapstone.fleet import builder_retire as custody
from skcapstone.fleet import builder_transport as transport
from tests.fleet.test_builder_continue import preserved as preserved
from tests.fleet.test_builder_retry import attempt as attempt
from tests.fleet.test_builder_retry import ready_retry as ready_retry
from tests.fleet.test_production_builder import production_setup as production_setup
from tests.fleet.test_source_bundle import git
from tests.fleet.test_source_bundle import source as source


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """Provide a synthetic exact session between trusted native transitions."""
    from types import SimpleNamespace

    paths = SimpleNamespace(root=tmp_path / "fleet")
    workspace = paths.root / "workspaces" / "pi-glm-worker-1234abcd"
    request = {"production": {"family": "glm", "model": "sk-glm-m"}}
    status = {"owner": workspace.name}
    filename = "2026-10-05T12-00-01-000Z_00000000-0000-0000-0000-000000000001.jsonl"
    directory = (
        tmp_path
        / ".pi/agent/sessions"
        / ("--" + str(workspace).strip("/").replace("/", "-") + "--")
    )
    directory.mkdir(parents=True)
    path = directory / filename
    events = [
        {
            "type": "session",
            "id": "00000000-0000-0000-0000-000000000001",
            "cwd": str(workspace),
            "timestamp": "2026-10-05T12:00:01Z",
        },
        {
            "type": "message",
            "timestamp": "2026-10-05T12:01:00Z",
            "message": {
                "role": "assistant",
                "provider": "skgateway",
                "model": "sk-glm-m",
                "stopReason": "error",
                "errorMessage": "413: "
                + json.dumps(
                    {
                        "code": "request_too_large",
                        "param": "body",
                        "actual_bytes": 2000010,
                        "limit_bytes": 2000000,
                        "retryable": False,
                    }
                ),
            },
        },
    ]
    start = int(datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp() * 1e6)
    monkeypatch.setattr(transport.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(transport.terminal, "prove", lambda *args, **kwargs: None)
    monkeypatch.setattr(transport.terminal, "boot_id", lambda: "boot")
    monkeypatch.setattr(
        transport.terminal,
        "history",
        lambda *args: (
            [
                {"__REALTIME_TIMESTAMP": str(start)},
                {"__REALTIME_TIMESTAMP": str(start + 61000000)},
            ],
            1000,
        ),
    )

    def write():
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        path.chmod(0o600)
        return {"session": filename, "sha256": custody.file_digest(path)}

    return SimpleNamespace(
        paths=paths, request=request, status=status, path=path, events=events, write=write
    )


def test_exact_native_transport_failure_has_only_hashed_public_metadata(transcript):
    a = transcript
    result = transport.proof(a.paths, a.request, a.status, a.write())
    assert result["session_id"] == "00000000-0000-0000-0000-000000000001"
    assert result["actual_bytes"] == 2000010
    assert "message" not in result and "errorMessage" not in result


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "path",
        "cwd",
        "before-start",
        "late-start",
        "after-terminal",
        "provider",
        "model",
        "not-error",
        "not-413",
        "wrong-code",
        "fits-limit",
        "retryable",
        "tail",
        "family",
        "redirected",
        "writable",
        "death",
    ],
)
def test_transport_refuses_unbound_or_different_failures(transcript, monkeypatch, change):
    a = transcript
    token = a.write()
    message = a.events[-1]["message"]
    if change == "digest":
        token["sha256"] = "f" * 64
    elif change == "path":
        token["session"] = "../outside.jsonl"
    elif change == "cwd":
        a.events[0]["cwd"] = "/other/worker"
    elif change == "before-start":
        a.events[0]["timestamp"] = "2026-10-05T11:59:59Z"
    elif change == "late-start":
        a.events[0]["timestamp"] = "2026-10-05T12:00:31Z"
    elif change == "after-terminal":
        a.events[-1]["timestamp"] = "2026-10-05T12:01:02Z"
    elif change == "provider":
        message["provider"] = "other"
    elif change == "model":
        message["model"] = "sk-glm-l"
    elif change == "not-error":
        message["stopReason"] = "stop"
    elif change == "not-413":
        message["errorMessage"] = "500: {}"
    elif change in {"wrong-code", "fits-limit", "retryable"}:
        error = json.loads(message["errorMessage"][5:])
        if change == "wrong-code":
            error["code"] = "another_error"
        elif change == "fits-limit":
            error["actual_bytes"] = 100
        else:
            error["retryable"] = True
        message["errorMessage"] = "413: " + json.dumps(error)
    elif change == "tail":
        a.events.append({"type": "message", "message": {"role": "user"}})
    elif change == "family":
        a.request["production"]["family"] = "codex"
    elif change == "redirected":
        other = a.path.with_suffix(".other")
        a.path.rename(other)
        a.path.symlink_to(other)
    elif change == "writable":
        a.path.chmod(0o666)
    else:

        def refuse(*args, **kwargs):
            raise ValueError("native death unavailable")

        monkeypatch.setattr(transport.terminal, "prove", refuse)
    if change not in {"digest", "path", "redirected", "writable", "death"}:
        token = a.write()
    with pytest.raises((ValueError, OSError)):
        transport.proof(a.paths, a.request, a.status, token)


@pytest.fixture
def transport_attempt(preserved, monkeypatch):
    from skcapstone.fleet import builder_continue as continuation
    from skcapstone.fleet import builder_dispatch as builder

    a = preserved
    a.request["production"].update(family="glm", model="sk-glm-m")
    a.status["production"] = deepcopy(a.request["production"])
    a.status["attempt"] = 2
    a.status["unit"] = builder.production_builder.unit_name(a.request, 2)
    a.outcome["ts"] = "2026-09-01T00:00:00Z"
    a.outcome["verdict"] = "PASS_FOR_REVIEW"
    path = builder.request_path(a.paths, "node-worker", a.request["card_id"])
    path.write_text(json.dumps(a.request))
    token = {
        "session": "2026-10-05T12-00-01-000Z_00000000-0000-0000-0000-000000000001.jsonl",
        "sha256": "f" * 64,
    }
    monkeypatch.setattr(
        builder.CardStore,
        "_read_events",
        lambda *args: [
            {
                "action": "claim",
                "owner": a.status["owner"],
                "claim_revision": a.status["claim_revision"],
                "ts": "2026-10-01T01:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(transport, "proof", lambda *args: {"session_sha256": "f" * 64})
    kwargs = dict(a.kwargs, transport_session=token["session"], transport_sha256=token["sha256"])
    # The production node is remote; this synthetic test shares its authority's lock file.
    monkeypatch.setattr(builder, "_request_exclusion", lambda *args: nullcontext())
    # Run the real node gate, including transport proof and preserved source checks.
    kwargs["probe"] = lambda host, payload: continuation.node_check(
        payload, paths=a.paths, home=a.home
    )
    a.kwargs = kwargs
    return a


def authorize_transport(a, *, apply=False):
    """Exercise the operator API with exact fixture identities."""
    from skcapstone.fleet import builder_continue as continuation

    return continuation.authorize(
        a.paths, a.home, "node-worker", a.request["card_id"], **a.kwargs, apply=apply
    )


def test_explicit_mode_retains_old_outcome_and_allows_final_attempt_once(transport_attempt):
    from skcapstone.fleet import builder_continue as continuation
    from skcapstone.fleet import builder_dispatch as builder

    a = transport_attempt
    path = builder.request_path(a.paths, "node-worker", a.request["card_id"])
    before = path.read_bytes()
    outcome = deepcopy(a.outcome)
    assert authorize_transport(a)["state"] == "qualified-check-only"
    result = authorize_transport(a, apply=True)
    assert result["state"] == "authorized-no-launch"
    assert a.outcome == outcome and path.read_bytes() == before
    assert continuation.attach(a.home, a.request, a.status)
    continuation.check_attempt(a.paths, a.home, a.request, a.status)
    with continuation.consume(a.paths, a.home, a.request, a.status):
        pass
    current = builder._load(builder.status_path(a.paths, "node-worker", a.request["card_id"]))
    assert current["attempt"] == 3 and current["owner"] == a.status["owner"]
    assert current["claim_revision"] == a.status["claim_revision"]
    assert continuation.original_outcome_pending(a.home, a.request, current)
    with pytest.raises(ValueError):
        continuation.check_attempt(a.paths, a.home, a.request, a.status)
    assert a.outcome == outcome


@pytest.mark.parametrize(
    "change",
    [
        "current-outcome",
        "invalid-date",
        "claim-conflict",
        "source-artifact",
        "published-artifact",
        "retry",
        "consumed",
        "third-attempt",
        "released",
        "foreign-claim",
        "family",
    ],
)
def test_transport_grant_refuses_changed_or_current_review_custody(transport_attempt, change):
    a = transport_attempt
    if change == "current-outcome":
        a.outcome["ts"] = "2026-10-01T02:00:00Z"
    elif change == "invalid-date":
        a.outcome["ts"] = "2026-09-01T00:00:00"
    elif change == "claim-conflict":
        a.card.meta["claim_conflicts"] = ["other"]
    elif change == "source-artifact":
        a.status["source_artifact"] = {"candidate": "published"}
    elif change == "published-artifact":
        from skcapstone.fleet import source_bundle

        root = source_bundle._root(a.home, a.request["card_id"])
        root.mkdir(parents=True)
        (root / "retained.json").write_text("{}")
    elif change == "retry":
        a.request["operator_retry"] = {"id": "prior"}
    elif change == "consumed":
        a.status["continuation_consumed"] = "f" * 64
    elif change == "third-attempt":
        a.status["attempt"] = 3
    elif change == "released":
        a.status["claim_released"] = True
    elif change == "foreign-claim":
        a.card.meta["_claim_revision"] = "f" * 32
    else:
        a.request["production"]["family"] = "codex"
    if change in {"retry", "family"}:
        from skcapstone.fleet import builder_dispatch as builder

        builder.request_path(a.paths, "node-worker", a.request["card_id"]).write_text(
            json.dumps(a.request)
        )
    with pytest.raises(ValueError):
        authorize_transport(a, apply=True)


def test_no_outcome_is_preserved_as_no_outcome(transport_attempt):
    from skcapstone.fleet import builder_continue as continuation

    a = transport_attempt
    a.outcome.clear()
    result = authorize_transport(a, apply=True)
    assert result["grant"]["binding"]["outcome"] == {}
    assert a.outcome == {}
    assert continuation.attach(a.home, a.request, a.status)
    with continuation.consume(a.paths, a.home, a.request, a.status):
        pass
    assert a.outcome == {}


@pytest.mark.host_systemd
@pytest.mark.parametrize("dirty", [False, True])
def test_real_git_transport_source_can_be_clean_or_dirty(source, tmp_path, monkeypatch, dirty):
    from types import SimpleNamespace

    from skcapstone.fleet import builder_continue as continuation

    workspace, base = source["workspace"], source["base"]
    git(workspace, "checkout", "-b", "retained-transport", base)
    if dirty:
        (workspace / "retained-uncommitted.txt").write_text("owner's retained implementation")
    paths = SimpleNamespace(root=tmp_path / "fleet")
    (paths.root / "workspaces").mkdir(parents=True)
    workspace.rename(paths.root / "workspaces/worker")
    request = {"production": {"host": "worker"}, "card_id": "1234abcd", "base_revision": base}
    monkeypatch.setattr(continuation.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(custody, "prove_dead", lambda *args: None)
    monkeypatch.setattr(transport, "proof", lambda *args: {"session_sha256": "f" * 64})
    proof = continuation.source_proof(
        paths,
        request,
        {"owner": "worker"},
        tmp_path / "evidence",
        transport={"session": "synthetic"},
    )
    assert proof["source"]["head"] == base
    assert proof["source"]["ref"] == "refs/heads/retained-transport"
    assert proof["transport_failure"] == {"session_sha256": "f" * 64}


@pytest.mark.host_systemd
def test_native_transport_continuation_launches_third_attempt_once(ready_retry, monkeypatch):
    from types import SimpleNamespace

    from skcapstone.fleet import builder_continue as continuation
    from skcapstone.fleet import builder_dispatch as builder
    from skcapstone.fleet import source_bundle

    a = ready_retry
    a.request.pop("operator_retry")
    a.status["attempt"] = 2
    a.status["unit"] = builder.production_builder.unit_name(a.request, 2)
    grant = {
        "schema": continuation.SCHEMA,
        "expires_at": "2099-01-01T00:00:00Z",
        "binding": {"transport": {"session": "synthetic", "sha256": "f" * 64}},
    }
    grant["id"] = custody.sha(custody.encoded(grant))
    target = continuation.directory(a.home, a.request)
    custody.private_directory(target)
    source_bundle._once(target / "grant.json", custody.encoded(grant))
    path = builder.request_path(a.paths, "node-worker", a.request["card_id"])
    path.write_text(json.dumps(a.request))
    builder.status_path(a.paths, "node-worker", a.request["card_id"]).write_text(
        json.dumps(a.status)
    )
    before = path.read_bytes()
    monkeypatch.setattr(continuation, "check_attempt", lambda *args: None)
    monkeypatch.setattr(continuation, "card_mutation_lock", lambda *args: nullcontext())
    launches = []

    def launch(command, workspace):
        current = builder._load(builder.status_path(a.paths, "node-worker", a.request["card_id"]))
        assert current["continuation_consumed"] == grant["id"]
        assert current["attempt"] == 3 and current["claim_revision"] == a.status["claim_revision"]
        launches.append(command)
        return SimpleNamespace(pid=12345679, poll=lambda: None)

    for _ in range(2):
        builder.consume_one(
            a.paths,
            a.home,
            "node-worker",
            launcher=launch,
            materializer=lambda *args: pytest.fail("retained source cannot reset"),
        )
    assert len(launches) == 1 and path.read_bytes() == before
    assert "gateway transport failure, not a product verdict" in launches[0][-1]
    assert "Original BLOCKED" not in launches[0][-1]
    assert "--unit=" + builder.production_builder.unit_name(a.request, 3) in launches[0]
