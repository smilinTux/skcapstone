"""Real unpublished Git candidate transport retains exact native custody."""

import hashlib
import subprocess
import time
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import source_bundle as bundle


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def source(tmp_path, monkeypatch, request):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SKCAPSTONE_HOME", str(home))
    workspace = tmp_path / "source"
    workspace.mkdir()
    git(workspace, "init", "-q", "-b", "main")
    git(workspace, "config", "user.name", "Source Test")
    git(workspace, "config", "user.email", "test@example.invalid")
    (workspace / "base.txt").write_text("published base\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "public base")
    base = git(workspace, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(workspace), str(remote))
    repository = str(remote)
    if getattr(request, "param", None) == "https":
        repository = "https://example.invalid/source.git"
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", repository)
    git(workspace, "checkout", "-qb", "work/source")
    card, owner = "24b00001", "pi-glm-builder-node-worker-24b00001"
    evidence = workspace / "docs/evidence/agents" / card / "COMPLETION-EVIDENCE.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("Candidate tests passed; independent review pending.\n")
    (workspace / "source.txt").write_text("unpublished candidate\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "candidate")
    head = git(workspace, "rev-parse", "HEAD")
    tree = git(workspace, "rev-parse", "HEAD^{tree}")
    assert git(remote, "rev-parse", "main") == base
    shared = home / "evidence/work" / card / "COMPLETION-EVIDENCE.md"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(evidence.read_bytes())
    shared.chmod(0o600)
    shared.parent.chmod(0o700)
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    store = CardStore(home)
    store.create(
        CardCore(
            id=card,
            title="[M] Source",
            created_by=owner,
            initial_labels=["source-only"],
            meta={"repository": repository, "base_revision": base, "base_ref": "main"},
        )
    )
    store.append_event(card, "claim", owner, owner=owner)
    claim = store.fold(card).meta["_claim_revision"]
    outcome = dict(
        verdict="PASS_FOR_REVIEW",
        candidate_commit=head,
        candidate_tree=tree,
        candidate_ref="refs/heads/work/source",
        candidate_path=str(shared),
        candidate_sha256=digest,
        expected_claim_revision=claim,
    )
    store.append_event(card, "verdict", owner, **outcome)
    core = {
        "meta": {
            "link_source_card": card,
            "link_head_revision": head,
            "producer_identity": owner,
            "candidate_evidence_sha256": digest,
            "candidate_tree": tree,
            "candidate_ref": "refs/heads/work/source",
        }
    }
    return dict(
        home=home,
        workspace=workspace,
        card=card,
        owner=owner,
        claim=claim,
        base=base,
        head=head,
        tree=tree,
        remote=repository,
        shared=shared,
        outcome=outcome,
        store=store,
        core=core,
        request={"card_id": card, "repository": repository, "base_revision": base},
    )


def publish(source):
    return bundle.publish_source(
        source["home"], source["request"], source["owner"], source["claim"], source["workspace"]
    )


@pytest.mark.host_systemd
def test_actual_unpublished_export_import_and_immutable_retry(source, tmp_path):
    result = publish(source)
    assert publish(source) == result
    assert result["head"] == source["head"]
    assert result["tree"] == source["tree"]
    assert Path(result["manifest"]).stat().st_mode & 0o077 == 0
    assert bundle.verify_review_source(source["core"], source["remote"], source["head"])
    target = tmp_path / "review"
    assert bundle.import_review_source(source["core"], source["remote"], source["head"], target)
    assert git(target, "rev-parse", "HEAD") == source["head"]
    assert git(target, "rev-parse", "HEAD^{tree}") == source["tree"]
    assert git(target, "status", "--porcelain") == ""
    assert git(target, "remote", "get-url", "origin") == source["remote"]
    assert source["store"].fold(source["card"]).owner == source["owner"]
    with pytest.raises(bundle.SourceBundleError, match="already exists"):
        bundle.import_review_source(source["core"], source["remote"], source["head"], target)


@pytest.mark.parametrize(
    "kind", ["dirty", "wrong-tree", "wrong-claim", "old-proposal", "evidence", "alternates"]
)
def test_export_refuses_changed_or_untrusted_source(source, kind):
    if kind == "dirty":
        (source["workspace"] / "untracked").write_text("not committed")
    elif kind == "wrong-tree":
        source["store"].append_event(
            source["card"],
            "verdict",
            source["owner"],
            **(source["outcome"] | {"candidate_tree": "a" * 40}),
        )
    elif kind == "wrong-claim":
        source["claim"] = "b" * 32
    elif kind == "old-proposal":
        source["store"].append_event(
            source["card"],
            "verdict",
            source["owner"],
            **(source["outcome"] | {"expected_claim_revision": "c" * 32}),
        )
    elif kind == "evidence":
        source["shared"].write_text("changed")
    else:
        (source["workspace"] / ".git/objects/info/alternates").write_text("/tmp/other-objects")
    with pytest.raises(bundle.SourceBundleError):
        publish(source)
    assert not (source["home"] / "evidence/work" / source["card"] / "source-bundles").exists()


@pytest.mark.parametrize(
    "kind", ["digest", "head", "repository", "producer", "evidence", "conflict", "symlink"]
)
@pytest.mark.host_systemd
def test_import_preflight_refuses_changed_binding_or_artifact(source, kind, tmp_path):
    result = publish(source)
    core, repository, head = source["core"], source["remote"], source["head"]
    if kind == "digest":
        blob = Path(result["manifest"]).parent / (result["bundle_sha256"] + ".bundle")
        blob.write_bytes(blob.read_bytes() + b"tampered")
    elif kind == "head":
        head = "d" * 40
    elif kind == "repository":
        repository += "-wrong"
    elif kind == "producer":
        core["meta"]["producer_identity"] = "another-owner"
    elif kind == "evidence":
        core["meta"]["candidate_evidence_sha256"] = "f" * 64
    elif kind == "conflict":
        core["links"] = {"link_head_revision": "e" * 40}
    else:
        path = Path(result["manifest"])
        retained = tmp_path / "redirect.json"
        retained.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(retained)
    with pytest.raises(bundle.SourceBundleError):
        bundle.verify_review_source(core, repository, head)


def test_ordinary_source_uses_existing_clone_but_missing_production_artifact_fails(source):
    assert bundle.verify_review_source({}, source["remote"], source["head"]) is False
    with pytest.raises(bundle.SourceBundleError):
        bundle.verify_review_source(source["core"], source["remote"], source["head"])


@pytest.mark.host_systemd
def test_existing_workspace_never_changes_on_refusal(source, tmp_path):
    publish(source)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "custody").write_text("must remain")
    with pytest.raises(bundle.SourceBundleError):
        bundle.import_review_source(source["core"], source["remote"], source["head"], target)
    assert (target / "custody").read_text() == "must remain"


@pytest.mark.host_systemd
def test_acknowledged_immutable_candidate_does_not_reexport_or_reupload(source, monkeypatch):
    from skcapstone.fleet import source_transport

    result = publish(source)
    monkeypatch.setattr(bundle, "_export", lambda *args: pytest.fail("already exported"))
    monkeypatch.setattr(
        source_transport, "push", lambda *args: pytest.fail("already acknowledged")
    )
    retained = bundle.publish_source(
        source["home"],
        source["request"],
        source["owner"],
        source["claim"],
        source["workspace"],
        acknowledged=result["manifest_sha256"],
    )
    assert retained == result


@pytest.mark.host_systemd
def test_inspection_preserves_sandbox_and_native_service_bounds(tmp_path, monkeypatch):
    calls = []
    run = subprocess.run

    def observed(command, **kwargs):
        calls.append(command)
        return run(command, **kwargs)

    monkeypatch.setattr(bundle.subprocess, "run", observed)
    marker = str(tmp_path / "host-only")
    (tmp_path / "host-only").write_text("host data")
    program = r"""
import errno, json, os, socket, sys
from pathlib import Path
assert not Path('/home').exists() and not Path('/var/tmp').exists()
assert not Path(sys.argv[1]).exists()
assert os.environ['HOME'] == '/tmp' and 'INSPECTION_SECRET_TEST' not in os.environ
try:
    Path('/work/write').write_text('denied')
except OSError as exc:
    assert exc.errno == errno.EROFS
else:
    raise AssertionError('writable source')
with socket.socket() as connection:
    connection.settimeout(1)
    try:
        connection.connect(('192.0.2.1', 9))
    except OSError as exc:
        assert exc.errno == errno.ENETUNREACH
    else:
        raise AssertionError('network available')
Path('/tmp/private').write_text('private tmpfs')
assert 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in Path('/proc/self/status').read_text()
print(json.dumps({'isolated': True, 'argument': sys.argv[2]}))
"""
    monkeypatch.setenv("INSPECTION_SECRET_TEST", "synthetic marker")
    literal = "$HOME ${USER} %n"
    assert bundle._inspect(tmp_path, program, marker, literal) == {
        "isolated": True,
        "argument": literal,
    }
    command = calls[0]
    assert command[:2] == ["/usr/bin/systemd-run", "--user"]
    for flag in [
        "--wait",
        "--pipe",
        "--collect",
        "--expand-environment=no",
        "--property=NoNewPrivileges=yes",
        "--property=RuntimeMaxSec=40",
        "--property=TimeoutStopSec=2",
        "--property=KillMode=control-group",
        "--property=CPUQuota=100%",
        "--property=MemoryMax=512M",
        "--property=TasksMax=64",
        "--property=UMask=0077",
        "--property=LimitFSIZE=" + str(2 * bundle.MAX_BUNDLE + 1),
    ]:
        assert flag in command
    sandbox = command[command.index("--") + 1 :]
    assert sandbox[:5] == [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
    ]
    assert sandbox[sandbox.index("--tmpfs") :][:2] == ["--tmpfs", "/tmp"]
    assert ["--ro-bind", str(tmp_path), "/work"] == sandbox[sandbox.index(str(tmp_path)) - 1 :][:3]
    unit = next(item.removeprefix("--unit=") for item in command if item.startswith("--unit="))
    assert calls[-1] == ["/usr/bin/systemctl", "--user", "stop", unit]
    assert not (tmp_path / "write").exists()


@pytest.mark.parametrize(
    "program", ["print('not JSON')", "raise SystemExit(7)", "print('x' * (16777216 + 1))"]
)
def test_inspection_refuses_malformed_failed_or_oversized_output(tmp_path, program):
    with pytest.raises(bundle.SourceBundleError):
        bundle._inspect(tmp_path, program)


@pytest.mark.host_systemd
def test_inspection_lost_launcher_stops_exact_unit_and_descendants(tmp_path, monkeypatch):
    run = subprocess.run
    units = []

    def interrupted(command, **kwargs):
        if command[0] == "/usr/bin/systemd-run":
            units.append(
                next(
                    item.removeprefix("--unit=") for item in command if item.startswith("--unit=")
                )
            )
            kwargs["timeout"] = 0.5
        return run(command, **kwargs)

    monkeypatch.setattr(bundle.subprocess, "run", interrupted)
    start = time.monotonic()
    with pytest.raises(bundle.SourceBundleError):
        bundle._inspect(
            tmp_path,
            "import os, signal, time; os.fork(); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
        )
    assert time.monotonic() - start < 8
    assert len(units) == 1
    state = run(
        [
            "systemctl",
            "--user",
            "show",
            units[0],
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "ControlGroup",
        ],
        capture_output=True,
        text=True,
    )
    assert "LoadState=not-found" in state.stdout
    assert "ActiveState=inactive" in state.stdout
    assert "ControlGroup=\n" in state.stdout


def test_inspection_missing_manager_fails_without_direct_fallback(tmp_path, monkeypatch):
    calls = []

    def unavailable(command, **kwargs):
        calls.append(command)
        if command[0] == "/usr/bin/systemd-run":
            raise OSError("user manager unavailable")
        return subprocess.CompletedProcess(command, 5)

    monkeypatch.setattr(bundle.subprocess, "run", unavailable)
    with pytest.raises(bundle.SourceBundleError):
        bundle._inspect(tmp_path, "print('{}')")
    assert [command[0] for command in calls] == ["/usr/bin/systemd-run", "/usr/bin/systemctl"]
