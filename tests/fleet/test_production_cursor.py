"""Cursor's plan-only boundary, native custody and sandboxed producer protocol."""

import io
import json
import subprocess
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import production_builder as builder
from skcapstone.fleet import production_cursor as cursor
from skcapstone.fleet import production_cursor_sandbox as sandbox
from skcapstone.fleet import production_cursor_worker as worker
from skcapstone.fleet import production_routes as routes
from skcapstone.fleet.production_policy import load_production_policy
from tests.fleet.test_source_bundle import git

CARD, OWNER, CLAIM = "24b00001", "pi-cursor-builder-chiap08-24b00001", "a" * 32


@pytest.fixture
def policy():
    path = Path(__file__).resolve().parents[2] / "scripts/fleet/production-policy.json"
    value = json.loads(path.read_text())
    value["lanes"]["cursor"]["enabled"] = True
    return value


@pytest.mark.parametrize(
    "model",
    [
        "auto",
        "default",
        "gpt-5",
        "claude-sonnet-4.6",
        "gemini-3",
        "grok-4.7",
        "composer-2.5-fast",
        "cursor-grok-4.7-high",
        "composer-2.5[thinking=high]",
        "cursor-grok-",
        "",
    ],
)
def test_other_models_and_unreviewed_aliases_refuse(model):
    with pytest.raises(ValueError, match="allowlist"):
        cursor.require_model(model)


@pytest.mark.parametrize(
    "size,model", [("s", "composer-2.5"), ("m", "composer-2.5"), ("l", "cursor-grok-4.6-high")]
)
def test_fixed_routes_never_probe_gateway(policy, monkeypatch, size, model):
    monkeypatch.setattr(routes, "candidates", lambda *a, **k: pytest.fail("gateway was consulted"))
    row = builder.route_binding(policy, CARD, "sk-" + size, ["source-only"], host="chiap08")
    assert row["family"] == "cursor" and row["model"] == model


@pytest.mark.parametrize(
    "host,route,labels",
    [
        ("chiap01", "sk-m", ["source-only"]),
        ("chiap08", "sk-xl", ["source-only"]),
        ("chiap08", "sk-s", ["source-only", "seat-seraph"]),
        ("chiap08", "sk-s", ["source-only", "private"]),
        ("chiap08", "sk-s", ["source-only", "codex-only"]),
        ("chiap08", "sk-s", ["source-only", "qwen-first"]),
        ("chiap08", "sk-s", []),
    ],
)
def test_placement_scope_and_size_fail_closed(policy, host, route, labels):
    assert cursor.candidate(policy, route, labels, host=host) is None


def test_absent_disabled_and_malformed_policy(policy, tmp_path):
    path = tmp_path / "policy.json"
    for lane in [None, {"enabled": False, "provider": "cursor", "host": "chiap08"}]:
        policy["lanes"].pop("cursor", None)
        if lane:
            policy["lanes"]["cursor"] = lane
        path.write_text(json.dumps(policy))
        parsed = load_production_policy(path, host="chiap08")
        assert cursor.candidate(parsed, "sk-m", ["source-only"]) is None
    for lane in [
        {"enabled": True, "provider": "skgateway", "host": "chiap08"},
        {"enabled": True, "provider": "cursor", "host": "chiap01"},
        {"enabled": True, "provider": "cursor", "host": "chiap08", "model": "auto"},
    ]:
        policy["lanes"]["cursor"] = lane
        path.write_text(json.dumps(policy))
        with pytest.raises(ValueError, match="Cursor policy"):
            load_production_policy(path, host="chiap08")


def test_selected_cursor_never_falls_back(policy, monkeypatch):
    monkeypatch.setattr(routes, "candidates", lambda *a, **k: pytest.fail("fallback attempted"))
    with pytest.raises(ValueError, match="Cursor plan route unavailable"):
        builder.route_binding(
            policy, CARD, "sk-m", ["source-only"], family="cursor", model="gpt-5"
        )
    with pytest.raises(ValueError, match="Cursor plan route unavailable"):
        builder.route_binding(
            policy, CARD, "sk-m", ["source-only"], family="cursor", host="chiap02"
        )


def test_preflight_has_no_inference_or_gateway_path(policy, monkeypatch, tmp_path):
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda p: p == Path("/usr/bin/bwrap") or real_is_file(p))
    monkeypatch.setattr(cursor.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(cursor, "runtime", lambda: (tmp_path / "cursor-agent", tmp_path / "auth"))
    monkeypatch.setattr(builder, "review_wrapper_path", lambda: "/installed/wrapper")
    monkeypatch.setattr(routes, "snapshot", lambda *a: pytest.fail("gateway probe"))
    row = cursor.candidate(policy, "sk-m", ["source-only"]) | {"host": "chiap08"}
    assert routes.preflight(policy, row)["probe"] == "local-only"
    monkeypatch.setattr(cursor.socket, "gethostname", lambda: "chiap01")
    with pytest.raises(ValueError, match="host changed"):
        routes.preflight(policy, row)


@pytest.fixture
def inputs(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir(mode=0o700)
    (workspace / ".git").mkdir()
    (workspace / ".cursor").mkdir()
    executable = tmp_path / "runtime/cursor-agent"
    executable.parent.mkdir(mode=0o700)
    for name in ("cursor-agent", "node", "index.js"):
        (executable.parent / name).write_text("placeholder")
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    auth.chmod(0o600)
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    sandbox.write_config(config, "composer-2.5")
    return workspace, executable, auth, config


def command(inputs):
    return sandbox.sandbox_command(
        *inputs, model="composer-2.5", owner=OWNER, card=CARD, claim=CLAIM, prompt="exact card"
    )


def test_sandbox_configuration_and_argv_are_pinned(inputs, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "must-not-cross")
    argv = command(inputs)
    assert argv[:4] == ["/usr/bin/bwrap", "--unshare-all", "--share-net", "--die-with-parent"]
    assert argv[argv.index("--sandbox") + 1] == "enabled"
    assert argv[argv.index("--model") + 1] == "composer-2.5"
    assert argv.count("--model") == 1 and "--force" not in argv and "--auto-review" not in argv
    assert "must-not-cross" not in " ".join(argv)
    assert argv[argv.index(str(inputs[2])) - 1] == "--ro-bind"
    assert argv.count("--bind") == 1
    assert "--trust" in argv and argv[argv.index("--workspace") + 1] == "/work"
    config = json.loads((inputs[3] / "cli-config.json").read_text())
    assert config["subagentModels"] == {"explore": "disabled"}
    assert config["sandbox"]["networkAccess"] == "user_config_only"
    assert config["sandbox"]["readBoundary"] == "workspace"
    assert "subagentStart" in json.loads((inputs[3] / "hooks.json").read_text())["hooks"]
    assert ["--ro-bind", str(inputs[3] / "empty"), "/work/.cursor"] == argv[
        argv.index("/work/.cursor") - 2 : argv.index("/work/.cursor") + 1
    ]


@pytest.mark.skipif(not Path("/usr/bin/bwrap").is_file(), reason="requires Linux bwrap")
def test_real_kernel_boundary_no_host_or_cardstore_writes(inputs, tmp_path):
    sentinel = tmp_path / "CardStore.jsonl"
    sentinel.write_text("untouched")
    argv = command(inputs)
    # Exercise the real container and its guard with a harmless Python child, no model calls.
    node = argv.index(str(inputs[1]), argv.index("--") + 1)
    argv = argv[:node] + [
        "/usr/bin/python3",
        "-c",
        """
from pathlib import Path
assert not Path(%r).exists()
Path('/work/allowed').write_text('source')
for path in ['/usr/cursor-denied', '/tmp/.cursor/cli-config.json',
             '/tmp/.config/cursor/auth.json']:
    try: Path(path).write_text('denied')
    except OSError: pass
    else: raise AssertionError(path)
""" % str(sentinel),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if result.returncode and (
        "Operation not permitted" in result.stderr or "No permissions" in result.stderr
    ):
        pytest.skip("host disables unprivileged user namespaces")
    assert result.returncode == 0, result.stderr
    assert sentinel.read_text() == "untouched"
    assert (inputs[0] / "allowed").read_text() == "source"


@pytest.mark.parametrize("target", [0, 2, 3])
def test_sandbox_rejects_redirected_inputs(inputs, tmp_path, target):
    paths = list(inputs)
    alias = tmp_path / "alias"
    alias.symlink_to(paths[target])
    paths[target] = alias
    with pytest.raises(ValueError, match="redirected"):
        command(paths)


def events(*extra):
    return [
        dict(type="system", subtype="init", model="Composer 2.5", session_id="session"),
        *[dict(event, session_id="session") for event in extra],
        dict(type="result", subtype="success", is_error=False, session_id="session"),
    ]


def stream(rows, progress=None):
    output = io.BytesIO()
    worker.consume_stream(
        io.BytesIO(b"".join(json.dumps(e).encode() + b"\n" for e in rows)),
        model="composer-2.5",
        progress=progress or (lambda _: None),
        output=output,
    )
    return output.getvalue()


def test_stream_only_assistant_and_tool_activity_is_progress():
    seen = []
    raw = stream(
        events(
            {"type": "user"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "tool_call", "subtype": "started"},
        ),
        seen.append,
    )
    assert len(seen) == 2 and seen[0] < seen[1] < len(raw)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        events()[:-1],
        events({"type": "error"}),
        events({"type": "assistant", "model": "GPT-5"}),
        events({"type": "result", "is_error": True}),
        events() + events(),
        [{"type": "assistant", "session_id": "session"}],
    ],
)
def test_stream_fails_closed(rows):
    with pytest.raises(ValueError):
        stream(rows)


def test_bounded_stream_and_incomplete_line_refuse():
    for raw in [b"x" * (worker.MAX_LINE + 1), b"{}", b"not json\n"]:
        with pytest.raises(ValueError):
            worker.consume_stream(
                io.BytesIO(raw), model="composer-2.5", progress=lambda _: None, output=io.BytesIO()
            )


@pytest.fixture
def source(tmp_path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir(mode=0o700)
    monkeypatch.setenv("SKCAPSTONE_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    git(workspace, "init", "-q", "-b", "work/source")
    git(workspace, "config", "user.name", OWNER)
    git(workspace, "config", "user.email", "test@example.invalid")
    repository = "https://github.com/example/source.git"
    git(workspace, "remote", "add", "origin", repository)
    report = workspace / f"docs/evidence/agents/{CARD}/COMPLETION-EVIDENCE.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "Implemented source. Local assertion passed; native qualification pending.\n"
    )
    report.chmod(0o600)
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "candidate")
    base = git(workspace, "rev-parse", "HEAD")
    store = CardStore(home)
    store.create(
        CardCore(
            id=CARD,
            title="[M] Source",
            created_by=OWNER,
            initial_labels=["source-only", "sk-m"],
            meta=dict(repository=repository, base_ref="main", base_revision=base),
        )
    )
    store.append_event(CARD, "claim", OWNER, owner=OWNER)
    claim = store.fold(CARD).meta["_claim_revision"]
    result = workspace / worker.RESULT
    result.write_text(json.dumps(dict(verdict="PASS_FOR_REVIEW", claim_revision=claim)))
    result.chmod(0o600)
    return (
        home,
        workspace,
        dict(card_id=CARD, repository=repository, base_ref="main", base_revision=base),
        claim,
        store,
        report,
    )


def test_handoff_uses_guarded_cli_and_is_idempotent(source):
    home, workspace, request, claim, store, report = source
    revision = worker.native_state(home, CARD)["revision"]
    worker.handoff(home, request, workspace, OWNER, claim, revision)
    worker.handoff(home, request, workspace, OWNER, claim, revision)
    verdicts = [e for e in store._read_events(CARD) if e["action"] == "verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "PASS_FOR_REVIEW"
    assert verdicts[0]["expected_claim_revision"] == claim
    assert Path(verdicts[0]["candidate_path"]).read_bytes() == report.read_bytes()
    assert store.fold(CARD).status.value == "doing"


@pytest.mark.parametrize("change", ["stale", "redirect", "dirty", "PASS", "changed-source"])
def test_handoff_refuses_without_any_board_write(source, change, tmp_path):
    home, workspace, request, claim, store, report = source
    revision = worker.native_state(home, CARD)["revision"]
    if change == "stale":
        claim = "f" * 32
    elif change == "redirect":
        target = tmp_path / "foreign"
        target.write_bytes(report.read_bytes())
        report.unlink()
        report.symlink_to(target)
    elif change == "dirty":
        (workspace / "new.txt").write_text("uncommitted")
    elif change == "PASS":
        (workspace / worker.RESULT).write_text(
            json.dumps(dict(verdict="PASS", claim_revision=claim))
        )
    elif change == "changed-source":
        request["repository"] = "https://github.com/example/other.git"
    before = store._read_events(CARD)
    with pytest.raises(ValueError):
        worker.handoff(home, request, workspace, OWNER, claim, revision)
    assert store._read_events(CARD) == before


def test_wrapper_reuses_heartbeat_and_preserves_unit_invocation(policy, inputs, monkeypatch):
    from skcapstone.fleet.builder_dispatch import worker_command
    from skcapstone.fleet.worker_git import preflight

    workspace, executable, auth, _ = inputs
    monkeypatch.setattr(cursor, "runtime", lambda: (executable, auth))
    monkeypatch.setattr(
        builder, "review_wrapper_path", lambda: "/installed/skfleet-worker-wrapper.py"
    )
    monkeypatch.setenv("SKCAPSTONE_HOME", str(workspace.parent / "sovereign"))
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "/reviewed/policy.json")
    monkeypatch.setenv("SKFLEET_AUTHORITY_HOST", "chiap08")
    request = dict(
        card_id=CARD,
        request_id="b" * 64,
        labels=["source-only"],
        logical_route="sk-m",
        repository="https://example.invalid/repo",
        base_revision="c" * 40,
        production=cursor.candidate(policy, "sk-m", ["source-only"])
        | dict(host="chiap08", resources=policy["node_quotas"]["chiap08"]),
    )
    command = worker_command(request, OWNER, CLAIM, workspace)
    preflight(command, workspace, OWNER)
    assert "/installed/skfleet-worker-wrapper.py" in command
    assert command[command.index("--worker-executable") + 1] == str(executable.parent / "index.js")
    assert "--live-snapshot" in command and "--claim-revision" in command
    home = workspace.parent / "sovereign"
    assert "SKCAPSTONE_HOME=" + str(home) in command
    assert Path(command[command.index("--stdout") + 1]).is_relative_to(home)
    assert Path(command[command.index("--evidence-dir") + 1]).is_relative_to(home)
    assert "skcapstone.fleet.production_cursor_worker" in " ".join(command)
    assert "shell-liveness" in " ".join(command)
    child = [
        "/usr/bin/env",
        "-i",
        "/usr/bin/python3",
        "-c",
        "import os; print(os.environ['INVOCATION_ID'])",
    ]
    service = builder.service_command(request, 1, child, workspace)
    actual = service[service.index("--") + 1 :]
    observed = subprocess.check_output(
        actual, env={"INVOCATION_ID": "new-unit-generation"}, text=True
    )
    assert observed.strip() == "new-unit-generation"


def test_progress_requires_exact_generation(tmp_path):
    path = worker.progress_path(tmp_path, OWNER, CLAIM)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(dict(owner=OWNER, card_id=CARD, claim_revision=CLAIM, progress_at=1, bytes=100))
    )
    path.chmod(0o600)
    assert worker.read_progress(tmp_path, OWNER, CARD, CLAIM)["bytes"] == 100
    assert worker.read_progress(tmp_path, OWNER, CARD, "b" * 32) is None
    assert worker.read_progress(tmp_path, OWNER, "deadbeef", CLAIM) is None


def test_missing_runtime_is_a_preclaim_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ValueError, match="runtime is unavailable"):
        cursor.runtime()


def test_corrupt_progress_does_not_crash_wrapper(tmp_path):
    path = worker.progress_path(tmp_path, OWNER, CLAIM)
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    path.chmod(0o600)
    assert worker.read_progress(tmp_path, OWNER, CARD, CLAIM) is None
