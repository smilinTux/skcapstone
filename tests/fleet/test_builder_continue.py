"""Stopped staged work continues only under exact one-use native custody."""

import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_continue as continuation
from skcapstone.fleet import builder_dispatch as builder
from skcapstone.fleet import builder_retire as custody
from skcapstone.fleet import source_bundle
from tests.fleet.test_builder_retry import attempt as attempt
from tests.fleet.test_builder_retry import ready_retry as ready_retry
from tests.fleet.test_production_builder import production_setup as production_setup
from tests.fleet.test_source_bundle import git
from tests.fleet.test_source_bundle import source as source


@pytest.fixture
def preserved(attempt, monkeypatch):
    a = attempt
    report = a.home / "evidence/work" / a.request["card_id"] / "blocked.md"
    report.parent.mkdir(parents=True)
    report.parent.chmod(0o700)
    report.write_text("Actual Git identity unavailable; staged source preserved.")
    report.chmod(0o600)
    a.outcome = {
        "event_id": "original-blocked",
        "action": "verdict",
        "writer": a.status["owner"],
        "verdict": "BLOCKED blocked_on=git_identity referent=git actual identity absent",
        "candidate_commit": a.request["base_revision"],
        "candidate_path": str(report),
        "candidate_sha256": custody.sha(report.read_bytes()),
        "candidate_tree": "e" * 40,
        "candidate_ref": "refs/heads/feature/preserved",
        "ts": "2026-10-01T02:00:00Z",
    }
    monkeypatch.setattr(
        builder.CardStore,
        "_read_events",
        lambda *args: [
            {
                "action": "claim",
                "claim_revision": a.status["claim_revision"],
                "ts": "2026-10-01T01:00:00Z",
            },
        ],
    )
    monkeypatch.setattr(continuation, "_latest_outcome", lambda *args: a.outcome)
    monkeypatch.setattr(
        continuation,
        "card_revision",
        lambda card: custody.sha(
            custody.encoded(
                {
                    "owner": card.owner,
                    "meta": card.meta,
                    "labels": card.labels,
                    "links": card.links,
                }
            )
        ),
    )
    monkeypatch.setattr(continuation, "card_mutation_lock", lambda *args: nullcontext())
    monkeypatch.setattr(builder.production_builder, "policy", lambda: {"authority_host": "worker"})
    monkeypatch.setattr(builder, "_validated_status", lambda *args: a.status)
    request_path = builder.request_path(a.paths, "node-worker", a.request["card_id"])
    request_path.parent.mkdir(parents=True)
    request_path.write_text(json.dumps(a.request))
    request_path.chmod(0o600)
    a.workspace = a.paths.root / "workspaces" / a.status["owner"]
    a.workspace.mkdir(parents=True)
    (a.workspace / "staged.py").write_text("preserved source")
    (a.workspace / "untracked.txt").write_text("preserved untracked bytes")
    monkeypatch.setattr(
        source_bundle,
        "_inspect",
        lambda *args: {
            "head": a.request["base_revision"],
            "tree": "e" * 40,
            "ref": "refs/heads/feature/preserved",
            "index_sha256": "f" * 64,
        },
    )
    a.kwargs = dict(
        request_id=a.request["request_id"],
        claim=a.status["claim_revision"],
        invocation=a.status["invocation"],
        actor="jarvis",
        reason="Git identity repaired",
    )

    def probe(host, payload):
        assert payload["binding"] == continuation.binding(a.home, a.request, a.status)
        return continuation.source_proof(
            a.paths,
            a.request,
            a.status,
            continuation.directory(a.home, a.request),
            apply=payload["apply"],
        )

    a.kwargs["probe"] = probe
    return a


def authorize(a, **kwargs):
    return continuation.authorize(
        a.paths, a.home, "node-worker", a.request["card_id"], **a.kwargs, **kwargs
    )


def test_check_only_then_exact_private_preservation_retains_offer_and_outcome(preserved):
    a = preserved
    request_path = builder.request_path(a.paths, "node-worker", a.request["card_id"])
    before = request_path.read_bytes()
    outcome = deepcopy(a.outcome)
    assert authorize(a)["state"] == "qualified-check-only"
    assert not continuation.directory(a.home, a.request).exists()
    result = authorize(a, apply=True)
    assert result["state"] == "authorized-no-launch"
    target = continuation.directory(a.home, a.request)
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "workspace.tar.gz").stat().st_mode & 0o777 == 0o600
    assert request_path.read_bytes() == before
    assert a.outcome == outcome
    assert continuation.attach(a.home, a.request, a.status)
    continuation.check_attempt(a.paths, a.home, a.request, a.status)
    with pytest.raises(ValueError, match="already authorized"):
        authorize(a, apply=True)


@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "live",
        "unknown",
        "outcome",
        "request",
        "staged",
        "untracked",
        "archive",
        "expiry",
        "status",
    ],
)
def test_changed_generation_cannot_consume(preserved, monkeypatch, change):
    a = preserved
    authorize(a, apply=True)
    continuation.attach(a.home, a.request, a.status)
    if change == "claim":
        a.card.meta["_claim_revision"] = "f" * 32
    elif change in {"live", "unknown"}:
        monkeypatch.setattr(
            builder, "_process_state", lambda *args: (True if change == "live" else None, None)
        )
    elif change == "outcome":
        a.outcome["event_id"] = "different"
    elif change == "request":
        a.request["base_ref"] = "changed"
    elif change in {"staged", "untracked"}:
        (a.workspace / ("staged.py" if change == "staged" else "untracked.txt")).write_text(
            "drift"
        )
    elif change == "archive":
        (continuation.directory(a.home, a.request) / "workspace.tar.gz").write_bytes(b"drift")
    elif change == "expiry":
        a.request["_continuation"]["expires_at"] = "2000-01-01T00:00:00Z"
    else:
        a.status["invocation"] = "f" * 32
    with pytest.raises((ValueError, OSError)):
        with continuation.consume(a.paths, a.home, a.request, a.status):
            pytest.fail("must not launch")


def test_consumption_is_once_and_launch_failure_retains_custody(preserved):
    a = preserved
    authorize(a, apply=True)
    continuation.attach(a.home, a.request, a.status)
    with pytest.raises(RuntimeError, match="launcher failed"):
        with continuation.consume(a.paths, a.home, a.request, a.status):
            raise RuntimeError("launcher failed")
    current = builder._load(builder.status_path(a.paths, "node-worker", a.request["card_id"]))
    assert current["claim_revision"] == a.status["claim_revision"]
    assert current["attempt"] == 2 and current["claim_released"] is False
    assert current["continuation_consumed"] == a.request["_continuation"]["id"]
    assert not continuation.attach(a.home, a.request, a.status)
    with pytest.raises(ValueError, match="consumed"):
        with continuation.consume(a.paths, a.home, a.request, a.status):
            pytest.fail("must not replay")
    assert continuation.original_outcome_pending(a.home, a.request, current)
    a.outcome = {"action": "verdict", "verdict": "PASS_FOR_REVIEW"}
    assert not continuation.original_outcome_pending(a.home, a.request, current)
    (continuation.directory(a.home, a.request) / "consumed.json").unlink()
    assert continuation.original_outcome_pending(a.home, a.request, current)


@pytest.mark.host_systemd
def test_real_git_source_proof_binds_index_and_untracked(source, tmp_path, monkeypatch):
    data = source
    workspace, base = data["workspace"], data["base"]
    git(workspace, "checkout", "-b", "preserved", base)
    (workspace / "added.txt").write_text("staged")
    git(workspace, "add", "added.txt")
    (workspace / "loose.txt").write_text("untracked")
    paths = SimpleNamespace(root=workspace.parent.parent)
    # source fixture workspace already occupies root/workspaces/name.
    paths.root = tmp_path / "fleet"
    (paths.root / "workspaces").mkdir(parents=True)
    destination = paths.root / "workspaces" / "worker"
    workspace.rename(destination)
    request = {"production": {"host": "worker"}, "card_id": "1234abcd", "base_revision": base}
    monkeypatch.setattr(continuation.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(custody, "prove_dead", lambda status: None)
    result = continuation.source_proof(paths, request, {"owner": "worker"}, tmp_path / "evidence")
    assert result["source"]["head"] == base
    assert result["source"]["index_sha256"] == custody.file_digest(destination / ".git/index")
    (destination / "loose.txt").write_text("changed")
    changed = continuation.source_proof(paths, request, {"owner": "worker"}, tmp_path / "evidence")
    assert changed["inventory_sha256"] != result["inventory_sha256"]


@pytest.mark.host_systemd
def test_native_dispatch_continues_once_without_materializing_or_releasing(
    ready_retry, monkeypatch
):
    a = ready_retry
    a.request.pop("operator_retry")
    # Existing fixture exercises native route/resource admission with synthetic source.
    grant = {"schema": continuation.SCHEMA, "expires_at": "2099-01-01T00:00:00Z"}
    grant["id"] = custody.sha(custody.encoded(grant))
    target = continuation.directory(a.home, a.request)
    custody.private_directory(target)
    source_bundle._once(target / "grant.json", custody.encoded(grant))
    path = builder.request_path(a.paths, "node-worker", a.request["card_id"])
    path.write_text(json.dumps(a.request))
    before = path.read_bytes()
    monkeypatch.setattr(continuation, "check_attempt", lambda *args: None)
    monkeypatch.setattr(continuation, "card_mutation_lock", lambda *args: nullcontext())
    launches = []

    def launch(command, workspace):
        current = builder._load(builder.status_path(a.paths, "node-worker", a.request["card_id"]))
        assert current["continuation_consumed"] == grant["id"]
        assert current["claim_revision"] == a.status["claim_revision"]
        launches.append(command)
        return SimpleNamespace(pid=12345679, poll=lambda: None)

    for _ in range(2):
        builder.consume_one(
            a.paths,
            a.home,
            "node-worker",
            launcher=launch,
            materializer=lambda *args: pytest.fail("preserved source cannot reset"),
        )
    assert len(launches) == 1
    assert path.read_bytes() == before
    assert "PRESERVED SOURCE CONTINUATION" in launches[0][-1]
    assert "correct any inaccurate test chronology" in launches[0][-1]
    assert "--unit=" + builder.production_builder.unit_name(a.request, 2) in launches[0]


def test_routine_heartbeat_does_not_invalidate_material_binding(preserved):
    a = preserved
    before = continuation.binding(a.home, a.request, a.status)
    a.status["heartbeat_at"] = "2099-01-01T00:00:00Z"
    assert continuation.binding(a.home, a.request, a.status) == before


@pytest.mark.parametrize("change", ["old-outcome", "missing-report", "public-archive", "grant"])
def test_additional_custody_refusals(preserved, change):
    a = preserved
    if change == "old-outcome":
        a.outcome["ts"] = "2026-09-01T00:00:00Z"
        with pytest.raises(ValueError, match="predates"):
            authorize(a)
        return
    if change == "missing-report":
        from pathlib import Path

        Path(a.outcome["candidate_path"]).unlink()
        with pytest.raises(OSError):
            authorize(a)
        return
    authorize(a, apply=True)
    continuation.attach(a.home, a.request, a.status)
    target = continuation.directory(a.home, a.request)
    if change == "public-archive":
        (target / "workspace.tar.gz").chmod(0o644)
    else:
        (target / "grant.json").write_text("{}")
    with pytest.raises(ValueError):
        continuation.check_attempt(a.paths, a.home, a.request, a.status)


def test_identity_preflight_failure_never_launches_or_releases_retry(ready_retry, monkeypatch):
    from skcapstone.fleet import worker_git

    a = ready_retry
    path = builder.status_path(a.paths, "node-worker", a.request["card_id"])
    before = path.read_bytes()
    monkeypatch.setattr(
        worker_git,
        "preflight",
        lambda *args: (_ for _ in ()).throw(ValueError("assigned identity differs")),
    )
    builder.consume_one(
        a.paths,
        a.home,
        "node-worker",
        launcher=lambda *args: pytest.fail("identity failed"),
        materializer=lambda *args: None,
    )
    assert path.read_bytes() == before


def test_native_cli_check_only_is_the_default(monkeypatch):
    from click.testing import CliRunner

    from skcapstone.fleet.cli import fleet

    calls = []
    monkeypatch.setattr(
        continuation,
        "authorize",
        lambda *args, **kwargs: (calls.append(kwargs) or {"state": "qualified-check-only"}),
    )
    result = CliRunner().invoke(
        fleet,
        [
            "builder-continue",
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
            "Git identity repaired",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["apply"] is False


def _unfinished_probe(a, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(
            builder.CardStore,
            "_read_events",
            lambda *args: [
                {
                    "action": "claim",
                    "claim_revision": a.status["claim_revision"],
                    "owner": a.status["owner"],
                    "ts": "2026-10-01T01:00:00Z",
                }
            ],
        )

    def probe(host, payload):
        assert payload["binding"] == continuation.binding(
            a.home, a.request, a.status, unfinished=True
        )
        return continuation.source_proof(
            a.paths,
            a.request,
            a.status,
            continuation.directory(a.home, a.request),
            apply=payload["apply"],
            unfinished=True,
        )

    return probe


def test_unfinished_work_without_outcome_gets_one_preserved_continuation(preserved, monkeypatch):
    a = preserved
    a.outcome = {}  # the worker stopped before any typed handoff
    a.kwargs["probe"] = _unfinished_probe(a, monkeypatch)
    result = authorize(a, unfinished=True, apply=True)
    assert result["state"] == "authorized-no-launch"
    assert result["grant"]["binding"]["unfinished"] is True
    target = continuation.directory(a.home, a.request)
    assert (target / "workspace.tar.gz").stat().st_mode & 0o777 == 0o600
    assert continuation.attach(a.home, a.request, a.status)
    continuation.check_attempt(a.paths, a.home, a.request, a.status)
    with pytest.raises(ValueError, match="already authorized"):
        authorize(a, unfinished=True, apply=True)


def test_unfinished_continuation_ignores_an_older_generation_outcome(preserved, monkeypatch):
    a = preserved
    a.outcome = dict(a.outcome, ts="2026-09-08T00:00:00Z", writer="september-author")
    a.kwargs["probe"] = _unfinished_probe(a, monkeypatch)
    assert authorize(a, unfinished=True)["state"] == "qualified-check-only"


def test_unfinished_continuation_refuses_a_current_outcome(preserved, monkeypatch):
    a = preserved  # its BLOCKED outcome postdates the claim
    a.kwargs["probe"] = _unfinished_probe(a, monkeypatch)
    with pytest.raises(ValueError, match="unfinished claim without a current outcome"):
        authorize(a, unfinished=True)


def test_a_continued_generation_is_never_continued_again(preserved, monkeypatch):
    a = preserved
    a.outcome = {}
    a.status = dict(a.status, continuation_consumed="f" * 64)
    a.kwargs["probe"] = _unfinished_probe(a, monkeypatch)
    with pytest.raises(ValueError):
        authorize(a, unfinished=True)


def test_sweep_continues_only_unfinished_awaiting_evidence(preserved, monkeypatch):
    a = preserved
    status_file = a.paths.status_path("node-worker", "dispatch", a.request["card_id"])
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text("{}")
    calls = []

    def fake_authorize(paths, home, node, card, **kwargs):
        calls.append((node, card, kwargs["unfinished"], kwargs["apply"]))
        return {"state": "authorized-no-launch"}

    monkeypatch.setattr(continuation, "authorize", fake_authorize)
    a.status = dict(
        a.status,
        state="awaiting-evidence",
        error=continuation.UNFINISHED_ERROR,
        request_id=a.request["request_id"],
    )
    monkeypatch.setattr(builder, "_validated_status", lambda *args: a.status)
    results = continuation.auto_continue_unfinished(a.paths, a.home)
    assert results == [
        {"card": a.request["card_id"], "node": "node-worker", "state": "authorized-no-launch"}
    ]
    assert calls == [("node-worker", a.request["card_id"], True, True)]
    calls.clear()
    a.status = dict(a.status, error="candidate source rejected: source proposal claim changed")
    assert continuation.auto_continue_unfinished(a.paths, a.home) == []
    assert calls == []
